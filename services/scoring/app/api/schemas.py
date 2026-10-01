from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ocr_common.kk import COLUMN_THRESHOLD_DESCRIPTION, parse_column_thresholds
from ocr_common.pipeline import SCORING, checked_sequence
from ocr_common.pipeline.schemas import (
    GuardrailsResult,
    OcrPayload,
    ScoringPayload,
    StructuringPayload,
)
from ocr_common.web.schemas import PIPELINE_SEQUENCE_DESCRIPTION, REQUEST_ID_EXAMPLE, JobStatusBase, SuccessEnvelope


class ConfidenceRequest(BaseModel):
    """Body of the synchronous `POST /v1/scoring/confidence` (§8.4).

    The structuring result is passed whole rather than pre-extracted into per-field signals: a
    Kartu Keluarga has a variable-length member list, so there is no fixed set of signal columns to
    flatten it into, and the model needs the two scores per field anyway.
    """

    structuring: StructuringPayload = Field(..., description="The structuring result, whole (§7.3)")
    guardrail_probability: float | None = Field(
        None,
        ge=0,
        le=1,
        description=(
            "`document.probability_bad` of the guardrails result -- the RAW model output, not the verdict's "
            "confidence. Null when guardrails was skipped, and also when the verdict was `unassessable`, "
            "which the model must impute rather than read as a number"
        ),
        examples=[0.0287],
    )
    guardrail_verdict: str | None = Field(
        None, description="`accepted`, `reject` or `unassessable`; null when guardrails was skipped"
    )
    avg_doc_score: float | None = Field(
        None, ge=0, le=1, description="Mean of the OCR box scores; null when there were none", examples=[0.814]
    )
    min_doc_score: float | None = Field(
        None, ge=0, le=1, description="Lowest OCR box score; null when there were none", examples=[0.2822]
    )
    text_regions_count: int | None = Field(None, ge=0, description="Number of OCR boxes", examples=[177])


class ConfidenceResponse(SuccessEnvelope):
    data: ScoringPayload


class ScoringJobRequest(BaseModel):
    """Body of `POST /v1/scoring/jobs` (§8.1)."""

    request_id: str = Field(
        ..., min_length=1, description="request_id of the pipeline run", examples=[REQUEST_ID_EXAMPLE]
    )
    document_type: str = Field("kk", description="Only `kk` is supported; anything else fails the job", examples=["kk"])
    guardrails: GuardrailsResult | None = Field(
        None, description="Guardrails result submitted with the OCR job; returned unchanged in the final result"
    )
    ocr: OcrPayload | None = Field(
        None, description="Result of the OCR stage; its aggregates feed the model as document-level features"
    )
    structuring: StructuringPayload | None = Field(
        None,
        description=(
            "Result of the structuring stage. Left out when the structuring service hands off by reference "
            "(`PIPELINE_HANDOFF_BY_REFERENCE`, recommended for KK): this service then reads "
            "`structuring_results` (and `ocr_results`) of the shared database"
        ),
    )
    pipeline_name_sequence: list[str] | None = Field(
        None,
        description=PIPELINE_SEQUENCE_DESCRIPTION,
        examples=[["guardrails", "ekstraksi", "structuring", "scoring"]],
    )

    @field_validator("pipeline_name_sequence")
    @classmethod
    def _sequence_includes_this_stage(cls, value: list[str] | None) -> list[str] | None:
        return checked_sequence(value, SCORING)

    column_confidence_threshold: dict[str, float] | None = Field(
        None,
        description=COLUMN_THRESHOLD_DESCRIPTION,
        examples=[{"no_kk": 0.9, "nik": 0.8}],
    )

    @field_validator("column_confidence_threshold", mode="before")
    @classmethod
    def _column_thresholds_are_known_fields(cls, value: Any) -> dict[str, float] | None:
        return parse_column_thresholds(value)


class ScoringJobResult(ScoringPayload):
    payload: dict[str, Any] | None = Field(
        None,
        description=(
            "Exactly what was scored, as an audit trail: §8.3 requires the numbers to be reproducible "
            "without re-running the pipeline, which is only true if the input is stored with the output"
        ),
    )


class ScoringJobStatus(JobStatusBase):
    stage: Literal["SCORING"] = Field("SCORING", description="Always `SCORING` on this service", examples=["SCORING"])
    result: ScoringJobResult | None = Field(None, description="Confidences once `status` is `DONE`; null otherwise")


class ScoringJobStatusResponse(SuccessEnvelope):
    data: ScoringJobStatus
