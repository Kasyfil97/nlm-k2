from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ocr_common.web.envelope import envelope
from ocr_common.web.request_id import get_request_id
from ocr_common.web.schemas import REQUEST_ID_EXAMPLE, UNAUTHORIZED, error, success_examples
from ocr_common.web.security import verify_api_key

from app.api.schemas import ScoringDirectRequest, ScoringDirectResponse
from app.api.scoring import CONFIDENCE_EXAMPLE, CONFIDENCE_PAYLOAD_EXAMPLE
from app.config import Settings, get_settings
from app.dependencies import get_confidence_service
from app.services.confidence_service import ConfidenceService

router = APIRouter(tags=["Direct"], dependencies=[Depends(verify_api_key)])


def _decided(value: str, confidence: int, threshold: float | None) -> dict:
    return {"value": value, "confidence": confidence, "threshold": threshold}


_RESULT_EXAMPLE = {
    **CONFIDENCE_EXAMPLE,
    "payload": CONFIDENCE_PAYLOAD_EXAMPLE,
    "decisions": {
        "no_kk": _decided("9901012609260001", 0, None),
        "nama_kepala_keluarga": _decided("BUDI SANTOSO", 1, 0.8),
        "anggota_keluarga": [
            {
                "nama_lengkap": _decided("BUDI SANTOSO", 1, 0.9),
                "nik": _decided("9901011203850001", 1, 0.9637),
                "pendidikan": _decided("S1", 0, 0.9929),
                "jenis_pekerjaan": _decided("KARYAWAN SWASTA", 0, 0.9),
                "status_hubungan_dalam_rumah_tangga": _decided("KEPALA KELUARGA", 1, 0.9),
                "ayah": _decided("SUTRISNO", 0, 0.9353),
                "ibu": _decided("SITI AMINAH", 0, 0.9),
            }
        ],
    },
}


@router.post(
    "/v1/scoring-direct",
    response_model=ScoringDirectResponse,
    operation_id="scoreDirect",
    summary="Score a structured document now, synchronous (no job, no callback)",
    description=(
        "**For testing one stage on its own (QC).** Takes the output of the previous stages, the structuring "
        "result (`data` of `POST /v1/structuring-direct`, or the `result` of a structuring job) and, like the "
        "pipeline, the OCR result and the guardrails report it depends on, builds the scoring payload from "
        "them, runs the trust model, and answers with the scoring result in the body. Nothing is recorded: no "
        "`scoring_jobs` row, no callback, no outcome row, so it never touches the orchestrator's data.\n\n"
        "The answer is exactly what `GET /v1/scoring/jobs/{request_id}` reports as `result`: the probabilities "
        "per field (internal names), the trust model's own thresholds, the exact payload that was scored, and "
        "`decisions`, the 0/1 per contract field with the threshold used (`column_confidence_threshold` of the "
        "request, else the trust model's own). `ocr` left out gives null document scores, `guardrails` left out "
        "a null guardrails probability; the model works with that input missing."
    ),
    responses={
        200: success_examples(
            "The structured document was scored",
            scored=("A household of one", envelope(200, "Success", _RESULT_EXAMPLE, REQUEST_ID_EXAMPLE)),
        ),
        400: error(400, "An unsupported `document_type`", "Unsupported document_type: ktp. Supported: ['kk']"),
        401: UNAUTHORIZED,
        422: error(422, "Validation Error", "body.structuring: Field required", errors="VALIDATION_ERROR"),
    },
)
async def score_direct(
    request: Request,
    body: ScoringDirectRequest,
    service: ConfidenceService = Depends(get_confidence_service),
    settings: Settings = Depends(get_settings),
):
    guardrails = body.guardrails.model_dump(exclude_unset=True) if body.guardrails is not None else None
    ocr = body.ocr.model_dump() if body.ocr is not None else None
    data = await run_in_threadpool(
        service.score,
        body.document_type,
        guardrails,
        ocr,
        body.structuring.model_dump(),
        settings.field_confidence_threshold,
        body.column_confidence_threshold,
    )
    return envelope(200, "Success", data, body.request_id or get_request_id(request))
