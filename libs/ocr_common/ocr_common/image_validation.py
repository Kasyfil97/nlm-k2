"""The checks every uploaded document goes through before a model sees it."""

from ocr_common.config import BaseServiceSettings
from ocr_common.errors import BadRequest, PayloadTooLarge

# A Kartu Keluarga photo is typically larger than a tax card, so the cap is 5 MB (§13.1); anything
# larger than the limit is refused before any model runs, with a message the client can show as is.
PAYLOAD_TOO_LARGE_MESSAGE = "Ukuran dokumen melebihi batas {limit}, pastikan hanya mengunggah foto Kartu Keluarga"


def upload_limit_label(max_upload_bytes: int) -> str:
    """`MAX_UPLOAD_BYTES` as the caller reads it: `2,5 MB`, `5 MB`."""
    megabytes = max_upload_bytes / (1024 * 1024)
    text = f"{megabytes:.1f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')} MB"


def validate_image(content_type: str | None, content: bytes, settings: BaseServiceSettings) -> None:
    """Raises `BadRequest` when the content type is not allowed or the upload is empty, and
    `PayloadTooLarge` (413) when it exceeds `MAX_UPLOAD_BYTES`.
    """
    content_type = (content_type or "").lower()
    if content_type not in settings.effective_content_types:
        raise BadRequest(f"Unsupported content type: {content_type or 'unknown'}")
    if not content:
        raise BadRequest("Uploaded file is empty")
    if len(content) > settings.max_upload_bytes:
        raise PayloadTooLarge(PAYLOAD_TOO_LARGE_MESSAGE.format(limit=upload_limit_label(settings.max_upload_bytes)))
