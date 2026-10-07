"""Only the request's threshold decides: without one the document passes and `probability_bad` is answered; with
one it is applied on its side."""

import pytest

from app.clients.reject_threshold import Threshold
from app.config import Settings
from app.services.guardrails_service import GuardrailsService
from tests.conftest import JPEG, StubModel


def _service(probability: float) -> GuardrailsService:
    return GuardrailsService(StubModel(probability), Settings(api_key="x", _env_file=None))


@pytest.mark.parametrize("probability", [0.0, 0.65, 0.99])
async def test_without_a_threshold_the_document_passes_with_its_probability(probability):
    report = await _service(probability).check("kk.jpg", "image/jpeg", JPEG)

    assert (report["passed"], report["reason"]) == (True, None)
    assert report["document"] == {
        "verdict": "accepted",
        "confidence": round(1.0 - probability, 4),
        "probability_bad": probability,
        "threshold_used": None,
        "threshold_target": None,
    }


async def test_the_model_stored_threshold_no_longer_decides():
    model = StubModel(0.65, reject_threshold=0.3)
    report = await GuardrailsService(model, Settings(api_key="x", _env_file=None)).check("kk.jpg", "image/jpeg", JPEG)

    assert (report["passed"], report["document"]["threshold_used"]) == (True, None)


async def test_the_request_threshold_decides_on_the_reject_side():
    service = _service(0.65)

    rejected = await service.check("kk.jpg", "image/jpeg", JPEG, override=Threshold(0.6, "reject"))
    accepted = await service.check("kk.jpg", "image/jpeg", JPEG, override=Threshold(0.7, "reject"))

    assert (rejected["passed"], rejected["document"]["verdict"], rejected["document"]["threshold_used"]) == (
        False,
        "reject",
        0.6,
    )
    assert (accepted["passed"], accepted["document"]["threshold_target"]) == (True, "reject")


async def test_the_request_threshold_decides_on_the_accept_side():
    report = await _service(0.65).check("kk.jpg", "image/jpeg", JPEG, override=Threshold(0.5, "accept"))

    # The probability the image is good, 0.35, is below 0.5.
    assert (report["passed"], report["document"]["threshold_target"]) == (False, "accept")
