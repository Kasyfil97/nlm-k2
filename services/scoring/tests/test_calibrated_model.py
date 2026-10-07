"""The `calibrated` trust model against its real artifact.

What these tests are for. The numbers themselves are not pinned -- retraining is expected to move
them, and a test that froze them would only make retraining annoying. What is pinned is everything
that can be wrong while the numbers still look plausible:

  * a feature vector assembled into the wrong column order,
  * the one-hot field column dropped, which makes every field share one calibrator,
  * a member list that drifts by one, handing person 1 person 2's confidence,
  * a missing vector quietly scored as if it were zeros instead of left unscored.

Each of those produces output that passes a smoke test and is wrong.
"""

import os
from pathlib import Path

import pytest

from ocr_common.kk import (
    DOC_CELL_FEATURES,
    MEMBER_CELL_FEATURES,
    SCORED_DOC_FIELDS,
    SCORED_MEMBER_FIELDS,
)

from app.ml.calibrated import CalibratedTrustModel

# `weights/kk_trust_model.joblib` kini artefak `kk_field` (s11); artefak `calibrated` (v2, pasangan
# `kk_regex`) ada di riwayat git. Arahkan ke sana untuk menjalankan uji ini:
#   git show 38598f2:services/scoring/weights/kk_trust_model.joblib > /tmp/calibrated.joblib
#   CALIBRATED_TRUST_MODEL=/tmp/calibrated.joblib pytest tests/test_calibrated_model.py
ARTIFACT = Path(
    os.environ.get("CALIBRATED_TRUST_MODEL")
    or Path(__file__).resolve().parent.parent / "weights" / "kk_trust_model.joblib"
)


def _is_calibrated(path: Path) -> bool:
    import joblib

    return path.exists() and "member" in joblib.load(path)


pytestmark = pytest.mark.skipif(
    not _is_calibrated(ARTIFACT), reason=f"{ARTIFACT} bukan artefak `calibrated` (set CALIBRATED_TRUST_MODEL)"
)


@pytest.fixture(scope="module")
def model() -> CalibratedTrustModel:
    return CalibratedTrustModel(ARTIFACT)


def features(names, offset: int) -> dict[str, float]:
    """A complete vector, distinct per `offset`. Completeness is what the model needs; realism is
    not what these tests are checking."""
    return {name: round(((offset * 11 + index * 7) % 97) / 97.0, 4) for index, name in enumerate(names)}


def field(value: str, offset: int, names=MEMBER_CELL_FEATURES, **extra) -> dict:
    return {
        "value": value,
        "ocr_conf": 0.99,
        "crf_conf": 0.97,
        "features": features(names, offset),
        **extra,
    }


def member(offset: int) -> dict:
    cells = {name: field(f"NILAI {name}", offset * 20 + position) for position, name in enumerate(SCORED_MEMBER_FIELDS)}
    cells["nik"] = field("9908680101601956", offset * 20 + 1)
    return cells


def payload(*members: dict, **overrides) -> dict:
    structuring = {
        "nomor_kk": field("9924187486671285", 1, DOC_CELL_FEATURES),
        "nama_kepala_keluarga": field("BUDI SANTOSO", 2, DOC_CELL_FEATURES),
        "anggota_keluarga": list(members),
        "reject_reason": None,
    }
    structuring.update(overrides.pop("structuring", {}))
    return {
        "structuring": structuring,
        "guardrail_probability": 0.0287,
        "guardrail_verdict": "accepted",
        "avg_doc_score": 0.9813,
        "min_doc_score": 0.7441,
        "text_regions_count": 177,
        **overrides,
    }


# --- shape ---------------------------------------------------------------------------------


def test_every_scored_field_gets_a_probability(model):
    result = model.predict(payload(member(1)))
    assert set(result["fields"]) == set(SCORED_DOC_FIELDS)
    assert set(result["anggota_keluarga"][0]) == set(SCORED_MEMBER_FIELDS)
    for score in [*result["fields"].values(), *result["anggota_keluarga"][0].values()]:
        assert isinstance(score, float) and 0.0 <= score <= 1.0


