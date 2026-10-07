import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile

from ocr_common.config import DEFAULT_MAX_UPLOAD_BYTES
from ocr_common.errors import UNREADABLE_FILE
from ocr_common.image_validation import PAYLOAD_TOO_LARGE_MESSAGE, upload_limit_label
from ocr_common.kk import COLUMN_THRESHOLD_DESCRIPTION, DOCUMENT_TYPE, column_thresholds_from_json
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
from app.clients.guardrails import GuardrailsThreshold
from app.dependencies import get_extract_service
from app.middleware import TOO_MANY_REQUESTS
from app.services.document_checks import TOO_MANY_PAGES_MESSAGE
from app.services.extract_service import ExtractOcrService
from app.services.pipeline_waiter import StageError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Extract OCR"], dependencies=[Depends(verify_api_key)])

RID = "OCR_9cb01af2-493d-446d-b191-af120333f6d0"
INVALID_SEQUENCE_CODE = "INVALID_PIPELINE_SEQUENCE"
INVALID_THRESHOLD_CODE = "INVALID_THRESHOLD"
# `pipeline_last_stage` of a request this service refuses itself, before calling any pipeline service.
ENTRY = "orchestrator"

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

_DATA = {
    "no_kk": {"value": "9901012609260001", "confidence": 1},
    "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "confidence": 1},
    "anggota_keluarga": [
        {
            "nama_lengkap": {"value": "BUDI SANTOSO", "confidence": 1},
            "nik": {"value": "9901011203850001", "confidence": 1},
            "pendidikan": {"value": "S1", "confidence": 1},
            "jenis_pekerjaan": {"value": "KARYAWAN SWASTA", "confidence": 1},
            "status_hubungan_dalam_rumah_tangga": {"value": "KEPALA KELUARGA", "confidence": 1},
            "ayah": {"value": "SUTRISNO", "confidence": 1},
            "ibu": {"value": "SITI AMINAH", "confidence": 0},
        },
        {
            "nama_lengkap": {"value": "SITI NURHALIZA", "confidence": 0},
            "nik": {"value": "9901015506880002", "confidence": 1},
            "pendidikan": {"value": "SLTA/SEDERAJAT", "confidence": 0},
            "jenis_pekerjaan": {"value": "MENGURUS RUMAH TANGGA", "confidence": 0},
            "status_hubungan_dalam_rumah_tangga": {"value": "ISTRI", "confidence": 0},
            "ayah": {"value": "AHMAD DAHLAN", "confidence": 1},
            "ibu": {"value": "RATNA SARI", "confidence": 0},
        },
    ],
}
_COMPLETED = extract_body(200, COMPLETED_MESSAGE, data=_DATA, guardrails=0, request_id=RID)
_PROCESSING = extract_body(202, PROCESSING_MESSAGE, request_id=RID)
_REJECTED = extract_body(
    400,
    "Gambar terlalu buram untuk diproses, mohon unggah ulang foto Kartu Keluarga",
    errors=REJECTED_CODE,
    guardrails=1,
    request_id=RID,
    pipeline_last_stage="guardrails",
)
_FAILED = extract_body(
    422,
    "structuring stage failed: parser raised on an unexpected layout",
    errors="STRUCTURING_FAILED",
    guardrails=0,
    request_id=RID,
    pipeline_last_stage="structuring",
)

_CONTRACT_TABLE = (
    "| Outcome | HTTP | `data` | `guardrails` | `errors` | `pipeline_last_stage` |\n"
    "|---|---|---|---|---|---|\n"
    "| Finished | 200 | the fields | `0` | null | null |\n"
    "| Still running | 202 | null | null | null | null |\n"
    f"| Rejected by the guardrails model | 400 | null | `1` | `{REJECTED_CODE}` | `guardrails` |\n"
    f"| Rejected by the KK validity gate | 400 | null | `1` | `{REJECTED_CODE}` | `structuring` |\n"
    "| A stage failed | 422 | null | `0` | `OCR_FAILED`, `STRUCTURING_FAILED` or "
    "`SCORING_FAILED` | the service that failed |\n\n"
    "The HTTP status (also in `status_code`) says where the request is: 200 finished, 202 still running, "
    "4xx / 5xx failed or refused.\n\n"
    "`pipeline_last_stage` is null on a success answer (200, 202) and names the service an error comes from: "
    "a pipeline service as `pipeline_name_sequence` names it (`guardrails`, `extraction`, `structuring`, "
    "`scoring`), also on a 400 / 500 / 503 / 504 from calling one of them, or `orchestrator` when this service "
    "refused the request itself before calling any (API key, file checks, `pipeline_name_sequence`, thresholds, "
    "`document_type`, an unknown request_id).\n\n"
)


