from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ocr_common.kk import COLUMN_THRESHOLD_DESCRIPTION, parse_column_thresholds
from ocr_common.pipeline import STRUCTURING, checked_sequence
from ocr_common.pipeline.schemas import (
    GuardrailsResult,
    OcrBoxPayload,
    OcrPayload,
    StructuringPayload,
)
from ocr_common.web.schemas import PIPELINE_SEQUENCE_DESCRIPTION, REQUEST_ID_EXAMPLE, JobStatusBase, SuccessEnvelope

#: Upper bounds on one synchronous request, carried over from K2Regex-v2 (`input_limits`), which
#: fronts the same parser on an endpoint the ML team calls directly.
#:
#: They are cost bounds, not shape rules. The parser is pure Python with several loops over every
#: box, and nothing inside it gives up: without a cap the work a single request can ask for is
#: unbounded, and K2Regex-v2's own note is that the caps -- not its 30-second timeout -- are the
#: real bound, because a timeout returns 504 while the thread keeps running.
#:
#: A real Kartu Keluarga is ~180 boxes of a few characters each, so both leave three orders of
#: magnitude of headroom. Over either, the whole request is refused 422 rather than the offending
#: item quietly dropped: a card silently missing a line reads as a card the parser could not read.
MAX_OCR_ITEMS = 5000
MAX_TEXT_CHARS = 2048


class BoundedOcrBoxPayload(OcrBoxPayload):
    """`OcrBoxPayload` with the per-item text bound of this endpoint.

    Subclassed rather than tightened in `ocr_common`, because the cap belongs to this door: the
    stage-to-stage payload carries whatever our own OCR produced, and refusing it there would turn
    an odd recognition into a pipeline failure instead of an odd field.
    """

    text: str = Field(..., max_length=MAX_TEXT_CHARS, description="The recognised text")


class StructureRequest(BaseModel):
    """Body of the legacy synchronous `POST /v1/ocr_postprocess`.

    `texts` keeps `min_length=1` here **on purpose**, and only here: §11 lists this endpoint as kept
    as it is, and the ML team calls it directly with text it already has. The asynchronous job
    endpoint must not inherit the constraint -- see `StructuringJobRequest`.
    """

    texts: list[BoundedOcrBoxPayload] = Field(
        ...,
        min_length=1,
        max_length=MAX_OCR_ITEMS,
        description="OCR text boxes in reading order (top to bottom)",
    )


class StructureResponse(SuccessEnvelope):
    data: StructuringPayload


class StructuringJobRequest(BaseModel):
    """Body of `POST /v1/structuring/jobs` (§7.1)."""

    request_id: str = Field(
        ..., min_length=1, description="request_id of the pipeline run", examples=[REQUEST_ID_EXAMPLE]
    )
    document_type: str = Field("kk", description="Only `kk` is supported", examples=["kk"])
    guardrails: GuardrailsResult | None = Field(
        None, description="Guardrails result submitted with the OCR job; only forwarded to the next stage"
    )
    ocr: OcrPayload | None = Field(
        None,
        description=(
            "Result of the OCR stage. Left out when the OCR service hands off by reference "
            "(`PIPELINE_HANDOFF_BY_REFERENCE`, recommended for KK): this service then reads `ocr_results` "
            "of the shared database. Its `texts` MAY be empty -- an image with no readable text has to "
            "reach the validity gate to be rejected there, so this endpoint does not require at least one"
        ),
    )
    pipeline_name_sequence: list[str] | None = Field(
        None,
        description=PIPELINE_SEQUENCE_DESCRIPTION,
        examples=[["guardrails", "extraction", "structuring", "scoring"]],
    )

    @field_validator("pipeline_name_sequence")
    @classmethod
    def _sequence_includes_this_stage(cls, value: list[str] | None) -> list[str] | None:
        return checked_sequence(value, STRUCTURING)

    column_confidence_threshold: dict[str, float] | None = Field(
        None,
        description=COLUMN_THRESHOLD_DESCRIPTION,
        examples=[{"no_kk": 0.9, "nik": 0.8}],
    )

    @field_validator("column_confidence_threshold", mode="before")
    @classmethod
    def _column_thresholds_are_known_fields(cls, value: Any) -> dict[str, float] | None:
        return parse_column_thresholds(value)


class StructuringJobStatus(JobStatusBase):
    stage: Literal["STRUCTURING"] = Field(
        "STRUCTURING", description="Always `STRUCTURING` on this service", examples=["STRUCTURING"]
    )
    result: StructuringPayload | None = Field(
        None,
        description=(
            "The structured document once `status` is `DONE`; null otherwise. A **rejected** document is "
            "also `DONE` and also has a result -- what it carries is `reject_reason`. The orchestrator reads "
            "that key here and turns it into the client's 400"
        ),
    )


class StructuringJobStatusResponse(SuccessEnvelope):
    data: StructuringJobStatus


class StructuringDirectRequest(BaseModel):
    """`POST /v1/structuring-direct`: the previous stage's output, nothing of the pipeline run."""

    request_id: str | None = Field(
        None,
        description="Echoed in the response; optional, nothing is recorded under it",
        examples=[REQUEST_ID_EXAMPLE],
    )
    document_type: str = Field("kk", description="Only `kk` is supported; anything else is 400", examples=["kk"])
    ocr: OcrPayload = Field(
        ...,
        description=(
            "The extraction service's output: `data` of its `POST /v1/extraction/extract`, or the `result` of "
            "`GET /v1/extraction/jobs/{request_id}`. Only `texts` is read, and it may be empty"
        ),
    )


class StructuringDirectResponse(SuccessEnvelope):
    data: StructuringPayload
