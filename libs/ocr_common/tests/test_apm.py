import pytest
from elasticapm.contrib.starlette import ElasticAPM
from fastapi.testclient import TestClient

from ocr_common.config import BaseServiceSettings
from ocr_common.testing import TEST_API_KEY
from ocr_common.web.apm import apm_config
from ocr_common.web.app import create_app


def make_settings(**values) -> BaseServiceSettings:
    return BaseServiceSettings(api_key=TEST_API_KEY, environment="local", _env_file=None, **values)


def build(settings: BaseServiceSettings):
    return create_app(settings=settings, title="Demo", description="demo", service_name="scoring")


def has_apm(app) -> bool:
    return any(middleware.cls is ElasticAPM for middleware in app.user_middleware)


@pytest.fixture
def no_send(monkeypatch):
    """The agent never talks to a server in tests."""
    monkeypatch.setenv("ELASTIC_APM_DISABLE_SEND", "true")
    monkeypatch.setenv("ELASTIC_APM_CLOUD_PROVIDER", "none")


def test_apm_is_off_without_a_server_url():
    assert not has_apm(build(make_settings()))


def test_config_names_each_service_and_keeps_payloads_out():
    config = apm_config(
        make_settings(
            elastic_apm_server_url="http://apm:8200",
            elastic_apm_secret_token="test-token",
            elastic_apm_sanitize_field_names="password, *token,,auth",
        ),
        "scoring",
    )
    assert config["SERVICE_NAME"] == "nilam-ocr-kk-scoring"
    assert config["ENVIRONMENT"] == "local"
    assert config["SECRET_TOKEN"] == "test-token"
    assert config["SANITIZE_FIELD_NAMES"] == ["password", "*token", "auth"]
    assert (config["CAPTURE_BODY"], config["CAPTURE_HEADERS"]) == ("off", False)
    assert (config["COLLECT_LOCAL_VARIABLES"], config["CENTRAL_CONFIG"]) == ("off", False)


def test_explicit_service_name_and_environment_win():
    config = apm_config(
        make_settings(
            elastic_apm_server_url="http://apm:8200", elastic_apm_service_name="x", elastic_apm_environment="dev"
        ),
        "scoring",
    )
    assert (config["SERVICE_NAME"], config["ENVIRONMENT"]) == ("x", "dev")


def test_apm_middleware_is_installed_and_requests_still_work(no_send):
    app = build(make_settings(elastic_apm_server_url="http://apm:8200"))
    assert has_apm(app)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_turning_body_capture_back_on_refuses_to_start(no_send, monkeypatch):
    monkeypatch.setenv("ELASTIC_APM_CAPTURE_BODY", "all")
    with pytest.raises(ValueError, match="ELASTIC_APM_CAPTURE_BODY"):
        build(make_settings(elastic_apm_server_url="http://apm:8200"))
