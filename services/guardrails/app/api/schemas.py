"""The response shape of §5.2.

These mirror `ocr_common.pipeline.schemas.GuardrailsDocument` / `GuardrailsResult`, which are frozen:
the same block is what the orchestrator forwards to `/v1/extraction/jobs` and what reaches scoring
unchanged. They are restated here rather than imported because this is the *producing* end and its
fields are required, while the pipeline copies are permissive by design -- they have to survive a
field being added upstream.
"""

from typing import Literal

from pydantic import BaseModel, Field

from ocr_common.web.schemas import SuccessEnvelope

Verdict = Literal["accepted", "reject", "unassessable"]


class DocumentResult(BaseModel):
    verdict: Verdict = Field(
        ...,
        description=(
            "`reject` when `probability_bad` reaches `threshold_used`, `accepted` below it or when the request sent "
            "no threshold, and "
            "`unassessable` when the model could not judge the image at all (undecodable, not an image, "
            "or dimensions outside the range it was trained on). `unassessable` is a verdict and not an "
            "error: this endpoint always answers 200"
        ),
        examples=["accepted"],
    )
    confidence: float | None = Field(
        ...,
        ge=0,
        le=1,
        description=(
            "Confidence in the verdict: `probability_bad` when rejected, `1 - probability_bad` when "
            "accepted, null when unassessable"
        ),
        examples=[0.9713],
    )
    probability_bad: float | None = Field(
        ...,
        ge=0,
        le=1,
        description=(
            "Raw model output: the probability that the image is bad. Travels unchanged to scoring as the "
            "`guardrail_probability` feature. Null when the verdict is `unassessable`"
        ),
        examples=[0.0287],
    )
    threshold_used: float | None = Field(
        None,
        gt=0,
        lt=1,
        description=(
            "The threshold that decided: the request's `threshold` form field. Null when the request sent "
            "none (the document then passes), and with the `remote` backend when that service does not state "
            "its own"
        ),
        examples=[0.5],
    )
    threshold_target: Literal["accept", "reject"] | None = Field(
        None,
        description=(
            "The side `threshold_used` applied to: `reject` (rejected when `probability_bad >= threshold_used`) "
            "or `accept` (accepted when `1 - probability_bad >= threshold_used`), as the request sent it. Null "
            "when the request sent no threshold, and with the `remote` backend"
        ),
        examples=["reject"],
    )


class GuardrailReport(BaseModel):
    passed: bool = Field(
        ...,
        description="true: the document may go on to the OCR stage. false: stop, and answer the client with `reason`",
        examples=[True],
    )
    reason: str | None = Field(
        None,
        description=(
            "Why the document was stopped, in Indonesian and shown to the end user as it stands; null when "
            "`passed`. The model is binary and judges the image as a whole, so the reason cannot say which "
            "part is wrong -- only whether the quality was too low or the image could not be judged at all"
        ),
        examples=[None],
    )
    document: DocumentResult


class GuardrailReportResponse(SuccessEnvelope):
    message: str = Field("OK", description="Human-readable outcome", examples=["OK"])
    data: GuardrailReport
