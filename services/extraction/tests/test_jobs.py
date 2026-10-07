"""How the stage is wired: what the callback carries, what the hand-off carries, and what a later
run of the same job can rebuild.

`test_kk_ocr_stage.py` drives the endpoints against the service's own pipeline and is about the
§7.1 payload and the intake rules. This file builds a pipeline with recording doubles in place of
the orchestrator and the next stage, because what it asserts is what those two *receive* -- which
the endpoints cannot show.
"""

import json

import pytest

from ocr_common.errors import ServiceError
from ocr_common.kk import DOCUMENT_TYPE
from ocr_common.pipeline import STAGE_OCR, InMemoryJobRepository, StagePipeline
from ocr_common.testing import RecordingCallback, RecordingNextStage, image_upload, make_client, wait_for_job

from app.dependencies import get_extraction_service, get_job_service
from app.main import app
from app.services.job_service import ExtractionJobService
from tests.conftest import GUARDRAILS

FILE_URL = "https://minio.example.internal/bucket/kk.jpg?sig=x"


def _service(**kwargs):
    callback, next_stage = RecordingCallback(), RecordingNextStage()
    pipeline = StagePipeline(
        stage=STAGE_OCR, repository=InMemoryJobRepository(), callback=callback, next_stage_client=next_stage
    )
    service = ExtractionJobService(pipeline, get_extraction_service(), 5 * 1024 * 1024, simulate_delay=True, **kwargs)
    return service, pipeline, callback, next_stage


@pytest.fixture
def harness():
    service, _, callback, next_stage = _service()
    app.dependency_overrides[get_job_service] = lambda: service
    with make_client(app) as client:
        yield client, callback, next_stage
    app.dependency_overrides.pop(get_job_service, None)


def _submit(client, auth, request_id, filename="kk.jpg", data_extra=None, **kwargs):
    data = {"request_id": request_id, "document_type": DOCUMENT_TYPE, "guardrails": json.dumps(GUARDRAILS)}
    data.update(data_extra or {})
    return client.post("/v1/extraction/jobs", headers=auth, data=data, files=image_upload(filename), **kwargs)


# --- what the orchestrator and structuring receive -----------------------------------------


def test_a_finished_job_sends_one_callback_and_hands_the_result_to_structuring(harness, auth):
    client, callback, next_stage = harness

    assert _submit(client, auth, "REQ_1").status_code == 202
    job = wait_for_job(client, "/v1/extraction/jobs/REQ_1")
    assert job["status"] == "DONE"

    # The callback carries no result: §9 says the stage result is read from GET .../jobs/{id}, and
    # for this stage that is 180 boxes nobody wants in a webhook body.
    assert [(c["stage"], c["status"], c["result"]) for c in callback.calls] == [("OCR", "DONE", None)]
    assert next_stage.payloads == [
        {
            "request_id": "REQ_1",
            "document_type": DOCUMENT_TYPE,
            "guardrails": GUARDRAILS,
            "ocr": job["result"],
        }
    ]


def test_the_guardrails_report_is_forwarded_unchanged(harness, auth):
    """§6.1 and §7.1: this stage does not read the report, it only passes it on -- scoring uses
    `document.probability_bad` as a feature and the final result returns it as it arrived."""
    client, _, next_stage = harness
    _submit(client, auth, "REQ_guard")
    wait_for_job(client, "/v1/extraction/jobs/REQ_guard")

    assert next_stage.payloads[0]["guardrails"] == GUARDRAILS


def test_a_job_without_a_guardrails_report_hands_off_a_null(harness, auth):
    """A `pipeline_name_sequence` without `guardrails` at the orchestrator leaves the field out entirely (§6.1)."""
    client, _, next_stage = harness
    client.post("/v1/extraction/jobs", headers=auth, data={"request_id": "REQ_noguard"}, files=image_upload("kk.jpg"))
    wait_for_job(client, "/v1/extraction/jobs/REQ_noguard")

    assert next_stage.payloads[0]["guardrails"] is None


