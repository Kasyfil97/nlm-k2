import pytest

from ocr_common.errors import ServiceError, UpstreamTimeout, UpstreamUnavailable
from ocr_common.testing import image_upload

from app.config import get_settings
from app.main import app
from app.services.pipeline_waiter import STATUS_REJECTED, WaitOutcome
from tests.conftest import EXPECTED_DATA, JPEG, REJECTED_REPORT

TOO_MANY_PAGES = "Jumlah halaman melebihi batas, pastikan hanya mengunggah foto Kartu Keluarga"


def _submit(client, auth, filename="kk.jpg", content=JPEG, content_type="image/jpeg", **data):
    return client.post(
        "/v1/extract-ocr",
        data={"request_id": "OCR_1", **data},
        files=image_upload(filename, content, content_type),
        headers=auth,
    )


def test_health_has_no_backends(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["backends"] == {}


def test_extract_ocr_follows_the_central_orchestrators_contract(client, auth, stub_guardrails, stub_ekstraksi):
    response = _submit(client, auth)

    assert response.status_code == 200
    assert response.json() == {
        "status_code": 200,
        "status_desc": "OK",
        "message": "OCR extraction completed successfully",
        "data": EXPECTED_DATA,
        "errors": None,
        "request_id": "OCR_1",
        "document_type": "kk",
        "job_status": "completed",
        "guardrails": 0,
        "params": None,
    }
    assert stub_guardrails.checked == [{"request_id": "OCR_1", "filename": "kk.jpg", "content_type": "image/jpeg"}]
    [handed] = stub_ekstraksi.submitted
    assert handed["guardrails"]["passed"] is True


def test_rejection_by_the_guardrails_model_is_400_with_guardrails_0(client, auth, stub_ekstraksi, stub_waiter):
    response = _submit(client, auth, filename="notkk.jpg")

    assert response.status_code == 400
    assert response.json() == {
        "status_code": 400,
        "status_desc": "Bad Request",
        "message": REJECTED_REPORT["reason"],
        "data": None,
        "errors": "DOWNSTREAM_VALIDATION_ERROR",
        "request_id": "OCR_1",
        "document_type": "kk",
        "job_status": "failed",
        "guardrails": 1,
        "params": None,
    }
    assert stub_ekstraksi.submitted == [] and stub_waiter.calls == []


def test_request_id_is_required(client, auth):
    response = client.post("/v1/extract-ocr", files=image_upload("kk.jpg", JPEG), headers=auth)
    assert response.status_code == 422
    body = response.json()
    assert body["errors"] == "VALIDATION_ERROR"
    assert body["message"] == "body.request_id: Field required"


def test_file_url_is_fetched_here_and_forwarded_as_url(client, auth, monkeypatch, stub_ekstraksi):
    async def fake_fetch(url, *, limit, timeout=10.0, policy):
        assert url == "http://minio.local/bucket/kk.jpg"
        return JPEG, "kk.jpg", "image/jpeg"

    monkeypatch.setattr("ocr_common.web.intake.fetch", fake_fetch)
    response = client.post(
        "/v1/extract-ocr",
        data={"request_id": "OCR_4", "file_url": "http://minio.local/bucket/kk.jpg"},
        headers=auth,
    )
    assert response.status_code == 200
    assert response.json()["job_status"] == "completed"
    assert stub_ekstraksi.submitted[0]["file_url"] == "http://minio.local/bucket/kk.jpg"


def test_unsupported_content_type_is_400_before_guardrails(client, auth, stub_guardrails):
    response = _submit(client, auth, content_type="text/plain")
    assert response.status_code == 400
    assert response.json()["message"].startswith("Unsupported content type")
    assert stub_guardrails.checked == []


def test_oversized_document_is_413_before_guardrails(client, auth, stub_guardrails):
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update={"max_upload_bytes": 10})
    try:
        response = _submit(client, auth)
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 413
    body = response.json()
    assert body["status_desc"] == "Payload Too Large"
    assert body["message"].startswith("Ukuran dokumen melebihi batas")
    assert stub_guardrails.checked == []


def _pdf(n_pages: int) -> bytes:
    fitz = pytest.importorskip("fitz", reason="PyMuPDF is only needed when PDF_ENABLED is on")
    document = fitz.open()
    for i in range(n_pages):
        document.new_page(width=300, height=200).insert_text((20, 40), f"halaman {i + 1}")
    return document.tobytes()


