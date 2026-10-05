"""`POST /v1/scoring-direct`: the trust model on the structuring service's output (plus the OCR and guardrails
results it depends on), synchronous, for testing this stage on its own. Nothing is recorded. Ported from nilam."""

import pytest

from ocr_common.kk import CONTRACT_DOC_FIELDS, CONTRACT_MEMBER_FIELDS, SCORED_DOC_FIELDS, SCORED_MEMBER_FIELDS

from tests.test_kk_scoring import member, structuring

GUARDRAILS = {
    "passed": True,
    "reason": None,
    "document": {"verdict": "accepted", "confidence": 0.97, "probability_bad": 0.03},
}
OCR = {"texts": [], "text_regions_count": 177, "avg_doc_score": 0.814, "min_doc_score": 0.2822}
STRUCTURING = structuring(member("BUDI SANTOSO", "9908680101601956"), member("SITI AMINAH", "9908684101651957"))


def _post(client, auth, **body):
    return client.post("/v1/scoring-direct", headers=auth, json=body)


def test_the_structuring_output_is_scored_now_and_nothing_is_recorded(client, auth):
    response = _post(client, auth, request_id="QC_1", guardrails=GUARDRAILS, ocr=OCR, structuring=STRUCTURING)

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"] == "QC_1"
    result = body["data"]
    assert set(result["fields"]) == set(SCORED_DOC_FIELDS)
    assert [set(row) for row in result["anggota_keluarga"]] == [set(SCORED_MEMBER_FIELDS)] * 2
    decisions = result["decisions"]
    assert set(decisions) == {*CONTRACT_DOC_FIELDS, "anggota_keluarga"}
    assert [set(row) for row in decisions["anggota_keluarga"]] == [set(CONTRACT_MEMBER_FIELDS)] * 2
    payload = result["payload"]
    assert payload["structuring"]["nomor_kk"]["value"] == STRUCTURING["nomor_kk"]["value"]
    assert (payload["guardrail_probability"], payload["guardrail_verdict"]) == (0.03, "accepted")
    assert (payload["avg_doc_score"], payload["min_doc_score"], payload["text_regions_count"]) == (0.814, 0.2822, 177)

    assert client.get("/v1/scoring/jobs/QC_1", headers=auth).status_code == 404


def test_the_answer_is_what_a_job_stores_as_its_result(client, auth):
    """The same `ConfidenceService.score` runs behind both, so QC sees exactly what the pipeline stores."""
    from ocr_common.testing import wait_for_job

    body = {"guardrails": GUARDRAILS, "ocr": OCR, "structuring": STRUCTURING}
    direct = _post(client, auth, **body).json()["data"]
    assert client.post("/v1/scoring/jobs", headers=auth, json={"request_id": "QC_2", **body}).status_code == 202

    assert wait_for_job(client, "/v1/scoring/jobs/QC_2")["result"] == direct


def test_the_structuring_output_alone_is_enough(client, auth):
    """`ocr` and `guardrails` left out: the inputs they feed are null, the model works with them missing."""
    response = _post(client, auth, structuring=STRUCTURING)

    assert response.status_code == 200
    payload = response.json()["data"]["payload"]
    assert (payload["avg_doc_score"], payload["min_doc_score"], payload["guardrail_probability"]) == (None, None, None)


def test_column_confidence_threshold_decides_the_0_1_confidences(client, auth):
    response = _post(client, auth, structuring=STRUCTURING, column_confidence_threshold={"all_field": 1})

    decisions = response.json()["data"]["decisions"]
    assert (decisions["no_kk"]["confidence"], decisions["no_kk"]["threshold"]) == (0, 1.0)
    assert {row["nik"]["threshold"] for row in decisions["anggota_keluarga"]} == {1.0}


def test_another_document_type_is_400(client, auth):
    response = _post(client, auth, document_type="ktp", structuring=STRUCTURING)

    assert response.status_code == 400
    assert response.json()["message"] == "Unsupported document_type: ktp. Supported: ['kk']"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"structuring": STRUCTURING, "column_confidence_threshold": {"nomor_kk": 0.9}},
    ],
)
def test_a_body_without_structuring_or_with_an_unknown_threshold_key_is_422(client, auth, body):
    response = client.post("/v1/scoring-direct", headers=auth, json=body)

    assert response.status_code == 422
    assert response.json()["errors"] == "VALIDATION_ERROR"


def test_requires_the_api_key(client):
    assert client.post("/v1/scoring-direct", json={"structuring": STRUCTURING}).status_code == 401