def test_the_member_list_is_positionally_aligned(model):
    """`kk.contract_fields` raises on a length mismatch, so a model that dropped an unscoreable
    member would turn every such document into a 500 rather than a lower confidence."""
    for count in (0, 1, 3):
        result = model.predict(payload(*[member(index + 1) for index in range(count)]))
        assert len(result["anggota_keluarga"]) == count


def test_the_model_reports_its_own_thresholds_and_bin_edges(model):
    """They travel with the scores because they belong to the trained model, not to the deployment:
    that is what makes the orchestrator's `auto` identical to this stage's without an env var."""
    result = model.predict(payload(member(1)))
    assert result["thresholds"], "tanpa ambang, seluruh gerbang jatuh ke satu angka global"
    assert set(result["thresholds"]) <= {*SCORED_DOC_FIELDS, *SCORED_MEMBER_FIELDS}
    assert all(0.0 < value <= 1.0 for value in result["thresholds"].values())

    edges = result["bin_edges"]["anggota_keluarga"]
    assert edges == sorted(edges) and len(edges) >= 2
    assert edges[-1] == 1.0, "bin teratas harus mencakup 1.0"


def test_a_field_without_a_request_threshold_is_its_probability(model):
    """`nomor_kk` (dan di v2 `nama_lengkap`) tidak punya ambang milik model: di data uji tidak ada titik
    yang di atasnya semua sel benar. Ambang model tidak lagi memutuskan: tanpa `column_confidence_threshold`
    dari request, `confidence` adalah probabilitasnya apa adanya; dengan ambang dari request, 0/1.
    """
    from ocr_common.kk import contract_fields

    from tests.test_kk_scoring import member as base_member
    from tests.test_kk_scoring import structuring as base_structuring

    tanpa = [name for name in (*SCORED_DOC_FIELDS, *SCORED_MEMBER_FIELDS) if name not in model.thresholds]
    assert "nomor_kk" in tanpa
    document = base_structuring(base_member("BUDI SANTOSO", "9908680101601956"))
    yakin = {
        "fields": dict.fromkeys(SCORED_DOC_FIELDS, 0.99),
        "anggota_keluarga": [dict.fromkeys(SCORED_MEMBER_FIELDS, 0.99)],
        "thresholds": model.thresholds,
    }
    data = contract_fields(document, yakin)
    assert data["no_kk"]["confidence"] == 0.99, "tanpa ambang dari request: probabilitasnya"
    assert contract_fields(document, yakin, {"no_kk": 0.9})["no_kk"]["confidence"] == 1
    assert contract_fields(document, yakin, {"no_kk": 0.995})["no_kk"]["confidence"] == 0


# --- the four ways this can be wrong while still looking right ------------------------------


def test_a_value_without_a_feature_vector_is_unscored_not_guessed(model):
    """Ini keadaan ketika structuring belum memancarkan `features`.

    Mengarang angka di situ adalah satu-satunya kegagalan yang tahap ini ada untuk mencegah:
    confidence yang terlihat berwibawa dan dihitung dari apa pun.
    """
    tanpa = {**member(1), "ayah": {"value": "SUTRISNO", "ocr_conf": 0.99, "crf_conf": 0.97}}
    result = model.predict(payload(tanpa))
    assert result["anggota_keluarga"][0]["ayah"] is None
    assert result["anggota_keluarga"][0]["ibu"] is not None, "sel lain tidak terpengaruh"


def test_an_empty_value_is_unscored(model):
    kosong = {**member(1), "ibu": {"value": "", "ocr_conf": None, "crf_conf": None, "features": None}}
    result = model.predict(payload(kosong))
    assert result["anggota_keluarga"][0]["ibu"] is None


