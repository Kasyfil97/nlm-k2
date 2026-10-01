"""The `paddle` backend: the ML team's PaddleOCR server on `POST /ocr`, over HTTP.

Ported from nilam's `test_paddle_engine_*`, with the one deliberate difference asserted
explicitly: the answer is the §7.1 box with its quadrilateral intact, not nilam's upright `bbox`.
"""

from typing import Any

import httpx
import pytest

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.errors import UpstreamTimeout, UpstreamUnavailable

from app.config import Settings
from app.dependencies import OCR_BACKENDS
from app.ml.base import RemoteOcrModel
from app.ml.paddle import PaddleOcrEngine

#: The server's answer, in the shape it really returns (see `test_ml_backends.VM_BODY`), with a
#: tilted box so a conversion to an upright rectangle would show, and a blank line to drop.
PADDLE_RESPONSE: dict[str, Any] = {
    "models": {
        "detection": "PP-OCRv6_medium_det",
        "recognition": "PP-OCRv6_medium_rec",
        "pipeline": "PaddleOCR",
        "device": "gpu",
    },
    "num_pages": 1,
    "pages": [
        {
            "page_index": 0,
            "width": 1000,
            "height": 620,
            "texts": [
                {
                    "text": "KARTU KELUARGA",
                    "score": 0.9991727471351624,
                    "poly": [[38, 43], [353, 47], [352, 77], [37, 73]],
                },
                {
                    "text": "No.9924187486671285",
                    "score": 0.9967303276062012,
                    "poly": [[38, 110], [406, 110], [406, 142], [38, 142]],
                },
                {"text": "   ", "score": 0.5, "poly": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            ],
        }
    ],
    "filename": "kk.jpg",
}


def _engine(handler, query: dict[str, Any] | None = None) -> PaddleOcrEngine:
    client = RemoteModelClient(
        "http://ocr.test", 5.0, name="ekstraksi OCR model", transport=httpx.MockTransport(handler)
    )
    return PaddleOcrEngine(client, query=query)


async def test_the_paddle_engine_posts_multipart_to_ocr_and_keeps_the_polygon():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.read()
        return httpx.Response(200, json=PADDLE_RESPONSE)

    result = await _engine(handler).extract("kk.jpg", b"\xff\xd8jpeg", "image/jpeg")

    assert seen["url"] == "http://ocr.test/ocr"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'name="file"; filename="kk.jpg"' in seen["body"]
    assert b"Content-Type: image/jpeg" in seen["body"]
    assert b"\xff\xd8jpeg" in seen["body"]

    assert result["model"] == "PP-OCRv6_medium_det+PP-OCRv6_medium_rec"
    assert [box["text"] for box in result["texts"]] == ["KARTU KELUARGA", "No.9924187486671285"]
    title = result["texts"][0]
    assert title["score"] == 0.9992
    # The tilt survives: nilam's backend would have answered {x1: 37, y1: 43, x2: 353, y2: 77} here.
    assert title["poly"] == [[38.0, 43.0], [353.0, 47.0], [352.0, 77.0], [37.0, 73.0]]
    assert set(title) == {"text", "score", "poly"}


async def test_the_query_goes_on_the_wire_and_no_form_fields_do():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        seen["body"] = request.read()
        return httpx.Response(200, json=PADDLE_RESPONSE)

    await _engine(handler, query={"use_doc_orientation_classify": True}).extract("kk.jpg", b"x", "image/jpeg")
    assert seen["query"] == {"use_doc_orientation_classify": "true"}
    assert seen["body"].count(b"Content-Disposition") == 1, "only the file part, no form fields"


async def test_a_list_of_documents_is_accepted():
    result = await _engine(lambda request: httpx.Response(200, json=[PADDLE_RESPONSE])).extract("kk.jpg", b"x")
    assert len(result["texts"]) == 2
    assert result["model"] == "PP-OCRv6_medium_det+PP-OCRv6_medium_rec"


async def test_only_page_one_of_a_pdf_is_read():
    """The server renders every page of a PDF; the layout parser reads one page frame, so page 2 is
    dropped -- in a single document and in the list-of-documents answer alike."""
    page_two = {
        "page_index": 1,
        "texts": [{"text": "LEGALISIR", "score": 0.99, "poly": [[0, 0], [9, 0], [9, 9], [0, 9]]}],
    }
    two_pages = {**PADDLE_RESPONSE, "num_pages": 2, "pages": [*PADDLE_RESPONSE["pages"], page_two]}
    split = [PADDLE_RESPONSE, {**PADDLE_RESPONSE, "pages": [page_two]}]

    for body in (two_pages, split):
        result = await _engine(lambda request, body=body: httpx.Response(200, json=body)).extract("kk.pdf", b"%PDF")
        assert [box["text"] for box in result["texts"]] == ["KARTU KELUARGA", "No.9924187486671285"]


async def test_a_document_the_server_could_not_decode_is_an_empty_read():
    empty = {"models": PADDLE_RESPONSE["models"], "num_pages": 0, "pages": [], "filename": "t.txt"}
    result = await _engine(lambda request: httpx.Response(200, json=empty)).extract("t.txt", b"hello")
    assert result["texts"] == []
    assert result["model"] == "PP-OCRv6_medium_det+PP-OCRv6_medium_rec"


async def test_filename_and_content_type_have_defaults():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json=PADDLE_RESPONSE)

    await _engine(handler).extract("", b"x", None)
    assert b'filename="upload"' in seen["body"]
    assert b"Content-Type: image/jpeg" in seen["body"]


async def test_an_unreachable_server_is_503_and_a_slow_one_504():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(UpstreamUnavailable):
        await _engine(refuse).extract("kk.jpg", b"x")
    with pytest.raises(UpstreamTimeout):
        await _engine(stall).extract("kk.jpg", b"x")


def test_paddle_is_a_registered_async_backend_and_needs_a_url():
    settings = Settings(api_key="x", ekstraksi_backend="paddle", ekstraksi_ocr_url="http://ocr.test", _env_file=None)
    engine = OCR_BACKENDS["paddle"](settings)
    assert isinstance(engine, PaddleOcrEngine)
    assert isinstance(engine, RemoteOcrModel), "awaited on the loop, not sent to the threadpool"
    assert engine.name == "paddle"

    with pytest.raises(RuntimeError, match="EKSTRAKSI_OCR_URL is required when EKSTRAKSI_BACKEND=paddle"):
        OCR_BACKENDS["paddle"](settings.model_copy(update={"ekstraksi_ocr_url": None}))
