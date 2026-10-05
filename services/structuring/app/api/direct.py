from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ocr_common.errors import BadRequest
from ocr_common.kk import DOCUMENT_TYPE
from ocr_common.web.envelope import envelope
from ocr_common.web.request_id import get_request_id
from ocr_common.web.schemas import REQUEST_ID_EXAMPLE, UNAUTHORIZED, error, success_examples
from ocr_common.web.security import verify_api_key

from app.api.schemas import StructuringDirectRequest, StructuringDirectResponse
from app.api.structuring import REJECTED_EXAMPLE, STRUCTURED_EXAMPLE
from app.dependencies import get_structuring_service
from app.services.structuring_service import StructuringService

router = APIRouter(tags=["Direct"], dependencies=[Depends(verify_api_key)])


@router.post(
    "/v1/structuring-direct",
    response_model=StructuringDirectResponse,
    operation_id="structureDirect",
    summary="Structure an OCR result now, synchronous (no job, no callback, no hand-off)",
    description=(
        "**For testing one stage on its own (QC).** Takes the output of the previous stage, the extraction "
        "service's OCR result (`data` of its `POST /v1/extraction/extract`, or the `result` of its job), runs "
        "the same parser and KK validity gate the pipeline runs, and answers with the structured document in the "
        "body. Nothing is recorded: no `structuring_jobs` row, no callback, no hand-off to scoring, so it never "
        "touches the orchestrator's data.\n\n"
        "The answer is exactly what `GET /v1/structuring/jobs/{request_id}` reports as `result`, and what the "
        "scoring stage receives as `structuring`: paste it into `POST /v1/scoring-direct` to test the next "
        "stage. Always 200 when the rules ran, also for a document the validity gate rejects (an OCR result "
        "without a single box included): read `reject_reason` (in the pipeline that rejection stops the job "
        "and gives the client a 400).\n\n"
        "Unlike `POST /v1/ocr_postprocess` (the K2Regex-v2 endpoint on a bare `texts` list), this takes the "
        "whole OCR payload as the pipeline hands it on."
    ),
    responses={
        200: success_examples(
            "The OCR result was structured",
            accepted=("A readable card", envelope(200, "Success", STRUCTURED_EXAMPLE, REQUEST_ID_EXAMPLE)),
            rejected=(
                "A document the validity gate rejects: `reject_reason` says why",
                envelope(200, "Success", REJECTED_EXAMPLE, REQUEST_ID_EXAMPLE),
            ),
        ),
        400: error(400, "An unsupported `document_type`", "Unsupported document_type: ktp. Supported: ['kk']"),
        401: UNAUTHORIZED,
        422: error(422, "Validation Error", "body.ocr: Field required", errors="VALIDATION_ERROR"),
    },
)
async def structure_direct(
    request: Request,
    body: StructuringDirectRequest,
    service: StructuringService = Depends(get_structuring_service),
):
    if body.document_type != DOCUMENT_TYPE:
        raise BadRequest(f"Unsupported document_type: {body.document_type}. Supported: ['{DOCUMENT_TYPE}']")
    boxes = StructuringService.boxes_from_ocr(body.ocr.model_dump(exclude_unset=True))
    # Off the event loop, like the job path and /v1/ocr_postprocess: the parser is CPU-bound.
    data = await run_in_threadpool(service.structure, boxes)
    return envelope(200, "Success", data, body.request_id or get_request_id(request))
