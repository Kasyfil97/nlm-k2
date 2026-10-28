"""A PDF that is only a wrapper around one photo, read as that photo.

BRISPOT exports a Kartu Keluarga as an A4 page: the photo of the card in the middle, "Foto Kartu
Keluarga" / "BRISPOT" / "Halaman 1 dari 1" as real text above and below it. Sent as a PDF, the OCR
server renders the whole page, so the card is a small landscape patch on a portrait sheet and the
header and footer come back as boxes. Structuring takes "the page" to be the extent of the boxes
(`kk_model`), which then runs from the header to the footer and not from card edge to card edge.

The embedded JPEG is the document. Handing it over as an image puts the card back on the path every
photo already takes, at its native resolution, with nothing else on the page.
"""

from dataclasses import dataclass

#: The photo must cover at least this share of the page, so a PDF with a logo or a stamp on a page of
#: real content is left to the OCR server as it is.
MIN_PAGE_COVERAGE = 0.25
#: A drawn image whose matrix is not a plain scale (rotated, sheared, flipped) is left alone: its
#: pixels would not be upright in the extracted file.
_SKEW_TOLERANCE = 1e-3

PDF_MAGIC = b"%PDF"
_MEDIA = {"jpeg": "image/jpeg", "png": "image/png"}


@dataclass(frozen=True)
class UnwrappedPdf:
    content: bytes
    content_type: str
    extension: str


def unwrap_photo_pdf(content: bytes) -> UnwrappedPdf | None:
    """The one photo page 1 of this PDF is made of, or None to send the PDF as it is.

    None covers everything that is not this shape, and every way of failing to read it: this is an
    optimisation of the input, so a PDF it cannot judge is the OCR server's, not an error here.
    """
    if not content.startswith(PDF_MAGIC):
        return None
    try:
        return _unwrap(content)
    except Exception:
        return None


def _unwrap(content: bytes) -> UnwrappedPdf | None:
    import pymupdf

    with pymupdf.open(stream=content, filetype="pdf") as document:
        if document.is_encrypted or document.page_count == 0:
            return None
        page = document[0]
        if page.rotation:
            return None

        # One entry per drawing of an image, and BRISPOT draws the same one twice: count the files.
        drawn = page.get_image_info(xrefs=True)
        xrefs = {info["xref"] for info in drawn}
        if len(xrefs) != 1 or 0 in xrefs:
            return None
        info = drawn[0]

        a, b, c, d, *_ = info["transform"]
        if abs(b) > _SKEW_TOLERANCE or abs(c) > _SKEW_TOLERANCE or a <= 0 or d <= 0:
            return None
        if a * d < MIN_PAGE_COVERAGE * page.rect.width * page.rect.height:
            return None

        return _as_image(document, info["xref"])


def _as_image(document, xref: int) -> UnwrappedPdf | None:
    import pymupdf

    stored = document.extract_image(xref)
    pixmap = pymupdf.Pixmap(document, xref)
    if pixmap.alpha or pixmap.colorspace is None or pixmap.colorspace.n != 3:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        if pixmap.alpha:
            pixmap = pymupdf.Pixmap(pixmap, 0)

    box = _content_box(pixmap)
    if box is not None:
        left, top, right, bottom = box
        stride = pixmap.stride
        rows = (pixmap.samples[y * stride + left * 3 : y * stride + right * 3] for y in range(top, bottom))
        cropped = pymupdf.Pixmap(pymupdf.csRGB, right - left, bottom - top, b"".join(rows), False)
        return UnwrappedPdf(cropped.tobytes("png"), _MEDIA["png"], "png")

    # Nothing to trim: the stored JPEG as it is when it is a plain one, so no re-encode and no
    # generation loss. An alpha mask, a CMYK/grey colourspace, or an EXIF orientation (which a PDF
    # viewer ignores and an image reader applies, so the card would turn) need the decoded pixels.
    plain = (
        stored.get("ext") == "jpeg"
        and stored.get("colorspace") == 3
        and not stored.get("smask")
        and b"Exif" not in stored["image"][:65536]
    )
    if plain:
        return UnwrappedPdf(stored["image"], _MEDIA["jpeg"], "jpg")
    return UnwrappedPdf(pixmap.tobytes("png"), _MEDIA["png"], "png")


#: A channel value at or above this is paper. Lower than 255 because JPEG leaves white slightly noisy.
_PAPER = 235
_NOT_PAPER = bytes(int(value < _PAPER) for value in range(256))
#: A row or column is content when this share of its samples is not paper, so a speck or a scan
#: edge does not hold the margin open.
_CONTENT_SHARE = 0.01
#: What is kept around the content, as a share of its longer side, so the card's edge is not clipped.
_KEEP_SHARE = 0.02
#: Not worth a re-encode below this much trimmed.
_MIN_TRIM = 0.03


def _content_box(pixmap) -> tuple[int, int, int, int] | None:
    """`(left, top, right, bottom)` of what is not white paper, or None when there is nothing to trim.

    Measured by the team: PaddleOCR reads a card badly with a white margin around it and well once the
    margin is cut off. Pure bytes, not numpy: this service has no array library.
    """
    width, height, stride = pixmap.width, pixmap.height, pixmap.stride
    mask = pixmap.samples.translate(_NOT_PAPER)

    row_limit = width * 3 * _CONTENT_SHARE
    rows = [y for y in range(height) if mask.count(1, y * stride, y * stride + width * 3) >= row_limit]
    column_limit = height * 3 * _CONTENT_SHARE
    columns = [
        x for x in range(width) if sum(mask[x * 3 + channel :: stride].count(1) for channel in range(3)) >= column_limit
    ]
    if not rows or not columns:
        return None

    keep = round(_KEEP_SHARE * max(columns[-1] - columns[0], rows[-1] - rows[0]))
    left, right = max(0, columns[0] - keep), min(width, columns[-1] + 1 + keep)
    top, bottom = max(0, rows[0] - keep), min(height, rows[-1] + 1 + keep)
    if (right - left) * (bottom - top) > (1 - _MIN_TRIM) * width * height:
        return None
    return left, top, right, bottom
