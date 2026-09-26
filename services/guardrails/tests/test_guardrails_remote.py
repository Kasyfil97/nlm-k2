"""The `remote` backend: the ML team's quality service, judging under its own threshold."""

from typing import Any

import httpx
import pytest

from ocr_common.clients.remote import RemoteModelClient
from ocr_common.errors import ServiceError
from ocr_common.testing import image_upload

from app.config import Settings
from app.dependencies import QUALITY_BACKENDS
from app.ml.remote import RemoteGuardrailsModel
from app.services.guardrails_service import REASON_REJECT, REASON_UNASSESSABLE, GuardrailsService
from tests.conftest import JPEG

ACCEPTED: dict[str, Any] = {
    "status_code": 200,
    "message": "OK",
    "data": {"document": {"verdict": "accepted", "probability_bad": 0.0287, "threshold_used": 0.62}},
}
REJECTED: dict[str, Any] = {
    "status_code": 200,
    "message": "OK",
    "data": {"document": {"verdict": "reject", "probability_bad": 0.8821, "threshold_used": 0.62}},
}
UNASSESSABLE: dict[str, Any] = {
    "status_code": 200,
    "message": "OK",
    "data": {"document": {"verdict": "unassessable", "probability_bad": None, "threshold_used": None}},
}


def _settings(**overrides) -> Settings:
    return Settings(api_key="x", _env_file=None, **overrides)


def _model(handler) -> RemoteGuardrailsModel:
    return RemoteGuardrailsModel(
        RemoteModelClient(
            "http://guardrails-model:8081",
            5.0,
            name="guardrails model",
            headers={"X-API-Key": "dummy-key"},
            transport=httpx.MockTransport(handler),
        )
    )


def _reply(body, status_code=200):
    return lambda request: httpx.Response(status_code, json=body)


async def test_request_follows_the_model_contract():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            method=request.method,
            path=request.url.path,
            api_key=request.headers.get("X-API-Key"),
            content_type=request.headers["Content-Type"],
            body=request.content,
        )
        return httpx.Response(200, json=ACCEPTED)

    await _model(handler).check_document("kk.jpg", JPEG, "image/jpeg")

    assert (seen["method"], seen["path"]) == ("POST", "/v1/predict/json")
    assert seen["api_key"] == "dummy-key"
    assert seen["content_type"].startswith("multipart/form-data")
    assert b'name="file"; filename="kk.jpg"' in seen["body"]
    assert b"Content-Type: image/jpeg" in seen["body"]
    assert JPEG in seen["body"]
    assert b"request_id" not in seen["body"]


async def test_an_accepted_document_passes_with_the_complement_as_confidence():
    report = await GuardrailsService(_model(_reply(ACCEPTED)), _settings()).check("kk.jpg", "image/jpeg", JPEG)
    assert report == {
        "passed": True,
        "reason": None,
        "document": {
            "verdict": "accepted",
            "confidence": 0.9713,
            "probability_bad": 0.0287,
            "threshold_used": 0.62,
        },
    }


async def test_a_rejected_document_gets_the_reason_the_orchestrator_relays():
    report = await GuardrailsService(_model(_reply(REJECTED)), _settings()).check("kk.jpg", "image/jpeg", JPEG)
    assert report["passed"] is False
    assert report["reason"] == REASON_REJECT
    assert report["document"]["confidence"] == 0.8821


async def test_the_remote_service_may_also_answer_unassessable():
    report = await GuardrailsService(_model(_reply(UNASSESSABLE)), _settings()).check("kk.jpg", "image/jpeg", JPEG)
    assert report["passed"] is False
    assert report["reason"] == REASON_UNASSESSABLE
    assert report["document"] == {
        "verdict": "unassessable",
        "confidence": None,
        "probability_bad": None,
        "threshold_used": None,
    }


async def test_local_threshold_settings_do_not_override_the_remote_verdict():
    """The remote service already judged. Re-deciding here would report a threshold that decided
    nothing and could disagree with the probability next to it."""
    settings = _settings(guardrails_threshold=0.001)
    report = await GuardrailsService(_model(_reply(ACCEPTED)), settings).check("kk.jpg", "image/jpeg", JPEG)
    assert report["document"]["verdict"] == "accepted"
    assert report["document"]["threshold_used"] == 0.62


