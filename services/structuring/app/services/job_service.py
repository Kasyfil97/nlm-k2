from collections.abc import Mapping
from typing import Any

from starlette.concurrency import run_in_threadpool

from ocr_common.errors import UnprocessableEntity
from ocr_common.kk import DOCUMENT_TYPE
from ocr_common.pipeline import STAGE_SCORING, HandoffPayload, StagePipeline, Work
from ocr_common.pipeline.results import StageResults, load_upstream
from ocr_common.types import OcrBox, StructuringResult

from app.services.structuring_service import StructuringService

Handoff = HandoffPayload


def _rejection(structuring: Mapping[str, Any]) -> str | None:
    """A rejecting rule of the KK validity gate stops the pipeline here: no scoring, a 400 for the client.

    Three lines, and this is the ONLY stage that supplies the hook. It merely *reads* a key the
    result already carries -- the reason is produced by the rules and lives in the payload, which is
    what lets a stateless orchestrator turn it into the client's 400 by reading
    `GET /v1/structuring/jobs/{request_id}`. A reason kept only in the outcome row could never get
    there: a `DONE` job with no marker is indistinguishable from a hand-off still in flight.
    """
    return structuring.get("reject_reason") or None


class StructuringJobService:
    def __init__(
        self,
        pipeline: StagePipeline,
        structuring: StructuringService,
        *,
        results: StageResults | None = None,
        handoff_by_reference: bool = False,
    ):
        self._pipeline = pipeline
        self._structuring = structuring
        self._results = results
        self._handoff_by_reference = handoff_by_reference

    async def submit(
        self, request_id: str, document_type: str, guardrails: dict[str, Any] | None, ocr: dict[str, Any] | None
    ) -> dict[str, Any]:
        if ocr is None and self._results is None:
            raise UnprocessableEntity(
                "ocr is missing: the request refers to the OCR result by request_id, but this service has no "
                "DATABASE_URL to read ocr_results from",
            )
        work, handoff = self._spec(request_id, document_type, guardrails, ocr)
        return await self._pipeline.submit(
            request_id,
            work,
            handoff_payload=handoff,
            next_stage=STAGE_SCORING,
            rejection=_rejection,
            input={"document_type": document_type, "guardrails": guardrails},
        )

    async def resume(self, request_id: str, input: dict[str, Any] | None) -> None:
        """Run again a job a dead process left `PROCESSING`: the OCR result is read from the database,
        the rest comes from the `input` stored when the job was claimed."""
        input = input or {}
        work, handoff = self._spec(
            request_id, input.get("document_type") or DOCUMENT_TYPE, input.get("guardrails"), None
        )
        await self._pipeline.resume(
            request_id, work, handoff_payload=handoff, next_stage=STAGE_SCORING, rejection=_rejection
        )

    async def get(self, request_id: str) -> dict[str, Any]:
        return await self._pipeline.get(request_id)

    def _spec(
        self, request_id: str, document_type: str, guardrails: dict[str, Any] | None, ocr: dict[str, Any] | None
    ) -> tuple[Work, Handoff]:
        chain: dict[str, Any] = {}

        async def work() -> StructuringResult:
            chain["ocr"] = ocr if ocr is not None else await load_upstream(self._results, "ocr", request_id)
            texts: list[OcrBox] = [
                {
                    "text": box.get("text") or "",
                    "score": box.get("score", 1.0),
                    "poly": box.get("poly") or [],
                }
                for box in chain["ocr"].get("texts") or []
            ]
            # MAY be empty: an image with no readable text must reach the validity gate to be
            # rejected there (§7.4, first rule), not refused earlier as a bad request.
            return await run_in_threadpool(self._structuring.structure, texts)

        def handoff(structuring: Mapping[str, Any]) -> dict[str, Any]:
            body: dict[str, Any] = {"request_id": request_id, "document_type": document_type, "guardrails": guardrails}
            if not self._handoff_by_reference:
                body.update(ocr=chain["ocr"], structuring=dict(structuring))
            return body

        return work, handoff
