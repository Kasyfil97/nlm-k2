import pytest

from ocr_common.errors import ServiceError, UpstreamTimeout, UpstreamUnavailable
from ocr_common.testing import image_upload

from app.config import get_settings
from app.main import app
from app.services.pipeline_waiter import STATUS_REJECTED, WaitOutcome
from tests.conftest import ACCEPTED_REPORT, EXPECTED_DATA, JPEG, OCR_RESULT, REJECTED_REPORT, STRUCTURING_RESULT

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
        "pipeline_last_stage": "scoring",
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
        "pipeline_last_stage": "guardrails",
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
    import fitz  # ty: ignore[unresolved-import]

    document = fitz.open()
    for i in range(n_pages):
        document.new_page(width=300, height=200).insert_text((20, 40), f"halaman {i + 1}")
    return document.tobytes()


def _submit_pdf(client, auth, content):
    return _submit(client, auth, filename="scan.pdf", content=content, content_type="application/pdf")


def test_a_pdf_is_accepted_and_sent_on_as_it_is(client, auth, stub_guardrails, stub_ekstraksi):
    """As in nilam: a PDF passes intake and goes to guardrails and ekstraksi unchanged; each of them
    reads its first page. R34a's switch is gone (`docs/decisions/2026-09-29-selaras-nilam.md`)."""
    response = _submit_pdf(client, auth, _pdf(1))

    assert response.status_code == 200
    assert len(stub_guardrails.checked) == 1
    assert len(stub_ekstraksi.submitted) == 1


def test_more_than_two_pages_is_400_before_guardrails(client, auth, settings_override, stub_guardrails, stub_ekstraksi):
    response = _submit_pdf(client, auth, _pdf(3))

    assert response.status_code == 400
    body = response.json()
    assert (body["message"], body["errors"], body["pipeline_last_stage"]) == (
        TOO_MANY_PAGES,
        "TOO_MANY_PAGES",
        "orchestrator",
    )
    assert "job_status" not in body
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_two_pages_are_within_the_limit(client, auth, settings_override, stub_guardrails):
    response = _submit_pdf(client, auth, _pdf(2))

    assert response.status_code == 200
    assert len(stub_guardrails.checked) == 1


def test_the_page_limit_is_a_setting(client, auth, settings_override, stub_guardrails):
    settings_override(max_document_pages=1)
    response = _submit_pdf(client, auth, _pdf(2))

    assert response.status_code == 400
    assert response.json()["message"] == TOO_MANY_PAGES
    assert stub_guardrails.checked == []


def test_unreadable_pdf_is_400_before_guardrails(client, auth, settings_override, stub_guardrails):
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


NO_GUARDRAILS = ["ekstraksi", "structuring", "scoring"]
FULL = ["guardrails", "ekstraksi", "structuring", "scoring"]


def test_without_a_sequence_the_whole_pipeline_runs(client, auth, stub_guardrails, stub_ekstraksi, stub_waiter):
    response = _submit(client, auth)

    assert response.status_code == 200
    assert len(stub_guardrails.checked) == 1
    assert stub_ekstraksi.submitted[0]["sequence"] == FULL
    assert stub_waiter.last_stages == ["SCORING"]


def test_the_sequence_is_taken_as_repeated_fields_or_as_a_json_array(client, auth, stub_ekstraksi, stub_waiter):
    stub_waiter.outcome = WaitOutcome("OCR", "DONE", results={"OCR": OCR_RESULT})
    repeated = _submit(client, auth, pipeline_name_sequence=["guardrails", "ekstraksi"])
    as_json = _submit(client, auth, pipeline_name_sequence='["guardrails", "ekstraksi"]')

    assert (repeated.status_code, as_json.status_code) == (200, 200)
    assert [handed["sequence"] for handed in stub_ekstraksi.submitted] == [["guardrails", "ekstraksi"]] * 2
    assert stub_waiter.last_stages == ["OCR", "OCR"]


@pytest.mark.parametrize("blank", ["", "  ", ["", ""]])
def test_a_blank_sequence_field_is_the_full_pipeline(client, auth, stub_guardrails, stub_ekstraksi, blank):
    """Swagger UI and Postman send an empty form field as "" rather than leaving it out."""
    response = _submit(client, auth, pipeline_name_sequence=blank)

    assert response.status_code == 200
    assert stub_ekstraksi.submitted[0]["sequence"] == FULL