def test_handoff_by_reference_leaves_the_card_out_of_the_payload(auth):
    """Recommended for KK: one card is ~180 boxes, and the hand-off body is also what an outbox row
    (and a dead letter, for 24 hours) would hold."""
    service, _, _, next_stage = _service(handoff_by_reference=True)
    app.dependency_overrides[get_job_service] = lambda: service
    try:
        with make_client(app) as client:
            _submit(client, auth, "REQ_ref")
            job = wait_for_job(client, "/v1/extraction/jobs/REQ_ref")
    finally:
        app.dependency_overrides.pop(get_job_service, None)

    assert job["status"] == "DONE"
    assert job["result"]["texts"], "stored and readable; just not shipped"
    assert next_stage.payloads == [{"request_id": "REQ_ref", "document_type": DOCUMENT_TYPE, "guardrails": GUARDRAILS}]


def test_nothing_is_handed_on_when_the_job_failed(harness, auth):
    """A `text/plain` upload never reaches the model; it is a FAILED job, not a 4xx at intake
    (§6.2: content validation happens in the background)."""
    client, callback, next_stage = harness
    response = client.post(
        "/v1/extraction/jobs",
        headers=auth,
        data={"request_id": "REQ_badtype"},
        files={"file": ("kk.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 202

    job = wait_for_job(client, "/v1/extraction/jobs/REQ_badtype")
    assert job["status"] == "FAILED"
    assert [(c["stage"], c["status"]) for c in callback.calls] == [("OCR", "FAILED")]
    assert callback.calls[0]["error_message"] == job["error_message"]
    assert next_stage.payloads == []


def test_a_handoff_that_cannot_be_delivered_is_reported_as_structuring_failed(harness, auth):
    """§2.6: the stage that never received the job cannot report for itself, so this one does it
    under the *next* stage's name -- the job here stays DONE, because the OCR did succeed."""
    client, callback, next_stage = harness
    next_stage.error = ServiceError(503, "structuring service is unavailable")

    _submit(client, auth, "REQ_handoff")
    assert wait_for_job(client, "/v1/extraction/jobs/REQ_handoff")["status"] == "DONE"
    for _ in range(100):
        if len(callback.calls) == 2:
            break
        wait_for_job(client, "/v1/extraction/jobs/REQ_handoff")

    assert [(c["stage"], c["status"]) for c in callback.calls] == [("OCR", "DONE"), ("STRUCTURING", "FAILED")]


def test_a_document_sent_as_file_url_is_downloaded_in_the_background(harness, auth, monkeypatch):
    client, _, next_stage = harness

    async def fake_fetch(url, *, limit, timeout=10.0, policy):
        assert url == FILE_URL
        assert policy is not None, "the download goes through the shared URL policy, not a bare GET"
        return b"\xff\xd8fake-jpeg-bytes", "kk.jpg", "image/jpeg"

    monkeypatch.setattr("app.services.job_service.fetch", fake_fetch)
    response = client.post("/v1/extraction/jobs", headers=auth, data={"request_id": "REQ_fetch", "file_url": FILE_URL})

    assert response.status_code == 202
    assert wait_for_job(client, "/v1/extraction/jobs/REQ_fetch")["status"] == "DONE"


def test_a_url_the_policy_refuses_fails_the_job_rather_than_the_request(harness, auth, monkeypatch):
    """§6.2 again: the URL is only fetched in the background, so a host that is not on the list
    surfaces as a FAILED job with the policy's own message -- not as a 400 at submit time."""
    from ocr_common.clients.fetch_url import FetchUrlError

    client, _, next_stage = harness

    async def refuse(url, *, limit, timeout=10.0, policy):
        raise FetchUrlError("host not allowed: evil.example.com")

    monkeypatch.setattr("app.services.job_service.fetch", refuse)
    assert (
        client.post(
            "/v1/extraction/jobs",
            headers=auth,
            data={"request_id": "REQ_badhost", "file_url": "https://evil.example.com/kk.jpg"},
        ).status_code
        == 202
    )

    job = wait_for_job(client, "/v1/extraction/jobs/REQ_badhost")
    assert job["status"] == "FAILED"
    assert "host not allowed" in job["error_message"]
    assert next_stage.payloads == []


# --- running a job again (the reaper's path) ------------------------------------------------


async def test_a_stale_job_sent_as_file_url_is_fetched_and_run_again(monkeypatch):
    """Why `file_url` is the recommended intake (§3.1): everything the job needs is in
    `ocr_extraction_jobs.input`, so the pod that picks it up next can rebuild the work."""
    service, pipeline, callback, next_stage = _service()
    stored = {"document_type": DOCUMENT_TYPE, "guardrails": GUARDRAILS, "file_url": FILE_URL}
    await pipeline.repository.claim("REQ_stale", input=stored)

    async def fake_fetch(url, *, limit, timeout=10.0, policy):
        assert url == FILE_URL
        return b"\xff\xd8fake-jpeg-bytes", "kk.jpg", "image/jpeg"

    monkeypatch.setattr("app.services.job_service.fetch", fake_fetch)
    await service.resume("REQ_stale", stored)
    await pipeline.runner.drain(5)

    assert (await pipeline.get("REQ_stale"))["status"] == "DONE"
    assert next_stage.payloads[0]["guardrails"] == GUARDRAILS
    assert [(c["stage"], c["status"]) for c in callback.calls] == [("OCR", "DONE")]


async def test_a_stale_job_of_an_inline_upload_fails_with_a_reason_that_says_what_to_do():
    """Tested as behaviour rather than left as a surprise. An inline upload is not stored, so there
    is nothing to read again; the message asks for a resend and names `file_url` as the way to
    avoid the situation entirely."""
    service, pipeline, callback, next_stage = _service()
    stored = {"document_type": DOCUMENT_TYPE, "guardrails": None, "file_url": None}
    await pipeline.repository.claim("REQ_gone", input=stored)

    await service.resume("REQ_gone", stored)
    await pipeline.runner.drain(5)

    job = await pipeline.get("REQ_gone")
    assert job["status"] == "FAILED"
    assert "uploaded inline" in job["error_message"] and "file_url" in job["error_message"]
    assert [(c["stage"], c["status"]) for c in callback.calls] == [("OCR", "FAILED")]
    assert next_stage.payloads == []


# --- pipeline_name_sequence ---------------------------------------------------------------------


def _submit_with_sequence(client, auth, request_id, sequence):
    data = {
        "request_id": request_id,
        "document_type": DOCUMENT_TYPE,
        "guardrails": json.dumps(GUARDRAILS),
        "pipeline_name_sequence": json.dumps(sequence),
    }
    return client.post("/v1/extraction/jobs", headers=auth, data=data, files=image_upload("kk.jpg"))


def test_a_sequence_ending_here_stops_with_the_ocr_result_as_the_answer(harness, auth):
    client, callback, next_stage = harness

    assert _submit_with_sequence(client, auth, "REQ_seq_end", ["guardrails", "extraction"]).status_code == 202

    job = wait_for_job(client, "/v1/extraction/jobs/REQ_seq_end")
    assert job["status"] == "DONE"
    assert job["pipeline_name_sequence"] == ["guardrails", "extraction"]
    assert next_stage.payloads == [], "nothing is handed on after the last service"
    [done] = callback.calls
    assert (done["stage"], done["status"], done["final"], done["result"]) == ("OCR", "DONE", True, job["result"])


def test_a_longer_sequence_is_handed_on_with_the_job(harness, auth):
    client, callback, next_stage = harness
    sequence = ["extraction", "structuring"]

    assert _submit_with_sequence(client, auth, "REQ_seq_on", sequence).status_code == 202

    job = wait_for_job(client, "/v1/extraction/jobs/REQ_seq_on")
    [payload] = next_stage.payloads
    assert payload["pipeline_name_sequence"] == sequence
    assert [(c["stage"], c["status"], c.get("final")) for c in callback.calls] == [("OCR", "DONE", None)]
    assert job["pipeline_name_sequence"] == sequence


def test_without_a_sequence_the_full_pipeline_runs_and_none_is_stored(harness, auth):
    client, _, next_stage = harness

    assert _submit(client, auth, "REQ_seq_none").status_code == 202

    job = wait_for_job(client, "/v1/extraction/jobs/REQ_seq_none")
    assert job["pipeline_name_sequence"] is None
    [payload] = next_stage.payloads
    assert "pipeline_name_sequence" not in payload


@pytest.mark.parametrize(
    "raw",
    [
        '["guardrails"]',
        '["structuring", "scoring"]',
        '["extraction", "scoring"]',
        '["ocr"]',
        '"ocr"',
        "not json",
    ],
)
def test_an_invalid_sequence_is_400(harness, auth, raw):
    client, _, _ = harness
    data = {"request_id": "REQ_seq_bad", "document_type": DOCUMENT_TYPE, "pipeline_name_sequence": raw}

    response = client.post("/v1/extraction/jobs", headers=auth, data=data, files=image_upload("kk.jpg"))

    assert response.status_code == 400
    assert "pipeline_name_sequence" in response.json()["message"]


async def test_a_stale_job_is_run_again_with_the_sequence_it_was_submitted_with(monkeypatch):
    service, pipeline, callback, next_stage = _service()
    stored = {
        "document_type": DOCUMENT_TYPE,
        "guardrails": GUARDRAILS,
        "file_url": FILE_URL,
        "pipeline_name_sequence": ["extraction"],
    }
    await pipeline.repository.claim("REQ_stale_seq", input=stored)

    async def fake_fetch(url, *, limit, timeout=10.0, policy):
        return b"\xff\xd8fake-jpeg-bytes", "kk.jpg", "image/jpeg"

    monkeypatch.setattr("app.services.job_service.fetch", fake_fetch)
    await service.resume("REQ_stale_seq", stored)
    await pipeline.runner.drain(5)

    assert next_stage.payloads == []
    assert [(c["stage"], c.get("final")) for c in callback.calls] == [("OCR", True)]


# --- column_confidence_threshold (ported from nilam 708d53f) --------------------------------------


def test_column_confidence_threshold_is_kept_with_the_job_and_handed_on(harness, auth):
    client, _, next_stage = harness
    columns = {"no_kk": 0.9, "nik": 0.8}

    response = _submit(client, auth, "REQ_cols", data_extra={"column_confidence_threshold": json.dumps(columns)})
    assert response.status_code == 202

    job = wait_for_job(client, "/v1/extraction/jobs/REQ_cols")
    assert job["column_confidence_threshold"] == columns
    [payload] = next_stage.payloads
    assert payload["column_confidence_threshold"] == columns


def test_without_column_confidence_threshold_nothing_is_handed_on(harness, auth):
    client, _, next_stage = harness
    _submit(client, auth, "REQ_nocols")

    wait_for_job(client, "/v1/extraction/jobs/REQ_nocols")
    [payload] = next_stage.payloads
    assert "column_confidence_threshold" not in payload


@pytest.mark.parametrize("raw", ["{not json", '{"nomor_kk": 0.9}', '{"nik": 2}'])
def test_an_invalid_column_confidence_threshold_is_400(harness, auth, raw):
    client, _, _ = harness

    response = _submit(client, auth, "REQ_badcols", data_extra={"column_confidence_threshold": raw})

    assert response.status_code == 400
    assert "column_confidence_threshold" in response.json()["message"]


async def test_a_stale_job_hands_on_its_stored_column_thresholds(monkeypatch):
    service, pipeline, _, next_stage = _service()
    stored = {
        "document_type": DOCUMENT_TYPE,
        "guardrails": GUARDRAILS,
        "file_url": FILE_URL,
        "column_confidence_threshold": {"no_kk": 0.9},
    }
    await pipeline.repository.claim("REQ_stale_cols", input=stored)

    async def fake_fetch(url, *, limit, timeout=10.0, policy):
        return b"\xff\xd8fake-jpeg-bytes", "kk.jpg", "image/jpeg"

    monkeypatch.setattr("app.services.job_service.fetch", fake_fetch)
    await service.resume("REQ_stale_cols", stored)
    await pipeline.runner.drain(5)

    [payload] = next_stage.payloads
    assert payload["column_confidence_threshold"] == {"no_kk": 0.9}
