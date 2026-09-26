import pytest

from ocr_common.testing import auth_headers, set_test_env

set_test_env(DATABASE_URL="", ORCHESTRATION_URL="", AUTH_DISABLED="false")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """Context-managed on purpose: without it Starlette tears the portal down after each request,
    which cancels the background job the stage just started -- the job then comes back FAILED with
    "interrupted by a service shutdown" instead of its result, and only in some test orders."""
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return auth_headers()