@pytest.mark.parametrize(
    ("sequence", "reason"),
    [
        (["ekstraksi", "scoring"], "without skipping one in the middle"),
        (["guardrails", "structuring", "scoring"], "without skipping one in the middle"),
        (["structuring", "scoring"], "structuring cannot come first"),
        (["guardrails", "scoring", "structuring", "ekstraksi"], "without skipping one in the middle"),
        (["guardrails", "guardrails"], "listed twice"),
        (["guardrails", "extraction"], "unknown service 'extraction'"),
        ('["guardrails", ', "JSON array of strings"),
    ],
)
def test_an_invalid_sequence_is_422_before_anything_runs(
    client, auth, stub_guardrails, stub_ekstraksi, sequence, reason
):
    response = _submit(client, auth, pipeline_name_sequence=sequence)

    assert response.status_code == 422
    body = response.json()
    assert body["errors"] == "INVALID_PIPELINE_SEQUENCE"
    assert body["message"].startswith("Invalid pipeline_name_sequence: ") and reason in body["message"]
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_guardrails_only_answers_with_the_report_as_it_is(client, auth, stub_ekstraksi, stub_waiter):
    response = _submit(client, auth, pipeline_name_sequence=["guardrails"])

    assert response.status_code == 200
    body = response.json()
    assert (body["job_status"], body["guardrails"], body["errors"]) == ("completed", 0, None)
    assert body["data"] == ACCEPTED_REPORT
    assert stub_ekstraksi.submitted == [] and stub_waiter.calls == []


def test_guardrails_only_still_rejects(client, auth):
    response = _submit(client, auth, filename="notkk.jpg", pipeline_name_sequence=["guardrails"])

    assert response.status_code == 400
    assert (response.json()["errors"], response.json()["guardrails"]) == ("DOWNSTREAM_VALIDATION_ERROR", 1)


def test_a_sequence_ending_at_ekstraksi_answers_with_the_ocr_result_as_it_is(client, auth, stub_waiter):
    stub_waiter.outcome = WaitOutcome("OCR", "DONE", results={"OCR": OCR_RESULT})

    response = _submit(client, auth, pipeline_name_sequence=["guardrails", "ekstraksi"])

    assert response.status_code == 200
    assert (response.json()["job_status"], response.json()["data"]) == ("completed", OCR_RESULT)


def test_a_sequence_ending_at_structuring_answers_with_its_result_as_it_is(client, auth, stub_waiter):
    stub_waiter.outcome = WaitOutcome(
        "STRUCTURING", "DONE", results={"OCR": OCR_RESULT, "STRUCTURING": STRUCTURING_RESULT}
    )

    response = _submit(client, auth, pipeline_name_sequence=["guardrails", "ekstraksi", "structuring"])

    assert response.status_code == 200
    assert response.json()["data"] == STRUCTURING_RESULT
    assert stub_waiter.last_stages == ["STRUCTURING"]


def test_without_guardrails_the_document_is_handed_on_without_a_report(client, auth, stub_guardrails, stub_ekstraksi):
    """Leaving guardrails out is the central orchestrator's call, as in nilam: no setting here can refuse it."""
    # A file name the guardrails model rejects: left out of the sequence, it never gets to judge it.
    response = _submit(client, auth, filename="notkk.jpg", pipeline_name_sequence=NO_GUARDRAILS)

    assert response.status_code == 200
    body = response.json()
    assert (body["job_status"], body["guardrails"], body["errors"]) == ("completed", 0, None)
    assert stub_guardrails.checked == []
    [handed] = stub_ekstraksi.submitted
    assert (handed["guardrails"], handed["sequence"]) == (None, NO_GUARDRAILS)


def test_without_guardrails_the_kk_validity_gate_still_rejects(client, auth, stub_waiter):
    stub_waiter.outcome = WaitOutcome("STRUCTURING", STATUS_REJECTED, "dokumen blur / blank")

    response = _submit(client, auth, pipeline_name_sequence=NO_GUARDRAILS)

    assert response.status_code == 400
    body = response.json()
    assert (body["message"], body["errors"], body["guardrails"]) == (
        "dokumen blur / blank",
        "DOWNSTREAM_VALIDATION_ERROR",
        1,
    )


def test_without_guardrails_the_file_checks_still_run(client, auth, settings_override, stub_ekstraksi):
    pages = _submit(
        client,
        auth,
        filename="scan.pdf",
        content=_pdf(3),
        content_type="application/pdf",
        pipeline_name_sequence=NO_GUARDRAILS,
    )
    settings_override(max_upload_bytes=10)
    size = _submit(client, auth, pipeline_name_sequence=NO_GUARDRAILS)

    assert (pages.status_code, pages.json()["message"]) == (400, TOO_MANY_PAGES)
    assert size.status_code == 413
    assert stub_ekstraksi.submitted == []


