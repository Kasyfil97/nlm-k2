from collections.abc import Mapping
from typing import Any

from app.ml.base import TrustModel


class ConfidenceService:
    """Builds the payload the trust model scores, and runs it.

    `payload` is kept whole in the stage result as an audit trail: §8.3 requires the numbers to be
    reproducible without re-running the pipeline, and that is only true if exactly what was scored
    is stored alongside what came out.
    """

    def __init__(self, model: TrustModel):
        self._model = model

    def predict(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._model.predict(payload)

    @staticmethod
    def payload_from_chain(
        guardrails: dict[str, Any] | None, ocr: dict[str, Any] | None, structuring: dict[str, Any]
    ) -> dict[str, Any]:
        """The scoring payload from the chained stage results.

        `guardrail_probability` is the raw model output, not the verdict's confidence: §10 types it
        as the feature. It is `None` when guardrails was skipped **and** when the verdict was
        `unassessable`, which the real model must impute rather than assume a number for -- the
        contract's §10 says `probability_bad` is a float, so a null here is a case the calibrator
        has to be told about explicitly.
        """
        document = (guardrails or {}).get("document") or {}
        ocr_result = ocr or {}
        return {
            "structuring": structuring,
            "guardrail_probability": document.get("probability_bad"),
            "guardrail_verdict": document.get("verdict"),
            "avg_doc_score": ocr_result.get("avg_doc_score"),
            "min_doc_score": ocr_result.get("min_doc_score"),
            "text_regions_count": ocr_result.get("text_regions_count"),
        }
