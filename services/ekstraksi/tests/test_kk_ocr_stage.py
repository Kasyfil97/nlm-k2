"""The OCR stage through its own endpoints: the §7.1 payload, idempotency, and the intake paths.

Written fresh rather than adapted from the previous pipeline's suite: `blocks[]` with an upright
`bbox` is gone, there is no synchronous endpoint left to test, and what matters here -- a `poly`
that is four points of floats, and an empty `texts` that is a *success* -- has no counterpart there.
"""

import json

from ocr_common.errors import UpstreamUnavailable
from ocr_common.kk import DOCUMENT_TYPE
from ocr_common.pipeline import InMemoryJobRepository
from ocr_common.testing import image_upload, wait_for_job

from app.dependencies import get_job_service, get_pipeline
from app.ml.mock import BLANK_TRIGGER, ERROR_TRIGGER
from app.ml.utils import POLY_POINTS
from tests.conftest import GUARDRAILS, JPEG

JOBS = "/v1/ekstraksi/jobs"


def submit(client, auth, request_id: str, filename: str = "kk.jpg", **extra):
    data = {"request_id": request_id, "document_type": DOCUMENT_TYPE, "guardrails": json.dumps(GUARDRAILS), **extra}
    files = image_upload(filename, JPEG) if "file_url" not in extra else None
    return client.post(JOBS, headers=auth, data=data, files=files)


def result_of(client, request_id: str, **kwargs) -> dict:
    """The finished job. `wait_for_job` polls: the stage answers 202 and works in the background."""
    return wait_for_job(client, f"{JOBS}/{request_id}", **kwargs)


# --- happy path: the §7.1 payload ----------------------------------------------------------


def test_a_submitted_card_is_accepted_and_read_in_the_background(client, auth):
    response = submit(client, auth, "REQ_happy")
    assert response.status_code == 202
    assert response.json()["data"] == {
        "request_id": "REQ_happy",
        "stage": "OCR",
        "status": "PROCESSING",
        "duplicate": False,
    }

    job = result_of(client, "REQ_happy")
    assert job["status"] == "DONE"
    assert job["stage"] == "OCR"
    assert job["error_message"] is None


def test_the_result_carries_texts_and_the_three_aggregates(client, auth):
    submit(client, auth, "REQ_shape")

    result = result_of(client, "REQ_shape")["result"]
    assert set(result) == {
        "engine",
        "model",
        "elapsed_ms",
        "text_regions_count",
        "avg_doc_score",
        "min_doc_score",
        "texts",
    }
    assert "blocks" not in result and "full_text" not in result, "the superseded shape must not come back"
    assert result["engine"] == "mock"
    assert result["text_regions_count"] == len(result["texts"]) > 0


def test_the_aggregates_are_computed_from_the_boxes_not_reported_by_the_model(client, auth):
    """§7.1 defines them as functions of `texts[].score`. Recomputing them here is what stops two
    definitions of `avg_doc_score` existing in one pipeline."""
    submit(client, auth, "REQ_aggregates")

    result = result_of(client, "REQ_aggregates")["result"]
    scores = [box["score"] for box in result["texts"]]
    assert result["min_doc_score"] == round(min(scores), 4)
    assert result["avg_doc_score"] == round(sum(scores) / len(scores), 4)


def test_every_poly_is_four_points_of_two_float_coordinates(client, auth):
    """The detector returns genuine quadrilaterals -- the spike measured a visible tilt on all 177
    boxes of one real card -- so an x1/y1/x2/y2 rectangle cannot represent them. Structuring reads
    columns from this geometry, so the shape is load-bearing, not decoration."""
    submit(client, auth, "REQ_poly")

    texts = result_of(client, "REQ_poly")["result"]["texts"]
    for box in texts:
        assert len(box["poly"]) == POLY_POINTS
        assert all(len(point) == 2 for point in box["poly"])
        assert all(isinstance(coordinate, float) for point in box["poly"] for coordinate in point)
    assert any(box["poly"][0][1] != box["poly"][1][1] for box in texts), "a tilted quad, not an upright box"


