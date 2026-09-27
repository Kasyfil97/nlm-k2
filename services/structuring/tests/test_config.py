from typing import Any

import pytest
from pydantic import ValidationError

from ocr_common.testing import TEST_API_KEY

from app.config import Settings

PROD: dict[str, Any] = {
    "environment": "production",
    "database_url": "postgresql+asyncpg://u:p@10.0.0.5:5432/db",
    "orchestration_url": "http://orkestrasi:8000",
    "scoring_service_url": "http://scoring:8044",
    # Now that a backend reading the document exists, `mock` is refused here the way it is in the
    # other model-bearing services -- so a production configuration has to name the real one.
    "structuring_backend": "kk_regex",
}


def test_the_mock_backend_is_refused_outside_local():
    """It invents a household, so in production it would answer a stranger's card with a stranger."""
    with pytest.raises(ValidationError, match="STRUCTURING_BACKEND=mock"):
        mock_backend: dict[str, Any] = {**PROD, "structuring_backend": "mock"}
        Settings(api_key=TEST_API_KEY, _env_file=None, **mock_backend)


def test_production_configuration_is_accepted():
    assert Settings(api_key=TEST_API_KEY, _env_file=None, **PROD).environment == "production"


def test_a_short_api_key_is_refused_outside_local():
    """R29: yang salah dari kunci satu huruf bukan namanya, melainkan panjangnya."""
    with pytest.raises(ValidationError, match="shorter than 16"):
        Settings(api_key="x", _env_file=None, **PROD)


def test_default_localhost_next_stage_is_refused_outside_local():
    with pytest.raises(ValidationError, match="SCORING_SERVICE_URL points to localhost"):
        local_next_stage: dict[str, Any] = {**PROD, "scoring_service_url": "http://127.0.0.1:8044"}
        Settings(api_key=TEST_API_KEY, _env_file=None, **local_next_stage)
