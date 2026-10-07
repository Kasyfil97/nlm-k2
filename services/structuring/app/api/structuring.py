from typing import cast

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ocr_common.types import OcrBox
from ocr_common.web.envelope import envelope
from ocr_common.web.request_id import get_request_id
from ocr_common.web.schemas import REQUEST_ID_EXAMPLE, UNAUTHORIZED, error, success_examples
from ocr_common.web.security import verify_api_key

from app.api.schemas import StructureRequest, StructureResponse
from app.dependencies import get_structuring_service
from app.services.structuring_service import StructuringService

router = APIRouter(tags=["Structuring"], dependencies=[Depends(verify_api_key)])


def _field(value: str, ocr_conf: float | None, crf_conf: float | None = None) -> dict:
    return {"value": value, "ocr_conf": ocr_conf, "crf_conf": crf_conf}


# The §7.3 shape, as `jobs.py` documents it too. Flat: the two document keys at the top level, then
# `anggota_keluarga` (seven keys per member) and `reject_reason`. Values are synthetic
# (`ocr_common.synthetic_kk`); `features` is left out of the example for length.
STRUCTURED_EXAMPLE = {
    "nomor_kk": _field("9924187486671285", 0.9991),
    "nama_kepala_keluarga": _field("BUDI SANTOSO", 0.9873),
    "anggota_keluarga": [
        {
            "nama_lengkap": _field("BUDI SANTOSO", 0.9954, 0.9931),
            "nik": _field("9908680101601956", 0.9975, 0.9887),
            "pendidikan": _field("SD/SEDERAJAT", 0.9840, 0.9102),
            "jenis_pekerjaan": _field("KARYAWAN SWASTA", 0.9611, 0.8774),
            "status_hubungan_dalam_keluarga": _field("KEPALA KELUARGA", 0.9788, 0.9440),
            "ayah": _field("RIZKY SANTOSO", 0.9705, 0.8312),
            "ibu": _field("NURUL NURHALIZA", 0.9682, 0.8190),
        }
    ],
    "reject_reason": None,
}

# A rejected document is still a complete object: every key present, values empty, both scores null.
# §7.4 says so explicitly, and the orchestrator depends on `reject_reason` being readable here.
REJECTED_EXAMPLE = {
    **{key: _field("", None) for key in STRUCTURED_EXAMPLE if key not in ("anggota_keluarga", "reject_reason")},
    "anggota_keluarga": [],
    "reject_reason": "Gambar tidak memuat teks yang terbaca, mohon unggah foto Kartu Keluarga",
}


@router.post(
    "/v1/ocr_postprocess",
    response_model=StructureResponse,
    operation_id="structureTexts",
    summary="Turn OCR boxes into named fields, synchronous (no job, no callback)",
    description=(
        "Maps raw OCR boxes to the Kartu Keluarga fields the contract carries: two document fields and seven "
        "per household member, each with two scores that are deliberately not fused (`ocr_conf` is how sure "
        "the recogniser was of the glyphs, `crf_conf` how sure the parser is that the text belongs in that column). "
        "\n\n"
        "This is the existing `K2Regex-v2` endpoint, kept as it is for the ML team's own calls, which is why "
        "`texts` still requires at least one box here. The asynchronous job endpoint deliberately does not: an "
        "image with no readable text has to reach the validity gate to be rejected there."
    ),
    responses={
        200: success_examples(
            "The fields found in the boxes",
            accepted=("A readable card", envelope(200, "Success", STRUCTURED_EXAMPLE, REQUEST_ID_EXAMPLE)),
            rejected=(
                "Nothing readable: a complete object with empty values and a reason",
                envelope(200, "Success", REJECTED_EXAMPLE, REQUEST_ID_EXAMPLE),
            ),
        ),
        401: UNAUTHORIZED,
        422: error(422, "Validation Error", "body.texts: Field required", errors="VALIDATION_ERROR"),
    },
)
async def structure(
    request: Request,
    body: StructureRequest,
    service: StructuringService = Depends(get_structuring_service),
):
    texts = [cast(OcrBox, box.model_dump()) for box in body.texts]
    # Off the event loop, like the job path does. The parser is CPU-bound and pure Python: a card
    # takes tens of milliseconds, but nothing here bounds how long an adversarial payload takes, and
    # on the loop that time is not this request's alone -- it stalls `/health`, the outbox relay and
    # every job this worker is carrying. K2Regex-v2 runs the same call through `asyncio.to_thread`
    # for the same reason. `StructureRequest` bounds the input; this bounds the blast radius.
    data = await run_in_threadpool(service.structure, texts)
    return envelope(200, "Success", data, get_request_id(request))
