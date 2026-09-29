import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile

from ocr_common.config import DEFAULT_MAX_UPLOAD_BYTES
from ocr_common.image_validation import PAYLOAD_TOO_LARGE_MESSAGE, upload_limit_label
from ocr_common.kk import DOCUMENT_TYPE
from ocr_common.pipeline import DEFAULT_SEQUENCE, InvalidSequence, validate_sequence
from ocr_common.web.intake import FileField, FileUrlField, read_image
from ocr_common.web.request_id import adopt_request_id, reset_request_id
from ocr_common.web.schemas import UNAUTHORIZED, error, success_examples
from ocr_common.web.security import verify_api_key

from app.api.extract_contract import (
    COMPLETED_MESSAGE,
    PROCESSING_MESSAGE,
    REJECTED_CODE,
    extract_body,
    extract_response,
)
from app.api.schemas import ExtractOcrResponse
from app.config import Settings, get_settings
from app.dependencies import get_extract_service
from app.middleware import TOO_MANY_REQUESTS
from app.services.document_checks import TOO_MANY_PAGES_MESSAGE
from app.services.extract_service import ExtractOcrService

router = APIRouter(tags=["Extract OCR"], dependencies=[Depends(verify_api_key)])

RID = "OCR_9cb01af2-493d-446d-b191-af120333f6d0"
INVALID_PARAMS_MESSAGE = "params must be valid JSON: an object, or a quoted string"
INVALID_SEQUENCE_CODE = "INVALID_PIPELINE_SEQUENCE"

# Setiap nomor di contoh ini memakai kode provinsi 99, yang tidak pernah diberikan Indonesia (lihat
# ocr_common.synthetic_kk). Contoh OpenAPI adalah tempat paling terlihat di seluruh repo, jadi nomor
# berbentuk sah dari wilayah nyata akan tampak -- dan bisa jadi -- NIK seseorang.
#
# Ini SENGAJA berbeda dari contoh di docs/api-contract.md, yang memakai 3273... (Kota Bandung). Nilainya
# ilustratif di kedua tempat dan tidak ada yang memvalidasinya, tetapi perbedaannya perlu diserap saat
# kontrak naik draf berikutnya, bukan dibiarkan jadi kejutan.
#: `MAX_UPLOAD_BYTES` bawaan sebagaimana klien membacanya, diturunkan dari konstantanya supaya
#: dokumentasinya tidak bisa menyimpang lagi dari nilainya.
UPLOAD_LIMIT = upload_limit_label(DEFAULT_MAX_UPLOAD_BYTES)

