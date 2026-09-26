import pytest

from ocr_common.testing import auth_headers, make_client, set_test_env

set_test_env(GUARDRAILS_BACKEND="mock", AUTH_DISABLED="false")

from app.config import get_settings  # noqa: E402
from app.dependencies import get_guardrails_service  # noqa: E402
from app.main import app  # noqa: E402
from app.ml.base import UnassessableImage  # noqa: E402
from app.services.guardrails_service import GuardrailsService  # noqa: E402

#: Enough bytes to look like a JPEG to anything that only glances at them. Nothing in the test suite
#: runs a real model, so the pixels never matter -- the `mock` backend reads the file name.
JPEG = b"\xff\xd8fake-jpeg-bytes"


class StubModel:
    """An in-process model with a fixed answer. `probability` may be an exception to raise."""

    name = "stub"

    def __init__(self, probability: float | BaseException = 0.25, reject_threshold: float | None = None):
        self.probability = probability
        self.reject_threshold = reject_threshold
        self.metadata: dict = {}
        self.seen: list[tuple[str, str | None, int]] = []

    def assess(self, filename: str, content_type: str | None, content: bytes) -> float:
        self.seen.append((filename, content_type, len(content)))
        if isinstance(self.probability, BaseException):
            raise self.probability
        return self.probability


def unassessable_model() -> StubModel:
    """A model that cannot judge anything -- the R14a path."""
    return StubModel(UnassessableImage("cv2.imdecode returned None: corrupt or unsupported image"))


@pytest.fixture(scope="session")
def client():
    return make_client(app)


@pytest.fixture
def auth() -> dict[str, str]:
    return auth_headers()


@pytest.fixture
def use_model():
    """Serve the routes with this model instead of the configured backend."""

    def _use(model, **settings):
        configured = get_settings().model_copy(update=settings) if settings else get_settings()
        app.dependency_overrides[get_guardrails_service] = lambda: GuardrailsService(model, configured)

    yield _use
    app.dependency_overrides.pop(get_guardrails_service, None)


@pytest.fixture
def settings_override():
    """Change settings for one test. The route reads them through a dependency, so this is an
    override rather than an env variable -- the module was imported once, at collection."""

    def install(**update):
        app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(update=update)

    yield install
    app.dependency_overrides.pop(get_settings, None)
