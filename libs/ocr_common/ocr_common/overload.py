"""Protection against overload: a bulkhead (a bounded number of callers running plus a bounded queue, the rest
refused at once), and the `Retry-After` header that goes with its refusal. Ported from nilam-ocr-npwp
(`ocr_common/overload.py`), the bulkhead part only.

Why: the guardrails model is CPU-bound, and every document it accepted went straight into the threadpool, so a
burst made every document slower at once (they all share the pod's CPUs) and held every image in memory; past
the orchestrator's timeout all of them failed together. With a bulkhead a burst waits in a short queue, and
beyond it the caller gets 503 `SERVICE_BUSY` with `Retry-After` before anything is started.

Every limit is off by default (0): the settings of each service turn them on (deploy manifests)."""

import asyncio
import math
from collections.abc import Callable
from types import TracebackType
from typing import Self

from prometheus_client import Counter, Gauge

from ocr_common.errors import ServiceBusy, ServiceError

BULKHEAD_RUNNING = Gauge("bulkhead_running", "Callers inside a bulkhead now", ["bulkhead"])
BULKHEAD_WAITING = Gauge("bulkhead_waiting", "Callers waiting for a slot of a bulkhead", ["bulkhead"])
BULKHEAD_REJECTED = Counter("bulkhead_rejected_total", "Callers refused because a bulkhead was full", ["bulkhead"])


def retry_after_header(seconds: float | None) -> str | None:
    """The `Retry-After` header value (whole seconds, at least 1) of `seconds`; None without one."""
    if seconds is None:
        return None
    return str(max(1, math.ceil(seconds)))


class Bulkhead:
    """At most `limit` callers inside at once, at most `queue` more waiting for a slot; one more is refused with
    `error` (503 `SERVICE_BUSY` by default) carrying `retry_after`. `limit` 0 means no limit at all.

    A caller reserves its place with `reserve()` (synchronous, so the count cannot be overtaken between the check and
    the wait) and enters with `async with ticket:`; `async with bulkhead.slot():` does both in one go."""

    def __init__(
        self,
        name: str,
        limit: int,
        queue: int = 0,
        *,
        retry_after: float = 5.0,
        error: Callable[[str, float], ServiceError] | None = None,
    ):
        self.name = name
        self.limit = max(limit, 0)
        self.queue = max(queue, 0)
        self.retry_after = retry_after
        self._error = error or (lambda message, after: ServiceBusy(message, retry_after=after))
        self._slots = asyncio.Semaphore(self.limit) if self.limit else None
        self._reserved = 0
        self._running = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def reserved(self) -> int:
        """Callers holding a place: running plus waiting."""
        return self._reserved

    @property
    def running(self) -> int:
        return self._running

    def reserve(self) -> "Ticket":
        """A place in the bulkhead, or `error` when running and waiting are both full."""
        if self.enabled and self._reserved >= self.limit + self.queue:
            BULKHEAD_REJECTED.labels(self.name).inc()
            raise self._error(
                f"{self.name} is busy: {self._running} running and {self._reserved - self._running} waiting "
                f"(limit {self.limit} + queue {self.queue}); send again later",
                self.retry_after,
            )
        self._reserved += 1
        self._observe()
        return Ticket(self)

    def slot(self) -> "Ticket":
        """`async with bulkhead.slot():` for a caller that reserves and enters in one go."""
        return self.reserve()

    def _observe(self) -> None:
        BULKHEAD_RUNNING.labels(self.name).set(self._running)
        BULKHEAD_WAITING.labels(self.name).set(self._reserved - self._running)


class Ticket:
    """A reserved place in a `Bulkhead`: `async with ticket:` waits for a slot and gives it back; `release()`
    gives the place back without entering (the request turned out not to need it)."""

    def __init__(self, bulkhead: Bulkhead):
        self._bulkhead = bulkhead
        self._done = False

    async def __aenter__(self) -> Self:
        bulkhead = self._bulkhead
        if bulkhead._slots is not None:
            try:
                await bulkhead._slots.acquire()
            except BaseException:
                self.release()
                raise
        bulkhead._running += 1
        bulkhead._observe()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        bulkhead = self._bulkhead
        bulkhead._running -= 1
        if bulkhead._slots is not None:
            bulkhead._slots.release()
        self.release()

    def release(self) -> None:
        """Give the place back (idempotent)."""
        if self._done:
            return
        self._done = True
        self._bulkhead._reserved -= 1
        self._bulkhead._observe()
