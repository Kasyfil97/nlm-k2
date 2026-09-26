"""The checks a document goes through here, before the guardrails model or any stage sees it, so the
services behind this one only judge what they are for.
"""

from ocr_common.errors import BadRequest
from ocr_common.image_validation import validate_image

from app.config import Settings

PDF_CONTENT_TYPE = "application/pdf"

# A Kartu Keluarga is one card. More pages than the limit is almost always another document bundled
# in. Only reachable with PDF_ENABLED; the message can be shown to the client as is.
TOO_MANY_PAGES_MESSAGE = "Jumlah halaman melebihi batas, pastikan hanya mengunggah foto Kartu Keluarga"


def check_document(content_type: str | None, content: bytes, settings: Settings) -> None:
    """Type and empty file (400), size above `MAX_UPLOAD_BYTES` (413), then -- only when PDF is
    switched on -- the page count above `MAX_DOCUMENT_PAGES` and an unreadable PDF (400).

    `validate_image` checks against `effective_content_types`, so with `PDF_ENABLED=false` a PDF is
    already refused above and the page count is never reached. That ordering is the point: the
    switch is the only thing that admits `application/pdf`, and it admits it at intake rather than
    leaving a half-open path where a PDF passes here and fails deeper as a 422.
    """
    validate_image(content_type, content, settings)
    if not settings.pdf_enabled:
        return
    if (content_type or "").lower() == PDF_CONTENT_TYPE and count_pdf_pages(content) > settings.max_document_pages:
        raise BadRequest(TOO_MANY_PAGES_MESSAGE)


def count_pdf_pages(content: bytes) -> int:
    """The page count from the PDF's page tree, without rendering anything; 400 when it is not a PDF
    PyMuPDF can read or it has no pages.

    `fitz` is imported inside the function, not at module level: with `PDF_ENABLED=false` -- the
    default -- this path never runs, and a module-level import would make PyMuPDF a hard dependency
    of a service that does not use it.
    """
    import fitz  # ty: ignore[unresolved-import]

    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise BadRequest("Uploaded file is not a readable PDF") from exc
    with document:
        if document.page_count == 0:
            raise BadRequest("PDF has no pages")
        return document.page_count
