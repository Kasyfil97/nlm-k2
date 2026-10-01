"""Validate the upload, run the OCR backend **off the event loop**, and shape the §7.1 result."""

import time

from starlette.concurrency import run_in_threadpool

from ocr_common.image_validation import validate_image
from ocr_common.kk import ocr_aggregates
from ocr_common.types import OcrResult

from app.config import Settings
from app.ml.base import OcrEngine, RemoteOcrModel


class ExtractionService:
    """The OCR stage's one piece of work: bytes in, `OcrResult` out.

    This is the single place that decides how a backend leaves the event loop, and that is the
    reason it exists as a layer at all. `StagePipeline._run_bound` awaits `work()` on the loop and
    the Unit 3 heartbeat is a loop task, so a synchronous in-process model called directly would
    freeze the beat for the whole inference -- the very case the beat was added for. Another
    replica's reaper would then reclaim a job that is running fine.

    An in-process backend (`OcrRecognizer`) is therefore synchronous by contract and goes through
    `run_in_threadpool`; a backend served over HTTP (`RemoteOcrModel`) is already off the loop and
    is awaited as it is. The branch is duck-typed -- `RemoteOcrModel` is a `runtime_checkable`
    Protocol, so the test is "does it have `extract` and `aclose`", the same shape of check
    guardrails makes between its two classifier kinds, with the difference that this one also
    narrows the union for the type checker instead of leaving it as `object`.
    """

    def __init__(self, engine: OcrEngine, settings: Settings):
        self._engine = engine
        self._settings = settings

    async def extract(self, filename: str, content_type: str | None, content: bytes) -> OcrResult:
        validate_image(content_type, content, self._settings)

        started = time.perf_counter()
        engine = self._engine
        if isinstance(engine, RemoteOcrModel):
            result = await engine.extract(filename, content, content_type)
        else:
            result = await run_in_threadpool(engine.read, filename, content, content_type)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

        texts = result["texts"]
        # Derived here, never reported by the model: §7.1 defines the three aggregates as functions
        # of `texts[].score`, and `ocr_aggregates` is the one definition the whole repo shares. It
        # also gets the empty case right -- both aggregates null rather than zero.
        aggregates = ocr_aggregates(texts)
        return {
            "engine": engine.name,
            "model": result.get("model"),
            "elapsed_ms": elapsed_ms,
            "text_regions_count": aggregates["text_regions_count"],
            "avg_doc_score": aggregates["avg_doc_score"],
            "min_doc_score": aggregates["min_doc_score"],
            "texts": texts,
        }
