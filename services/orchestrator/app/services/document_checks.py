"""The checks a document goes through here, before the guardrails model or any stage sees it, so the
services behind this one only judge what they are for.
"""

from ocr_common.errors import BadRequest
from ocr_common.image_validation import validate_image

from app.config import Settings

PDF_CONTENT_TYPE = "application/pdf"

# A Kartu Keluarga is one sheet; a PDF of it is at most `MAX_DOCUMENT_PAGES` (2, ML team 23 Sep 2026),
# and more is almost always another document bundled in. Only the first page is read downstream.
# The message can be shown to the client as is.
TOO_MANY_PAGES_MESSAGE = "Jumlah halaman melebihi batas, pastikan hanya mengunggah foto Kartu Keluarga"


def check_document(content_type: str | None, content: bytes, settings: Settings) -> int:
    """Type and empty file (400), size above `MAX_UPLOAD_BYTES` (413), then for a PDF the page count
    above `MAX_DOCUMENT_PAGES` and an unreadable PDF (400), as in nilam.

    Returns the page count -- 1 for an image -- which the guardrails log records as `n_pages`.
    """
    validate_image(content_type, content, settings)
    if (content_type or "").lower() != PDF_CONTENT_TYPE:
        return 1
    pages = count_pdf_pages(content)
    if pages > settings.max_document_pages:
        raise BadRequest(TOO_MANY_PAGES_MESSAGE)
    return pages


def count_pdf_pages(content: bytes) -> int:
    """The page count from the PDF's page tree, without rendering anything; 400 when it is not a PDF
    PyMuPDF can read or it has no pages.

    `fitz` is imported here rather than at module level only so that importing the app does not pay
    for PyMuPDF (and its deprecation notice) before the first PDF arrives; it is a hard dependency.
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