#: Dibalas middleware rate limit, bukan router ini: lihat app/middleware.py.
TOO_MANY_REQUESTS_RESPONSE = error(
    429,
    "The caller went over `RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`. `Retry-After` says how "
    "many seconds to wait. The limit is counted per API key, per process",
    TOO_MANY_REQUESTS,
    request_id=RID,
)

# `errors` when calling a pipeline service failed, by the status it failed with. Ported from nilam.
STAGE_ERROR_CODES = {
    400: UNREADABLE_FILE,  # guardrails refused the file (the type and size were checked here)
    500: "DOWNSTREAM_SERVER_ERROR",
    503: "DOWNSTREAM_UNAVAILABLE",
    504: "DOWNSTREAM_TIMEOUT",
}


def _stage_error_body(exc: StageError, *, request_id: str) -> dict[str, Any]:
    """The answer when calling a pipeline service failed (unreachable, timed out, refused the file, answered
    wrongly): the error envelope's status and message, in the extract-ocr shape, naming that service."""
    if exc.status_code >= 500:
        logger.error("%s -> %d: %s", exc.service, exc.status_code, exc.message)
    return extract_body(
        exc.status_code,
        exc.message,
        errors=STAGE_ERROR_CODES.get(exc.status_code, "DOWNSTREAM_BAD_REQUEST"),
        request_id=request_id,
        pipeline_last_stage=exc.service,
    )


def _stage_error_response(code: int, description: str, service: str, message: str) -> dict[str, Any]:
    example = extract_body(code, message, errors=STAGE_ERROR_CODES[code], request_id=RID, pipeline_last_stage=service)
    return {
        "model": ExtractOcrResponse,
        "description": description,
        "content": {"application/json": {"example": example}},
    }


async def _answer(service: ExtractOcrService, response: Response, body: dict[str, Any]) -> dict[str, Any]:
    """Gives `body` as the answer, with its `status_code` as the HTTP status, and keeps it as the request's final
    result (`nilam_ocr_results`). Errors raised out of the routes (a bad or too large file, 404) are answered by
    the exception handlers and not kept: no envelope of this service exists for them."""
    response.status_code = body["status_code"]
    await service.answered(body["request_id"], body)
    return body


class _InvalidThreshold(Exception):
    pass


def _parse_guardrails_threshold(value: str | None) -> GuardrailsThreshold | None:
    """`guardrails_confidence_threshold`, a JSON object `{"acc_rej": 0.8}` with the value between 0 and 1.
    Always applies to the `rejected` side. None (nothing sent) leaves the guardrails service's own threshold in
    force. Raises `_InvalidThreshold`."""
    value = (value or "").strip()
    if not value:
        return None
    if not value.startswith("{"):
        raise _InvalidThreshold('send guardrails_confidence_threshold as {"acc_rej": 0.8}')
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or set(parsed) != {"acc_rej"}:
        raise _InvalidThreshold('guardrails_confidence_threshold must be a JSON object like {"acc_rej": 0.8}')
    raw: Any = parsed["acc_rej"]
    try:
        if isinstance(raw, bool):
            raise ValueError
        threshold = float(raw)
    except (TypeError, ValueError):
        threshold = float("nan")
    if not 0 < threshold < 1:
        raise _InvalidThreshold(
            f"guardrails_confidence_threshold.acc_rej must be a number between 0 and 1, got {raw!r}"
        )
    return GuardrailsThreshold(threshold, "reject")


