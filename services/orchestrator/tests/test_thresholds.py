"""Thresholds from the central orchestrator, per request. Ported from nilam (708d53f / bb63efe).

`column_confidence_threshold` decides each field's 0/1 `confidence`; a field without one gets the probability
itself. It travels with the job so the GET decides with the same numbers as the POST.
"""

import json
from copy import deepcopy

import pytest

from ocr_common.kk import contract_fields, scored_fields

from app.services.pipeline_waiter import WaitOutcome
from tests.conftest import JPEG, OCR_RESULT, SCORING_RESULT, STRUCTURING_RESULT

RID = "REQ_thresholds"


def _submit(client, auth, **form):
    return client.post(
        "/v1/extract-ocr",
        headers=auth,
        data={"request_id": RID, **form},
        files={"file": ("kk.jpg", JPEG, "image/jpeg")},
    )


def _done(scoring):
    return WaitOutcome(
        "SCORING", "DONE", results={"OCR": OCR_RESULT, "STRUCTURING": STRUCTURING_RESULT, "SCORING": scoring}
    )


def test_column_confidence_threshold_reaches_the_pipeline_and_decides_the_fields(client, auth, stub_extraction):
    columns = {"no_kk": 0.95, "jenis_pekerjaan": 0.1}

    response = _submit(client, auth, column_confidence_threshold=json.dumps(columns))

    assert response.status_code == 200
    [handed] = stub_extraction.submitted
    assert handed["column_thresholds"] == columns
    data = response.json()["data"]
    assert data["no_kk"]["confidence"] == 0, "0.94 below the request's 0.95"
    assert data["anggota_keluarga"][1]["jenis_pekerjaan"]["confidence"] == 1, "0.4118 above the request's 0.1"
    assert data == contract_fields(STRUCTURING_RESULT, SCORING_RESULT, columns)
    assert data["nama_kepala_keluarga"]["confidence"] == SCORING_RESULT["fields"]["nama_kepala_keluarga"]


def test_without_thresholds_every_confidence_is_the_probability(client, auth, stub_extraction):
    response = _submit(client, auth)

    assert stub_extraction.submitted[0]["column_thresholds"] is None
    data = response.json()["data"]
    assert data == contract_fields(STRUCTURING_RESULT, SCORING_RESULT)
    assert data["no_kk"]["confidence"] == SCORING_RESULT["fields"]["nomor_kk"]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("{not json", "valid JSON"),
        ('{"nomor_kk": 0.9}', "unknown field(s) nomor_kk"),
        ('{"nik": 1.5}', "between 0 and 1"),
    ],
)
def test_a_threshold_that_cannot_be_read_is_422_and_nothing_runs(
    client, auth, stub_guardrails, stub_extraction, raw, reason
):
    response = _submit(client, auth, column_confidence_threshold=raw)

    assert response.status_code == 422
    body = response.json()
    assert body["errors"] == "INVALID_THRESHOLD"
    assert reason in body["message"]
    assert stub_guardrails.checked == [] and stub_extraction.submitted == []


def test_the_decisions_scoring_stored_win_over_deciding_again(client, auth, stub_waiter):
    """The outcome row is written from `decisions`; answering from them too is what keeps the POST, the GET
    and the row identical."""
    scoring = deepcopy(SCORING_RESULT)
    scoring["decisions"] = scored_fields(STRUCTURING_RESULT, SCORING_RESULT, {"no_kk": 0.99})
    stub_waiter.outcome = _done(scoring)

    data = _submit(client, auth).json()["data"]

    assert data == contract_fields(STRUCTURING_RESULT, SCORING_RESULT, {"no_kk": 0.99})
    assert data["no_kk"]["confidence"] == 0


def test_the_get_answers_with_the_thresholds_stored_with_the_job(client, auth, stub_waiter):
    stub_waiter.snapshot_outcome = WaitOutcome(
        "SCORING",
        "DONE",
        results={"OCR": OCR_RESULT, "STRUCTURING": STRUCTURING_RESULT, "SCORING": SCORING_RESULT},
        column_thresholds={"no_kk": 0.95},
    )

    data = client.get(f"/v1/extract-ocr/{RID}", headers=auth).json()["data"]

    assert data["no_kk"]["confidence"] == 0
    assert data == contract_fields(STRUCTURING_RESULT, SCORING_RESULT, {"no_kk": 0.95})


# --- the guardrails threshold (ported from nilam 708d53f) ------------------------------------------


def test_the_guardrails_threshold_reaches_guardrails_and_is_recorded_as_the_requests(
    client, auth, stub_guardrails, guardrails_log
):
    response = _submit(client, auth, guardrails_confidence_threshold='{"acc_rej": 0.3}')

    assert response.status_code == 200
    [threshold] = stub_guardrails.thresholds
    assert (threshold.value, threshold.target) == (0.3, "reject")
    [record] = guardrails_log.records
    assert record["threshold_from_request"] is True


def test_without_a_guardrails_threshold_none_is_sent(client, auth, stub_guardrails, guardrails_log):
    _submit(client, auth)

    assert stub_guardrails.thresholds == [None]
    assert guardrails_log.records[0]["threshold_from_request"] is False


def test_the_acc_rej_object_applies_to_the_rejected_side_by_default(client, auth, stub_guardrails):
    response = _submit(client, auth, guardrails_confidence_threshold='{"acc_rej": 0.8}')

    assert response.status_code == 200
    [threshold] = stub_guardrails.thresholds
    assert (threshold.value, threshold.target) == (0.8, "reject")


@pytest.mark.parametrize(
    ("form", "reason"),
    [
        ({"guardrails_confidence_threshold": "0.5"}, "acc_rej"),
        ({"guardrails_confidence_threshold": '{"other": 0.5}'}, "acc_rej"),
        ({"guardrails_confidence_threshold": '{"acc_rej": 1}'}, "between 0 and 1"),
        ({"guardrails_confidence_threshold": '{"acc_rej": "tinggi"}'}, "between 0 and 1"),
    ],
)
def test_a_guardrails_threshold_that_cannot_be_read_is_422_and_nothing_runs(
    client, auth, stub_guardrails, stub_extraction, form, reason
):
    response = _submit(client, auth, **form)

    assert response.status_code == 422
    body = response.json()
    assert body["errors"] == "INVALID_THRESHOLD" and reason in body["message"]
    assert stub_guardrails.checked == [] and stub_extraction.submitted == []
