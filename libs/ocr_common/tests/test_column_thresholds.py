"""`column_confidence_threshold`: the central orchestrator's per-field thresholds. Ported from nilam, with
the KK contract names as keys."""

import pytest

from ocr_common.kk import CONTRACT_FIELDS, column_thresholds_from_json, parse_column_thresholds


def test_the_central_orchestrators_thresholds_are_read():
    assert column_thresholds_from_json('{"no_kk": 0.9, "nik": 0.5}') == {"no_kk": 0.9, "nik": 0.5}
    assert parse_column_thresholds({"ibu": 1}) == {"ibu": 1.0}


def test_the_keys_are_the_nine_contract_names():
    assert CONTRACT_FIELDS == (
        "no_kk",
        "nama_kepala_keluarga",
        "nama_lengkap",
        "nik",
        "pendidikan",
        "jenis_pekerjaan",
        "status_hubungan_dalam_rumah_tangga",
        "ayah",
        "ibu",
    )
    assert parse_column_thresholds(dict.fromkeys(CONTRACT_FIELDS, 0.5)) == dict.fromkeys(CONTRACT_FIELDS, 0.5)


@pytest.mark.parametrize("raw", [None, "", "   ", "{}"])
def test_nothing_given_means_no_threshold_for_any_field(raw):
    assert column_thresholds_from_json(raw) is None


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("{not json", "valid JSON"),
        ("[0.9, 0.5]", "JSON object"),
        ('{"nomor_kk": 0.9}', "unknown field(s) nomor_kk"),
        ('{"status_hubungan_dalam_keluarga": 0.9}', "unknown field(s) status_hubungan_dalam_keluarga"),
        ('{"nik": 1.5}', "between 0 and 1"),
        ('{"nik": -0.1}', "between 0 and 1"),
        ('{"nik": "tinggi"}', "must be a number"),
        ('{"nik": true}', "must be a number"),
    ],
)
def test_anything_else_is_refused_with_the_reason(raw, reason):
    """Internal names (`nomor_kk`, `status_hubungan_dalam_keluarga`) are refused: the caller only ever sees
    the contract names in `data`."""
    with pytest.raises(ValueError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        column_thresholds_from_json(raw)


def test_all_field_sets_every_field_and_a_named_field_overrides_it():
    from ocr_common.kk import CONTRACT_FIELDS

    every = column_thresholds_from_json('{"all_field": 0.8}')
    assert every == dict.fromkeys(CONTRACT_FIELDS, 0.8)
    mixed = column_thresholds_from_json('{"all_field": 0.8, "nik": 0.5}')
    assert mixed["nik"] == 0.5 and mixed["no_kk"] == 0.8