def test_each_field_is_scored_by_its_own_calibrator(model, monkeypatch):
    """Vektor yang sama persis di bawah nama field yang berbeda harus memberi angka yang berbeda.

    Kolom one-hot field dibangun ulang dari nama field, bukan dari data yang kebetulan ada. Kalau
    kolom itu hilang, seluruh field berbagi satu kalibrator -- dan base rate-nya jauh berbeda
    (nama_lengkap 79% benar, status_hubungan 96%), jadi angkanya salah untuk hampir semuanya.

    Diuji sebelum isotonic: kalibratornya berupa anak tangga, jadi dua masukan berbeda boleh jatuh
    di anak tangga yang sama. Yang harus berbeda adalah keluaran model di bawahnya.
    """
    monkeypatch.setitem(model._member, "isotonic", _Identitas())
    sama = {name: field("NILAI SAMA", 7) for name in SCORED_MEMBER_FIELDS}
    skor = model.predict(payload(sama))["anggota_keluarga"][0]
    assert len(set(skor.values())) > 1, "satu vektor, tujuh field: angkanya tidak boleh seragam"


class _Identitas:
    """Kalibrator yang tidak mengubah apa pun, untuk melihat keluaran model mentah."""

    def predict(self, x):
        return x


def test_changing_one_member_does_not_move_another(model):
    """Satu baris matriks per sel. Kalau perakitannya bocor antar baris, mengubah satu anggota
    menggeser tetangganya -- dan itu tidak akan terlihat pada dokumen satu anggota."""
    dasar = model.predict(payload(member(1), member(2)))["anggota_keluarga"]
    diubah = model.predict(payload(member(1), member(9)))["anggota_keluarga"]
    assert diubah[0] == dasar[0], "anggota pertama tidak disentuh"
    assert diubah[1] != dasar[1], "anggota kedua memang berubah"


def test_the_column_order_of_the_artifact_is_what_is_used(model):
    """Urutan kunci pada `features` tidak boleh berpengaruh: artefak membawa urutan kolomnya sendiri
    dan mencari tiap nama. Kalau perakitannya mengandalkan urutan dict, membalik kunci akan
    menggeser seluruh vektor -- dan tetap menghasilkan angka yang kelihatan wajar."""
    lurus = member(1)
    terbalik = {
        name: {**cell, "features": dict(reversed(list(cell["features"].items())))} for name, cell in lurus.items()
    }
    assert model.predict(payload(lurus)) == model.predict(payload(terbalik))


def test_a_partial_vector_still_scores_and_says_which_names_were_missing(model, caplog):
    """Sebagian fitur hilang -> NaN, yang memang cara model dilatih membaca nilai hilang. Tapi
    diamnya bukan pilihan: nama yang hilang harus muncul di log."""
    kurang = dict(member(1))
    potong = dict(kurang["pendidikan"]["features"])
    potong.pop(MEMBER_CELL_FEATURES[0])
    kurang["pendidikan"] = {**kurang["pendidikan"], "features": potong}
    with caplog.at_level("WARNING"):
        result = model.predict(payload(kurang))
    assert result["anggota_keluarga"][0]["pendidikan"] is not None
    assert MEMBER_CELL_FEATURES[0] in caplog.text


def test_document_fields_use_the_ocr_aggregates_from_the_payload(model):
    """Empat fitur field dokumen tidak datang dari structuring -- ia tidak punya keempatnya.
    `avg_doc_score` dan kawan-kawan masuk dari payload tahap ini, jadi mengubahnya harus
    menggerakkan angkanya; kalau tidak, keempatnya tidak pernah sampai ke model."""
    dasar = model.predict(payload(member(1)))["fields"]
    lain = model.predict(payload(member(1), avg_doc_score=0.41, min_doc_score=0.08))["fields"]
    assert lain != dasar


# --- the contract between structuring and this model ----------------------------------------