@pytest.mark.parametrize(
    ("sequence", "outcome", "status", "stage"),
    [
        (None, None, 200, "scoring"),
        (["guardrails"], None, 200, "guardrails"),
        (["guardrails", "ekstraksi"], WaitOutcome("OCR", "DONE", results={"OCR": OCR_RESULT}), 200, "ekstraksi"),
        (None, WaitOutcome("OCR", "FAILED", "OCR model is unavailable"), 422, "ekstraksi"),
        (None, WaitOutcome("STRUCTURING", STATUS_REJECTED, "dokumen blur / blank"), 400, "structuring"),
        (None, WaitOutcome("STRUCTURING", "PROCESSING"), 202, "structuring"),
    ],
)
def test_pipeline_last_stage_names_the_service_the_answer_comes_from(
    client, auth, stub_waiter, sequence, outcome, status, stage
):
    if outcome is not None:
        stub_waiter.outcome = outcome
    data = {"pipeline_name_sequence": sequence} if sequence else {}

    response = _submit(client, auth, **data)

    assert (response.status_code, response.json()["pipeline_last_stage"]) == (status, stage)


def test_a_guardrails_rejection_comes_from_guardrails(client, auth):
    response = _submit(client, auth, filename="notkk.jpg")

    assert (response.status_code, response.json()["pipeline_last_stage"]) == (400, "guardrails")


def test_a_refusal_before_any_pipeline_service_names_the_orchestrator(client, auth):
    response = _submit(client, auth, pipeline_name_sequence=["ekstraksi", "scoring"])

    assert (response.status_code, response.json()["pipeline_last_stage"]) == (422, "orchestrator")


def test_missing_api_key_returns_401_envelope(client):
    response = client.post("/v1/extract-ocr", data={"request_id": "OCR_7"}, files=image_upload())
    assert response.status_code == 401
    body = response.json()
    assert (body["message"], body["errors"], body["pipeline_last_stage"]) == (
        "Invalid or missing API key",
        "UNAUTHORIZED",
        "orchestrator",
    )


# --- the scenarios the plan names for this unit --------------------------------------------


def test_the_get_returns_the_same_data_as_the_post_that_produced_it(client, auth):
    """§3.2 is one contract answered by two endpoints. Two shapes for one request would make the
    `202` -> poll path answer something the caller did not get on the direct path."""
    posted = _submit(client, auth)
    fetched = client.get("/v1/extract-ocr/OCR_1", headers=auth)

    assert (posted.status_code, fetched.status_code) == (200, 200)
    assert posted.json()["data"] == fetched.json()["data"]
    assert fetched.json()["params"] is None, "params is not stored, so the GET cannot echo it"


def test_a_guardrails_rejection_is_answered_again_by_the_get_from_guardrails_results(
    client, auth, stub_waiter, guardrails_log
):
    """§2.6: a document the guardrails model rejects never enters the pipeline, so no stage has a
    job for it. Its verdict is kept in guardrails_results (as in nilam), so the GET answers the same
    `400` as the POST; only without a kept verdict (no DATABASE_URL) is it a 404."""
    rejected = _submit(client, auth, filename="notkk.jpg")
    stub_waiter.snapshot_outcome = None
    fetched = client.get("/v1/extract-ocr/OCR_1", headers=auth)
    guardrails_log.records.clear()
    forgotten = client.get("/v1/extract-ocr/OCR_1", headers=auth)

    assert (rejected.status_code, fetched.status_code, forgotten.status_code) == (400, 400, 404)
    assert fetched.json()["message"] == rejected.json()["message"]


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


@pytest.mark.parametrize("failing", ["guardrails", "ekstraksi"])
def test_an_unreachable_service_is_named_in_the_error(client, auth, stub_guardrails, stub_ekstraksi, failing):
    """Ported from nilam: the answer names the service that could not be reached, in the extract-ocr shape."""
    stub = stub_guardrails if failing == "guardrails" else stub_ekstraksi
    stub.error = UpstreamUnavailable(f"{failing} service is unavailable")

    response = _submit(client, auth, params='{"refno": "X1"}')

    assert response.status_code == 503
    body = response.json()
    assert (body["pipeline_last_stage"], body["message"], body["errors"]) == (
        failing,
        f"{failing} service is unavailable",
        "DOWNSTREAM_UNAVAILABLE",
    )
    assert (body["request_id"], body["params"]) == ("OCR_1", {"refno": "X1"})
