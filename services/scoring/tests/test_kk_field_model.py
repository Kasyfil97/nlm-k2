"""The `kk_field` trust model against its real artifact.

As with `calibrated`, the numbers are not pinned here -- retraining moves them; that they equal the
training code's is checked on the corpus by `scripts/check_kk_model_parity.py`. What is pinned is what
can be wrong while the numbers still look plausible:

  * a field scored under the wrong name (the model knows `no_kk` and `pekerjaan`, not the internal names),
  * a member list that drifts by one, handing person 1 person 2's confidence,
  * a vector from the other structuring (`kk_regex`) read as if it were this one's,
  * the artifact's columns drifting from what this service builds.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ocr_common.kk import (
    FIELD_FEATURES,
    MEMBER_CELL_FEATURES,
    MODEL_FIELD_NAMES,
    SCORED_DOC_FIELDS,
    SCORED_MEMBER_FIELDS,
)

from app.ml.kk_field import FieldTrustModel

ARTIFACT = Path(__file__).resolve().parent.parent / "weights" / "kk_trust_model.joblib"


def _is_kk_field(path: Path) -> bool:
    import joblib

    return path.exists() and "spec" in joblib.load(path)


pytestmark = pytest.mark.skipif(not _is_kk_field(ARTIFACT), reason=f"{ARTIFACT} bukan artefak `kk_field`")


@pytest.fixture(scope="module")
def model() -> FieldTrustModel:
    return FieldTrustModel(ARTIFACT)


def features(offset: int) -> dict[str, float]:
    """A complete vector, distinct per `offset`; plausible ranges are not what is under test."""
    return {name: round(((offset * 11 + index * 7) % 97) / 97.0, 4) for index, name in enumerate(FIELD_FEATURES)}


def cell(value: str, offset: int, **extra) -> dict:
    return {"value": value, "ocr_conf": 0.99, "crf_conf": 0.97, "features": features(offset), **extra}


def member(offset: int) -> dict:
    cells = {name: cell(f"NILAI {name.upper()}", offset * 20 + i) for i, name in enumerate(SCORED_MEMBER_FIELDS)}
    cells["nik"] = cell("9908680101601956", offset * 20 + 1)
    return cells


def payload(*members: dict, **structuring) -> dict:
    document = {
        "nomor_kk": cell("9924187486671285", 1, crf_conf=None),
        "nama_kepala_keluarga": cell("BUDI SANTOSO", 2, crf_conf=None),
        "anggota_keluarga": list(members),
        "reject_reason": None,
        **structuring,
    }
    return {"structuring": document, "avg_doc_score": 0.98, "min_doc_score": 0.74, "text_regions_count": 177}


# --- shape ---------------------------------------------------------------------------------


def test_every_scored_field_gets_a_probability(model):
    out = model.predict(payload(member(0), member(1)))
    assert set(out["fields"]) == set(SCORED_DOC_FIELDS)
    assert len(out["anggota_keluarga"]) == 2
    for scores in [out["fields"], *out["anggota_keluarga"]]:
        assert all(isinstance(p, float) and 0.0 <= p <= 1.0 for p in scores.values()), scores
    assert out["model"] == "kk-trust-s11_blend_lr_hgb"


def test_the_member_list_is_positionally_aligned(model):
    assert model.predict(payload())["anggota_keluarga"] == []
    assert len(model.predict(payload(member(0), member(1), member(2)))["anggota_keluarga"]) == 3


def test_changing_one_member_does_not_move_another(model):
    first = model.predict(payload(member(0), member(1)))
    second = model.predict(payload(member(0), member(7)))
    assert first["anggota_keluarga"][0] == second["anggota_keluarga"][0]
    assert first["anggota_keluarga"][1] != second["anggota_keluarga"][1]


# --- what is not scored --------------------------------------------------------------------


def test_an_empty_value_is_unscored(model):
    orang = member(0)
    orang["ayah"] = {"value": "", "ocr_conf": None, "crf_conf": None, "features": None}
    out = model.predict(payload(orang))
    assert out["anggota_keluarga"][0]["ayah"] is None
    assert out["anggota_keluarga"][0]["ibu"] is not None


def test_a_kk_regex_vector_is_unscored_not_misread(model, caplog):
    """The other structuring's 46 features are not this model's 42: pairing `kk_field` with `kk_regex`
    must ask a human for every field, not produce numbers from the wrong inputs."""
    orang = member(0)
    orang["nik"] = {**orang["nik"], "features": dict.fromkeys(MEMBER_CELL_FEATURES, 0.5)}
    with caplog.at_level(logging.WARNING):
        out = model.predict(payload(orang))
    assert out["anggota_keluarga"][0]["nik"] is None
    assert "nik" in caplog.text


def test_a_partial_or_non_numeric_vector_is_unscored(model):
    orang = member(0)
    partial = features(0)
    partial.pop("xname_exact")
    orang["nik"] = {**orang["nik"], "features": partial}
    orang["ibu"] = {**orang["ibu"], "features": {**features(3), "cpw_rel": None}}
    out = model.predict(payload(orang))
    assert out["anggota_keluarga"][0]["nik"] is None
    assert out["anggota_keluarga"][0]["ibu"] is None
    assert out["anggota_keluarga"][0]["ayah"] is not None


# --- names and inputs ----------------------------------------------------------------------


def test_fields_are_scored_under_the_names_the_model_was_trained_with(model):
    """`nomor_kk` is `no_kk` to the model, `jenis_pekerjaan` is `pekerjaan`: the one-hot and the text
    model are built from the trained name. Scoring a row by hand under that name gives the same number."""
    document = payload(member(0))["structuring"]
    out = model.predict({"structuring": document})
    for internal, slot in (("nomor_kk", None), ("jenis_pekerjaan", 0)):
        source = document[internal] if slot is None else document["anggota_keluarga"][slot][internal]
        row = {**source["features"], "field": MODEL_FIELD_NAMES[internal], "value": source["value"]}
        expected = round(float(model._predict([row])[0]), 4)
        got = out["fields"][internal] if slot is None else out["anggota_keluarga"][slot][internal]
        assert got == expected, internal
    assert MODEL_FIELD_NAMES["nomor_kk"] == "no_kk" and MODEL_FIELD_NAMES["jenis_pekerjaan"] == "pekerjaan"


def test_the_field_and_the_value_both_reach_the_model(model):
    vector = features(5)
    as_nik = {**vector, "field": "nik", "value": "9908680101601956"}
    as_ibu = {**vector, "field": "ibu", "value": "9908680101601956"}
    other_value = {**vector, "field": "nik", "value": "99086801016019"}
    p_nik, p_ibu, p_other = model._predict([as_nik, as_ibu, other_value])
    assert p_nik != p_ibu
    assert p_nik != p_other


# --- thresholds ----------------------------------------------------------------------------


def test_the_model_reports_one_threshold_per_scored_field(model):
    thresholds = model.predict(payload(member(0)))["thresholds"]
    assert set(thresholds) == set(SCORED_DOC_FIELDS) | set(SCORED_MEMBER_FIELDS)
    assert all(0.5 < t < 1.0 for t in thresholds.values())


def test_the_projection_decides_with_the_requests_thresholds_only(model):
    from ocr_common.kk import CONTRACT_MEMBER_FIELDS, contract_fields, parse_column_thresholds

    document = payload(member(0), member(1))["structuring"]
    scoring = model.predict({"structuring": document})
    data = contract_fields(document, scoring)
    assert len(data["anggota_keluarga"]) == 2
    for orang, scores in zip(data["anggota_keluarga"], scoring["anggota_keluarga"], strict=True):
        assert set(orang) == set(CONTRACT_MEMBER_FIELDS)
        assert orang["nik"]["confidence"] == scores["nik"], "no threshold: the probability as it is"
    nik = scoring["thresholds"]["nik"]
    decided = contract_fields(document, scoring, {"nik": nik})
    for orang, scores in zip(decided["anggota_keluarga"], scoring["anggota_keluarga"], strict=True):
        assert orang["nik"]["confidence"] == int(scores["nik"] >= nik)
    everything = contract_fields(document, scoring, parse_column_thresholds({"all_field": 0.0}))
    assert all(c["confidence"] == 1 for orang in everything["anggota_keluarga"] for c in orang.values())


# --- the artifact --------------------------------------------------------------------------


def test_columns_that_drift_from_this_service_refuse_to_load(tmp_path):
    import joblib

    artefak = joblib.load(ARTIFACT)
    artefak["est"]["parts"][0]["names"] = artefak["est"]["parts"][0]["names"][:-1]
    path = tmp_path / "drifted.joblib"
    joblib.dump(artefak, path)
    with pytest.raises(RuntimeError, match="columns"):
        FieldTrustModel(path)


def test_a_calibrated_artifact_is_refused(tmp_path):
    import joblib

    path = tmp_path / "calibrated.joblib"
    joblib.dump({"versi": "kk-trust-hgb-isotonic-v2", "member": {}}, path)
    with pytest.raises(RuntimeError, match="not a kk_field export"):
        FieldTrustModel(path)


def test_kk_field_is_the_default_backend():
    from app.config import Settings
    from app.dependencies import TRUST_MODEL_BACKENDS

    assert Settings.model_fields["scoring_backend"].default == "kk_field"
    assert Settings.model_fields["scoring_model_path"].default == Path("weights/kk_trust_model.joblib")
    assert "kk_field" in TRUST_MODEL_BACKENDS


# --- through HTTP --------------------------------------------------------------------------


def full_document() -> dict:
    """The complete §7.3 object the HTTP schema validates, its nine scored fields carrying vectors."""
    from tests.test_kk_scoring import member as base_member
    from tests.test_kk_scoring import structuring as base_structuring

    document = base_structuring(base_member("BUDI SANTOSO", "9908680101601956"))
    for offset, name in enumerate(SCORED_DOC_FIELDS):
        document[name] = {**document[name], "features": features(offset)}
    for index, orang in enumerate(document["anggota_keluarga"]):
        for offset, name in enumerate(SCORED_MEMBER_FIELDS):
            orang[name] = {**orang[name], "features": features(index * 20 + offset)}
    return document


def test_the_sync_endpoint_serves_the_kk_field_model(client, auth, model):
    """The result has to pass ScoringPayload, not only exist as a dict: `bin_edges` is absent here."""
    from app.dependencies import get_confidence_service
    from app.main import app as fastapi_app
    from app.services.confidence_service import ConfidenceService

    fastapi_app.dependency_overrides[get_confidence_service] = lambda: ConfidenceService(model)
    try:
        response = client.post(
            "/v1/scoring/confidence",
            headers=auth,
            json={"structuring": full_document(), "avg_doc_score": 0.98},
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_confidence_service, None)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["model"] == "kk-trust-s11_blend_lr_hgb"
    assert set(data["thresholds"]) == set(SCORED_DOC_FIELDS) | set(SCORED_MEMBER_FIELDS)
    assert all(isinstance(v, float) for v in data["anggota_keluarga"][0].values())


def test_the_job_stores_the_scores_thresholds_and_decisions(client, auth, model):
    from ocr_common.testing import wait_for_job

    from app.dependencies import get_job_service, get_pipeline, get_results
    from app.main import app as fastapi_app
    from app.services.confidence_service import ConfidenceService
    from app.services.job_service import ScoringJobService

    fastapi_app.dependency_overrides[get_job_service] = lambda: ScoringJobService(
        get_pipeline(), ConfidenceService(model), results=get_results()
    )
    try:
        submitted = client.post(
            "/v1/scoring/jobs",
            headers=auth,
            json={"request_id": "REQ_kk_field", "document_type": "kk", "structuring": full_document()},
        )
        assert submitted.status_code == 202, submitted.text
        result = wait_for_job(client, "/v1/scoring/jobs/REQ_kk_field")["result"]
    finally:
        fastapi_app.dependency_overrides.pop(get_job_service, None)

    assert result["model"] == "kk-trust-s11_blend_lr_hgb"
    assert result["thresholds"]["nik"] > 0.5, "kept with the scores, for reference"
    nik = result["decisions"]["anggota_keluarga"][0]["nik"]
    assert nik["threshold"] is None, "the request sent none"
    assert nik["confidence"] == result["anggota_keluarga"][0]["nik"]
