"""`pipeline_name_sequence` at structuring: hand on to scoring, or end the request here.

Ported from nilam's `test_jobs.py`. A pipeline with recording doubles in place of the orchestrator and
scoring, because what is asserted is what those two receive.
"""

import pytest

from ocr_common.pipeline import STAGE_STRUCTURING, InMemoryJobRepository, StagePipeline
from ocr_common.testing import RecordingCallback, RecordingNextStage, make_client, wait_for_job

from app.dependencies import get_job_service, get_structuring_service
from app.main import app
from app.services.job_service import StructuringJobService
from tests.test_kk_structuring import JOBS, job_body


@pytest.fixture
def harness():
    callback, next_stage = RecordingCallback(), RecordingNextStage()
    pipeline = StagePipeline(
        stage=STAGE_STRUCTURING, repository=InMemoryJobRepository(), callback=callback, next_stage_client=next_stage
    )
    service = StructuringJobService(pipeline, get_structuring_service())
    app.dependency_overrides[get_job_service] = lambda: service
    with make_client(app) as client:
        yield client, callback, next_stage
    app.dependency_overrides.pop(get_job_service, None)


def _body(request_id, sequence, *texts):
    return {**job_body(request_id, *(texts or ("KARTU KELUARGA",))), "pipeline_name_sequence": sequence}


def test_a_sequence_ending_here_stops_with_the_structuring_result_as_the_answer(harness, auth):
    client, callback, next_stage = harness
    sequence = ["guardrails", "extraction", "structuring"]

    assert client.post(JOBS, headers=auth, json=_body("REQ_seq_end", sequence)).status_code == 202

    job = wait_for_job(client, f"{JOBS}/REQ_seq_end")
    assert (job["status"], job["pipeline_name_sequence"]) == ("DONE", sequence)
    assert next_stage.payloads == [], "scoring is not part of this request"
    [done] = callback.calls
    assert (done["stage"], done["status"], done["final"], done["result"]) == (
        "STRUCTURING",
        "DONE",
        True,
        job["result"],
    )


def test_the_validity_gate_still_rejects_when_structuring_is_last(harness, auth):
    """Ending the chain here does not bypass §7.4: a document with no text is still a rejection."""
    client, callback, next_stage = harness

    body = {**job_body("REQ_seq_reject"), "pipeline_name_sequence": ["extraction", "structuring"]}
    client.post(JOBS, headers=auth, json=body)

    wait_for_job(client, f"{JOBS}/REQ_seq_reject")
    [failed] = callback.calls
    assert (failed["status"], failed["error_code"]) == ("FAILED", "DOWNSTREAM_VALIDATION_ERROR")
    assert "final" not in failed
    assert next_stage.payloads == []


def test_the_full_sequence_is_handed_on_to_scoring(harness, auth):
    client, _, next_stage = harness
    sequence = ["extraction", "structuring", "scoring"]

    client.post(JOBS, headers=auth, json=_body("REQ_seq_on", sequence))

    wait_for_job(client, f"{JOBS}/REQ_seq_on")
    [payload] = next_stage.payloads
    assert payload["pipeline_name_sequence"] == sequence


@pytest.mark.parametrize(
    "sequence", [["guardrails", "extraction"], ["extraction", "scoring"], ["scoring"], ["extraction", "structuring"]]
)
def test_a_sequence_without_this_stage_or_out_of_order_is_422(harness, auth, sequence):
    client, _, _ = harness

    assert client.post(JOBS, headers=auth, json=_body("REQ_seq_bad", sequence)).status_code == 422


# --- column_confidence_threshold (ported from nilam 708d53f) --------------------------------------


def test_column_confidence_threshold_is_kept_with_the_job_and_handed_on_to_scoring(harness, auth):
    client, _, next_stage = harness
    body = {**_body("REQ_cols", None), "column_confidence_threshold": {"no_kk": 0.9, "nik": 0.8}}

    assert client.post(JOBS, headers=auth, json=body).status_code == 202

    job = wait_for_job(client, f"{JOBS}/REQ_cols")
    assert job["column_confidence_threshold"] == {"no_kk": 0.9, "nik": 0.8}
    [payload] = next_stage.payloads
    assert payload["column_confidence_threshold"] == {"no_kk": 0.9, "nik": 0.8}


def test_without_column_confidence_threshold_nothing_is_handed_on(harness, auth):
    client, _, next_stage = harness
    client.post(JOBS, headers=auth, json=_body("REQ_nocols", None))

    wait_for_job(client, f"{JOBS}/REQ_nocols")
    [payload] = next_stage.payloads
    assert "column_confidence_threshold" not in payload


def test_an_invalid_column_confidence_threshold_is_422(harness, auth):
    client, _, _ = harness
    body = {**_body("REQ_badcols", None), "column_confidence_threshold": {"nomor_kk": 0.9}}

    assert client.post(JOBS, headers=auth, json=body).status_code == 422
