from collections.abc import Mapping
from typing import Any, cast

from ocr_common.errors import BadRequest
from ocr_common.kk import DOCUMENT_TYPE, scored_fields
from ocr_common.types import ScoringResult

from app.ml.base import TrustModel


class ConfidenceService:
    """Builds the payload the trust model scores, and runs it.

    `payload` is kept whole in the stage result as an audit trail: §8.3 requires the numbers to be
    reproducible without re-running the pipeline, and that is only true if exactly what was scored
    is stored alongside what came out.
    """

    def __init__(self, model: TrustModel, claim_gate_p_bad: float = 0.0704, claim_gate_p_crop: float = 0.777):
        self._model = model
        self._claim_gate_p_bad = claim_gate_p_bad
        self._claim_gate_p_crop = claim_gate_p_crop

    def predict(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._model.predict(payload)

    def score(
        self,
        document_type: str,
        guardrails: dict[str, Any] | None,
        ocr: dict[str, Any] | None,
        structuring: dict[str, Any],
        column_thresholds: Mapping[str, float] | None = None,
    ) -> ScoringResult:
        """The scoring stage's result for the chained stage results: the payload built from them, the trust
        model's probabilities, and the decision per contract field with the threshold that decided it (0/1, or the
        probability itself for a field the request gave no threshold). The
        same for a job and for `/v1/scoring-direct`."""
        if document_type != DOCUMENT_TYPE:
            raise BadRequest(f"Unsupported document_type: {document_type}. Supported: ['{DOCUMENT_TYPE}']")
        payload = self.payload_from_chain(guardrails, ocr, structuring)
        result = self.predict(payload)
        stored: dict[str, Any] = {
            "document_type": document_type,
            "fields": result["fields"],
            "anggota_keluarga": result["anggota_keluarga"],
            "model": result.get("model"),
            "payload": payload,
        }
        # The model's thresholds and bin edges are STORED with the scores for audit; they no longer decide a
        # field (only the request's `column_confidence_threshold` does).
        for key in ("thresholds", "bin_edges"):
            if result.get(key):
                stored[key] = result[key]
        # The decision per contract field, with the threshold that decided it (as nilam stores its
        # `fields`): the outcome row and the orchestrator's POST / GET all project from this one place.
        # The claim gate is decided here, once, and stored: every projection of `decisions` reads it from the
        # stored result (`scored_fields`), so no reader can come to a different answer.
        stored["claim_gate"] = self.claim_gate(guardrails)
        stored["decisions"] = scored_fields(structuring, stored, column_thresholds)
        return cast(ScoringResult, stored)

    def claim_gate(self, guardrails: dict[str, Any] | None) -> dict[str, Any]:
        """Whether this document's fields may be claimed at all (`confidence` 1), with the numbers that decided.

        Open only when both guardrails probabilities are there and below their limits. A missing one closes it:
        a run without guardrails, an unassessable image or a backend without the crop model gives no evidence
        that the card is sharp and whole, and leaving guardrails out of the request must not be a way around it.
        """
        document = (guardrails or {}).get("document") or {}
        p_bad, p_crop = _number(document.get("probability_bad")), _number(document.get("probability_crop"))
        is_open = p_bad is not None and p_crop is not None
        is_open = is_open and p_bad < self._claim_gate_p_bad and p_crop < self._claim_gate_p_crop
        return {
            "open": is_open,
            "probability_bad": p_bad,
            "probability_crop": p_crop,
            "p_bad_max": self._claim_gate_p_bad,
            "p_crop_max": self._claim_gate_p_crop,
        }

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


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