_PARAMS = {"nik": "9901011203850001", "refno": "PK19039Y8U"}
_DATA = {
    "no_kk": {"value": "9901012609260001", "confidence": 0.9913, "bin": 10, "auto": True},
    "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "confidence": 0.9041, "bin": 9, "auto": True},
    "anggota_keluarga": [
        {
            "nama_lengkap": {"value": "BUDI SANTOSO", "confidence": 0.9886, "bin": 10, "auto": True},
            "nik": {"value": "9901011203850001", "confidence": 0.9902, "bin": 10, "auto": True},
            "pendidikan": {"value": "S1", "confidence": 0.9951, "bin": 10, "auto": True},
            "jenis_pekerjaan": {"value": "KARYAWAN SWASTA", "confidence": 0.9833, "bin": 10, "auto": True},
            "status_hubungan_dalam_rumah_tangga": {
                "value": "KEPALA KELUARGA",
                "confidence": 0.9914,
                "bin": 10,
                "auto": True,
            },
            "ayah": {"value": "SUTRISNO", "confidence": 0.9602, "bin": 9, "auto": True},
            "ibu": {"value": "SITI AMINAH", "confidence": 0.9418, "bin": 8, "auto": False},
        },
        {
            "nama_lengkap": {"value": "SITI NURHALIZA", "confidence": 0.9077, "bin": 6, "auto": False},
            "nik": {"value": "9901015506880002", "confidence": 0.9701, "bin": 9, "auto": True},
            "pendidikan": {"value": "SLTA/SEDERAJAT", "confidence": 0.9724, "bin": 9, "auto": False},
            "jenis_pekerjaan": {"value": "MENGURUS RUMAH TANGGA", "confidence": 0.6318, "bin": 2, "auto": False},
            "status_hubungan_dalam_rumah_tangga": {"value": "ISTRI", "confidence": 0.9130, "bin": 6, "auto": False},
            "ayah": {"value": "AHMAD DAHLAN", "confidence": 0.9355, "bin": 8, "auto": True},
            "ibu": {"value": "RATNA SARI", "confidence": 0.8802, "bin": 5, "auto": False},
        },
    ],
}
_COMPLETED = extract_body(
    200,
    COMPLETED_MESSAGE,
    data=_DATA,
    job_status="completed",
    guardrails=0,
    errors=None,
    request_id=RID,
    document_type="kk",
    params=_PARAMS,
)
_PROCESSING = extract_body(
    202,
    PROCESSING_MESSAGE,
    job_status="processing",
    request_id=RID,
    document_type="kk",
    params=_PARAMS,
)
_REJECTED = extract_body(
    400,
    "Gambar terlalu buram untuk diproses, mohon unggah ulang foto Kartu Keluarga",
    errors=REJECTED_CODE,
    job_status="failed",
    guardrails=1,
    request_id=RID,
    document_type="kk",
    params=_PARAMS,
)
_FAILED = extract_body(
    422,
    "structuring stage failed: parser raised on an unexpected layout",
    errors="STRUCTURING_FAILED",
    job_status="failed",
    guardrails=0,
    request_id=RID,
    document_type="kk",
    params=_PARAMS,
)

_CONTRACT_TABLE = (
    "| Outcome | HTTP | `job_status` | `data` | `guardrails` | `errors` |\n"
    "|---|---|---|---|---|---|\n"
    "| Finished | 200 | `completed` | the fields | `0` | null |\n"
    "| Still running | 202 | `processing` | null | null | null |\n"
    f"| Rejected by the guardrails model | 400 | `failed` | null | `1` | `{REJECTED_CODE}` |\n"
    f"| Rejected by the KK validity gate | 400 | `failed` | null | `1` | `{REJECTED_CODE}` |\n"
    "| A stage failed | 422 | `failed` | null | `0` | `OCR_FAILED`, `STRUCTURING_FAILED` or "
    "`SCORING_FAILED` |\n\n"
)


#: Dibalas middleware rate limit, bukan router ini: lihat app/middleware.py.
TOO_MANY_REQUESTS_RESPONSE = error(
    429,
    "The caller went over `RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`. `Retry-After` says how "
    "many seconds to wait. The limit is counted per API key, per process",
    TOO_MANY_REQUESTS,
    request_id=RID,
)


class _InvalidParams(Exception):
    pass


def _parse_sequence(values: list[str] | None) -> tuple[str, ...]:
    """`pipeline_name_sequence` as repeated form fields, or as one JSON array string; the full pipeline when
    omitted. Raises `InvalidSequence`."""
    if not values:
        return DEFAULT_SEQUENCE
    if len(values) == 1 and values[0].lstrip().startswith("["):
        try:
            parsed = json.loads(values[0])
        except ValueError:
            parsed = None
        if not isinstance(parsed, list) or not all(isinstance(name, str) for name in parsed):
            raise InvalidSequence("send it as a JSON array of strings, or as repeated form fields")
        values = parsed
    return validate_sequence(values)