def test_every_score_is_a_probability(client, auth):
    submit(client, auth, "REQ_scores")

    for box in result_of(client, "REQ_scores")["result"]["texts"]:
        assert 0.0 <= box["score"] <= 1.0


def test_the_same_bytes_always_read_the_same(client, auth):
    submit(client, auth, "REQ_stable_a")
    submit(client, auth, "REQ_stable_b")

    first = result_of(client, "REQ_stable_a")["result"]["texts"]
    second = result_of(client, "REQ_stable_b")["result"]["texts"]
    assert [box["text"] for box in first] == [box["text"] for box in second]


# --- idempotency (§2.5, §6.2) --------------------------------------------------------------


def test_the_same_request_id_twice_is_one_job(client, auth):
    assert submit(client, auth, "REQ_dup").json()["data"]["duplicate"] is False
    result_of(client, "REQ_dup")

    again = submit(client, auth, "REQ_dup")
    assert again.status_code == 202
    data = again.json()["data"]
    assert data["duplicate"] is True
    assert data["status"] == "DONE", "the existing job's status, not PROCESSING"


# --- what a later run can and cannot rebuild -----------------------------------------------


def test_a_job_sent_as_file_url_keeps_the_url_so_it_can_be_run_again(client, auth):
    """§3.1: the URL is stored in `ocr_jobs.input`, which is what lets the reaper run an abandoned
    job again. Hence the advice that a presigned URL outlive `PIPELINE_JOB_LEASE_SECONDS`.

    The download itself then fails (nothing is serving that host), which is the right outcome and
    not what this test is about: the claim is that the URL survived the claim transaction.
    """
    url = "https://minio.example.internal/bucket/kk.jpg"
    response = submit(client, auth, "REQ_url", file_url=url)
    assert response.status_code == 202

    assert _stored_input("REQ_url")["file_url"] == url


def test_an_inline_upload_cannot_be_run_again_and_says_so(client, auth):
    """Tested as behaviour rather than left as a surprise: an inline upload is not stored anywhere,
    so a job the reaper reclaims has nothing to read again. The message asks for a resend and names
    `file_url` as the way to avoid it."""
    submit(client, auth, "REQ_inline")
    result_of(client, "REQ_inline")

    stale = _stored_input("REQ_inline")
    assert stale.get("file_url") is None

    client.portal.call(_resume_and_drain, get_job_service(), get_pipeline(), "REQ_inline", stale)

    job = result_of(client, "REQ_inline")
    assert job["status"] == "FAILED"
    assert "uploaded inline" in job["error_message"]
    assert "file_url" in job["error_message"]


def _stored_input(request_id: str) -> dict:
    """What `ocr_jobs.input` holds for this job. Reached through the in-memory repository's own
    store rather than an endpoint: nothing exposes `input`, and nothing should -- it carries the
    presigned URL."""
    from app.dependencies import get_pipeline

    repository = get_pipeline().repository
    assert isinstance(repository, InMemoryJobRepository)
    return repository._inputs[request_id] or {}


async def _resume_and_drain(service, pipeline, request_id: str, input: dict) -> None:
    await service.resume(request_id, input)
    await pipeline.runner.drain(5)


# --- the mock's levers (R20, and Unit 10 depends on both) ----------------------------------


def test_an_image_with_no_readable_text_is_a_done_job_not_a_failure(client, auth):
    """§6.3, and the trigger the first rule of §7.4 needs. This stage NEVER rejects a document: the
    empty read travels on so that structuring can reject it, which keeps every content-based
    rejection in one place with one channel."""
    submit(client, auth, "REQ_blank", filename=f"{BLANK_TRIGGER}-kk.jpg")

    job = result_of(client, "REQ_blank")
    assert job["status"] == "DONE"
    assert job["result"]["texts"] == []
    assert job["result"]["text_regions_count"] == 0
    assert job["result"]["avg_doc_score"] is None, "null, not 0: nothing scored badly, nothing scored"
    assert job["result"]["min_doc_score"] is None


def test_the_mock_can_be_made_to_fail_and_that_is_a_failed_job(client, auth):
    """The other half of §6.3: a model that breaks is `FAILED`, which the caller sees as a 422 --
    not a 4xx here, and not a rejection."""
    submit(client, auth, "REQ_boom", filename=f"{ERROR_TRIGGER}-kk.jpg")

    job = result_of(client, "REQ_boom")
    assert job["status"] == "FAILED"
    assert job["result"] is None
    assert job["error_message"]


