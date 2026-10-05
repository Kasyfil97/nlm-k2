from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from starlette.concurrency import run_in_threadpool

from ocr_common.errors import UnprocessableEntity
from ocr_common.kk import DOCUMENT_TYPE, contract_data, contract_fields, final_result
from ocr_common.pipeline import StagePipeline, Work, stored
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
        sequence: Sequence[str] | None = None,
        column_thresholds: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        """Scoring is always the last service of a pipeline_name_sequence, so it ends every request it runs
        for; `sequence` is only kept with the job for the orchestrator's GET. `column_thresholds` (the central
        orchestrator's `column_confidence_threshold`) decide each field's 0/1 `confidence` before the trust
        model's own thresholds do (`kk.scored_fields`)."""
        if structuring is None and self._results is None:
            raise UnprocessableEntity(
                "structuring is missing: the request refers to the structuring result by request_id, but this "
                "service has no DATABASE_URL to read nilam_structuring_results from",
            )
        work, final, chain_structuring = self._spec(
            request_id, document_type, guardrails, ocr, structuring, column_thresholds
        )
        return await self._pipeline.submit(
            request_id,
            work,
            callback_result=final,
            outcome_data=self._outcome_data(chain_structuring, column_thresholds),
            input={
                "document_type": document_type,
                "guardrails": guardrails,
                "pipeline_name_sequence": stored(sequence),
                "column_confidence_threshold": dict(column_thresholds) if column_thresholds else None,
            },
        )

    async def resume(self, request_id: str, input: dict[str, Any] | None) -> None:
        """Run again a job a dead process left `PROCESSING`: structuring and OCR results are read from the
        database, the rest comes from the `input` stored when the job was claimed."""
        input = input or {}
        columns = input.get("column_confidence_threshold")
        work, final, chain_structuring = self._spec(
            request_id, input.get("document_type") or DOCUMENT_TYPE, input.get("guardrails"), None, None, columns
        )
        await self._pipeline.resume(
            request_id,
            work,
            callback_result=final,
            outcome_data=self._outcome_data(chain_structuring, columns),
        )

    async def get(self, request_id: str) -> dict[str, Any]:
        return await self._pipeline.get(request_id)

    def _outcome_data(
        self, chain_structuring: Callable[[], Mapping[str, Any]], column_thresholds: Mapping[str, float] | None
    ) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        """The outcome row's `data`: the 0/1 decisions stored with the result, so the row and the
        orchestrator's answer are the same projection of the same numbers. A result stored before
        `decisions` existed is decided again here."""

        def data(scoring: Mapping[str, Any]) -> Mapping[str, Any]:
            if scoring.get("decisions"):
                return contract_data(scoring["decisions"])
            return contract_fields(chain_structuring(), scoring, self._confidence_threshold, column_thresholds)

        return data

    def _spec(
        self,
        request_id: str,
        document_type: str,
        guardrails: dict[str, Any] | None,
        ocr: dict[str, Any] | None,
        structuring: dict[str, Any] | None,
        column_thresholds: Mapping[str, float] | None = None,
    ) -> tuple[Work, Final, Callable[[], Mapping[str, Any]]]:
        chain: dict[str, Any] = {}

        async def work() -> ScoringResult:
            chain["structuring"] = (
                structuring
                if structuring is not None
                else await load_upstream(self._results, "structuring", request_id)
            )
            ocr_result = ocr
            if ocr_result is None and self._results is not None:
                ocr_result = await self._results.get("ocr", request_id)
            return await run_in_threadpool(
                self._confidence.score,
                document_type,
                guardrails,
                ocr_result,
                chain["structuring"],
                self._confidence_threshold,
                column_thresholds,
            )

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