def _submit_pdf(client, auth, content):
    return _submit(client, auth, filename="scan.pdf", content=content, content_type="application/pdf")


def test_a_pdf_is_refused_at_intake_while_pdf_is_off(client, auth, stub_guardrails, stub_ekstraksi):
    """R34a: `PDF_ENABLED` is the ONLY thing that admits application/pdf, and it admits it at
    intake. Off, a PDF is an unsupported content type -- a clean 400 -- rather than a file that
    passes here and fails deeper as a 422."""
    response = _submit_pdf(client, auth, b"%PDF-1.4 whatever")

    assert response.status_code == 400
    assert response.json()["message"].startswith("Unsupported content type")
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_the_page_check_is_not_even_reached_while_pdf_is_off(client, auth, stub_guardrails):
    """A PDF with too many pages gets the content-type refusal, not the page one: the switch is
    checked first, so PyMuPDF is never imported on the default path."""
    response = _submit_pdf(client, auth, b"%PDF-1.4 whatever")
    assert response.json()["message"] != TOO_MANY_PAGES
    assert stub_guardrails.checked == []


def test_more_than_two_pages_is_400_before_guardrails(client, auth, settings_override, stub_guardrails, stub_ekstraksi):
    settings_override(pdf_enabled=True)
    response = _submit_pdf(client, auth, _pdf(3))

    assert response.status_code == 400
    body = response.json()
    assert (body["message"], body["errors"]) == (TOO_MANY_PAGES, TOO_MANY_PAGES)
    assert "job_status" not in body
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_two_pages_are_within_the_limit(client, auth, settings_override, stub_guardrails):
    settings_override(pdf_enabled=True)
    response = _submit_pdf(client, auth, _pdf(2))

    assert response.status_code == 200
    assert len(stub_guardrails.checked) == 1


def test_the_page_limit_is_a_setting(client, auth, settings_override, stub_guardrails):
    settings_override(pdf_enabled=True, max_document_pages=1)
    response = _submit_pdf(client, auth, _pdf(2))

    assert response.status_code == 400
    assert response.json()["message"] == TOO_MANY_PAGES
    assert stub_guardrails.checked == []


def test_unreadable_pdf_is_400_before_guardrails(client, auth, settings_override, stub_guardrails):
    pytest.importorskip("fitz", reason="PyMuPDF is only needed when PDF_ENABLED is on")
    settings_override(pdf_enabled=True)
    response = _submit_pdf(client, auth, b"%PDF-1.4 garbage")

    assert response.status_code == 400
    assert response.json()["message"] == "Uploaded file is not a readable PDF"
    assert stub_guardrails.checked == []


def test_a_refusal_of_the_guardrails_service_is_answered_as_it_is(client, auth, stub_guardrails, stub_ekstraksi):
    stub_guardrails.error = ServiceError(400, "Uploaded file is not a readable image")

    response = _submit(client, auth)

    assert response.status_code == 400
    assert response.json()["message"] == "Uploaded file is not a readable image"
    assert stub_ekstraksi.submitted == []


def test_guardrails_unreachable_is_503_and_nothing_starts(client, auth, stub_guardrails, stub_ekstraksi):
    stub_guardrails.error = UpstreamUnavailable("guardrails service is unavailable")

    response = _submit(client, auth)

    assert response.status_code == 503
    assert response.json()["message"] == "guardrails service is unavailable"
    assert stub_ekstraksi.submitted == []


def test_skip_guardrails_is_refused_with_403_while_not_allowed(client, auth, stub_guardrails, stub_ekstraksi):
    response = _submit(client, auth, skip_guardrails="true")

    assert response.status_code == 403
    assert response.json() == {
        "status_code": 403,
        "status_desc": "Forbidden",
        "message": "skip_guardrails is not allowed here: GUARDRAILS_SKIP_ALLOWED is off",
        "data": None,
        "errors": "GUARDRAILS_SKIP_NOT_ALLOWED",
        "request_id": "OCR_1",
        "document_type": "kk",
        "job_status": None,
        "guardrails": None,
        "params": None,
    }
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_skip_guardrails_false_is_never_refused(client, auth, stub_guardrails):
    response = _submit(client, auth, skip_guardrails="false")

    assert response.status_code == 200
    assert len(stub_guardrails.checked) == 1


def test_skip_guardrails_must_be_a_boolean(client, auth, stub_guardrails):
    response = _submit(client, auth, skip_guardrails="maybe")

    assert response.status_code == 422
    assert response.json()["errors"] == "VALIDATION_ERROR"
    assert stub_guardrails.checked == []