def test_a_model_that_cannot_be_reached_is_a_failed_job_carrying_its_message(client, auth, use_engine):
    """§6.3: the model being unreachable is one of the three ways this stage fails, and the caller
    sees a 422 with this message rather than a generic one."""

    class Unreachable:
        name = "unreachable"

        def read(self, filename, content, content_type=None):
            raise UpstreamUnavailable("ekstraksi OCR model is unavailable")

    use_engine(Unreachable())
    submit(client, auth, "REQ_unreachable")

    job = result_of(client, "REQ_unreachable")
    assert job["status"] == "FAILED"
    assert job["error_message"] == "ekstraksi OCR model is unavailable"


def test_structuring_levers_in_the_file_name_reach_the_ocr_text(client, auth):
    """The structuring mock reads `MOCK:key=value` out of the OCR lines, so without this the §7.4
    paths could only be driven by calling structuring directly -- not from the front door, which is
    where Unit 10 drives them from."""
    submit(client, auth, "REQ_lever", filename="kk-MOCK:members=0.jpg")

    texts = [box["text"] for box in result_of(client, "REQ_lever")["result"]["texts"]]
    assert "MOCK:members=0" in texts, "the extension must not end up inside the lever's value"


def test_a_file_name_carrying_a_delay_holds_the_job_open(client, auth):
    """R25 walks the orchestrator's 202 path with this: the job outlives `PIPELINE_WAIT_SECONDS`
    without needing a slow model. One second here -- the point is that the hook fires, and
    `ocr_common.simulation` caps it at 120 s in any case."""
    submit(client, auth, "REQ_delay", filename="delay1s-kk.jpg")

    assert client.get(f"{JOBS}/REQ_delay", headers=auth).json()["data"]["status"] == "PROCESSING"
    assert result_of(client, "REQ_delay", timeout=10)["status"] == "DONE"


# --- intake errors --------------------------------------------------------------------------


def test_neither_file_nor_file_url_is_a_400(client, auth):
    response = client.post(JOBS, headers=auth, data={"request_id": "REQ_nofile"})
    assert response.status_code == 400
    assert "exactly one" in response.json()["message"]


def test_both_file_and_file_url_is_a_400(client, auth):
    response = client.post(
        JOBS,
        headers=auth,
        data={"request_id": "REQ_both", "file_url": "https://minio.example.internal/kk.jpg"},
        files=image_upload("kk.jpg", JPEG),
    )
    assert response.status_code == 400


def test_guardrails_that_is_not_a_json_object_is_a_400(client, auth):
    """§6.1. It is a form field carrying JSON, so the shape cannot be checked by the schema."""
    response = submit(client, auth, "REQ_badguard", guardrails="[1, 2, 3]")
    assert response.status_code == 400
    assert "JSON object" in response.json()["message"]


def test_a_missing_request_id_is_a_422(client, auth):
    assert client.post(JOBS, headers=auth, files=image_upload("kk.jpg", JPEG)).status_code == 422


def test_an_unknown_request_id_is_a_404(client, auth):
    assert client.get(f"{JOBS}/REQ_never_seen", headers=auth).status_code == 404


def test_every_endpoint_needs_the_api_key(client):
    assert client.post(JOBS, data={"request_id": "REQ_noauth"}, files=image_upload("kk.jpg", JPEG)).status_code == 401
    assert client.get(f"{JOBS}/REQ_noauth").status_code == 401
    assert client.get("/v1/ekstraksi/outbox").status_code == 401


def test_there_is_no_synchronous_ekstraksi_endpoint(client, auth):
    """§11 lists a synchronous debug endpoint for structuring and scoring, and for neither the
    orchestrator nor this stage. The route that used to exist is gone, not merely undocumented."""
    assert client.post("/v1/ekstraksi/extract", headers=auth, files=image_upload("kk.jpg", JPEG)).status_code == 404
    assert "/v1/ekstraksi/extract" not in client.get("/openapi.json").json()["paths"]
