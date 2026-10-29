"""A PDF that wraps one photo (BRISPOT) is read as the photo, not as an A4 page around it."""

import asyncio

import pymupdf
import pytest

from app.config import get_settings
from app.services.extraction_service import ExtractionService
from app.services.pdf_source import unwrap_photo_pdf

A4 = pymupdf.Rect(0, 0, 595, 842)
CARD_RECT = pymupdf.Rect(45, 231, 550, 610)  # a landscape card on a portrait sheet, as BRISPOT draws it


def _photo(width: int = 600, height: int = 400, fmt: str = "jpeg") -> bytes:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pixmap.set_rect(pixmap.irect, (200, 120, 130))
    return pixmap.tobytes(fmt)


def _brispot_pdf(photo: bytes, rect: pymupdf.Rect = CARD_RECT, *, twice: bool = True) -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=A4.width, height=A4.height)
    page.insert_text((262, 28), "Foto Kartu Keluarga")
    page.insert_text((20, 820), "BRISPOT")
    page.insert_text((478, 820), "Halaman 1 dari 1")
    page.insert_image(rect, stream=photo)
    if twice:  # BRISPOT's exports draw the same image twice
        page.insert_image(rect, stream=photo)
    return document.tobytes()


def test_the_embedded_jpeg_is_returned_untouched():
    photo = _photo()

    unwrapped = unwrap_photo_pdf(_brispot_pdf(photo))

    assert unwrapped is not None
    assert (unwrapped.content_type, unwrapped.extension) == ("image/jpeg", "jpg")
    assert unwrapped.content == photo


def _padded_card(width: int = 800, height: int = 600) -> bytes:
    """A pink card with a 100px white margin all round, like the BRISPOT photos."""
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pixmap.set_rect(pixmap.irect, (255, 255, 255))
    pixmap.set_rect(pymupdf.IRect(100, 100, width - 100, height - 100), (200, 120, 130))
    return pixmap.tobytes("png")


def test_the_white_margin_around_the_card_is_cropped_off():
    unwrapped = unwrap_photo_pdf(_brispot_pdf(_padded_card()))

    assert unwrapped is not None
    cropped = pymupdf.Pixmap(unwrapped.content)
    # 600x400 of card plus a 2% keep (12px) each side; the 100px margins are gone.
    assert (cropped.width, cropped.height) == (600 + 2 * 12, 400 + 2 * 12)
    assert tuple(cropped.pixel(cropped.width // 2, cropped.height // 2)) == (200, 120, 130)
    assert tuple(cropped.pixel(0, 0)) == (255, 255, 255)


def test_a_png_photo_comes_back_as_png():
    unwrapped = unwrap_photo_pdf(_brispot_pdf(_photo(fmt="png")))

    assert unwrapped is not None
    assert unwrapped.content_type == "image/png"
    assert unwrapped.content.startswith(b"\x89PNG")


def test_a_jpeg_with_an_exif_orientation_is_decoded_so_the_card_does_not_turn():
    photo = _photo()
    exif = b"\xff\xe1\x00\x10Exif\x00\x00" + b"\x00" * 8
    tagged = photo[:2] + exif + photo[2:]

    unwrapped = unwrap_photo_pdf(_brispot_pdf(tagged))

    assert unwrapped is not None
    assert unwrapped.content_type == "image/png"


def test_a_pdf_with_a_small_logo_is_left_to_the_ocr_server():
    logo = pymupdf.Rect(20, 20, 120, 70)
    assert unwrap_photo_pdf(_brispot_pdf(_photo(), logo)) is None


def test_two_different_images_are_not_one_photo():
    document = pymupdf.open()
    page = document.new_page(width=A4.width, height=A4.height)
    page.insert_image(pymupdf.Rect(45, 40, 550, 400), stream=_photo(600, 400))
    page.insert_image(pymupdf.Rect(45, 420, 550, 800), stream=_photo(601, 401))

    assert unwrap_photo_pdf(document.tobytes()) is None


def test_a_rotated_page_is_left_alone():
    document = pymupdf.open(stream=_brispot_pdf(_photo()), filetype="pdf")
    document[0].set_rotation(90)

    assert unwrap_photo_pdf(document.tobytes()) is None


@pytest.mark.parametrize("content", [b"\xff\xd8\xff not a pdf", b"%PDF-1.4 garbage", b""])
def test_anything_else_is_not_unwrapped(content):
    assert unwrap_photo_pdf(content) is None


class _RecordingEngine:
    name = "recording"

    def __init__(self):
        self.calls: list[tuple[str, bytes, str | None]] = []

    def read(self, filename, content, content_type=None):
        self.calls.append((filename, content, content_type))
        return {"texts": [], "model": None}


def test_the_stage_hands_the_engine_the_photo_not_the_page():
    photo = _photo()
    engine = _RecordingEngine()

    asyncio.run(ExtractionService(engine, get_settings()).extract("kk.pdf", "application/pdf", _brispot_pdf(photo)))

    assert engine.calls == [("kk.jpg", photo, "image/jpeg")]


def test_a_pdf_that_is_not_a_photo_reaches_the_engine_as_a_pdf():
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "text only")
    pdf = document.tobytes()
    engine = _RecordingEngine()

    asyncio.run(ExtractionService(engine, get_settings()).extract("kk.pdf", "application/pdf", pdf))

    assert engine.calls == [("kk.pdf", pdf, "application/pdf")]