async def test_a_per_request_override_is_ignored_by_the_remote_backend():
    service = GuardrailsService(_model(_reply(ACCEPTED)), _settings())
    report = await service.check("kk.jpg", "image/jpeg", JPEG, override=0.001)
    assert report["document"] == ACCEPTED["data"]["document"] | {"confidence": 0.9713}


async def test_extra_fields_from_the_model_are_dropped():
    """The block travels unchanged to scoring, so an unknown key here would outlive whoever knew
    what it meant."""
    body = {
        "status_code": 200,
        "message": "OK",
        "data": {"document": {**REJECTED["data"]["document"], "model_version": "v7", "heatmap": "..."}},
    }
    document = await _model(_reply(body)).check_document("kk.jpg", JPEG, "image/jpeg")
    assert set(document) == {"verdict", "confidence", "probability_bad", "threshold_used"}


@pytest.mark.parametrize(
    "body",
    [
        {"status_code": 200, "message": "OK"},
        {"data": {}},
        {"data": {"document": {"verdict": "maybe", "probability_bad": 0.5}}},
        {"data": {"document": {"verdict": "reject"}}},
        {"data": {"document": {"verdict": "reject", "probability_bad": 1.4}}},
        {"data": {"document": {"verdict": "accepted", "probability_bad": 0.1, "threshold_used": 0}}},
        ["not", "an", "object"],
    ],
)
async def test_unexpected_response_shape_is_500_not_a_guess(body):
    with pytest.raises(ServiceError) as exc:
        await _model(_reply(body)).check_document("kk.jpg", JPEG, "image/jpeg")
    assert exc.value.status_code == 500
    assert exc.value.message == "guardrails model returned an unexpected response"


async def test_model_error_status_becomes_500_with_detail():
    model = _model(_reply({"status_code": 401, "message": "Invalid API key"}, status_code=401))
    with pytest.raises(ServiceError) as exc:
        await model.check_document("kk.jpg", JPEG, "image/jpeg")
    assert exc.value.status_code == 500
    assert exc.value.message == "guardrails model error (401): Invalid API key"


async def test_unreachable_model_is_503_and_slow_model_is_504():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ServiceError) as exc:
        await _model(refuse).check_document("kk.jpg", JPEG, "image/jpeg")
    assert (exc.value.status_code, exc.value.message) == (503, "guardrails model is unavailable")

    with pytest.raises(ServiceError) as exc:
        await _model(stall).check_document("kk.jpg", JPEG, "image/jpeg")
    assert (exc.value.status_code, exc.value.message) == (504, "guardrails model timed out after 5.0s")


def test_remote_backend_requires_model_url():
    with pytest.raises(RuntimeError, match="GUARDRAILS_MODEL_URL is required"):
        QUALITY_BACKENDS["remote"](_settings(guardrails_backend="remote"))


async def test_remote_backend_is_built_from_settings():
    model = QUALITY_BACKENDS["remote"](
        _settings(
            guardrails_backend="remote",
            guardrails_model_url="http://localhost:8081/",
            guardrails_model_api_key="dummy-key",
        )
    )
    try:
        assert isinstance(model, RemoteGuardrailsModel)
        assert model._client.base_url == "http://localhost:8081"
        assert model._client._client.headers["X-API-Key"] == "dummy-key"
    finally:
        await model.aclose()


def test_http_check_with_remote_backend(client, auth, use_model):
    use_model(_model(_reply(REJECTED)))
    response = client.post(
        "/v1/guardrails/check", data={"request_id": "OCR_R1"}, files=image_upload("kk.jpg", JPEG), headers=auth
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "OCR_R1"
    assert body["data"]["passed"] is False
    assert body["data"]["document"]["threshold_used"] == 0.62


def test_http_model_unreachable_returns_503_envelope(client, auth, use_model):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    use_model(_model(refuse))
    response = client.post(
        "/v1/guardrails/check", data={"request_id": "OCR_R2"}, files=image_upload("kk.jpg", JPEG), headers=auth
    )
    assert response.status_code == 503
    assert response.json()["message"] == "guardrails model is unavailable"
