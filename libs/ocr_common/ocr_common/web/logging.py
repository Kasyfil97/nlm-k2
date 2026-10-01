"""Process-wide logging for the services: one line per record, either JSON (for Cloud Logging) or text
(for a terminal), and every record carries the `request_id` of the request or job it belongs to.

`request_id` comes from the contextvar that `RequestIdMiddleware` binds for the duration of a request
and that the pipeline binds for the duration of a background job, so a log line written deep inside a
model client still says which request it was for."""

import json
import logging
import queue
import sys
import threading
from datetime import UTC, datetime
from typing import Any, Literal

from ocr_common.web.request_id import current_request_id

LogFormat = Literal["json", "text"]

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"
_MARK = "_ocr_common_handler"
_ES_MARK = "_ocr_common_es_handler"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_ES_BATCH_SIZE = 50
_ES_FLUSH_INTERVAL = 5.0  # seconds; worker flushes the batch if nothing arrives within this window


class RequestIdFilter(logging.Filter):
    """Adds `request_id` to every record (`-` outside a request or job) so formats can rely on it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. `severity` and `message` are the field names Cloud Logging reads."""

    def __init__(self, service: str | None) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or "-",
        }
        if self._service:
            entry["service"] = self._service
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class ElasticsearchHandler(logging.Handler):
    """Async-safe logging handler that ships records to Elasticsearch in batches.

    Records are enqueued from the logging call (never blocks the caller) and flushed by a
    daemon background thread either when the batch reaches `_ES_BATCH_SIZE` or after
    `_ES_FLUSH_INTERVAL` seconds of inactivity. A full queue drops records silently; ES
    errors are swallowed so they never affect the service.

    Requires `elasticsearch>=8` to be installed; the import is deferred to __init__ so the
    class can be defined even when the package is absent (disabled path never instantiates it).
    """

    def __init__(
        self,
        *,
        url: str,
        index: str,
        service: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        super().__init__()
        from elasticsearch import Elasticsearch  # lazy: only needed when ES is enabled

        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        elif username and password:
            kwargs["basic_auth"] = (username, password)

        self._client = Elasticsearch(url, **kwargs)
        self._index = index
        self._service = service
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=10_000)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="es-log-worker")
        self._thread.start()

    def _make_doc(self, record: logging.LogRecord) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "@timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or "-",
        }
        if self._service:
            doc["service"] = self._service
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return doc

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(self._make_doc(record))
        except queue.Full:
            pass  # drop on overflow; ES backpressure must never block or crash the service

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        try:
            from elasticsearch.helpers import bulk

            actions = [{"_index": self._index, "_source": doc} for doc in batch]
            bulk(self._client, actions)
        except Exception:
            pass  # ES errors are never allowed to propagate into the logging machinery

    def _worker(self) -> None:
        batch: list[dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get(timeout=_ES_FLUSH_INTERVAL)
                if item is None:  # shutdown signal
                    break
                batch.append(item)
                if len(batch) >= _ES_BATCH_SIZE:
                    self._flush(batch)
                    batch = []
            except queue.Empty:
                if batch:
                    self._flush(batch)
                    batch = []
        if batch:
            self._flush(batch)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5.0)
        try:
            self._client.close()
        except Exception:
            pass
        super().close()


def configure_logging(
    *,
    fmt: LogFormat,
    level: str,
    service: str | None = None,
    elasticsearch_url: str | None = None,
    elasticsearch_index: str = "ocr-logs",
    elasticsearch_api_key: str | None = None,
    elasticsearch_username: str | None = None,
    elasticsearch_password: str | None = None,
) -> None:
    """Installs one stderr handler on the root logger (replacing an earlier one of ours, never a
    handler someone else added, such as pytest's) and routes uvicorn's loggers through it, so the
    access log has the same shape as the application log.

    When `elasticsearch_url` is given an `ElasticsearchHandler` is also attached, shipping records
    to the `elasticsearch_index` index in batches via a daemon background thread."""
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _MARK, True)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter(service) if fmt == "json" else logging.Formatter(TEXT_FORMAT))

    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers if not getattr(h, _MARK, False) and not getattr(h, _ES_MARK, False)
    ] + [handler]
    root.setLevel(level.upper())

    if elasticsearch_url:
        es_handler = ElasticsearchHandler(
            url=elasticsearch_url,
            index=elasticsearch_index,
            service=service,
            api_key=elasticsearch_api_key,
            username=elasticsearch_username,
            password=elasticsearch_password,
        )
        es_handler.addFilter(RequestIdFilter())
        setattr(es_handler, _ES_MARK, True)
        root.addHandler(es_handler)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
