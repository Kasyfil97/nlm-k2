"""Gerbang bentuk fase 0 (R6 butir pertama).

Fixture emas di `fixtures/` adalah artefak, bukan alat bantu: merekalah yang membuktikan tipe dan
skema Pydantic yang dibekukan benar-benar bisa membawa data KK. Uji di sini sengaja menegaskan hal
yang *tidak* boleh ada juga (`fields`, `flag`, `blocks`, `full_text`, `bbox`), karena kegagalan yang
paling mahal bukan bentuk yang salah melainkan bentuk NPWP yang lolos diam-diam.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ocr_common.kk import (
    CONTRACT_DOC_FIELDS,
    CONTRACT_MEMBER_FIELDS,
    DOC_FIELDS,
    DOCUMENT_TYPE,
    MEMBER_FIELDS,
    SCORED_DOC_FIELDS,
    SCORED_MEMBER_FIELDS,
    contract_data,
    contract_fields,
    parse_column_thresholds,
    scored_fields,
)
from ocr_common.pipeline.schemas import GuardrailsResult, OcrPayload, StructuringPayload

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def structuring() -> dict[str, Any]:
    return load("structuring_result.json")


@pytest.fixture
def scoring() -> dict[str, Any]:
    return load("scoring_result.json")


# --- daftar field --------------------------------------------------------------------------


def test_field_lists_match_the_contract():
    """§10 daftar A dan B. Dua daftar yang tidak boleh tertukar."""
    assert DOCUMENT_TYPE == "kk"
    assert len(DOC_FIELDS) == 2
    assert len(MEMBER_FIELDS) == 7
    assert len(CONTRACT_DOC_FIELDS) == 2
    assert len(CONTRACT_MEMBER_FIELDS) == 7
    assert DOC_FIELDS[0] == "nomor_kk", "parser memancarkan nomor_kk, bukan no_kk"
    assert CONTRACT_DOC_FIELDS[0] == "no_kk", "kontrak keluar memakai no_kk"
    assert "status_hubungan_dalam_keluarga" in MEMBER_FIELDS
    assert "status_hubungan_dalam_rumah_tangga" in CONTRACT_MEMBER_FIELDS
    assert "status_hubungan_dalam_rumah_tangga" not in MEMBER_FIELDS
    assert "no_paspor" not in MEMBER_FIELDS and "no_kitap" not in MEMBER_FIELDS


def test_scoring_covers_only_the_nine_contract_fields():
    """§8.3: scoring menilai sembilan field, memakai nama internal."""
    assert len(SCORED_DOC_FIELDS) == 2
    assert len(SCORED_MEMBER_FIELDS) == 7
    assert SCORED_DOC_FIELDS == DOC_FIELDS, "structuring hanya memancarkan yang dinilai scoring"
    assert SCORED_MEMBER_FIELDS == MEMBER_FIELDS
    assert "status_hubungan_dalam_keluarga" in SCORED_MEMBER_FIELDS


# --- §7.3 hasil structuring ----------------------------------------------------------------


def test_structuring_fixture_validates(structuring):
    StructuringPayload.model_validate(structuring)


def test_structuring_is_flat_with_four_top_level_keys(structuring):
    assert set(structuring) == set(DOC_FIELDS) | {"anggota_keluarga", "reject_reason"}
    assert "fields" not in structuring, "§7.3 datar: tidak ada pembungkus `fields`"


def test_structuring_has_no_npwp_era_keys(structuring):
    for gone in ("document_type", "flag", "flag_reason"):
        assert gone not in structuring


def test_every_field_carries_two_scores(structuring):
    for name in DOC_FIELDS:
        assert set(structuring[name]) == {"value", "ocr_conf", "crf_conf"}
        assert structuring[name]["crf_conf"] is None, "field dokumen tidak pernah lewat Viterbi"
    for member in structuring["anggota_keluarga"]:
        assert set(member) == set(MEMBER_FIELDS), "tiap anggota selalu membawa ketujuh kunci"
        for name in MEMBER_FIELDS:
            assert set(member[name]) == {"value", "ocr_conf", "crf_conf"}


def test_value_is_never_null(structuring):
    def check(field: dict[str, Any]) -> None:
        assert isinstance(field["value"], str)

    for name in DOC_FIELDS:
        check(structuring[name])
    for member in structuring["anggota_keluarga"]:
        for name in MEMBER_FIELDS:
            check(member[name])


def test_missing_field_has_empty_value_and_null_scores():
    """Sebuah field kosong tetap ada, dengan value `""` dan kedua skor null; payload menerimanya."""
    empty = {"value": "", "ocr_conf": None, "crf_conf": None}
    member = dict.fromkeys(MEMBER_FIELDS, empty)
    payload = {**dict.fromkeys(DOC_FIELDS, empty), "anggota_keluarga": [member], "reject_reason": None}
    parsed = StructuringPayload.model_validate(payload)
    assert parsed.anggota_keluarga[0].ayah.model_dump(exclude_unset=True) == empty


def test_structuring_payload_requires_the_nine_fields_and_nothing_else(structuring):
    """Sembilan field wajib; field kartu lain yang masih terbawa (hasil lama) diteruskan, tidak ditolak."""
    assert set(StructuringPayload.model_fields) == set(DOC_FIELDS) | {"anggota_keluarga", "reject_reason"}
    without = {key: value for key, value in structuring.items() if key != "nama_kepala_keluarga"}
    with pytest.raises(ValidationError):
        StructuringPayload.model_validate(without)
    StructuringPayload.model_validate({**structuring, "alamat": {"value": "JL. MERDEKA", "ocr_conf": 0.9}})


def test_a_non_finite_feature_travels_as_null(structuring):
    """`kk_model` mengirim fitur NaN/inf sebagai null; payload ke scoring harus menerimanya."""
    cell = {**structuring["nomor_kk"], "features": {"lg_conf": 1.5, "sim_gap": None}}
    StructuringPayload.model_validate({**structuring, "nomor_kk": cell})


# --- §6.1/§7.1 muatan OCR ------------------------------------------------------------------


def test_ocr_fixture_validates():
    OcrPayload.model_validate(load("ocr_payload.json"))


def test_ocr_payload_shape_matches_the_observed_backend():
    """Dibangun dari pengamatan, bukan dari contoh kontrak.

    Lihat docs/decisions/2026-09-26-spike-bentuk-ocr-71.md: poly nyata 4x2 bertipe float dan
    MIRING, jadi kotak tegak `bbox` tidak bisa mewakilinya.
    """
    ocr = load("ocr_payload.json")
    assert set(ocr) >= {
        "engine",
        "model",
        "elapsed_ms",
        "text_regions_count",
        "avg_doc_score",
        "min_doc_score",
        "texts",
    }
    assert "blocks" not in ocr, "§7.1 memakai texts[], bukan blocks[]"
    assert "full_text" not in ocr, "redundan dengan texts[]; tidak ada yang mengonsumsinya"

    for box in ocr["texts"]:
        assert set(box) == {"text", "score", "poly"}
        assert "bbox" not in box and "confidence" not in box
        assert len(box["poly"]) == 4, "poly selalu 4 titik"
        assert all(len(point) == 2 for point in box["poly"])
        assert all(isinstance(c, float) for point in box["poly"] for c in point)

    tilted = ocr["texts"][0]["poly"]
    assert tilted[0][1] != tilted[1][1], "poly nyata miring; fixture harus mempertahankannya"


def test_ocr_aggregates_agree_with_texts():
    ocr = load("ocr_payload.json")
    scores = [box["score"] for box in ocr["texts"]]
    assert ocr["text_regions_count"] == len(scores)
    assert ocr["min_doc_score"] == pytest.approx(min(scores))
    assert ocr["avg_doc_score"] == pytest.approx(sum(scores) / len(scores), abs=1e-3)


# --- §5.2 guardrails -----------------------------------------------------------------------


def test_guardrails_fixture_validates():
    GuardrailsResult.model_validate(load("guardrails_result.json"))


def test_guardrails_has_no_page_concept():
    report = load("guardrails_result.json")
    assert "pages" not in report, "KK selalu satu gambar"
    document = report["document"]
    assert set(document) == {"verdict", "confidence", "probability_bad", "threshold_used"}
    for gone in ("n_pages", "n_approve", "n_reject"):
        assert gone not in document


# --- §3.3.1 proyeksi -----------------------------------------------------------------------


HALF = parse_column_thresholds({"all_field": 0.5})


def test_projection_matches_the_golden_contract_data(structuring, scoring):
    expected = {k: v for k, v in load("contract_data.json").items() if not k.startswith("_")}
    assert contract_fields(structuring, scoring, HALF) == expected


def test_projection_renames_exactly_two_keys(structuring, scoring):
    data = contract_fields(structuring, scoring)
    assert set(data) == {"no_kk", "nama_kepala_keluarga", "anggota_keluarga"}
    assert "nomor_kk" not in data
    for member in data["anggota_keluarga"]:
        assert set(member) == set(CONTRACT_MEMBER_FIELDS)
        assert "status_hubungan_dalam_keluarga" not in member


def _all(data):
    return [data["no_kk"], data["nama_kepala_keluarga"], *(f for m in data["anggota_keluarga"] for f in m.values())]


def test_every_field_is_value_and_a_0_1_confidence_under_a_threshold(structuring, scoring):
    """Seperti nilam: `{value, confidence}` saja, confidence 1 atau 0 bila request memberi ambang."""
    for field in _all(contract_fields(structuring, scoring, HALF)):
        assert set(field) == {"value", "confidence"}
        assert field["confidence"] in (0, 1)


def test_without_a_threshold_confidence_is_the_probability_as_it_is(structuring, scoring):
    """Tanpa `column_confidence_threshold`, confidence adalah probabilitas trust model apa adanya, bukan 0/1."""
    data = contract_fields(structuring, scoring)
    assert data["no_kk"]["confidence"] == 0.9412
    assert data["nama_kepala_keluarga"]["confidence"] == 0.8871
    pertama, kedua = data["anggota_keluarga"]
    assert (pertama["jenis_pekerjaan"]["confidence"], kedua["jenis_pekerjaan"]["confidence"]) == (0.7733, 0.3184)
    assert pertama["status_hubungan_dalam_rumah_tangga"]["confidence"] == 0.9218
    for field in _all(data):
        assert isinstance(field["confidence"], float)


def test_confidence_is_1_from_the_threshold_up_and_0_below(structuring, scoring):
    """Anggota kedua: jenis_pekerjaan diskor 0.3184, di bawah 0.5; anggota pertama 0.7733, di atas."""
    data = contract_fields(structuring, scoring, HALF)
    assert data["anggota_keluarga"][1]["jenis_pekerjaan"]["confidence"] == 0
    assert data["anggota_keluarga"][0]["jenis_pekerjaan"]["confidence"] == 1
    exact = contract_fields(structuring, scoring, {"jenis_pekerjaan": 0.7733})
    assert exact["anggota_keluarga"][0]["jenis_pekerjaan"]["confidence"] == 1


def test_the_models_own_thresholds_no_longer_decide(structuring, scoring):
    """Ambang milik model ikut tersimpan di hasil, tapi hanya request yang memutuskan."""
    scored = {**scoring, "thresholds": {"pendidikan": 0.99, "ayah": 0.80}}
    pertama = contract_fields(structuring, scored)["anggota_keluarga"][0]
    assert (pertama["pendidikan"]["confidence"], pertama["ayah"]["confidence"]) == (0.841, 0.8064)


def test_per_field_only_the_fields_with_a_threshold_are_0_1(structuring, scoring):
    """`column_confidence_threshold` per nama KONTRAK berlaku untuk tiap anggota; field yang tidak ia sebut
    mendapat probabilitasnya sendiri."""
    columns = {"ayah": 0.9, "jenis_pekerjaan": 0.3, "status_hubungan_dalam_rumah_tangga": 0.0}
    data = contract_fields(structuring, scoring, columns)
    pertama, kedua = data["anggota_keluarga"]
    assert pertama["ayah"]["confidence"] == 0, "0.8064 di bawah 0.9 dari request"
    assert (pertama["jenis_pekerjaan"]["confidence"], kedua["jenis_pekerjaan"]["confidence"]) == (1, 1)
    assert pertama["status_hubungan_dalam_rumah_tangga"]["confidence"] == 1, "nama kontrak, bukan nama internal"
    assert pertama["nik"]["confidence"] == 0.9655, "tidak disebut request: probabilitasnya"
    assert data["no_kk"]["confidence"] == 0.9412


def test_all_field_with_a_field_override(structuring, scoring):
    columns = parse_column_thresholds({"all_field": 0.95, "ayah": 0.5})
    pertama = contract_fields(structuring, scoring, columns)["anggota_keluarga"][0]
    assert (pertama["ayah"]["confidence"], pertama["pendidikan"]["confidence"], pertama["nik"]["confidence"]) == (
        1,
        0,
        1,
    )


def test_scored_fields_keep_the_threshold_that_decided(structuring, scoring):
    decided = scored_fields(structuring, scoring, {"no_kk": 0.5})
    assert (decided["no_kk"]["threshold"], decided["no_kk"]["confidence"]) == (0.5, 1)
    nik = decided["anggota_keluarga"][0]["nik"]
    assert nik == {"value": "3273011203850001", "confidence": 0.9655, "threshold": None}
    assert contract_data(decided) == contract_fields(structuring, scoring, {"no_kk": 0.5})


def test_member_scores_are_positional_not_shared(structuring, scoring):
    """Salah geser satu indeks menghasilkan 200 yang tampak benar dengan confidence milik anggota lain,
    jadi skor tiap anggota sengaja dibuat berbeda di fixture."""
    data = contract_fields(structuring, scoring, {"ayah": 0.8})
    first, second = data["anggota_keluarga"]
    assert (first["ayah"]["confidence"], second["ayah"]["confidence"]) == (1, 0), "0.8064 vs 0.7702"
    raw = contract_fields(structuring, scoring)["anggota_keluarga"]
    assert (raw[0]["ayah"]["confidence"], raw[1]["ayah"]["confidence"]) == (0.8064, 0.7702)


def test_length_mismatch_is_an_error_not_a_silent_truncation(structuring, scoring):
    short = {**scoring, "anggota_keluarga": scoring["anggota_keluarga"][:1]}
    with pytest.raises(ValueError, match="anggota_keluarga"):
        contract_fields(structuring, short)


def test_contract_field_value_is_empty_string_never_null(structuring, scoring):
    """§3.3: field yang tidak ditemukan adalah `{"value": "", "confidence": 0}` -- bukan null."""
    blank = {**structuring, "nama_kepala_keluarga": {"value": "", "ocr_conf": None, "crf_conf": None}}
    assert contract_fields(blank, scoring, HALF)["nama_kepala_keluarga"] == {"value": "", "confidence": 0}
    assert contract_fields(blank, scoring)["nama_kepala_keluarga"] == {"value": "", "confidence": 0.0}


def test_null_score_yields_zero_confidence(structuring, scoring):
    unscored = {**scoring, "fields": {**scoring["fields"], "nomor_kk": None}}
    data = contract_fields(structuring, unscored, {"no_kk": 0.0})
    assert data["no_kk"] == {"value": "3273012345678901", "confidence": 0}, "nilainya tetap keluar"
    assert contract_fields(structuring, unscored)["no_kk"] == {"value": "3273012345678901", "confidence": 0.0}


def test_empty_member_list_projects_to_empty_list(structuring, scoring):
    none = {**structuring, "anggota_keluarga": []}
    scored = {**scoring, "anggota_keluarga": []}
    data = contract_fields(none, scored)
    assert data["anggota_keluarga"] == []
    assert set(data) == {"no_kk", "nama_kepala_keluarga", "anggota_keluarga"}
