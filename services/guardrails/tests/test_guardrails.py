"""§5.2: one verdict per document, always 200, the verdict in `data.passed`."""

import pytest

from ocr_common.pipeline.schemas import GuardrailsResult
from ocr_common.testing import image_upload

from app.config import Settings
from app.ml.mock import PROBABILITY_BAD_ACCEPT, PROBABILITY_BAD_REJECT, REJECT_TRIGGERS
from app.services.guardrails_service import (
    REASON_REJECT,
    REASON_UNASSESSABLE,
    GuardrailsService,
)
from tests.conftest import JPEG, StubModel, unassessable_model


def _settings(**overrides) -> Settings:
    return Settings(api_key="x", _env_file=None, **overrides)


async def _check(model, *, filename="kk.jpg", override=None, **overrides) -> dict:
    service = GuardrailsService(model, _settings(**overrides))
    return await service.check(filename, "image/jpeg", JPEG, override=override)


# --- the verdicts --------------------------------------------------------------------------


async def test_a_good_image_is_accepted_and_confidence_is_the_complement():
    report = await _check(StubModel(0.0287))
    assert report["passed"] is True
    assert report["reason"] is None
    assert report["document"] == {
        "verdict": "accepted",
        "confidence": 0.9713,
        "probability_bad": 0.0287,
        "threshold_used": 0.5,
    }
    # The contract states the relation, not the rounding; both must hold.
    assert report["document"]["confidence"] == pytest.approx(1 - report["document"]["probability_bad"])


async def test_a_bad_image_is_rejected_with_an_indonesian_reason():
    report = await _check(StubModel(0.8821))
    assert report["passed"] is False
    assert report["document"] == {
        "verdict": "reject",
        "confidence": 0.8821,
        "probability_bad": 0.8821,
        "threshold_used": 0.5,
    }
    # `reason` is relayed as the orchestrator's `message` (§3.5), so it is the end user's sentence.
    assert report["reason"] == REASON_REJECT == "Kualitas gambar terlalu rendah, mohon unggah foto yang lebih jelas"


async def test_the_threshold_is_inclusive_at_the_boundary():
    """§5.2 defines `is_bad` as `probability_bad >= threshold`, so exactly at it is a rejection."""
    assert (await _check(StubModel(0.5)))["document"]["verdict"] == "reject"
    assert (await _check(StubModel(0.4999)))["document"]["verdict"] == "accepted"


async def test_an_image_that_cannot_be_judged_is_a_verdict_and_not_an_error():
    """R14a. Distinct from `reject`, and with its own sentence: the quality wording would be the
    wrong diagnosis for a file that never decoded."""
    report = await _check(unassessable_model())
    assert report["passed"] is False
    assert report["reason"] == REASON_UNASSESSABLE
    assert report["reason"] != REASON_REJECT
    assert report["document"]["verdict"] == "unassessable"
    assert report["document"]["probability_bad"] is None
    assert report["document"]["confidence"] is None
    assert report["document"]["threshold_used"] == 0.5


async def test_the_report_fits_the_frozen_pipeline_shape():
    """The same block travels to ekstraksi, structuring and scoring, so it has to parse as the
    frozen `GuardrailsResult` -- not merely look like it."""
    for model in (StubModel(0.0287), StubModel(0.8821), unassessable_model()):
        report = await _check(model)
        parsed = GuardrailsResult.model_validate(report)
        assert parsed.model_dump() == report


# --- the threshold chain (R15) ---------------------------------------------------------------


async def test_the_per_request_threshold_beats_the_environment():
    model = StubModel(0.30)
    lenient = await _check(model, guardrails_threshold=0.9)
    assert (lenient["document"]["verdict"], lenient["document"]["threshold_used"]) == ("accepted", 0.9)

    strict = await _check(model, override=0.25, guardrails_threshold=0.9)
    assert strict["document"]["verdict"] == "reject"
    assert strict["document"]["threshold_used"] == 0.25, "the report states the threshold that decided"


async def test_the_environment_beats_the_value_stored_with_the_weights():
    model = StubModel(0.30, reject_threshold=0.2)
    assert (await _check(model))["document"]["threshold_used"] == 0.2
    assert (await _check(model, guardrails_threshold=0.9))["document"]["threshold_used"] == 0.9


async def test_the_floor_is_half_when_nothing_else_says_otherwise():
    assert (await _check(StubModel(0.30)))["document"]["threshold_used"] == 0.5


# --- the mock backend (R25 depends on it) ------------------------------------------------------


