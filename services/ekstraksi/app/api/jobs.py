import json
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile

from ocr_common.kk import COLUMN_THRESHOLD_DESCRIPTION, DOCUMENT_TYPE, column_thresholds_from_json, ocr_aggregates
from ocr_common.pipeline import EKSTRAKSI, InvalidSequence, StagePipeline, checked_sequence
from ocr_common.pipeline.outbox_status import (
    OUTBOX_RELEASE_DESCRIPTION,
    OUTBOX_RELEASE_SUMMARY,
    OUTBOX_STATUS_DESCRIPTION,
    OUTBOX_STATUS_SUMMARY,
    OutboxReleaseResponse,
    OutboxStatusResponse,
    outbox_release,
    outbox_release_responses,
    outbox_status,
    outbox_status_responses,
)
from ocr_common.synthetic_kk import household, nomor_kk
from ocr_common.types import OcrBox
from ocr_common.web.envelope import envelope
from ocr_common.web.intake import FileField, FileUrlField, resolve_intake
from ocr_common.web.request_id import get_request_id
from ocr_common.web.schemas import (
    PIPELINE_SEQUENCE_DESCRIPTION,
    REQUEST_ID_EXAMPLE,
    UNAUTHORIZED,
    JobAcceptedResponse,
    error,
    success_examples,
)
from ocr_common.web.security import verify_api_key

from app.api.schemas import OcrJobStatusResponse
from app.dependencies import get_job_service, get_pipeline
from app.services.job_service import EkstraksiJobService, Source

router = APIRouter(tags=["Pipeline"], dependencies=[Depends(verify_api_key)])

_JOB = {"request_id": REQUEST_ID_EXAMPLE, "stage": "OCR", "created_at": "2026-09-18T04:00:00+00:00"}

# Two of the ~180 boxes one card produces, in the shape and the geometry the §7.1 spike measured:
# tilted quadrilaterals of floats, not upright integer rectangles. The values come from
# `ocr_common.synthetic_kk` (province code 99, which Indonesia never assigns), so the published
# example cannot be anyone's card -- the contract's own `3273...` example numbers are not copied here.
_EXAMPLE_TEXTS: list[OcrBox] = [
    {
        "text": "KARTU KELUARGA",
        "score": 0.9999,
        "poly": [[515.0, 100.0], [984.0, 108.0], [982.0, 148.0], [513.0, 140.0]],
    },
    {"text": nomor_kk(), "score": 0.9991, "poly": [[520.0, 210.0], [1100.0, 219.0], [1098.0, 259.0], [518.0, 250.0]]},
    {
        "text": household(1)[0].nama_lengkap,
        "score": 0.9873,
        "poly": [[520.0, 268.0], [889.0, 274.0], [888.0, 312.0], [519.0, 306.0]],
    },
]
OCR_RESULT_EXAMPLE = {
    "engine": "kk_ocr",
    "model": "PP-OCRv5_server_det+PP-OCRv5_server_rec",
    "elapsed_ms": 1842.5,
    **ocr_aggregates(_EXAMPLE_TEXTS),
    "texts": _EXAMPLE_TEXTS,
}

_GUARDRAILS_EXAMPLE = json.dumps(
    {
        "passed": True,
        "reason": None,
        "document": {
            "verdict": "accepted",
            "confidence": 0.9821,
            "probability_bad": 0.0179,
            "threshold_used": 0.5,
        },
    }
)


def _parse_guardrails(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="guardrails must be a JSON object")
    return value


