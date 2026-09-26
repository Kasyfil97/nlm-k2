from collections.abc import Callable, Mapping
from typing import Any, cast

from starlette.concurrency import run_in_threadpool

from ocr_common.errors import BadRequest, UnprocessableEntity
from ocr_common.kk import DOCUMENT_TYPE, contract_fields, final_result
from ocr_common.pipeline import StagePipeline, Work
from ocr_common.pipeline.results import StageResults, load_upstream
from ocr_common.types import FinalResult, ScoringResult, StructuringResult

from app.services.confidence_service import ConfidenceService

Final = Callable[[Mapping[str, Any]], FinalResult]


class ScoringJobService:
    def __init__(
        self,
        pipeline: StagePipeline,
        confidence: ConfidenceService,
        confidence_threshold: float = 0.5,
        *,
        results: StageResults | None = None,
    ):
        self._pipeline = pipeline
        self._confidence = confidence
        self._confidence_threshold = confidence_threshold
        self._results = results

    async def submit(
        self,
        request_id: str,
        document_type: str,
        guardrails: dict[str, Any] | None,
        ocr: dict[str, Any] | None,
        structuring: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if structuring is None and self._results is None:
            raise UnprocessableEntity(
                "structuring is missing: the request refers to the structuring result by request_id, but this "
                "service has no DATABASE_URL to read structuring_results from",
            )
        work, final, chain_structuring = self._spec(request_id, document_type, guardrails, ocr, structuring)
        return await self._pipeline.submit(
            request_id,
            work,
            callback_result=final,
            outcome_data=lambda scoring: contract_fields(chain_structuring(), scoring, self._confidence_threshold),
            input={"document_type": document_type, "guardrails": guardrails},
        )

    async def resume(self, request_id: str, input: dict[str, Any] | None) -> None:
        """Run again a job a dead process left `PROCESSING`: structuring and OCR results are read from the
        database, the rest comes from the `input` stored when the job was claimed."""
        input = input or {}
        work, final, chain_structuring = self._spec(
            request_id, input.get("document_type") or DOCUMENT_TYPE, input.get("guardrails"), None, None
        )
        await self._pipeline.resume(
            request_id,
            work,
            callback_result=final,
            outcome_data=lambda scoring: contract_fields(chain_structuring(), scoring, self._confidence_threshold),
        )

    async def get(self, request_id: str) -> dict[str, Any]:
        return await self._pipeline.get(request_id)

    def _spec(
        self,
        request_id: str,
        document_type: str,
        guardrails: dict[str, Any] | None,
        ocr: dict[str, Any] | None,
        structuring: dict[str, Any] | None,
    ) -> tuple[Work, Final, Callable[[], Mapping[str, Any]]]:
        chain: dict[str, Any] = {}

        async def work() -> ScoringResult:
            if document_type != DOCUMENT_TYPE:
                raise BadRequest(f"Unsupported document_type: {document_type}. Supported: ['{DOCUMENT_TYPE}']")
            chain["structuring"] = (
                structuring
                if structuring is not None
                else await load_upstream(self._results, "structuring", request_id)
            )
            ocr_result = ocr
            if ocr_result is None and self._results is not None:
                ocr_result = await self._results.get("ocr", request_id)
            payload = self._confidence.payload_from_chain(guardrails, ocr_result, chain["structuring"])
            result = await run_in_threadpool(self._confidence.predict, payload)
            return {
                "document_type": document_type,
                "fields": result["fields"],
                "anggota_keluarga": result["anggota_keluarga"],
                "model": result.get("model"),
                "payload": payload,
            }

        def final(scoring: Mapping[str, Any]) -> FinalResult:
            # Dicast: keduanya JSON yang sudah divalidasi model muatannya di tahap asalnya,
            # bukan TypedDict yang bisa dibuktikan pemeriksa tipe di sini.
            return final_result(
                document_type,
                guardrails,
                cast(StructuringResult, chain["structuring"]),
                cast(ScoringResult, scoring),
            )

        # `contract_fields` needs the structuring result, not the bundled final one: the member
        # alignment check lives there and reads both lists. `work` fills `chain` before any callback
        # or outcome row is built, so by the time this is read it is populated.
        return work, final, lambda: chain["structuring"]
