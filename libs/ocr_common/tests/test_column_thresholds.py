"""`column_confidence_threshold`: the central orchestrator's per-field thresholds. Ported from nilam, with
the KK contract names as keys."""

import pytest

from ocr_common.kk import CONTRACT_FIELDS, column_thresholds_from_json, guardrails_value, parse_column_thresholds


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


# --- null is no threshold (central orchestrator rule of 9 Oct 2026) ---------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "{}", '{"all_field": null}', '{"no_kk": null, "nama_kepala_keluarga": null}', '{"unknown": null}'],
)
def test_nothing_or_only_nulls_leave_every_field_its_probability(raw):
    assert column_thresholds_from_json(raw) is None


def test_all_field_sets_every_field():
    assert column_thresholds_from_json('{"all_field": 0.8}') == dict.fromkeys(CONTRACT_FIELDS, 0.8)


def test_a_fields_own_null_wins_over_all_field():
    thresholds = column_thresholds_from_json('{"all_field": 0.8, "nik": null}') or {}

    assert "nik" not in thresholds
    assert thresholds == {name: 0.8 for name in CONTRACT_FIELDS if name != "nik"}


def test_one_field_alone():
    assert column_thresholds_from_json('{"nik": 0.5}') == {"nik": 0.5}


def test_a_number_under_an_unknown_key_is_still_refused():
    with pytest.raises(ValueError, match="unknown field"):
        column_thresholds_from_json('{"nomor_kk": 0.9}')


# --- `guardrails` of an answer ----------------------------------------------------------------------


def _report(passed: bool, probability_bad: float | None, threshold_used: float | None) -> dict:
    document = {"probability_bad": probability_bad, "threshold_used": threshold_used}
    return {"passed": passed, "reason": None, "document": document}


def test_guardrails_is_the_accepted_probability_without_a_threshold():
    assert guardrails_value(_report(True, 0.0287, None)) == 0.9713


def test_guardrails_is_0_or_1_with_a_threshold():
    assert guardrails_value(_report(True, 0.0287, 0.8)) == 0
    assert guardrails_value(_report(False, 0.91, 0.8)) == 1


def test_an_image_guardrails_could_not_assess_stays_rejected():
    assert guardrails_value(_report(False, None, None)) == 1


def test_no_report_no_guardrails():
    assert guardrails_value(None) is None
