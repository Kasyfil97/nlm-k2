"""The `kk_model` backend: structuring m04, the pair of scoring's `kk_field` trust model.

Two kinds of test. Without a corpus: the adapter maps the model's output onto §7.3 correctly, carries
the full feature vector the trust model reads, keeps the validity gate, and derives its answer from the
boxes. With the corpus (`../raw_ocr_v6`, real KK, kept out of the repository): every document gives
exactly the values the training code gives (`fixtures/kk_model_baseline.json`, hashes only). The
scores half of that baseline needs scoring too and is checked by `scripts/check_kk_model_parity.py`.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from ocr_common.kk import DOC_FIELDS, FIELD_FEATURES, MEMBER_FIELDS, MODEL_FIELD_NAMES
from ocr_common.synthetic_kk import household, nomor_kk

from app.ml.kk_model import KKModelStructurer
from app.ml.validity import NO_TEXT, NOT_A_KK
from app.services.structuring_service import StructuringService
from tests.kk_boxes import box, kk_boxes

SERVICE = Path(__file__).resolve().parent.parent
ARTIFACT = SERVICE / "weights" / "kk_structuring_model.joblib"
BASELINE = Path(__file__).resolve().parent / "fixtures" / "kk_model_baseline.json"
REPO_ROOT = SERVICE.parent.parent
CORPUS_DIR = REPO_ROOT.parent / "raw_ocr_v6"

pytestmark = pytest.mark.skipif(not ARTIFACT.exists(), reason="weights/kk_structuring_model.joblib tidak ada")

SCORED = [name for name in MODEL_FIELD_NAMES if name not in ("nomor_kk", "nama_kepala_keluarga")]


@pytest.fixture(scope="module")
def structurer() -> KKModelStructurer:
    return KKModelStructurer(ARTIFACT)


@pytest.fixture(scope="module")
def service(structurer) -> StructuringService:
    return StructuringService(structurer)


# --- reading a card ---------------------------------------------------------------------------


def test_it_reads_the_card_it_is_given(service):
    people = household(2, seed=0)
    result = service.structure(kk_boxes(people))

    assert result["reject_reason"] is None
    assert result["nomor_kk"]["value"] == nomor_kk(0)
    assert result["nama_kepala_keluarga"]["value"] == people[0].nama_lengkap
    assert [m["nama_lengkap"]["value"] for m in result["anggota_keluarga"]] == [p.nama_lengkap for p in people]
    assert [m["nik"]["value"] for m in result["anggota_keluarga"]] == [p.nik for p in people]


def test_a_different_card_gives_a_different_answer(service):
    """Provenance: the answer comes from the boxes, not from anything the backend carries."""
    one = service.structure(kk_boxes(seed=0))
    two = service.structure(kk_boxes(seed=1))
    assert one["nomor_kk"]["value"] != two["nomor_kk"]["value"]
    assert one["anggota_keluarga"][0]["nik"]["value"] != two["anggota_keluarga"][0]["nik"]["value"]


def test_the_shape_is_the_complete_7_3_object(service):
    """Exactly the nine fields the model reads -- no keys for the rest of the card."""
    result = service.structure(kk_boxes())
    assert set(result) == set(DOC_FIELDS) | {"anggota_keluarga", "reject_reason"}
    for member in result["anggota_keluarga"]:
        assert set(member) == set(MEMBER_FIELDS)


# --- what scoring reads -----------------------------------------------------------------------


def test_every_filled_scored_field_carries_the_full_finite_vector(service):
    result = service.structure(kk_boxes())
    cells = [result["nomor_kk"], result["nama_kepala_keluarga"]]
    cells += [member[name] for member in result["anggota_keluarga"] for name in SCORED]
    filled = [cell for cell in cells if cell["value"]]
    assert len(filled) >= 10
    for cell in filled:
        assert list(cell["features"]) == list(FIELD_FEATURES)
        assert all(isinstance(v, float) and math.isfinite(v) for v in cell["features"].values())
        assert 0.0 <= cell["ocr_conf"] <= 1.0


def test_crf_conf_is_the_placement_evidence_for_members_only(service):
    """§7.3: document fields never carry `crf_conf`. A member field carries the lowest box-class
    probability, which is what the trust model reads as `lg_struct_min`."""
    result = service.structure(kk_boxes())
    assert result["nomor_kk"]["crf_conf"] is None
    nik = result["anggota_keluarga"][0]["nik"]
    assert 0.0 < nik["crf_conf"] <= 1.0
    assert nik["crf_conf"] == pytest.approx(1 / (1 + math.exp(-nik["features"]["lg_struct_min"])), abs=1e-4)


def test_the_vendored_features_are_the_contract_list():
    from app.vendor.kk_model.field_features import NUM_FEATS

    assert tuple(NUM_FEATS) == FIELD_FEATURES


# --- the validity gate ------------------------------------------------------------------------


def test_no_boxes_is_no_text(service):
    assert service.structure([])["reject_reason"] == NO_TEXT


def test_blank_boxes_only_is_no_text(service):
    blank = [box("", 10.0, 10.0, 60.0), box("  ", 10.0, 40.0, 60.0)]
    assert service.structure(blank)["reject_reason"] == NO_TEXT


def test_a_card_without_its_number_is_not_a_kk(service):
    assert service.structure(kk_boxes(no_kk=""))["reject_reason"] == NOT_A_KK


def test_a_text_that_is_not_a_card_is_not_a_kk(service):
    boxes = [box(text, 20.0, 20.0 + 30 * i) for i, text in enumerate(["STRUK BELANJA", "TOTAL 45.000", "TERIMA KASIH"])]
    assert service.structure(boxes)["reject_reason"] == NOT_A_KK


# --- blank boxes ------------------------------------------------------------------------------


def test_blank_boxes_reach_kk_model_and_no_other_structurer(structurer):
    seen: list[int] = []

    class Spy:
        name = "spy"

        def structure(self, texts):
            seen.append(len(texts))
            return {}

    boxes = [box("A", 0.0, 0.0), box("", 0.0, 20.0)]
    StructuringService(Spy()).structure(boxes)
    assert seen == [1]
    assert structurer.reads_blank_boxes is True


# --- the artifact -----------------------------------------------------------------------------


def test_a_foreign_artifact_is_refused(tmp_path):
    import joblib

    path = tmp_path / "other.joblib"
    joblib.dump({"versi": "kk-trust-hgb-isotonic-v2", "member": {}}, path)
    with pytest.raises(RuntimeError, match="not a kk_model export"):
        KKModelStructurer(path)


def test_the_backend_is_registered_and_reads_its_setting(monkeypatch):
    from app.config import Settings
    from app.dependencies import STRUCTURER_BACKENDS

    monkeypatch.setenv("STRUCTURING_MODEL_PATH", str(ARTIFACT))
    built = STRUCTURER_BACKENDS["kk_model"](Settings())
    assert isinstance(built, KKModelStructurer)
    assert built.version == "kk-structuring-m04_06102026"


# --- the corpus -------------------------------------------------------------------------------


def _parity():
    script = REPO_ROOT / "scripts" / "check_kk_model_parity.py"
    spec = importlib.util.spec_from_file_location("check_kk_model_parity", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not CORPUS_DIR.is_dir(), reason=f"corpus raw_ocr_v6 absent at {CORPUS_DIR} (real KK)")
def test_the_service_reproduces_the_training_values_exactly(service):
    """Nilai dan conf structuring = rantai training, per dokumen. 60 dokumen dari 418 (urutan fixture:
    test dulu); seluruhnya, plus skornya, lewat scripts/check_kk_model_parity.py."""
    values_hash = _parity().values_hash
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))["hashes"]
    checked = 0
    for doc_id, expected in list(baseline.items())[:60]:
        source = CORPUS_DIR / f"{doc_id}.json"
        if not source.is_file():
            continue
        page = json.loads(source.read_text(encoding="utf-8"))["pages"][0]
        result = json.loads(json.dumps(service.structure(service.boxes_from_ocr(page))))
        assert values_hash(result) == expected["values"], doc_id
        checked += 1
    assert checked