def test_skipped_guardrails_hand_the_document_on_without_a_report(
    client, auth, settings_override, stub_guardrails, stub_ekstraksi
):
    settings_override(guardrails_skip_allowed=True)

    # A file name the guardrails model rejects: with the check skipped it never gets to judge it.
    response = _submit(client, auth, filename="notkk.jpg", skip_guardrails="true")

    assert response.status_code == 200
    body = response.json()
    assert (body["job_status"], body["guardrails"], body["errors"]) == ("completed", 0, None)
    assert stub_guardrails.checked == []
    [handed] = stub_ekstraksi.submitted
    assert handed["guardrails"] is None


def test_with_guardrails_skipped_the_kk_validity_gate_still_rejects(client, auth, settings_override, stub_waiter):
    settings_override(guardrails_skip_allowed=True)
    stub_waiter.outcome = WaitOutcome("STRUCTURING", STATUS_REJECTED, "dokumen blur / blank")

    response = _submit(client, auth, skip_guardrails="true")

    assert response.status_code == 400
    body = response.json()
    assert (body["message"], body["errors"], body["guardrails"]) == (
        "dokumen blur / blank",
        "DOWNSTREAM_VALIDATION_ERROR",
        1,
    )


def test_with_guardrails_skipped_the_file_checks_still_run(client, auth, settings_override, stub_ekstraksi):
    settings_override(guardrails_skip_allowed=True, pdf_enabled=True)
    pages = _submit(
        client, auth, filename="scan.pdf", content=_pdf(3), content_type="application/pdf", skip_guardrails="true"
    )
    settings_override(guardrails_skip_allowed=True, max_upload_bytes=10)
    size = _submit(client, auth, skip_guardrails="true")

    assert (pages.status_code, pages.json()["message"]) == (400, TOO_MANY_PAGES)
    assert size.status_code == 413
    assert stub_ekstraksi.submitted == []


def test_missing_api_key_returns_401_envelope(client):
    response = client.post("/v1/extract-ocr", data={"request_id": "OCR_7"}, files=image_upload())
    assert response.status_code == 401
    assert response.json()["errors"] == "Invalid or missing API key"


# --- the scenarios the plan names for this unit --------------------------------------------


def test_the_get_returns_the_same_data_as_the_post_that_produced_it(client, auth):
    """§3.2 is one contract answered by two endpoints. Two shapes for one request would make the
    `202` -> poll path answer something the caller did not get on the direct path."""
    posted = _submit(client, auth)
    fetched = client.get("/v1/extract-ocr/OCR_1", headers=auth)

    assert (posted.status_code, fetched.status_code) == (200, 200)
    assert posted.json()["data"] == fetched.json()["data"]
    assert fetched.json()["params"] is None, "params is not stored, so the GET cannot echo it"


def test_a_guardrails_rejection_leaves_nothing_for_the_get_to_find(client, auth, stub_waiter):
    """§2.6: a document the guardrails model rejects never enters the pipeline, so no stage has a
    job for it and the GET is a 404. The `400` of the POST is the final answer."""
    rejected = _submit(client, auth, filename="notkk.jpg")
    stub_waiter.snapshot_outcome = None
    fetched = client.get("/v1/extract-ocr/OCR_1", headers=auth)

    assert (rejected.status_code, fetched.status_code) == (400, 404)


def test_the_same_request_id_twice_gives_two_consistent_answers(client, auth, stub_ekstraksi):
    """§2.5. The orchestrator stores nothing, so idempotency is the stage's: the second hand-off
    comes back `duplicate: true` and the pipeline does not run again. What this pins is that the
    caller cannot tell the two apart."""
    first = _submit(client, auth)
    second = _submit(client, auth)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(stub_ekstraksi.submitted) == 2, "both are handed over; the stage decides it is a duplicate"


def test_an_empty_file_is_400_before_guardrails(client, auth, stub_guardrails, stub_ekstraksi):
    response = _submit(client, auth, content=b"")

    assert response.status_code == 400
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_guardrails_timing_out_is_504_and_nothing_starts(client, auth, stub_guardrails, stub_ekstraksi):
    stub_guardrails.error = UpstreamTimeout("guardrails service did not answer in time")

    response = _submit(client, auth)

    assert response.status_code == 504
    assert stub_ekstraksi.submitted == []
