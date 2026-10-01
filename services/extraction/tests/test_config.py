import pathlib
from typing import Any

import pytest
from pydantic import ValidationError

from ocr_common.testing import TEST_API_KEY

from app.config import Settings

PROD: dict[str, Any] = {
    "environment": "production",
    "database_url": "postgresql+asyncpg://u:p@10.0.0.5:5432/db",
    "orchestration_url": "http://orkestrasi:8000",
    "extraction_backend": "kk_ocr",
    "structuring_service_url": "http://nlm-k2-structuring:8043",
    "file_url_allowed_hosts": "minio.internal",
}


def settings(**overrides) -> Settings:
    return Settings(api_key=TEST_API_KEY, _env_file=None, **{**PROD, **overrides})


def test_production_configuration_is_accepted():
    assert settings().environment == "production"


def test_mock_ocr_is_refused_outside_local():
    """A mock fabricates a card. There is no reading of `EXTRACTION_BACKEND=mock` in a deployed
    environment that is not a mistake."""
    with pytest.raises(ValidationError, match="EXTRACTION_BACKEND=mock fabricates results"):
        settings(extraction_backend="mock")


def test_default_localhost_next_stage_is_refused_outside_local():
    with pytest.raises(ValidationError, match="STRUCTURING_SERVICE_URL points to localhost"):
        settings(structuring_service_url="http://127.0.0.1:8043")


def test_local_keeps_the_laptop_defaults():
    local = Settings(api_key=TEST_API_KEY, _env_file=None, environment="local")
    assert local.extraction_backend == "mock"
    assert local.structuring_service_url == "http://127.0.0.1:8043"
    assert local.port == 8042


# --- R18a: the download allow-list ---------------------------------------------------------


def test_an_empty_file_url_allowlist_is_refused_outside_local():
    """This service downloads `file_url` itself, so the list is required here as it is in the
    orchestrator -- always, not behind a switch. An empty list already denies every URL, so the
    download path would be dead; failing at start-up says that once instead of once per job."""
    with pytest.raises(ValidationError, match="FILE_URL_ALLOWED_HOSTS must list the hosts"):
        settings(file_url_allowed_hosts="")


def test_an_empty_allowlist_is_fine_locally():
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").file_url_allowed_hosts == ""


def test_the_policy_is_the_shared_one_and_not_a_second_implementation():
    """Two divergent SSRF implementations in one repository is the failure this guards against, so
    the assertion is about *which* policy the service hands to the downloader, not about re-testing
    the policy's rules -- those belong to `ocr_common.clients.fetch_url` and are tested there."""
    policy = settings(file_url_allowed_hosts="minio.internal,.storage.example.com").file_url_policy
    assert policy.allowed_hosts == ("minio.internal", ".storage.example.com")
    assert (policy.allow_http, policy.allow_private, policy.allow_any_host) == (False, False, False)

    local = Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").file_url_policy
    assert (local.allow_http, local.allow_private, local.allow_any_host) == (True, True, True)


def test_the_job_service_is_built_with_that_policy():
    """The guard above is worth nothing if the job service is constructed with the default one."""
    from app.dependencies import get_job_service, get_settings

    assert get_job_service()._url_policy == get_settings().file_url_policy


# --- the file-name hooks are gated at the config layer -------------------------------------


def test_the_simulation_hooks_are_local_only():
    """The file name crosses the trust boundary in the multipart request, so `delay20s-…` must not
    be something a caller can use to hold a deployed worker open."""
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").simulation_hooks_enabled is True
    assert settings().simulation_hooks_enabled is False


# --- R24: the two per-service checks -------------------------------------------------------


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
            database_url="postgresql+asyncpg://u:p@10.0.0.5:5432/db",
            orchestration_url="http://orkestrasi:8000",
            extraction_backend="kk_ocr",
            structuring_service_url="http://nlm-k2-structuring:8043",
            file_url_allowed_hosts="minio.internal",
        )