def _parse_sequence(values: list[str] | None) -> tuple[str, ...]:
    """`pipeline_name_sequence` as repeated form fields, a comma-separated string, or one JSON array string;
    the full pipeline when omitted. A blank field (a form client's "send empty value") counts as omitted.
    Raises `InvalidSequence`."""
    values = [value.strip() for value in values or () if value.strip()]
    if not values:
        return DEFAULT_SEQUENCE
    if len(values) == 1 and "," in values[0] and not values[0].lstrip().startswith("["):
        values = [v.strip() for v in values[0].split(",") if v.strip()]
    if len(values) == 1 and values[0].lstrip().startswith("["):
        try:
            parsed = json.loads(values[0])
        except ValueError:
            parsed = None
        if not isinstance(parsed, list) or not all(isinstance(name, str) for name in parsed):
            raise InvalidSequence("send it as a JSON array of strings, or as repeated form fields")
        values = parsed
    return validate_sequence(values)


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
        "`{value, confidence}`, as in nilam: `value` is empty when not found -- never null -- and `confidence` "
        "is `1` when the trust model's probability that the value is exactly correct reaches the field's "
        "threshold, else `0`. The threshold is `column_confidence_threshold` for that field when sent, else the "
        "trust model's own (the point above which every held-out sample of the field was correct); `no_kk` has "
        f"none, so it is `0` unless the request gives it one. An unreadable threshold is `422` "
        f"`{INVALID_THRESHOLD_CODE}` and nothing runs.\n\n"
        "**Rejected by the KK validity gate**: the only content gate in the pipeline, and it lives at structuring. "
        "Three rules, first match wins: no readable text boxes at all; the KK number missing; or no member with "
        "both a NIK and a name. `message` is the gate's Indonesian reason. A rejected document is still a "
        "`DONE` job whose result stays readable at `GET /v1/structuring/jobs/{request_id}`.\n\n"
        "**Refused before anything runs** (plain error envelope, no `guardrails`): a document above "
        f"`MAX_UPLOAD_BYTES` ({UPLOAD_LIMIT} by default) answers `413`, and a PDF with more than "
        "`MAX_DOCUMENT_PAGES` (2) pages answers `400`. Both carry an Indonesian `message` the client can show "
        "as is. JPEG, PNG and PDF are accepted; of a PDF only the first page is judged and read.\n\n"
        "**Which services run.** `pipeline_name_sequence` names them, in order, from `guardrails`, `extraction`, "
        "`structuring`, `scoring`: guardrails may be left out at the front and the end cut off, but nothing in "
        f"the middle may be skipped and the order may not change (else `422` `{INVALID_SEQUENCE_CODE}` and "
        "nothing runs). Omitted: all four. The last one ends the request and its result is `data`, as it is: the "
        "guardrails report, the OCR result, the structuring result, or the nine fields after scoring. Without "
        "`guardrails` the file checks above still run and the KK validity gate still rejects; the trust model "
        "gets no guardrails probability and works with that input missing.\n\n"
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
                "`message` says why), `pipeline_name_sequence` is "
                f"not a valid sequence (`{INVALID_SEQUENCE_CODE}`), a threshold cannot be read "
                f"(`{INVALID_THRESHOLD_CODE}`), or a required field is missing (`VALIDATION_ERROR`)"
            ),
            "content": {"application/json": {"example": _FAILED}},
        },
        500: _stage_error_response(
            500,
            "The guardrails or extraction service failed, or answered in an unexpected shape "
            "(`DOWNSTREAM_SERVER_ERROR`); `pipeline_last_stage` names it",
            "guardrails",
            "guardrails service returned an unexpected response",
        ),
        503: _stage_error_response(
            503,
            "The guardrails service, its model, or the extraction service is unreachable (`DOWNSTREAM_UNAVAILABLE`); "
            "nothing was started. `pipeline_last_stage` names it",
            "extraction",
            "extraction service is unavailable",
        ),
        504: _stage_error_response(
            504,
            "The guardrails service, its model, or the extraction service did not answer in time "
            "(`DOWNSTREAM_TIMEOUT`); `pipeline_last_stage` names it",
            "extraction",
            "extraction service timed out after 10.0s",
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
    file: UploadFile | str | None = FileField,
    file_url: str | None = FileUrlField,
    pipeline_name_sequence: list[str] | None = Form(
        None,
        description=(
            "The services to run, in order: `guardrails`, `extraction`, `structuring`, `scoring`; guardrails "
            "optional at the front, the end may be cut off, nothing skipped in the middle. Repeated form fields, "
            "a comma-separated string, or one JSON array string. Omitted or blank: all four. The last one's result "
            "is `data`, as it is"
        ),
        examples=[["guardrails", "extraction", "structuring", "scoring"]],
    ),
    guardrails_confidence_threshold: str | None = Form(
        None,
        description=(
            'Guardrails threshold for this document, a JSON object string `{"acc_rej": 0.8}` with the value '
            "between 0 and 1 (exclusive). Applies to the rejected side. "
            "Omitted: no threshold applies -- the document passes guardrails and its `probability_bad` travels "
            "on to scoring"
        ),
        examples=['{"acc_rej": 0.8}'],
    ),
    column_confidence_threshold: str | None = Form(
        None,
        description=f"{COLUMN_THRESHOLD_DESCRIPTION}. A JSON object string",
        examples=['{"all_field": 0.8}'],
    ),
    service: ExtractOcrService = Depends(get_extract_service),
):
    received_at = time.monotonic()
    if document_type != DOCUMENT_TYPE:
        return await _answer(
            service,
            response,
            extract_body(
                400,
                f"Unsupported document_type: {document_type}. Supported: {DOCUMENT_TYPE}",
                errors="UNSUPPORTED_DOCUMENT_TYPE",
                request_id=request_id,
                pipeline_last_stage=ENTRY,
            ),
        )
    try:
        sequence = _parse_sequence(pipeline_name_sequence)
    except InvalidSequence as exc:
        return await _answer(
            service,
            response,
            extract_body(
                422,
                f"Invalid pipeline_name_sequence: {exc}",
                errors=INVALID_SEQUENCE_CODE,
                request_id=request_id,
                pipeline_last_stage=ENTRY,
            ),
        )
    try:
        guardrails_threshold = _parse_guardrails_threshold(guardrails_confidence_threshold)
        try:
            column_thresholds = column_thresholds_from_json(column_confidence_threshold)
        except ValueError as exc:
            raise _InvalidThreshold(str(exc)) from exc
    except _InvalidThreshold as exc:
        return await _answer(
            service,
            response,
            extract_body(
                422,
                str(exc),
                errors=INVALID_THRESHOLD_CODE,
                request_id=request_id,
                pipeline_last_stage=ENTRY,
            ),
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
            guardrails_threshold=guardrails_threshold,
            column_thresholds=column_thresholds,
        )
    except StageError as exc:
        return await _answer(service, response, _stage_error_body(exc, request_id=request_id))
    finally:
        reset_request_id(token)
    _, body = extract_response(
        outcome,
        request_id=request_id,
        column_thresholds=column_thresholds,
    )
    return await _answer(service, response, body)