def _parse_params(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise _InvalidParams from exc
    if not isinstance(value, dict | str):
        raise _InvalidParams
    return value


@router.post(
    "/v1/extract-ocr",
    response_model=ExtractOcrResponse,
    operation_id="extractOcr",
    summary="Judge a document, run the pipeline, answer with the OCR result or 202",
    description=(
        "**The call the central orchestrator makes.** Checks the file, has the guardrails service judge it, "
        "hands it to the OCR stage when it passes, then waits for OCR -> structuring -> scoring for up to "
        "`PIPELINE_WAIT_SECONDS` (30 s by default), counted from when this request arrived. The response follows "
        'the central orchestrator\'s `extract-ocr` contract ("Finished" meaning finished within the wait):\n\n'
        + _CONTRACT_TABLE
        + "`data` holds NINE fields: `no_kk`, `nama_kepala_keluarga`, and `anggota_keluarga[]` with seven per "
        "member. Structuring extracts 11 document fields and 15 per member; only these leave, and the rest "
        "stay readable through `GET /v1/structuring/jobs/{request_id}`. Each is "
        "`{value, confidence, bin, auto}`: `value` is empty when not found -- never null -- `confidence` is "
        "the trust model's calibrated P(this value is exactly correct), `bin` places it in one of ten bins "
        "whose top one is cut where held-out precision reached 100%, and `auto` is true when the field "
        "cleared the threshold calibrated for that field specifically. Read `auto` to decide whether a "
        "human must look, `confidence` to decide what to look at first. `params` is returned as sent.\n\n"
        "**Rejected by the KK validity gate**: the only content gate in the pipeline, and it lives at structuring. "
        "Three rules, first match wins: no readable text boxes at all; the KK number missing; or no member with "
        "both a NIK and a name. `message` is the gate's Indonesian reason. A rejected document is still a "
        "`DONE` job whose result stays readable at `GET /v1/structuring/jobs/{request_id}`.\n\n"
        "**Refused before anything runs** (plain error envelope, no `job_status`): a document above "
        f"`MAX_UPLOAD_BYTES` ({UPLOAD_LIMIT} by default) answers `413`, and a PDF with more than "
        "`MAX_DOCUMENT_PAGES` (2) pages answers `400`. Both carry an Indonesian `message` the client can show "
        "as is. JPEG, PNG and PDF are accepted; of a PDF only the first page is judged and read.\n\n"
        "**Which services run.** `pipeline_name_sequence` names them, in order, from `guardrails`, `ekstraksi`, "
        "`structuring`, `scoring`: guardrails may be left out at the front and the end cut off, but nothing in "
        f"the middle may be skipped and the order may not change (else `422` `{INVALID_SEQUENCE_CODE}` and "
        "nothing runs). Omitted: all four. The last one ends the request and its result is `data`, as it is: the "
        "guardrails report, the OCR result, the structuring result, or the nine fields after scoring; "
        "`pipeline_last_stage` names it. Without `guardrails` the file checks above still run and the KK "
        "validity gate still rejects; the trust model gets no guardrails probability and works with that input "
        "missing.\n\n"
        "On 202 the result arrives by callback (sent by the pipeline stages), and can be read with "
        "`GET /v1/extract-ocr/{request_id}`. Give this call an HTTP timeout well above `PIPELINE_WAIT_SECONDS` "
        "(e.g. +15 s) to cover a slow guardrails check or hand-off.\n\n"
        "Send `request_id` plus the document as `file`, or as `file_url`; exactly one of the two. A `file_url` "
        "is downloaded here for the guardrails check, and the URL itself (not the bytes) is handed to the OCR "
        "stage, which downloads it again, also when it re-runs a job left behind by a dead process: the URL "
        "must stay valid for longer than the job lease. The raw guardrails report (`probability_bad` and the "
        "threshold it was compared against) is not part of this response: it travels down the pipeline and "
        "comes back in the SCORING callback.\n\n"
        "**Idempotency.** The same request_id again re-runs the guardrails check, but the pipeline does not run "
        "twice unless the earlier OCR attempt `FAILED` or outlived the job lease (`PIPELINE_JOB_LEASE_SECONDS`); "
        "a request_id that already finished answers again with its stored outcome."
    ),
    responses={
        200: success_examples(
            "Accepted and finished within the wait",
            completed=("The OCR result", _COMPLETED),
        ),
        202: {
            **success_examples(
                "Accepted, but still running when the wait ran out: the result follows by callback",
                processing=("Still processing", _PROCESSING),
            ),
            "model": ExtractOcrResponse,
        },
        400: {
            "model": ExtractOcrResponse,
            "description": (
                f"Rejected by the guardrails model or by the KK validity gate (`{REJECTED_CODE}`, "
                "`guardrails: 1`), unsupported `document_type` (`UNSUPPORTED_DOCUMENT_TYPE`), a PDF above "
                f"`MAX_DOCUMENT_PAGES` pages (`{TOO_MANY_PAGES_MESSAGE}`), or a bad "
                "file / intake (empty, unsupported type, unreadable, `file_url` refused)"
            ),
            "content": {"application/json": {"example": _REJECTED}},
        },
        401: UNAUTHORIZED,
        429: TOO_MANY_REQUESTS_RESPONSE,
        413: error(
            413,
            f"The document exceeds `MAX_UPLOAD_BYTES` ({UPLOAD_LIMIT} by default); nothing was started",
            PAYLOAD_TOO_LARGE_MESSAGE.format(limit=UPLOAD_LIMIT),
        ),
        422: {
            "model": ExtractOcrResponse,
            "description": (
                "A pipeline stage failed within the wait (`OCR_FAILED`, `STRUCTURING_FAILED`, `SCORING_FAILED`; "
                "`message` says why), `params` is not valid JSON (`INVALID_PARAMS`), `pipeline_name_sequence` is "
                f"not a valid sequence (`{INVALID_SEQUENCE_CODE}`), or a required field is missing (`VALIDATION_ERROR`)"
            ),
            "content": {"application/json": {"example": _FAILED}},
        },
        500: error(
            500,
            "The guardrails or ekstraksi service failed, or answered in an unexpected shape",
            "guardrails service returned an unexpected response",
        ),
        503: error(
            503,
            "The guardrails service, its model, or the ekstraksi service is unreachable; nothing was started",
            "ekstraksi service is unavailable",
        ),
        504: error(
            504,
            "The guardrails service, its model, or the ekstraksi service did not answer in time",
            "ekstraksi service timed out after 10.0s",
        ),
    },
)
async def extract_ocr(
    request: Request,
    response: Response,
    request_id: str = Form(..., description="request_id minted by the central orchestrator", examples=[RID]),
    document_type: str = Form(
        DOCUMENT_TYPE, description="Document type chosen by the client. Only `kk` is supported", examples=["kk"]
    ),
    params: str | None = Form(
        None,
        description=(
            "Client metadata as JSON: an object, or a quoted string. Not interpreted; returned unchanged in `params`"
        ),
        examples=['{"nik": "9901011203850001", "refno": "PK19039Y8U"}'],
    ),
    file: UploadFile | str | None = FileField,
    file_url: str | None = FileUrlField,
    pipeline_name_sequence: list[str] | None = Form(
        None,
        description=(
            "The services to run, in order: `guardrails`, `ekstraksi`, `structuring`, `scoring`; guardrails "
            "optional at the front, the end may be cut off, nothing skipped in the middle. Repeated form fields, or "
            "one JSON array string. Omitted: all four. The last one's result is `data`, as it is"
        ),
        examples=[["guardrails", "ekstraksi", "structuring", "scoring"]],
    ),
    service: ExtractOcrService = Depends(get_extract_service),
    settings: Settings = Depends(get_settings),
):
    received_at = time.monotonic()
    try:
        parsed_params = _parse_params(params)
    except _InvalidParams:
        response.status_code = 422
        return extract_body(
            422,
            INVALID_PARAMS_MESSAGE,
            errors="INVALID_PARAMS",
            request_id=request_id,
            document_type=document_type,
        )
    if document_type != DOCUMENT_TYPE:
        response.status_code = 400
        return extract_body(
            400,
            f"Unsupported document_type: {document_type}. Supported: {DOCUMENT_TYPE}",
            errors="UNSUPPORTED_DOCUMENT_TYPE",
            request_id=request_id,
            document_type=document_type,
        )
    try:
        sequence = _parse_sequence(pipeline_name_sequence)
    except InvalidSequence as exc:
        response.status_code = 422
        return extract_body(
            422,
            f"Invalid pipeline_name_sequence: {exc}",
            errors=INVALID_SEQUENCE_CODE,
            request_id=request_id,
            document_type=document_type,
        )

    # The central orchestrator's request_id becomes the id of this request: in the envelope of an error raised
    # below (413, a bad file, an unreachable stage), in the X-Request-ID response header and outbound calls, and
    # in our log lines, so one id follows the request through guardrails and every stage.
    token = adopt_request_id(request, request_id)
    try:
        content, filename, content_type = await read_image(request, file, file_url)
        outcome = await service.submit(
            request_id,
            document_type,
            filename,
            content_type,
            content,
            received_at=received_at,
            file_url=file_url,
            sequence=sequence,
        )
    finally:
        reset_request_id(token)
    status_code, body = extract_response(
        outcome,
        request_id=request_id,
        document_type=document_type,
        params=parsed_params,
        threshold=settings.field_confidence_threshold,
    )
    response.status_code = status_code
    return body


@router.get(
    "/v1/extract-ocr/{request_id}",
    response_model=ExtractOcrResponse,
    operation_id="getExtractOcr",
    summary="Where a request is now: the extract-ocr answer, without waiting",
    description=(
        "Reads the OCR, structuring and scoring jobs of `request_id` once each, in pipeline order, and answers in "
        'the same contract as `POST /v1/extract-ocr` ("Finished" meaning finished by now):\n\n'
        + _CONTRACT_TABLE
        + "Use it for a request that was answered `202`, e.g. when a callback did not arrive. `params` is always "
        "null here (it is not stored) and `document_type` is `kk`.\n\n"
        "**404** means no stage has a job for this request_id: it was refused or rejected by the guardrails model "
        "(the `400` of the POST is its final answer), refused before the check, or its POST is still being "
        "judged by guardrails.\n\n"
        "**Limitation.** A hand-off between two stages that failed for good (its retries ran out, or it became a "
        "dead letter in the outbox) leaves the next stage without a job, so this endpoint keeps answering `202` "
        "for it. The `FAILED` callback and the central orchestrator's own tables carry that final state; this "
        "endpoint only reads the stages' jobs."
    ),
    responses={
        200: success_examples(
            "Finished",
            completed=("The OCR result", {**_COMPLETED, "params": None}),
        ),
        202: {
            **success_examples(
                "Still running",
                processing=("Still processing", {**_PROCESSING, "params": None}),
            ),
            "model": ExtractOcrResponse,
        },
        400: {
            "model": ExtractOcrResponse,
            "description": f"Rejected by the KK validity gate (`{REJECTED_CODE}`, `guardrails: 1`)",
            "content": {
                "application/json": {
                    "example": {
                        **_REJECTED,
                        "message": "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil ekstraksi tidak lengkap",
                        "params": None,
                    }
                }
            },
        },
        401: UNAUTHORIZED,
        429: TOO_MANY_REQUESTS_RESPONSE,
        404: error(
            404,
            "No stage has a job for this request_id (rejected by the guardrails model, or not submitted yet)",
            f"No request found for request_id {RID}",
        ),
        422: {
            "model": ExtractOcrResponse,
            "description": "A pipeline stage failed (`OCR_FAILED`, `STRUCTURING_FAILED`, `SCORING_FAILED`)",
            "content": {"application/json": {"example": {**_FAILED, "params": None}}},
        },
        500: error(
            500,
            "A stage answered in an unexpected shape, or refused this service (e.g. a wrong API key)",
            "structuring service error (401): Invalid or missing API key",
        ),
        503: error(503, "A stage service is unreachable", "structuring service is unavailable"),
        504: error(504, "A stage service did not answer in time", "structuring service timed out after 10.0s"),
    },
)
async def get_extract_ocr(
    request_id: str,
    request: Request,
    response: Response,
    service: ExtractOcrService = Depends(get_extract_service),
    settings: Settings = Depends(get_settings),
):
    token = adopt_request_id(request, request_id)
    try:
        outcome = await service.status(request_id)
    finally:
        reset_request_id(token)
    status_code, body = extract_response(
        outcome,
        request_id=request_id,
        document_type=DOCUMENT_TYPE,
        params=None,
        threshold=settings.field_confidence_threshold,
    )
    response.status_code = status_code
    return body
