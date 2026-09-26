from typing import Any

import pytest

from ocr_common.testing import auth_headers, set_test_env

# Setiap kunci pipeline yang uji ini mengandaikan MATI dimatikan di sini, bukan hanya dua yang
# pertama. `set_test_env` menulis ke os.environ, yang mengalahkan `.env` -- tetapi kunci yang tidak
# disebut tetap dibaca dari `.env` pengembang. Sebuah `.env` yang menyalakan
# PIPELINE_HANDOFF_BY_REFERENCE (mis. untuk `make smoke`) dulu membuat seluruh suite gagal saat
# impor conftest, dengan pesan validasi yang tidak menyebut-nyebut uji.
set_test_env(
    PIPELINE_HANDOFF_BY_REFERENCE="false",
    PIPELINE_OUTBOX="false",
    ORCHESTRATION_OUTCOME_TABLE="",
    EKSTRAKSI_BACKEND="mock",
    DATABASE_URL="",
    ORCHESTRATION_URL="",
    AUTH_DISABLED="false",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.dependencies import get_job_service, get_pipeline  # noqa: E402
from app.main import app  # noqa: E402
from app.services.ekstraksi_service import EkstraksiService  # noqa: E402
from app.services.job_service import EkstraksiJobService  # noqa: E402

#: Only the content type and the size of a document are checked before the model; the bytes
#: themselves never have to be a real JPEG for the `mock` backend.
JPEG = b"\xff\xd8fake-jpeg-bytes"

#: What the orchestrator forwards from the guardrails service, verbatim (§5.2, §6.1).
GUARDRAILS: dict[str, Any] = {
    "passed": True,
    "reason": None,
    "document": {"verdict": "accepted", "confidence": 0.9821, "probability_bad": 0.0179, "threshold_used": 0.5},
}


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


@pytest.fixture
def use_engine():
    """Run the jobs of one test with this OCR backend instead of the configured one.

    The override is on `get_job_service`, not on `get_ekstraksi_service`: the composition root
    calls the latter itself rather than taking it through `Depends`, so overriding it would
    silently do nothing. The pipeline (and with it the shared in-memory job table) is reused, so a
    job started under an override is still readable through the ordinary endpoint.
    """

    def _use(engine):
        settings = get_settings()
        service = EkstraksiJobService(
            get_pipeline(),
            EkstraksiService(engine, settings),
            settings.max_upload_bytes,
            url_policy=settings.file_url_policy,
            simulate_delay=settings.simulation_hooks_enabled,
            handoff_by_reference=settings.pipeline_handoff_by_reference,
        )
        app.dependency_overrides[get_job_service] = lambda: service

    yield _use
    app.dependency_overrides.pop(get_job_service, None)


@pytest.fixture
def settings_override():
    """Change settings for one test. The app reads them through a dependency, so this is an
    override rather than an env variable -- the module was imported once, at collection."""

    def install(**update):
        app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update=update)

    yield install
    app.dependency_overrides.pop(get_settings, None)
