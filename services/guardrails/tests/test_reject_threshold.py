"""R15: the five rungs of the threshold chain, and what happens when one of them is broken."""

import pytest

from ocr_common.errors import UpstreamUnavailable

from app.clients.reject_threshold import RejectThreshold, Threshold, default_threshold, parse_threshold
from app.config import Settings
from app.ml.mock import MockQualityModel
from app.services.guardrails_service import GuardrailsService
from tests.conftest import JPEG, StubModel


class StubOrchestrator:
    """The orchestrator's threshold endpoint: answers each GET with the next of `answers` (an exception is
    raised), repeating the last one."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.paths: list[str] = []
        self.closed = False

    async def get_json(self, path, *, params=None):
        self.paths.append(path)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def aclose(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _threshold(stub, clock=None, cache_seconds=60.0, default=0.5) -> RejectThreshold:
    return RejectThreshold(
        stub, "/v1/thresholds/guardrails", default, cache_seconds=cache_seconds, clock=clock or Clock()
    )


# --- rungs 3 to 5 ------------------------------------------------------------------------------


def test_the_environment_wins_over_the_stored_value():
    assert default_threshold(0.3, StubModel(reject_threshold=0.8)) == 0.3


def test_the_stored_value_wins_over_the_floor():
    assert default_threshold(None, StubModel(reject_threshold=0.62)) == 0.62


def test_a_backend_with_no_stored_value_falls_to_the_floor():
    """`None`, not `0.5`, is what a backend without a stored threshold reports -- otherwise rung 4
    and rung 5 are the same number and a chain that stopped working looks exactly like one that
    did not. Today every shipped backend is in this position: K2Quality keeps its operating point
    in config.yaml, not in the six artifacts."""
    assert MockQualityModel().reject_threshold is None
    assert default_threshold(None, MockQualityModel()) == 0.5
    assert default_threshold(None, object()) == 0.5


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reject_threshold": None},
        {"reject_threshold": "0.5"},
        {"reject_threshold": True},
        {"reject_threshold": 0},
        {"reject_threshold": 1},
        {"reject_threshold": 1.5},
        {"reject_threshold": float("nan")},
        [0.5],
    ],
)
def test_parse_refuses_anything_but_a_threshold_between_0_and_1(body):
    with pytest.raises(ValueError):
        parse_threshold(body)


# --- rung 2: the central orchestrator ------------------------------------------------------------


async def test_without_url_the_default_is_used_and_nothing_is_called():
    assert await RejectThreshold(None, "", 0.4, cache_seconds=60).get() == 0.4


async def test_the_orchestrators_threshold_is_used_and_cached():
    stub, clock = StubOrchestrator({"reject_threshold": 0.7}), Clock()
    threshold = _threshold(stub, clock)

    assert await threshold.get() == 0.7
    clock.now += 59
    assert await threshold.get() == 0.7
    assert stub.paths == ["/v1/thresholds/guardrails"]


async def test_a_change_at_the_orchestrator_applies_after_the_cache_expires():
    stub, clock = StubOrchestrator({"reject_threshold": 0.7}, {"reject_threshold": 0.3}), Clock()
    threshold = _threshold(stub, clock)

    assert await threshold.get() == 0.7
    clock.now += 60
    assert await threshold.get() == 0.3
    assert len(stub.paths) == 2


async def test_unreachable_orchestrator_gives_the_default():
    stub = StubOrchestrator(UpstreamUnavailable("orchestrator reject threshold is unavailable"))
    assert await _threshold(stub).get() == 0.5


async def test_failure_after_a_success_keeps_the_last_value_and_retries_after_the_cache():
    stub = StubOrchestrator({"reject_threshold": 0.7}, {"reject_threshold": "x"}, {"reject_threshold": 0.6})
    clock = Clock()
    threshold = _threshold(stub, clock)

    assert await threshold.get() == 0.7
    clock.now += 60
    assert await threshold.get() == 0.7  # invalid answer: the last value stays
    clock.now += 30
    assert await threshold.get() == 0.7  # not asked again before the cache expires
    clock.now += 30
    assert await threshold.get() == 0.6
    assert len(stub.paths) == 3


async def test_aclose_closes_the_client():
    stub = StubOrchestrator({"reject_threshold": 0.7})
    await _threshold(stub).aclose()
    assert stub.closed


# --- the whole chain, through the service --------------------------------------------------------


async def test_the_service_judges_with_the_orchestrators_threshold_and_reports_it():
    settings = Settings(api_key="x", _env_file=None)
    model = StubModel(0.65)

    lenient = _threshold(StubOrchestrator({"reject_threshold": 0.7}))
    report = await GuardrailsService(model, settings, lenient).check("kk.jpg", "image/jpeg", JPEG)
    assert (report["passed"], report["document"]["threshold_used"]) == (True, 0.7)

    fallback = await GuardrailsService(model, settings).check("kk.jpg", "image/jpeg", JPEG)
    assert (fallback["passed"], fallback["document"]["threshold_used"]) == (False, 0.5)


async def test_the_per_request_override_outranks_the_orchestrator():
    """Rung 1. It also means the orchestrator's endpoint is not even consulted for that request."""
    stub = StubOrchestrator({"reject_threshold": 0.7})
    service = GuardrailsService(StubModel(0.65), Settings(api_key="x", _env_file=None), _threshold(stub))
    report = await service.check("kk.jpg", "image/jpeg", JPEG, override=Threshold(0.6, "reject"))
    assert (report["document"]["verdict"], report["document"]["threshold_used"]) == ("reject", 0.6)
    assert stub.paths == [], "the remote source was not consulted for an overridden request"
