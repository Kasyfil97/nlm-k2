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
    DATABASE_URL="",
    ORCHESTRATION_URL="",
    AUTH_DISABLED="false",
    # Disebut eksplisit sejak bawaannya jadi `calibrated`: berkas uji di sebelah memang menguji
    # backend `mock` -- bentuk §8.3, penyelarasan posisional, jalur penolakan -- dan tidak mengirim
    # blok `features`, yang pada model sungguhan justru membuat setiap field diskor `null` dengan
    # benar. Model terkalibrasi diuji tersendiri di test_calibrated_model.py, terhadap artefaknya.
    SCORING_BACKEND="mock",
)

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
