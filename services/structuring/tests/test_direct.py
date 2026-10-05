"""`POST /v1/structuring-direct`: the structuring rules on the extraction service's output, synchronous, for
testing this stage on its own. Nothing is recorded. Ported from nilam."""

import pytest

from ocr_common.kk import DOC_FIELDS
from ocr_common.testing import wait_for_job

from app.ml.mock import NO_TEXT
from tests.test_kk_structuring import box

OCR = {"engine": "mock", "texts": [box("KARTU KELUARGA"), box("No. 9924187486671285")]}


def _post(client, auth, **body):
    return client.post("/v1/structuring-direct", headers=auth, json=body)


def test_the_ocr_output_is_structured_now_and_nothing_is_recorded(client, auth):
    response = _post(client, auth, request_id="QC_1", ocr=OCR)

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "QC_1"
    assert set(body["data"]) == set(DOC_FIELDS) | {"anggota_keluarga", "reject_reason"}
    assert client.get("/v1/structuring/jobs/QC_1", headers=auth).status_code == 404


def test_the_answer_is_what_a_job_stores_as_its_result(client, auth):
    direct = _post(client, auth, ocr=OCR).json()["data"]
    job = {
        "request_id": "QC_2",
        "document_type": "kk",
        "ocr": OCR,
        "pipeline_name_sequence": ["extraction", "structuring"],
    }
    assert client.post("/v1/structuring/jobs", headers=auth, json=job).status_code == 202

    assert wait_for_job(client, "/v1/structuring/jobs/QC_2")["result"] == direct


def test_an_ocr_result_without_boxes_is_answered_200_with_its_rejection(client, auth):
    """The validity gate's first rule, not a 400: the same as the job path, where it is a DONE job."""
    response = _post(client, auth, ocr={"texts": []})

    assert response.status_code == 200
    assert response.json()["data"]["reject_reason"] == NO_TEXT


def test_another_document_type_is_400(client, auth):
    response = _post(client, auth, document_type="ktp", ocr=OCR)

    assert response.status_code == 400
    assert response.json()["message"] == "Unsupported document_type: ktp. Supported: ['kk']"


@pytest.mark.parametrize("body", [{}, {"request_id": "QC_3"}])
def test_a_body_without_ocr_is_422(client, auth, body):
    response = client.post("/v1/structuring-direct", headers=auth, json=body)

    assert response.status_code == 422
    assert response.json()["errors"] == "VALIDATION_ERROR"


def test_requires_the_api_key(client):
    assert client.post("/v1/structuring-direct", json={"ocr": OCR}).status_code == 401
