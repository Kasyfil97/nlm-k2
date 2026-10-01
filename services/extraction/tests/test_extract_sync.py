"""`POST /v1/extraction/extract`: synchronous OCR answering the §7.1 payload, storing nothing.

Ported from nilam's `test_http_extract_*`; the payload is this repo's §7.1 one, not
nilam's `blocks` / `bbox` / `full_text`.
"""

from ocr_common.errors import UpstreamUnavailable
from ocr_common.pipeline.schemas import OcrPayload
from ocr_common.testing import image_upload

FILE_URL = "https://storage.example.com/kk.jpg"


def test_the_answer_is_the_71_payload_and_is_deterministic_per_content(client, auth):
    first = client.post("/v1/extraction/extract", headers=auth, files=image_upload())
    second = client.post("/v1/extraction/extract", headers=auth, files=image_upload(filename="other.jpg"))

    assert first.status_code == 200
    data = first.json()["data"]
    OcrPayload.model_validate(data)  # the frozen shape structuring reads, not a debug variant
    assert data["engine"] == "mock"
    assert data["texts"], "the mock reads a synthetic card"
    assert all(len(box["poly"]) == 4 for box in data["texts"])
    assert data["text_regions_count"] == len(data["texts"])
    assert second.json()["data"]["texts"] == data["texts"]


def test_nothing_is_stored(client, auth):
    """No job: the synchronous path has no request_id of its own to read back."""
    assert client.post("/v1/extraction/extract", headers=auth, files=image_upload()).status_code == 200
    assert client.get("/v1/extraction/jobs/extract", headers=auth).status_code == 404


def test_an_empty_file_is_400(client, auth):
    response = client.post("/v1/extraction/extract", headers=auth, files=image_upload(content=b""))
    assert response.status_code == 400


def test_a_pdf_is_accepted(client, auth):
    """As in nilam; the backend reads its first page."""
    response = client.post(
        "/v1/extraction/extract",
        headers=auth,
        files=image_upload(filename="kk.pdf", content=b"%PDF-1.4 ...", content_type="application/pdf"),
    )
    assert response.status_code == 200


def test_the_api_key_is_required(client):
    assert client.post("/v1/extraction/extract", files=image_upload()).status_code == 401


def test_exactly_one_of_file_and_file_url(client, auth):
    assert client.post("/v1/extraction/extract", headers=auth).status_code == 400
    both = client.post("/v1/extraction/extract", headers=auth, files=image_upload(), data={"file_url": FILE_URL})
    assert both.status_code == 400


def test_file_url_is_fetched_by_the_service(client, auth, monkeypatch):
    async def fake_fetch(url, *, limit, timeout=10.0, policy):
        assert url == FILE_URL
        assert policy is not None, "the download goes through the shared URL policy"
        return b"\xff\xd8fake-jpeg-bytes", "kk.jpg", "image/jpeg"

    monkeypatch.setattr("ocr_common.web.intake.fetch", fake_fetch)
    response = client.post("/v1/extraction/extract", headers=auth, data={"file_url": FILE_URL})
    assert response.status_code == 200
    assert response.json()["data"]["texts"]


def test_an_unreachable_model_is_503(client, auth, use_sync_engine):
    class Down:
        name = "paddle"

        async def extract(self, filename, content, content_type=None):
            raise UpstreamUnavailable("extraction OCR model is unavailable")

        async def aclose(self):
            pass

    use_sync_engine(Down())
    response = client.post("/v1/extraction/extract", headers=auth, files=image_upload())
    assert response.status_code == 503
    assert "unavailable" in response.json()["message"]