def test_the_artifact_needs_exactly_what_the_contract_promises(model):
    """Nama fitur di `ocr_common.kk` adalah kewajiban structuring; kolom di artefak adalah apa yang
    model sungguh-sungguh baca. Kalau keduanya menyimpang, salah satu dari dua hal terjadi diam-diam:
    structuring memancarkan fitur yang tidak dipakai siapa pun, atau model membaca fitur yang tidak
    pernah dikirim -- dan yang kedua menjadi NaN, bukan galat.

    Inilah uji yang membuat penyimpangan itu terlihat pada hari ia mendarat, bukan pada hari
    angkanya ternyata aneh.
    """
    from app.ml.calibrated import DOC_FROM_FIELD, DOC_FROM_PAYLOAD, MEMBER_FROM_FIELD

    member_kolom = {c for c in model._member["kolom"] if not c.startswith("f_")}
    assert member_kolom == set(MEMBER_CELL_FEATURES) | set(MEMBER_FROM_FIELD)

    doc_kolom = {c for c in model._doc["kolom"] if not c.startswith("f_")}
    diketahui = set(DOC_CELL_FEATURES) | set(DOC_FROM_FIELD) | set(DOC_FROM_PAYLOAD)
    assert doc_kolom <= diketahui, f"artefak membaca fitur yang tak ada yang kirim: {doc_kolom - diketahui}"
    # `guardrail_probability` sengaja TIDAK ada di artefak: korpus GT tidak punya hasil guardrails,
    # jadi model tidak pernah bisa belajar darinya dan dibuang saat latih. Begitu guardrails masuk
    # GT, model harus DILATIH ULANG -- bukan sekadar diberi kolomnya.
    assert "guardrail_probability" not in doc_kolom


def test_the_one_hot_columns_cover_every_scored_field(model):
    """Satu kolom one-hot per field, dibangun dari daftar tetap. Field yang tidak muncul di sebuah
    dokumen harus tetap menghasilkan kolom nol; apa pun yang menurunkan kolom dari data yang
    kebetulan ada akan menggeser seluruh vektor tanpa suara."""
    assert {c[2:] for c in model._member["kolom"] if c.startswith("f_")} == set(SCORED_MEMBER_FIELDS)
    assert {c[2:] for c in model._doc["kolom"] if c.startswith("f_")} == set(SCORED_DOC_FIELDS)


# --- the whole flow, through HTTP and on to the outgoing contract ----------------------------


def _with_features(document: dict) -> dict:
    """The §7.3 structuring result as the ported parser will send it: every scored field carrying
    its vector, every other field carrying none."""
    out = dict(document)
    for position, name in enumerate(SCORED_DOC_FIELDS):
        out[name] = {**out[name], "features": features(DOC_CELL_FEATURES, position + 1)}
    out["anggota_keluarga"] = [
        {
            name: (
                {**cell, "features": features(MEMBER_CELL_FEATURES, index * 20 + position)}
                if name in SCORED_MEMBER_FIELDS
                else cell
            )
            for position, (name, cell) in enumerate(orang.items())
        }
        for index, orang in enumerate(out["anggota_keluarga"])
    ]
    return out


def test_the_sync_endpoint_serves_the_calibrated_model(client, auth, model):
    """§8.4, jalur yang dipakai tim ML. Diuji lewat HTTP karena di situlah bentuknya divalidasi:
    `thresholds` dan `bin_edges` harus lolos ScoringPayload, bukan hanya ada di dict Python."""
    from app.dependencies import get_confidence_service
    from app.main import app as fastapi_app
    from app.services.confidence_service import ConfidenceService
    from tests.test_kk_scoring import member as base_member
    from tests.test_kk_scoring import structuring as base_structuring

    document = _with_features(base_structuring(base_member("BUDI SANTOSO", "9908680101601956")))
    fastapi_app.dependency_overrides[get_confidence_service] = lambda: ConfidenceService(model)
    try:
        response = client.post(
            "/v1/scoring/confidence",
            headers=auth,
            json={"structuring": document, "avg_doc_score": 0.98, "min_doc_score": 0.74, "text_regions_count": 177},
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_confidence_service, None)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["model"].startswith("kk-trust-")
    assert data["thresholds"] and data["bin_edges"]["anggota_keluarga"][-1] == 1.0
    assert all(isinstance(v, float) for v in data["anggota_keluarga"][0].values())


