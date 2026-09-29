import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, cast

from prometheus_client import Counter

from ocr_common.errors import NotFound
from ocr_common.kk import DOCUMENT_TYPE, final_result
from ocr_common.pipeline import DEFAULT_SEQUENCE, GUARDRAILS, STAGE_OF, STAGE_SCORING, STAGE_STRUCTURING, STATUS_DONE
from ocr_common.types import ScoringResult, StructuringResult

from app.clients.ekstraksi import EkstraksiJobClient
from app.clients.guardrails import GuardrailsClient, GuardrailsThreshold
from app.config import Settings
from app.services.document_checks import check_document
from app.services.guardrails_log import GuardrailsLog, NoGuardrailsLog
from app.services.pipeline_waiter import PipelineWait, WaitOutcome

logger = logging.getLogger(__name__)

GUARDRAILS_SKIPPED = Counter(
    "guardrails_skipped_total",
    "Documents sent into the pipeline without the guardrails model (a pipeline_name_sequence without guardrails)",
)


class ExtractOcrService:
    """The pipeline behind `extract-ocr`: the guardrails check, the hand-off to the OCR stage, and the
    wait for OCR -> structuring -> scoring, or for the services the request's pipeline_name_sequence names."""

    def __init__(
        self,
        guardrails: GuardrailsClient,
        ekstraksi: EkstraksiJobClient,
        waiter: PipelineWait,
        settings: Settings,
        log: GuardrailsLog | None = None,
    ):
        self._guardrails = guardrails
        self._log = log or NoGuardrailsLog()
        self._ekstraksi = ekstraksi
        self._waiter = waiter
        self._settings = settings

    async def submit(
        self,
        request_id: str,
        document_type: str,
        filename: str,
        content_type: str | None,
        content: bytes,
        *,
        received_at: float | None = None,
        file_url: str | None = None,
        sequence: Sequence[str] = DEFAULT_SEQUENCE,
        guardrails_threshold: GuardrailsThreshold | None = None,
        column_thresholds: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        """Check the file (type, empty, `MAX_UPLOAD_BYTES`, `MAX_DOCUMENT_PAGES`) before anyone else sees it,
        judge it with the guardrails model, hand it to the OCR stage when it passes, and wait for the
        pipeline for what is left of `PIPELINE_WAIT_SECONDS` since `received_at`.

        `sequence` (pipeline_name_sequence, validated by the caller) names the services to run. Without
        `guardrails` the model is left out (the file checks still run): the stages get no guardrails report,
        so scoring imputes its guardrail probability, and the structuring rules still reject. With only
        `guardrails` the report is the answer and nothing enters the pipeline. Otherwise the stages hand
        the job on up to the last service of `sequence`, and that one's result is the answer."""
        started = time.monotonic() if received_at is None else received_at
        n_pages = check_document(content_type, content, self._settings)
        if GUARDRAILS in sequence:
            report = await self._guardrails.check(request_id, filename, content_type, content, guardrails_threshold)
            await self._log.record(
                request_id,
                report,
                threshold_from_request=guardrails_threshold is not None,
                n_pages=n_pages,
                sequence=sequence,
            )
            if not report["passed"]:
                return {**report, "job": None, "pipeline": None, "result": None}
            verdict: dict[str, Any] = report
            if tuple(sequence) == (GUARDRAILS,):
                done = {"stage": STAGE_OF[GUARDRAILS], "status": STATUS_DONE, "error_message": None}
                return {**report, "job": None, "pipeline": done, "result": report}
        else:
            logger.info("guardrails left out for request_id %s (pipeline_name_sequence %s)", request_id, sequence)
            GUARDRAILS_SKIPPED.inc()
            report = None
            verdict = {"passed": True, "reason": None}

        job = await self._ekstraksi.submit(
            request_id,
            document_type,
            report,
            filename,
            content_type,
            content,
            file_url=file_url,
            sequence=sequence,
            column_thresholds=column_thresholds,
        )
        wait_seconds = self._settings.pipeline_wait_seconds
        if wait_seconds <= 0:
            return {**verdict, "job": job, "pipeline": None, "result": None}

        remaining = wait_seconds - (time.monotonic() - started)
        outcome = await self._waiter.wait(request_id, remaining, last_stage=STAGE_OF[sequence[-1]])
        return {**verdict, "job": job, **_pipeline(document_type, report, outcome)}

    async def status(self, request_id: str) -> dict[str, Any]:
        """Where the request is now, read from the stages without waiting. A request no stage has a job for is
        answered from its last guardrails verdict (`_judged_only`); 404 when there is none either. The
        guardrails report is not stored with the stages, so it is not part of a pipeline result."""
        outcome = await self._waiter.snapshot(request_id)
        if outcome is None:
            judged = await self._judged_only(request_id)
            if judged is None:
                raise NotFound(f"No request found for request_id {request_id}")
            return judged
        return {
            "passed": True,
            "reason": None,
            **_pipeline(DOCUMENT_TYPE, None, outcome),
            "column_thresholds": outcome.column_thresholds,
        }

    async def _judged_only(self, request_id: str) -> dict[str, Any] | None:
        """A request no stage has a job for, answered from its last guardrails verdict, as its POST was: rejected
        (the 400), or guardrails was its only service (the report as `data`). None otherwise: never judged, the
        verdict was not kept, or it passed and its hand-off to ekstraksi failed (nothing ran)."""
        verdict = await self._log.latest(request_id)
        if verdict is None:
            return None
        report = verdict["report"]
        if not report.get("passed"):
            return {**report, "job": None, "pipeline": None, "result": None}
        if verdict["sequence"] == [GUARDRAILS]:
            done = {"stage": STAGE_OF[GUARDRAILS], "status": STATUS_DONE, "error_message": None}
            return {**report, "job": None, "pipeline": done, "result": report}
        return None


def _pipeline(document_type: str, report: dict[str, Any] | None, outcome: WaitOutcome) -> dict[str, Any]:
    """`result` once the pipeline is DONE: the final result when scoring ended it, else the result of the
    stage that did (the last of the sequence), as it is. `pipeline.stage` names that stage."""
    result = None
    if outcome.status == STATUS_DONE and outcome.stage == STAGE_SCORING:
        # Dicast: yang datang dari tahap adalah JSON yang sudah divalidasi model muatannya di sana,
        # bukan TypedDict yang bisa dibuktikan pemeriksa tipe di sini.
        result = final_result(
            document_type,
            report,
            cast(StructuringResult, outcome.results[STAGE_STRUCTURING]),
            cast(ScoringResult, outcome.results[STAGE_SCORING]),
        )
    elif outcome.status == STATUS_DONE:
        result = outcome.results[outcome.stage]
    pipeline = {"stage": outcome.stage, "status": outcome.status, "error_message": outcome.error_message}
    return {"pipeline": pipeline, "result": result}
