"""Response models of this service. The OCR payload itself is **not** defined here.

`OcrPayload` / `OcrBoxPayload` are frozen in `ocr_common.pipeline.schemas` because three services
have to agree on them (§7.1, §10): this stage writes them, structuring and scoring read them. A
local copy would be a second definition of a frozen shape, and the first drift between the two would
show up as a 422 at the next stage rather than as a failing test here.
"""

from typing import Literal

from pydantic import Field

from ocr_common.pipeline.schemas import OcrPayload
from ocr_common.web.schemas import JobStatusBase, SuccessEnvelope


class OcrJobStatus(JobStatusBase):
    stage: Literal["OCR"] = Field("OCR", description="Always `OCR` on this service", examples=["OCR"])
    result: OcrPayload | None = Field(
        None,
        description=(
            "The OCR result once `status` is `DONE`; null otherwise. Its `texts` may be EMPTY: an image "
            "with no readable text is a successful OCR job (§6.3) and is rejected by the structuring "
            "rules one stage later, not here"
        ),
    )


class OcrJobStatusResponse(SuccessEnvelope):
    data: OcrJobStatus


class ExtractResponse(SuccessEnvelope):
    """`POST /v1/ekstraksi/extract`: the same §7.1 payload a job stores, answered directly."""

    data: OcrPayload