def test_the_projection_turns_the_scores_into_the_outgoing_contract(model):
    """Ujung ke ujung sampai bentuk yang keluar: hasil model -> `contract_fields` -> sembilan field
    `{value, confidence 0|1}`, diputuskan dengan ambang dari request."""
    from ocr_common.kk import CONTRACT_MEMBER_FIELDS, contract_fields, parse_column_thresholds

    from tests.test_kk_scoring import member as base_member
    from tests.test_kk_scoring import structuring as base_structuring

    document = _with_features(
        base_structuring(
            base_member("BUDI SANTOSO", "9908680101601956"),
            base_member("SITI NURHALIZA", "9908114806713444"),
        )
    )
    scoring = model.predict({"structuring": document, "avg_doc_score": 0.98, "min_doc_score": 0.74})
    data = contract_fields(document, scoring, parse_column_thresholds({"all_field": 0.5}))

    assert set(data) == {"no_kk", "nama_kepala_keluarga", "anggota_keluarga"}
    assert len(data["anggota_keluarga"]) == 2
    for orang in data["anggota_keluarga"]:
        assert set(orang) == set(CONTRACT_MEMBER_FIELDS)
        for name, cell in orang.items():
            assert set(cell) == {"value", "confidence"}, name
            assert cell["confidence"] in (0, 1)

    # `confidence` mengikuti ambang dari request: dengan 0.0 semua field bernilai lolos, dan uji ini tidak
    # akan membuktikan apa pun kalau angkanya diabaikan.
    longgar = contract_fields(document, scoring, parse_column_thresholds({"all_field": 0.0}))
    assert all(cell["confidence"] == 1 for orang in longgar["anggota_keluarga"] for cell in orang.values())
    # Tanpa ambang: probabilitasnya, float.
    mentah = contract_fields(document, scoring)
    for orang, skor in zip(mentah["anggota_keluarga"], scoring["anggota_keluarga"], strict=True):
        assert orang["nik"]["confidence"] == skor["nik"]


def test_the_job_stores_the_thresholds_with_the_scores(client, auth, model):
    """§8.5: baris outcome dan respons `extract-ocr` harus identik untuk satu request.

    Ambang dan tepi bin ikut DISIMPAN, tidak hanya dipakai sekali: orchestrator merakit `data` dari
    baris hasil ini lewat `contract_fields`, jadi menyimpan angka milik model bersama skornya
    membuat kesamaan itu terjadi secara konstruksi. Yang hilang di sini akan muncul nanti sebagai
    `confidence` 0/1 yang berbeda antara dua pembaca hasil yang sama.
    """
    from ocr_common.testing import wait_for_job

    from app.dependencies import get_job_service, get_pipeline, get_results
    from app.main import app as fastapi_app
    from app.services.confidence_service import ConfidenceService
    from app.services.job_service import ScoringJobService
    from tests.test_kk_scoring import member as base_member
    from tests.test_kk_scoring import structuring as base_structuring

    document = _with_features(base_structuring(base_member("BUDI SANTOSO", "9908680101601956")))
    fastapi_app.dependency_overrides[get_job_service] = lambda: ScoringJobService(
        get_pipeline(), ConfidenceService(model), results=get_results()
    )
    try:
        submitted = client.post(
            "/v1/scoring/jobs",
            headers=auth,
            json={"request_id": "REQ_calibrated", "document_type": "kk", "structuring": document},
        )
        assert submitted.status_code == 202
        result = wait_for_job(client, "/v1/scoring/jobs/REQ_calibrated")["result"]
    finally:
        fastapi_app.dependency_overrides.pop(get_job_service, None)

    assert result["model"].startswith("kk-trust-")
    assert result["thresholds"]["nik"] > 0
    assert result["bin_edges"]["anggota_keluarga"][-1] == 1.0
    assert result["payload"]["structuring"], "jejak audit tetap disimpan"
