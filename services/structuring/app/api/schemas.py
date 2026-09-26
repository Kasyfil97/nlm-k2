from typing import Literal

from pydantic import BaseModel, Field

from ocr_common.pipeline.schemas import (
    GuardrailsResult,
    OcrBoxPayload,
    OcrPayload,
    StructuringPayload,
)
from ocr_common.web.schemas import REQUEST_ID_EXAMPLE, JobStatusBase, SuccessEnvelope


class StructureRequest(BaseModel):
    """Body of the legacy synchronous `POST /v1/ocr_postprocess`.

    `texts` keeps `min_length=1` here **on purpose**, and only here: §11 lists this endpoint as kept
    as it is, and the ML team calls it directly with text it already has. The asynchronous job
    endpoint must not inherit the constraint -- see `StructuringJobRequest`.
    """

    texts: list[OcrBoxPayload] = Field(..., min_length=1, description="OCR text boxes in reading order (top to bottom)")


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
