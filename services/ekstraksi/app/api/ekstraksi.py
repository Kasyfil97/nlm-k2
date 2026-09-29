"""`POST /v1/ekstraksi/extract`: the OCR of one document, synchronous -- no job, no callback, no table.

For debugging and for the ML team: the same backend and the same §7.1 payload the async stage stores
in `ocr_results`, answered in the request. Because it is that payload and not a looser debug shape,
its `data` can be sent as it is to structuring's `POST /v1/ocr_postprocess`.
"""

from fastapi import APIRouter, Depends, Request, UploadFile

from ocr_common.image_validation import PAYLOAD_TOO_LARGE_MESSAGE
from ocr_common.web.envelope import envelope
from ocr_common.web.intake import FileField, FileUrlField, read_image
from ocr_common.web.request_id import get_request_id
from ocr_common.web.schemas import REQUEST_ID_EXAMPLE, UNAUTHORIZED, error, success_examples
from ocr_common.web.security import verify_api_key

from app.api.jobs import OCR_RESULT_EXAMPLE
from app.api.schemas import ExtractResponse
from app.dependencies import get_ekstraksi_service
from app.services.ekstraksi_service import EkstraksiService

router = APIRouter(tags=["Ekstraksi"], dependencies=[Depends(verify_api_key)])


@router.post(
    "/v1/ekstraksi/extract",
    response_model=ExtractResponse,
    operation_id="extractText",
    summary="OCR of a Kartu Keluarga, synchronous (no job, no callback)",
    description=(
        "Runs the configured OCR backend on the document and answers the §7.1 payload -- one "
        "`{text, score, poly}` box per recognised line plus the three document aggregates -- in the response. "
        "Nothing is stored and nothing is handed on.\n\n"
        "Send the document as `file` or `file_url`, exactly one. JPEG, PNG or PDF (only the first page of a "
        "PDF is read), at most `MAX_UPLOAD_BYTES`. As in the async stage, an image with no readable text is a "
        "200 with an empty `texts`, not an error."
    ),
    responses={
        200: success_examples(
            "Text lines found in the document",
            kartu_keluarga=("A Kartu Keluarga", envelope(200, "Success", OCR_RESULT_EXAMPLE, REQUEST_ID_EXAMPLE)),
        ),
        400: error(400, "Bad file (empty, unsupported type) or bad intake", "Uploaded file is empty"),
        413: error(
            413,
            "The document exceeds `MAX_UPLOAD_BYTES` (5 MB by default)",
            PAYLOAD_TOO_LARGE_MESSAGE.format(limit="5 MB"),
        ),
        401: UNAUTHORIZED,
        500: error(500, "OCR backend failed", "ekstraksi OCR model error (500): error: OpenCV ..."),
        503: error(503, "OCR model unreachable", "ekstraksi OCR model is unavailable"),
        504: error(504, "OCR model did not answer in time", "ekstraksi OCR model timed out after 30.0s"),
        422: error(422, "Validation Error", "body.file: Expected UploadFile, received: str", errors="VALIDATION_ERROR"),
    },
)
async def extract(
    request: Request,
    file: UploadFile | str | None = FileField,
    file_url: str | None = FileUrlField,
    service: EkstraksiService = Depends(get_ekstraksi_service),
):
    content, filename, content_type = await read_image(request, file, file_url)
    data = await service.extract(filename, content_type, content)
    return envelope(200, "Success", data, get_request_id(request))