@pytest.mark.parametrize("trigger", REJECT_TRIGGERS)
def test_the_mock_rejects_its_trigger_names(client, auth, trigger):
    response = client.post(
        "/v1/guardrails/check",
        data={"request_id": "OCR_3"},
        files=image_upload(f"{trigger}.jpg", JPEG),
        headers=auth,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["passed"] is False
    assert data["document"]["verdict"] == "reject"
    assert data["document"]["probability_bad"] == PROBABILITY_BAD_REJECT
    assert data["reason"] == REASON_REJECT


def test_the_mock_is_deterministic_and_accepts_everything_else(client, auth):
    for _ in range(3):
        response = client.post(
            "/v1/guardrails/check", data={"request_id": "OCR_4"}, files=image_upload("kk.jpg", JPEG), headers=auth
        )
        assert response.json()["data"]["document"]["probability_bad"] == PROBABILITY_BAD_ACCEPT


def test_the_mock_matches_the_trigger_anywhere_in_the_name(client, auth):
    """The smoke run and the orchestrator's tests name files after the outcome they want, not with
    an exact file name."""
    response = client.post(
        "/v1/guardrails/check",
        data={"request_id": "OCR_5"},
        files=image_upload("scan-notkk-002.jpg", JPEG),
        headers=auth,
    )
    assert response.json()["data"]["passed"] is False


def test_the_mock_occupies_no_rung_of_the_threshold_chain():
    """It has no checkpoint, so it must not look like one: a 0.5 here would be indistinguishable
    from the 0.5 floor and would hide a chain that stopped working."""
    from app.ml.mock import MockQualityModel

    assert MockQualityModel().reject_threshold is None


# --- the HTTP surface ---------------------------------------------------------------------------


def test_health_lists_backend(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["backends"] == {"guardrails": "mock"}


def test_ready_is_always_200(client):
    """This service has no database and nothing to warm, so readiness has nothing to check."""
    assert client.get("/ready").status_code == 200


def test_check_returns_the_report_in_the_envelope(client, auth):
    response = client.post(
        "/v1/guardrails/check", data={"request_id": "OCR_1"}, files=image_upload("kk.jpg", JPEG), headers=auth
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["message"], body["request_id"]) == ("OK", "OCR_1")
    assert body["data"] == {
        "passed": True,
        "reason": None,
        "document": {
            "verdict": "accepted",
            "confidence": round(1 - PROBABILITY_BAD_ACCEPT, 4),
            "probability_bad": PROBABILITY_BAD_ACCEPT,
            "threshold_used": 0.5,
        },
    }


def test_an_undecodable_image_answers_200_and_not_400(client, auth, use_model):
    """The behaviour this unit changed: the inherited service raised a 400 here; §5.2 requires 200."""
    use_model(unassessable_model())
    response = client.post(
        "/v1/guardrails/check", data={"request_id": "OCR_6"}, files=image_upload("x.jpg", b"garbage"), headers=auth
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["passed"], data["document"]["verdict"]) == (False, "unassessable")
    assert data["document"]["probability_bad"] is None


def test_a_per_request_threshold_travels_through_the_endpoint(client, auth, use_model):
    use_model(StubModel(0.30))
    response = client.post(
        "/v1/guardrails/check",
        data={"request_id": "OCR_7", "threshold": "0.25"},
        files=image_upload("kk.jpg", JPEG),
        headers=auth,
    )
    data = response.json()["data"]
    assert (data["document"]["verdict"], data["document"]["threshold_used"]) == ("reject", 0.25)


@pytest.mark.parametrize("threshold", ["0", "1", "1.5", "-0.2"])
def test_a_threshold_outside_the_open_unit_interval_is_400(client, auth, threshold):
    response = client.post(
        "/v1/guardrails/check",
        data={"request_id": "OCR_8", "threshold": threshold},
        files=image_upload("kk.jpg", JPEG),
        headers=auth,
    )
    assert response.status_code == 400
    assert "between 0 and 1" in response.json()["message"]


def test_a_threshold_that_is_not_a_number_is_422(client, auth):
    response = client.post(
        "/v1/guardrails/check",
        data={"request_id": "OCR_9", "threshold": "strict"},
        files=image_upload("kk.jpg", JPEG),
        headers=auth,
    )
    assert response.status_code == 422
    assert response.json()["errors"] == "VALIDATION_ERROR"


def test_missing_api_key_returns_401_envelope(client):
    response = client.post("/v1/guardrails/check", data={"request_id": "OCR_10"}, files=image_upload())
    assert response.status_code == 401
    assert response.json()["errors"] == "Invalid or missing API key"


def test_the_entry_point_is_not_here(client, auth):
    """`POST /v1/extract-ocr` belongs to the orchestrator; guardrails only judges."""
    response = client.post(
        "/v1/extract-ocr", data={"request_id": "OCR_11"}, files=image_upload("kk.jpg", JPEG), headers=auth
    )
    assert response.status_code == 404


def test_sending_neither_file_nor_file_url_is_400(client, auth):
    """An unusable *request* is still a 400; only the *document* is always judged with a 200."""
    response = client.post("/v1/guardrails/check", data={"request_id": "OCR_12"}, headers=auth)
    assert response.status_code == 400
    assert response.json()["message"] == "Send exactly one of file or file_url"
