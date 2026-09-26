from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ocr_common.web.envelope import envelope
from ocr_common.web.request_id import get_request_id
from ocr_common.web.schemas import REQUEST_ID_EXAMPLE, UNAUTHORIZED, error, success_examples
from ocr_common.web.security import verify_api_key

from app.api.schemas import ConfidenceRequest, ConfidenceResponse
from app.dependencies import get_confidence_service
from app.services.confidence_service import ConfidenceService

router = APIRouter(tags=["Scoring"], dependencies=[Depends(verify_api_key)])

# What `jobs.py` shows as the audit trail. Imported from here, which is why this module is rewritten
# before `jobs.py` compiles.
CONFIDENCE_PAYLOAD_EXAMPLE = {
    "structuring": "{...the §7.3 structuring result, whole...}",
    "guardrail_probability": 0.0287,
    "guardrail_verdict": "accepted",
    "avg_doc_score": 0.814,
    "min_doc_score": 0.2822,
    "text_regions_count": 177,
}

CONFIDENCE_EXAMPLE = {
    "document_type": "kk",
    "fields": {"nomor_kk": 0.9412, "nama_kepala_keluarga": 0.8871},
    "anggota_keluarga": [
        {
            "nama_lengkap": 0.9702,
            "nik": 0.9655,
            "pendidikan": 0.8410,
            "jenis_pekerjaan": 0.7733,
            "status_hubungan_dalam_keluarga": 0.9218,
            "ayah": 0.8064,
            "ibu": 0.7951,
        },
        {
            "nama_lengkap": 0.9333,
            "nik": 0.9510,
            "pendidikan": 0.7126,
            "jenis_pekerjaan": None,
            "status_hubungan_dalam_keluarga": 0.9047,
            "ayah": 0.7702,
            "ibu": 0.7588,
        },
    ],
    "model": "kk-trust-mock-v1",
}


@router.post(
    "/v1/scoring/confidence",
    response_model=ConfidenceResponse,
    operation_id="predictConfidence",
    summary="Per-field confidence from the trust model, synchronous (no job, no callback)",
    description=(
        "P(each extracted field is correct), after fusing the two structuring scores and calibrating. "
        "Only the **nine contract fields** are scored -- two document fields and seven per household member -- "
        "not all 26 that structuring extracts: each field needs its own calibrator, and training 26 of them "
        "for 9 numbers anyone reads would be waste. The keys here are the INTERNAL names; the rename to the "
        "outgoing contract happens in the orchestrator."
        "\n\n"
        "`anggota_keluarga` comes back with exactly as many rows as were sent, in the same order. There is no "
        "document-level score and no approve/reject decision: the threshold belongs to the caller."
    ),
    responses={
        200: success_examples(
            "P(correct) per contract field",
            scored=(
                "Two members; the second one's occupation was not read, so it scores null",
                envelope(200, "Success", CONFIDENCE_EXAMPLE, REQUEST_ID_EXAMPLE),
            ),
        ),
        401: UNAUTHORIZED,
        422: error(422, "Validation Error", "body.structuring: Field required", errors="VALIDATION_ERROR"),
    },
)
async def confidence(
    request: Request,
    body: ConfidenceRequest,
    service: ConfidenceService = Depends(get_confidence_service),
):
    data = await run_in_threadpool(service.predict, body.model_dump())
    return envelope(200, "Success", data, get_request_id(request))
