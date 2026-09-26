import pathlib

import pytest
from pydantic import ValidationError

from ocr_common.testing import TEST_API_KEY

from app.config import Settings

GUARDRAILS = "http://nlm-k2-guardrails:8041"
EKSTRAKSI = "http://nlm-k2-ekstraksi:8042"
STRUCTURING = "http://nlm-k2-structuring:8043"
SCORING = "http://nlm-k2-scoring:8044"
LOCALHOST = "http://127.0.0.1:9999"


def _deployed(
    guardrails: str = GUARDRAILS,
    ekstraksi: str = EKSTRAKSI,
    structuring: str = STRUCTURING,
    scoring: str = SCORING,
    **extra,
) -> Settings:
    return Settings(
        api_key=TEST_API_KEY,
        _env_file=None,
        environment="production",
        file_url_allowed_hosts="minio.internal",
        pipeline_wait_seconds=0,
        guardrails_service_url=guardrails,
        ekstraksi_service_url=ekstraksi,
        structuring_service_url=structuring,
        scoring_service_url=scoring,
        **extra,
    )


def test_service_addresses_are_accepted_outside_local():
    assert _deployed()


@pytest.mark.parametrize(
    ("setting", "overrides"),
    [
        ("GUARDRAILS_SERVICE_URL", {"guardrails": LOCALHOST}),
        ("EKSTRAKSI_SERVICE_URL", {"ekstraksi": LOCALHOST}),
        ("STRUCTURING_SERVICE_URL", {"structuring": LOCALHOST}),
        ("SCORING_SERVICE_URL", {"scoring": LOCALHOST}),
    ],
)
def test_localhost_services_are_refused_outside_local(setting, overrides):
    """All four, also with PIPELINE_WAIT_SECONDS=0: GET /v1/extract-ocr/{request_id} reads the stages anyway."""
    with pytest.raises(ValidationError, match=f"{setting} points to localhost"):
        _deployed(**overrides)


def test_localhost_services_are_fine_locally():
    settings = Settings(api_key=TEST_API_KEY, _env_file=None, environment="local")
    assert settings.guardrails_service_url == "http://127.0.0.1:8041"
    assert settings.port == 8040


def test_skipping_guardrails_is_not_allowed_by_default():
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").guardrails_skip_allowed is False


def test_pipeline_wait_defaults_to_the_30_seconds_the_contract_documents():
    # Kontrak §13 mendaftarkan 30. Uji ini ada karena nilainya pernah 15 di kode dan 30 di
    # kontrak selama satu unit penuh tanpa ada yang melihatnya.
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").pipeline_wait_seconds == 30.0
    assert (
        Settings(
            api_key=TEST_API_KEY, _env_file=None, environment="local", pipeline_wait_seconds=8
        ).pipeline_wait_seconds
        == 8
    )
    with pytest.raises(ValidationError, match="pipeline_wait_seconds"):
        # The invalid value is the point of this check.
        # pyrefly: ignore[bad-argument-type]
        Settings(api_key=TEST_API_KEY, _env_file=None, environment="local", pipeline_wait_seconds=-1)


# --- R18a: the download allow-list, and the edge settings ----------------------------------


def test_an_empty_file_url_allowlist_is_refused_outside_local():
    """An empty list already denies every URL, so the download path would be dead. Failing at
    start-up says so once, instead of once per request."""
    with pytest.raises(ValidationError, match="FILE_URL_ALLOWED_HOSTS must list the hosts"):
        Settings(
            api_key=TEST_API_KEY,
            _env_file=None,
            environment="production",
            guardrails_service_url=GUARDRAILS,
            ekstraksi_service_url=EKSTRAKSI,
            structuring_service_url=STRUCTURING,
            scoring_service_url=SCORING,
        )


def test_an_empty_allowlist_is_fine_locally():
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").file_url_allowed_hosts == ""


def test_the_rate_limit_is_on_by_default(monkeypatch):
    # conftest raises RATE_LIMIT_REQUESTS for the whole process so the shared client is not throttled;
    # this is the one test that wants the shipped default, so it takes the variable back out.
    monkeypatch.delenv("RATE_LIMIT_REQUESTS", raising=False)
    settings = Settings(api_key=TEST_API_KEY, _env_file=None, environment="local")
    assert settings.rate_limit_enabled is True
    assert (settings.rate_limit_requests, settings.rate_limit_window_seconds) == (60, 60.0)


def test_cors_is_off_by_default_and_parses_to_a_list():
    """Off is the right default: the contract's caller is server-to-server, and an open CORS policy
    on an API-key service is how a leaked key becomes usable from any page."""
    assert Settings(api_key=TEST_API_KEY, _env_file=None, environment="local").cors_origins == []
    settings = _deployed(cors_allow_origins="https://a.example, https://b.example ,")
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


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
            file_url_allowed_hosts="minio.internal",
            guardrails_service_url=GUARDRAILS,
            ekstraksi_service_url=EKSTRAKSI,
            structuring_service_url=STRUCTURING,
            scoring_service_url=SCORING,
        )