@router.get(
    "/v1/extract-ocr/{request_id}",
    response_model=ExtractOcrResponse,
    operation_id="getExtractOcr",
    summary="Where a request is now: the extract-ocr answer, without waiting",
    description=(
        "Reads the jobs of `request_id` once each, in pipeline order, up to the last service of the "
        "`pipeline_name_sequence` stored with its extraction job, and answers in "
        'the same contract as `POST /v1/extract-ocr` ("Finished" meaning finished by now):\n\n'
        + _CONTRACT_TABLE
        + "Use it for a request that was answered `202`, e.g. when a callback did not arrive.\n\n"
        "**No stage job.** A request that never reached a stage is answered from its last guardrails verdict "
        "(`nilam_guardrails_results`), as its POST was: `400` when guardrails rejected it, `200` with the report as "
        "`data` when guardrails was its only service. **404** otherwise: refused before the check, still being "
        "judged, passed but its hand-off to extraction failed, or no verdict kept (no `DATABASE_URL`).\n\n"
        "**Limitation.** A hand-off between two stages that failed for good (its retries ran out, or it became a "
        "dead letter in the outbox) leaves the next stage without a job, so this endpoint keeps answering `202` "
        "for it. The `FAILED` callback and the central orchestrator's own tables carry that final state; this "
        "endpoint only reads the stages' jobs."
    ),
    responses={
        200: success_examples(
            "Finished",
            completed=("The OCR result", _COMPLETED),
        ),
        202: {
            **success_examples(
                "Still running",
                processing=("Still processing", _PROCESSING),
            ),
            "model": ExtractOcrResponse,
        },
        400: {
            "model": ExtractOcrResponse,
            "description": (
                "Rejected by the KK validity gate or, read from nilam_guardrails_results, by the guardrails model "
                f"(`{REJECTED_CODE}`, `guardrails: 1`)"
            ),
            "content": {
                "application/json": {
                    "example": {
                        **_REJECTED,
                        "message": "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil extraction tidak lengkap",
                    }
                }
            },
        },
        401: UNAUTHORIZED,
        429: TOO_MANY_REQUESTS_RESPONSE,
        404: error(
            404,
            "No stage has a job for this request_id and no guardrails verdict answers for it (not submitted yet, "
            "refused before the check, or its hand-off to extraction failed)",
            f"No request found for request_id {RID}",
            errors="REQUEST_ID_NOT_FOUND",
        ),
        422: {
            "model": ExtractOcrResponse,
            "description": "A pipeline stage failed (`OCR_FAILED`, `STRUCTURING_FAILED`, `SCORING_FAILED`)",
            "content": {"application/json": {"example": _FAILED}},
        },
        500: _stage_error_response(
            500,
            "A stage answered in an unexpected shape, or refused this service (e.g. a wrong API key)",
            "structuring",
            "structuring service error (401): Invalid or missing API key",
        ),
        503: _stage_error_response(
            503, "A stage service is unreachable", "structuring", "structuring service is unavailable"
        ),
        504: _stage_error_response(
            504, "A stage service did not answer in time", "structuring", "structuring service timed out after 10.0s"
        ),
    },
)
async def get_extract_ocr(
    request_id: str,
    request: Request,
    response: Response,
    service: ExtractOcrService = Depends(get_extract_service),
):
    token = adopt_request_id(request, request_id)
    try:
        outcome = await service.status(request_id)
    except StageError as exc:
        return await _answer(service, response, _stage_error_body(exc, request_id=request_id))
    finally:
        reset_request_id(token)
    _, body = extract_response(
        outcome,
        request_id=request_id,
        column_thresholds=outcome.get("column_thresholds"),
    )
    return await _answer(service, response, body)
