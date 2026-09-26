from fastapi import APIRouter, Depends, Form, Request, UploadFile

from ocr_common.errors import BadRequest
from ocr_common.web.envelope import envelope
from ocr_common.web.intake import FileField, FileUrlField, read_image
from ocr_common.web.request_id import get_request_id
from ocr_common.web.schemas import UNAUTHORIZED, error, success_examples
from ocr_common.web.security import verify_api_key

from app.api.schemas import GuardrailReportResponse
from app.config import Settings, get_settings
from app.dependencies import get_guardrails_service
from app.services.guardrails_service import REASON_REJECT, REASON_UNASSESSABLE, GuardrailsService

router = APIRouter(tags=["Guardrails"], dependencies=[Depends(verify_api_key)])

RID = "OCR_9cb01af2-493d-446d-b191-af120333f6d0"

_ACCEPTED_REPORT = {
    "passed": True,
    "reason": None,
    "document": {"verdict": "accepted", "confidence": 0.9713, "probability_bad": 0.0287, "threshold_used": 0.5},
}
_REJECTED_REPORT = {
    "passed": False,
    "reason": REASON_REJECT,
    "document": {"verdict": "reject", "confidence": 0.8821, "probability_bad": 0.8821, "threshold_used": 0.5},
}
_UNASSESSABLE_REPORT = {
    "passed": False,
    "reason": REASON_UNASSESSABLE,
    "document": {
        "verdict": "unassessable",
        "confidence": None,
        "probability_bad": None,
        "threshold_used": 0.5,
    },
}

ThresholdField = Form(
    None,
    description=(
        "Reject threshold for this request only, overriding every configured source. Strictly between 0 "
        "and 1; normally not sent. Ignored by the `remote` backend, which judges under its own"
    ),
    examples=[0.5],
)


def _validate_threshold(threshold: float | None) -> float | None:
    """Rung 1 of the R15 chain. Outside (0, 1) is a 400 and not a clamp: 0 would reject every
    document and 1 almost none, so a caller who sends one has a bug worth hearing about."""
    if threshold is None:
        return None
    if not 0 < threshold < 1:
        raise BadRequest(f"threshold must be strictly between 0 and 1, got {threshold}")
    return threshold


@router.post(
    "/v1/guardrails/check",
    response_model=GuardrailReportResponse,
    operation_id="checkDocument",
    summary="Judge a Kartu Keluarga image (internal: called by the orchestrator)",
    description=(
        "Runs the guardrails model on the image and answers a verdict, **without** starting anything: no "
        "OCR job, no callback. A Kartu Keluarga is one image, so there is one verdict and no page list. "
        "The model runs in this process (`kk_quality`) or in the ML team's quality service (`remote`, POST "
        "/v1/predict/json); the report is the same either way. The orchestrator calls it for every "
        "`extract-ocr` and forwards `data` unchanged to the OCR stage when `passed`.\n\n"
        "**Always 200 when the document was received**: read `data.passed`. An image the model cannot "
        'judge is `verdict: "unassessable"` with `probability_bad: null` and `passed: false`, not an '
        "error status. The 400s below are about the *request*, not about the document: an intake that "
        "names neither or both of `file` and `file_url`, a `threshold` outside (0, 1), a refused "
        "`file_url`, or a `file_url` while `GUARDRAILS_FETCH_URL=false`. Type and size are checked by the "
        "orchestrator before this endpoint is called."
    ),
    responses={
        200: success_examples(
            "The document was judged",
            accepted=("Accepted", envelope(200, "OK", _ACCEPTED_REPORT, RID)),
            rejected=("Rejected", envelope(200, "OK", _REJECTED_REPORT, RID)),
            unassessable=("Could not be judged", envelope(200, "OK", _UNASSESSABLE_REPORT, RID)),
        ),
        400: error(400, "Bad intake, or a threshold outside (0, 1)", "Send exactly one of file or file_url"),
        401: UNAUTHORIZED,
        422: error(
            422, "Validation Error", "body.threshold: Input should be a valid number", errors="VALIDATION_ERROR"
        ),
        500: error(500, "The guardrails model failed", "guardrails model returned an unexpected response"),
        503: error(503, "The guardrails model service (`remote`) is unreachable", "guardrails model is unavailable"),
        504: error(
            504,
            "The guardrails model service (`remote`) did not answer in time",
            "guardrails model timed out after 30.0s",
        ),
    },
)
async def check_document(
    request: Request,
    request_id: str | None = Form(None, description="Echoed in the response; optional", examples=[RID]),
    file: UploadFile | str | None = FileField,
    file_url: str | None = FileUrlField,
    threshold: float | None = ThresholdField,
    settings: Settings = Depends(get_settings),
    service: GuardrailsService = Depends(get_guardrails_service),
):
    override = _validate_threshold(threshold)
    if file_url and not settings.guardrails_fetch_url:
        # R18a: with the switch off this service never downloads, and the orchestrator is expected
        # to have fetched the bytes already. Saying so is better than silently ignoring the field.
        raise BadRequest("file_url is not accepted by this service (GUARDRAILS_FETCH_URL=false); send file instead")
    content, filename, content_type = await read_image(request, file, file_url)
    report = await service.check(filename, content_type, content, override=override)
    return envelope(200, "OK", report, request_id or get_request_id(request))