def _parse_column_thresholds(raw: str | None) -> dict[str, float] | None:
    """The form's JSON object; None when omitted (the trust model's own thresholds). 400 when invalid."""
    try:
        return column_thresholds_from_json(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_sequence(raw: str | None) -> list[str] | None:
    """The form's JSON array; None when omitted (the full pipeline). 400 when it is not a valid sequence
    that includes this stage."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise HTTPException(status_code=400, detail="pipeline_name_sequence must be a JSON array of strings")
    try:
        return checked_sequence(value, EKSTRAKSI)
    except InvalidSequence as exc:
        raise HTTPException(status_code=400, detail=f"Invalid pipeline_name_sequence: {exc}") from exc


@router.post(
    "/v1/ekstraksi/jobs",
    status_code=202,
    response_model=JobAcceptedResponse,
    operation_id="submitOcrJob",
    summary="Start the pipeline for a Kartu Keluarga (OCR stage)",
    description=(
        "**Step 2 of the pipeline, asynchronous: the start of the OCR -> structuring -> scoring chain.** "
        "Called by the orchestrator from its `POST /v1/extract-ocr` once the guardrails model passed the "
        "document; the central orchestrator does not call it. Calling it directly skips the guardrails check.\n\n"
        "Records the job (`ocr_jobs`, idempotent per request_id), answers **202 immediately**, then in the "
        "background: reads the document (`file`, or downloads `file_url`), runs detection + recognition, stores "
        "`{text, score, poly}` per line (`ocr_results`), and hands the job to the structuring service -- result, "
        "hand-off and the orchestrator's outcome row in one transaction.\n\n"
        "**This stage never rejects a document.** An image with no readable text is a successful job whose "
        "`texts` is empty; it is the structuring rules that reject it, so every content-based rejection has one "
        "place and one channel. A document that cannot be read at all (unopenable image, model unreachable, "
        "unsupported `document_type`) becomes a `FAILED` job, which the caller sees as a 422 -- not a 4xx here.\n\n"
        "**Document.** Send `file` (multipart) or `file_url`, exactly one. JPEG, PNG or PDF (only its first "
        "page is read), at most `MAX_UPLOAD_BYTES` (5 MB). `file_url` is downloaded in the "
        "background from a host listed in `FILE_URL_ALLOWED_HOSTS`, so make a presigned URL live longer than "
        "`PIPELINE_JOB_LEASE_SECONDS`; an expired or unreachable URL becomes a `FAILED` job, not a `4xx`.\n\n"
        "**Idempotency.** The same request_id again answers `202` with `duplicate: true` and does not run OCR "
        "twice, unless the earlier attempt `FAILED` or has been `PROCESSING` for longer than the job lease "
        "(`PIPELINE_JOB_LEASE_SECONDS`, 5 minutes by default), in which case it is run again.\n\n"
        "**Where the chain stops.** `pipeline_name_sequence` decides: when `ekstraksi` is its last service, the "
        "job ends here, nothing is handed on, and the OCR result is the request's answer, as it is (the `OCR` "
        "callback then carries `final: true`)."
    ),
    responses={
        202: success_examples(
            "The job was accepted (or already existed)",
            accepted=(
                "New job",
                envelope(
                    202,
                    "Accepted",
                    {"request_id": REQUEST_ID_EXAMPLE, "stage": "OCR", "status": "PROCESSING", "duplicate": False},
                    REQUEST_ID_EXAMPLE,
                ),
            ),
            duplicate=(
                "Same request_id sent again: nothing is re-run, `status` is the existing job's status",
                envelope(
                    202,
                    "Accepted",
                    {"request_id": REQUEST_ID_EXAMPLE, "stage": "OCR", "status": "DONE", "duplicate": True},
                    REQUEST_ID_EXAMPLE,
                ),
            ),
        ),
        400: error(
            400,
            "Neither or both of file / file_url, `guardrails` is not a JSON object, `pipeline_name_sequence` is "
            "not a valid sequence that includes `ekstraksi`, or `column_confidence_threshold` is invalid",
            "Send exactly one of file or file_url",
        ),
        401: UNAUTHORIZED,
        422: error(422, "Validation Error", "body.request_id: Field required", errors="VALIDATION_ERROR"),
    },
)
async def submit_job(
    request_id: str = Form(..., description="request_id minted by the orchestrator", examples=[REQUEST_ID_EXAMPLE]),
    document_type: str = Form(
        DOCUMENT_TYPE,
        description="Document type chosen by the client. Only `kk` is supported",
        examples=[DOCUMENT_TYPE],
    ),
    guardrails: str | None = Form(
        None,
        description=(
            "The guardrails report (`data` of the guardrails service's `POST /v1/guardrails/check`), serialised "
            "as a JSON string; the orchestrator fills it in and it is left out entirely when the "
            "`pipeline_name_sequence` has no `guardrails`. Forwarded down the chain unchanged: scoring uses "
            "`document.probability_bad` as a trust-model feature, and the final result returns the report as it "
            "arrived"
        ),
        examples=[_GUARDRAILS_EXAMPLE],
    ),
    pipeline_name_sequence: str | None = Form(
        None,
        description=f"{PIPELINE_SEQUENCE_DESCRIPTION}. Serialised as a JSON array string",
        examples=['["guardrails", "ekstraksi", "structuring", "scoring"]'],
    ),
    column_confidence_threshold: str | None = Form(
        None,
        description=f"{COLUMN_THRESHOLD_DESCRIPTION}. A JSON object string; kept with the job and handed on",
        examples=['{"no_kk": 0.9, "nik": 0.8}'],
    ),
    file: UploadFile | str | None = FileField,
    file_url: str | None = FileUrlField,
    service: EkstraksiJobService = Depends(get_job_service),
):
    upload, url = resolve_intake(file, file_url)
    source: Source
    if upload is not None:
        source = (await upload.read(), upload.filename or "", upload.content_type)
    else:
        assert url is not None
        source = url
    sequence = _parse_sequence(pipeline_name_sequence)
    columns = _parse_column_thresholds(column_confidence_threshold)
    data = await service.submit(request_id, document_type, _parse_guardrails(guardrails), source, sequence, columns)
    return envelope(202, "Accepted", data, request_id)


@router.get(
    "/v1/ekstraksi/jobs/{request_id}",
    response_model=OcrJobStatusResponse,
    operation_id="getOcrJob",
    summary="Status and result of the OCR stage",
    description=(
        "Status of this stage only, and its `{text, score, poly}` lines once `DONE`. Internal: the "
        "orchestrator reads it (while it waits, and for its `GET /v1/extract-ocr/{request_id}`), and it helps "
        "to debug. The later stages have the same endpoint on their own service "
        "(`/v1/structuring/jobs/{request_id}`, `/v1/scoring/jobs/{request_id}`).\n\n"
        "The example below is shortened: one Kartu Keluarga typically yields well over a hundred boxes, which "
        "is why `PIPELINE_HANDOFF_BY_REFERENCE` is recommended -- the hand-off then names this result instead "
        "of carrying it."
    ),
    responses={
        200: success_examples(
            "The job exists",
            processing=(
                "Still running",
                envelope(
                    200,
                    "Success",
                    {
                        **_JOB,
                        "status": "PROCESSING",
                        "error_message": None,
                        "result": None,
                        "updated_at": "2026-09-18T04:00:00+00:00",
                    },
                    REQUEST_ID_EXAMPLE,
                ),
            ),
            done=(
                "Finished",
                envelope(
                    200,
                    "Success",
                    {
                        **_JOB,
                        "status": "DONE",
                        "error_message": None,
                        "result": OCR_RESULT_EXAMPLE,
                        "updated_at": "2026-09-18T04:00:01+00:00",
                    },
                    REQUEST_ID_EXAMPLE,
                ),
            ),
            no_text=(
                "Finished, but the image had no readable text: a DONE job with an empty `texts` and both "
                "aggregates null. The rejection happens at structuring, not here",
                envelope(
                    200,
                    "Success",
                    {
                        **_JOB,
                        "status": "DONE",
                        "error_message": None,
                        "result": {
                            "engine": "kk_ocr",
                            "model": "PP-OCRv5_server_det+PP-OCRv5_server_rec",
                            "elapsed_ms": 631.2,
                            "text_regions_count": 0,
                            "avg_doc_score": None,
                            "min_doc_score": None,
                            "texts": [],
                        },
                        "updated_at": "2026-09-18T04:00:01+00:00",
                    },
                    REQUEST_ID_EXAMPLE,
                ),
            ),
            failed=(
                "Failed: same `error_message` as the FAILED callback. Resubmitting the request_id runs it again",
                envelope(
                    200,
                    "Success",
                    {
                        **_JOB,
                        "status": "FAILED",
                        "error_message": "the uploaded document could not be decoded as an image",
                        "result": None,
                        "updated_at": "2026-09-18T04:00:03+00:00",
                    },
                    REQUEST_ID_EXAMPLE,
                ),
            ),
        ),
        401: UNAUTHORIZED,
        404: error(
            404,
            "No OCR job for this request_id",
            f"No OCR job found for request_id: {REQUEST_ID_EXAMPLE}",
            request_id=REQUEST_ID_EXAMPLE,
        ),
        422: error(
            422,
            "Validation Error",
            "path.request_id: Field required",
            request_id=REQUEST_ID_EXAMPLE,
            errors="VALIDATION_ERROR",
        ),
    },
)
async def get_job(request_id: str, service: EkstraksiJobService = Depends(get_job_service)):
    data = await service.get(request_id)
    return envelope(200, "Success", data, request_id)


@router.get(
    "/v1/ekstraksi/outbox",
    response_model=OutboxStatusResponse,
    operation_id="getEkstraksiOutboxStatus",
    summary=OUTBOX_STATUS_SUMMARY,
    description=OUTBOX_STATUS_DESCRIPTION,
    responses=outbox_status_responses("OCR"),
)
async def get_outbox_status(request: Request, pipeline: StagePipeline = Depends(get_pipeline)):
    return envelope(200, "Success", await outbox_status(pipeline), get_request_id(request))


@router.post(
    "/v1/ekstraksi/outbox/release",
    response_model=OutboxReleaseResponse,
    operation_id="releaseOcrOutbox",
    summary=OUTBOX_RELEASE_SUMMARY,
    description=OUTBOX_RELEASE_DESCRIPTION,
    responses=outbox_release_responses("OCR"),
)
async def release_outbox(
    request: Request, request_id: str | None = None, pipeline: StagePipeline = Depends(get_pipeline)
):
    data = await outbox_release(pipeline, request_id)
    return envelope(200, "Success", data, request_id or get_request_id(request))
