"""`pipeline_name_sequence` at scoring: always the last service, so it ends every request it runs for.

Ported from nilam's `test_jobs.py`.
"""

import pytest

from ocr_common.pipeline import STAGE_SCORING, InMemoryJobRepository, StagePipeline
from ocr_common.testing import RecordingCallback, make_client, wait_for_job

from app.dependencies import get_confidence_service, get_job_service
from app.main import app
from app.services.job_service import ScoringJobService
from tests.test_kk_scoring import JOBS, job_body


@pytest.fixture
def harness():
    callback = RecordingCallback()
    pipeline = StagePipeline(stage=STAGE_SCORING, repository=InMemoryJobRepository(), callback=callback)
    service = ScoringJobService(pipeline, get_confidence_service())
    app.dependency_overrides[get_job_service] = lambda: service
    with make_client(app) as client:
        yield client, callback
    app.dependency_overrides.pop(get_job_service, None)


def test_scoring_ends_every_request_it_runs_for(harness, auth):
    client, callback = harness
    sequence = ["extraction", "structuring", "scoring"]

    client.post(JOBS, headers=auth, json={**job_body("REQ_seq"), "pipeline_name_sequence": sequence})

    job = wait_for_job(client, f"{JOBS}/REQ_seq")
    assert (job["status"], job["pipeline_name_sequence"]) == ("DONE", sequence)
    [done] = callback.calls
    assert (done["stage"], done["status"], done["final"]) == ("SCORING", "DONE", True)


@pytest.mark.parametrize("sequence", [["guardrails", "extraction", "structuring"], ["extraction", "scoring"]])
def test_a_sequence_without_scoring_or_out_of_order_is_422(harness, auth, sequence):
    client, _ = harness

    response = client.post(JOBS, headers=auth, json={**job_body("REQ_seq_bad"), "pipeline_name_sequence": sequence})

    assert response.status_code == 422


# --- column_confidence_threshold (ported from nilam 1f331b6 / bb63efe) --------------------------


def test_the_0_1_decision_is_stored_with_the_result_with_its_threshold(harness, auth):
    """`decisions` is what the outcome row and the orchestrator's answer are projected from, so the
    request's thresholds are applied once, here, and kept with the scores."""
    client, _ = harness
    body = {**job_body("REQ_cols"), "column_confidence_threshold": {"no_kk": 0.0, "nik": 1.0}}

    assert client.post(JOBS, headers=auth, json=body).status_code == 202

    job = wait_for_job(client, f"{JOBS}/REQ_cols")
    decisions = job["result"]["decisions"]
    assert job["column_confidence_threshold"] == {"no_kk": 0.0, "nik": 1.0}
    assert decisions["no_kk"] == {"value": "9924187486671285", "confidence": 1, "threshold": 0.0}
    [orang] = decisions["anggota_keluarga"]
    assert (orang["nik"]["confidence"], orang["nik"]["threshold"]) == (0, 1.0), "the request's 1.0, not the default"
    # a field the request leaves out: the mock ships no thresholds, so FIELD_CONFIDENCE_THRESHOLD decides
    assert orang["ayah"]["threshold"] == 0.5


def test_without_column_confidence_threshold_every_field_uses_the_default(harness, auth):
    client, _ = harness
    client.post(JOBS, headers=auth, json=job_body("REQ_nocols"))

    job = wait_for_job(client, f"{JOBS}/REQ_nocols")
    assert job["column_confidence_threshold"] is None
    assert job["result"]["decisions"]["no_kk"]["threshold"] == 0.5


@pytest.mark.parametrize("columns", [{"nomor_kk": 0.9}, {"nik": 1.5}, {"nik": "tinggi"}, [0.9]])
def test_an_invalid_column_confidence_threshold_is_422(harness, auth, columns):
    client, _ = harness

    response = client.post(JOBS, headers=auth, json={**job_body("REQ_badcols"), "column_confidence_threshold": columns})

    assert response.status_code == 422
