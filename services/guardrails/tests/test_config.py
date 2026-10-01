import pathlib

import pytest
from pydantic import ValidationError

from ocr_common.config import Environment
from ocr_common.testing import TEST_API_KEY

from app.config import Settings

MODEL_SERVICE = "http://nlm-k2-guardrails-model:8081"
LOCALHOST = "http://127.0.0.1:9999"


def _deployed(environment: Environment = "production", guardrails_backend: str = "kk_quality", **extra) -> Settings:
    """Settings that would actually start in production, so a test can break exactly one thing."""
    return Settings(
        api_key=TEST_API_KEY,
        _env_file=None,
        environment=environment,
        guardrails_backend=guardrails_backend,
        **extra,
    )


def test_a_deployable_configuration_starts():
    assert _deployed().guardrails_backend == "kk_quality"


def test_mock_backend_is_refused_outside_local():
    with pytest.raises(ValidationError, match="GUARDRAILS_BACKEND=mock fabricates results"):
        Settings(api_key=TEST_API_KEY, _env_file=None, environment="production")


def test_real_backends_are_accepted_outside_local():
    assert _deployed()
    assert _deployed(environment="staging", guardrails_backend="remote", guardrails_model_url=MODEL_SERVICE)


def test_localhost_model_service_is_refused_outside_local():
    with pytest.raises(ValidationError, match="GUARDRAILS_MODEL_URL points to localhost"):
        _deployed(guardrails_backend="remote", guardrails_model_url="http://localhost:8081")


def test_localhost_threshold_source_is_refused_outside_local():
    with pytest.raises(ValidationError, match="GUARDRAILS_THRESHOLD_URL points to localhost"):
        _deployed(guardrails_threshold_url=LOCALHOST)


def test_settings_of_the_pipeline_are_ignored(monkeypatch):
    """The entry-point and stage settings live in other services; an environment that still sets
    them (an old ConfigMap, a shared .env) must not stop guardrails from starting."""
    monkeypatch.setenv("EKSTRAKSI_SERVICE_URL", "http://127.0.0.1:8042")
    monkeypatch.setenv("PIPELINE_WAIT_SECONDS", "15")
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/db")
    settings = _deployed()
    assert not hasattr(settings, "pipeline_wait_seconds")
    assert not hasattr(settings, "database_url")


# --- R15: the threshold, and its name ---------------------------------------------------------


def test_the_threshold_env_key_is_the_contract_one(monkeypatch):
    """§13.3 calls it GUARDRAILS_THRESHOLD. nilam's GUARDRAILS_REJECT_THRESHOLD is not read, and a
    deployment that still sets it must not silently run on 0.5 while believing otherwise -- which
    is only visible if the contract name is the one that works."""
    monkeypatch.setenv("GUARDRAILS_THRESHOLD", "0.62")
    monkeypatch.setenv("GUARDRAILS_REJECT_THRESHOLD", "0.01")
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").guardrails_threshold == 0.62


def test_the_threshold_is_unset_by_default_so_the_lower_rungs_can_be_reached():
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").guardrails_threshold is None


@pytest.mark.parametrize("value", [0, 1, 1.5, -0.1])
def test_a_threshold_outside_the_open_unit_interval_is_refused_at_start(value):
    with pytest.raises(ValidationError):
        _deployed(guardrails_threshold=value)


# --- R18a: the download switch ------------------------------------------------------------------


def test_downloading_is_off_by_default():
    """The orchestrator has already fetched the bytes; this service is not a second downloader
    unless someone says so."""
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").guardrails_fetch_url is False


def test_an_empty_allowlist_with_the_switch_on_is_refused_outside_local():
    """An empty list already denies every URL, so the download path would be dead. Failing at
    start-up says so once, instead of once per request."""
    with pytest.raises(ValidationError, match="FILE_URL_ALLOWED_HOSTS must list the hosts"):
        _deployed(guardrails_fetch_url=True)


def test_an_empty_allowlist_is_fine_while_the_switch_is_off():
    """This is why the check is conditional: a guardrails that never downloads needs no list."""
    assert _deployed().file_url_allowed_hosts == ""


def test_the_switch_starts_outside_local_once_the_hosts_are_listed():
    settings = _deployed(guardrails_fetch_url=True, file_url_allowed_hosts="minio.internal")
    assert settings.file_url_policy.allowed_hosts == ("minio.internal",)
    assert settings.file_url_policy.allow_private is False


def test_an_empty_allowlist_with_the_switch_on_is_fine_locally():
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local", guardrails_fetch_url=True)


# --- PDF: accepted by default, as in nilam -------------------------------------------------------


def test_pdf_is_accepted_by_default_and_there_is_no_switch():
    """R34a's `PDF_ENABLED` is gone (`docs/decisions/2026-09-29-selaras-nilam.md`): a PDF passes
    intake here as it does at the orchestrator, and page 1 is judged."""
    settings = Settings(api_key=TEST_API_KEY, _env_file=None, environment="local")
    assert "application/pdf" in settings.allowed_content_types
    assert not hasattr(settings, "pdf_enabled")


# --- R24: the two per-service checks ----------------------------------------------------------


def test_the_shipped_env_example_would_refuse_to_start_in_production():
    """R29. The library proves the guard works; this proves THIS service's `.env.example` is on
    the wrong side of it, so a deployment that copies it unchanged fails at start instead of
    running with a key anyone can read in the repository."""
    example = pathlib.Path(".env.example").read_text(encoding="utf-8")
    [line] = [line for line in example.splitlines() if line.startswith("API_KEY=")]
    with pytest.raises(ValidationError):
        Settings(
            api_key=line.removeprefix("API_KEY="),
            _env_file=None,
            environment="production",
            guardrails_backend="kk_quality",
        )
