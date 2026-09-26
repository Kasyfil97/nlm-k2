from typing import cast

from fastapi import APIRouter, Depends, Request

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


# The §7.3 shape, as `jobs.py` documents it too. Flat: eleven document keys at the top level, then
# `anggota_keluarga` and `reject_reason`. Values are synthetic (`ocr_common.synthetic_kk`).
STRUCTURED_EXAMPLE = {
    "nomor_kk": _field("9924187486671285", 0.9991),
    "nama_kepala_keluarga": _field("BUDI SANTOSO", 0.9873),
    "alamat": _field("JL. MERDEKA NO. 12", 0.9642),
    "desa_kelurahan": _field("CIHAPIT", 0.9810),
    "rt": _field("003", 0.9755),
    "rw": _field("007", 0.9755),
    "kecamatan": _field("BANDUNG WETAN", 0.9888),
    "kabupaten_kota": _field("KOTA BANDUNG", 0.9901),
    "provinsi": _field("JAWA BARAT", 0.9934),
    "kode_pos": _field("40114", 0.9702),
    "tanggal_dikeluarkan": _field("12-03-2019", 0.9219),
    "anggota_keluarga": [
        {
            "nama_lengkap": _field("BUDI SANTOSO", 0.9954, 0.9931),
            "nik": _field("9908680101601956", 0.9975, 0.9887),
            "jenis_kelamin": _field("LAKI-LAKI", 0.9968, 0.9954),
            "tempat_lahir": _field("BANDUNG", 0.9891, 0.9803),
            "tanggal_lahir": _field("01-01-1960", 0.9903, 0.9877),
            "agama": _field("ISLAM", 0.9944, 0.9912),
            "pendidikan": _field("SD/SEDERAJAT", 0.9840, 0.9102),
            "jenis_pekerjaan": _field("KARYAWAN SWASTA", 0.9611, 0.8774),
            "golongan_darah": _field("-", 0.7120, 0.4415),
            "status_perkawinan": _field("KAWIN", 0.9821, 0.9688),
            "tanggal_perkawinan": _field("08-08-2010", 0.9560, 0.9341),
            "status_hubungan_dalam_keluarga": _field("KEPALA KELUARGA", 0.9788, 0.9440),
            "kewarganegaraan": _field("WNI", 0.9972, 0.9955),
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
        "Maps raw OCR boxes to the Kartu Keluarga fields: eleven document fields and fifteen per household "
        "member, each with two scores that are deliberately not fused (`ocr_conf` is how sure the recogniser "
        "was of the glyphs, `crf_conf` how sure the parser is that the text belongs in that column). "
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
    data = service.structure(texts)
    return envelope(200, "Success", data, get_request_id(request))
