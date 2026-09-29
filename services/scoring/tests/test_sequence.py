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
    sequence = ["ekstraksi", "structuring", "scoring"]

    client.post(JOBS, headers=auth, json={**job_body("REQ_seq"), "pipeline_name_sequence": sequence})

    job = wait_for_job(client, f"{JOBS}/REQ_seq")
    assert (job["status"], job["pipeline_name_sequence"]) == ("DONE", sequence)
    [done] = callback.calls
    assert (done["stage"], done["status"], done["final"]) == ("SCORING", "DONE", True)


@pytest.mark.parametrize("sequence", [["guardrails", "ekstraksi", "structuring"], ["ekstraksi", "scoring"]])
def test_a_sequence_without_scoring_or_out_of_order_is_422(harness, auth, sequence):
    client, _ = harness

    response = client.post(JOBS, headers=auth, json={**job_body("REQ_seq_bad"), "pipeline_name_sequence": sequence})

    assert response.status_code == 422
