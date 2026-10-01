"""The §3.3.1 projection: the nine fields that leave, and the four ways it can be got wrong."""

import json
from copy import deepcopy

import pytest

from ocr_common.kk import CONTRACT_DOC_FIELDS, CONTRACT_MEMBER_FIELDS, contract_fields

from app.config import get_settings
from app.main import app
from tests.conftest import JPEG, SCORING_RESULT, STRUCTURING_RESULT

RID = "REQ_contract"


def _submit(client, auth, **form):
    return client.post(
        "/v1/extract-ocr",
        headers=auth,
        data={"request_id": RID, **form},
        files={"file": ("kk.jpg", JPEG, "image/jpeg")},
    )


def _projected(threshold=0.5, *, structuring=None, scoring=None):
    return contract_fields(structuring or STRUCTURING_RESULT, scoring or SCORING_RESULT, threshold)


def test_only_the_nine_contract_fields_leave():
    """Structuring extracts 11 document fields and 15 per member; the projection is the whole reason
    `alamat`, `agama` and `tanggal_lahir` do not appear in a response."""
    data = _projected()
    assert set(data) == {*CONTRACT_DOC_FIELDS, "anggota_keluarga"}
    for member in data["anggota_keluarga"]:
        assert set(member) == set(CONTRACT_MEMBER_FIELDS)


def test_the_two_renames_happen_and_nothing_else_does():
    """`nomor_kk` -> `no_kk` and `status_hubungan_dalam_keluarga` ->
    `status_hubungan_dalam_rumah_tangga`. The other seven keep their names, which is exactly what
    makes a mix-up easy to miss."""
    data = _projected()
    assert data["no_kk"]["value"] == STRUCTURING_RESULT["nomor_kk"]["value"]
    assert "nomor_kk" not in data
    member, source = data["anggota_keluarga"][0], STRUCTURING_RESULT["anggota_keluarga"][0]
    assert member["status_hubungan_dalam_rumah_tangga"]["value"] == source["status_hubungan_dalam_keluarga"]["value"]
    assert "status_hubungan_dalam_keluarga" not in member


def test_confidence_is_1_from_the_threshold_up_and_0_just_below():
    """Seperti nilam: `{value, confidence}` dengan confidence 1 atau 0 -- tidak ada `bin` atau `auto`."""
    scoring = deepcopy(SCORING_RESULT)
    scoring["fields"]["nomor_kk"] = 0.5
    scoring["fields"]["nama_kepala_keluarga"] = 0.4999
    data = _projected(0.5, scoring=scoring)
    assert (data["no_kk"], data["nama_kepala_keluarga"]) == (
        {"value": STRUCTURING_RESULT["nomor_kk"]["value"], "confidence": 1},
        {"value": STRUCTURING_RESULT["nama_kepala_keluarga"]["value"], "confidence": 0},
    )


def test_the_models_own_thresholds_travel_with_the_scores():
    """§8.5 menuntut baris outcome dan respons extract-ocr identik untuk satu request, jadi ambang ikut di
    dalam hasil scoring, bukan di konfigurasi. Model yang membawa ambang memutuskan SETIAP field: yang tidak
    ia beri ambang tetap 0, walau lolos ambang global."""
    scoring = deepcopy(SCORING_RESULT)
    scoring["thresholds"] = {"nomor_kk": 0.95}
    scoring["fields"]["nomor_kk"] = 0.94
    scoring["fields"]["nama_kepala_keluarga"] = 0.97
    data = _projected(0.5, scoring=scoring)
    assert data["no_kk"]["confidence"] == 0, "0.94 lolos ambang global 0.5 tapi bukan ambang modelnya"
    assert data["nama_kepala_keluarga"]["confidence"] == 0, "model membawa ambang, tapi tidak untuk field ini"


def test_the_requests_column_thresholds_come_first():
    """`column_confidence_threshold` dari Orkestrasi pusat mengalahkan ambang model, per nama kontrak."""
    scoring = deepcopy(SCORING_RESULT)
    scoring["thresholds"] = {"nomor_kk": 0.95}
    scoring["fields"]["nomor_kk"] = 0.94
    data = contract_fields(STRUCTURING_RESULT, scoring, 0.5, {"no_kk": 0.9, "nama_kepala_keluarga": 0.9})
    assert (data["no_kk"]["confidence"], data["nama_kepala_keluarga"]["confidence"]) == (1, 1)


def test_a_low_score_on_one_member_field_does_not_touch_the_others():
    """The fixture scores member 2's `jenis_pekerjaan` below the threshold on purpose: a projection
    that applied one number to the whole member would be invisible if every field passed."""
    members = _projected()["anggota_keluarga"]
    assert members[1]["jenis_pekerjaan"]["confidence"] == 0
    assert members[1]["nama_lengkap"]["confidence"] == 1
    assert members[0]["jenis_pekerjaan"]["confidence"] == 1


def test_a_missing_value_is_an_empty_string_never_null():
    """§3.3.1: `value` is always a string and the field object is never replaced by null, so a
    consumer never has to distinguish 'absent' from 'empty'."""
    structuring = deepcopy(STRUCTURING_RESULT)
    structuring["nama_kepala_keluarga"] = {"value": "", "ocr_conf": None, "crf_conf": None}
    del structuring["anggota_keluarga"][0]["ibu"]
    data = _projected(structuring=structuring)
    assert data["nama_kepala_keluarga"] == {"value": "", "confidence": 0}
    assert data["anggota_keluarga"][0]["ibu"] == {"value": "", "confidence": 0}


def test_a_value_without_a_score_is_confidence_0_not_an_error():
    scoring = deepcopy(SCORING_RESULT)
    scoring["fields"]["nomor_kk"] = None
    assert _projected(scoring=scoring)["no_kk"] == {"value": STRUCTURING_RESULT["nomor_kk"]["value"], "confidence": 0}


def test_members_are_zipped_positionally_not_matched_by_nik():
    """Nothing keys the two lists together, so an index shift would silently hand member 1 member
    2's confidence. This pins the order rather than the content."""
    scoring = deepcopy(SCORING_RESULT)
    scoring["anggota_keluarga"][0]["nik"] = 0.99
    scoring["anggota_keluarga"][1]["nik"] = 0.01
    members = _projected(scoring=scoring)["anggota_keluarga"]
    assert (members[0]["nik"]["confidence"], members[1]["nik"]["confidence"]) == (1, 0)


def test_a_member_length_mismatch_raises_instead_of_truncating():
    """A mismatch is a defect in this pipeline, not a property of the document. Truncating or
    padding would produce a 200 that looks right while carrying another person's confidence."""
    scoring = deepcopy(SCORING_RESULT)
    scoring["anggota_keluarga"].pop()
    with pytest.raises(ValueError, match="anggota_keluarga length mismatch"):
        _projected(scoring=scoring)


def test_a_household_with_no_members_projects_to_an_empty_list():
    """The first §7.4 rule rejects a card with no readable text, but a *passing* result with zero
    members is still shape-valid and must not raise."""
    data = _projected(
        structuring={**STRUCTURING_RESULT, "anggota_keluarga": []},
        scoring={**SCORING_RESULT, "anggota_keluarga": []},
    )
    assert data["anggota_keluarga"] == []
    assert data["no_kk"]["value"] == STRUCTURING_RESULT["nomor_kk"]["value"]


# --- the same projection, through the endpoint ---------------------------------------------


def test_the_endpoint_returns_the_projection(client, auth):
    response = _submit(client, auth)
    assert response.status_code == 200
    assert response.json()["data"] == _projected(0.5)


def test_confidence_threshold_can_be_changed(client, auth):
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"field_confidence_threshold": 0.9}
    )
    try:
        response = _submit(client, auth)
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.json()["data"] == _projected(0.9)


@pytest.mark.parametrize(
    "params",
    ['{"nik": "9901011203850001", "refno": "PK19039Y8U"}', '"halo"'],
)
def test_params_are_returned_unchanged(client, auth, params):
    response = _submit(client, auth, params=params)
    assert response.status_code == 200
    assert response.json()["params"] == json.loads(params)


@pytest.mark.parametrize("params", ["{not json", "[1, 2]", "42"])
def test_invalid_params_are_422_before_anything_runs(client, auth, stub_guardrails, stub_ekstraksi, params):
    response = _submit(client, auth, params=params)
    assert response.status_code == 422
    body = response.json()
    assert (body["errors"], body["message"]) == (
        "INVALID_PARAMS",
        "params must be valid JSON: an object, or a quoted string",
    )
    assert body["job_status"] is None
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_unsupported_document_type_is_400_before_anything_runs(client, auth, stub_guardrails, stub_ekstraksi):
    response = _submit(client, auth, document_type="ktp")
    assert response.status_code == 400
    body = response.json()
    assert (body["errors"], body["message"]) == (
        "UNSUPPORTED_DOCUMENT_TYPE",
        "Unsupported document_type: ktp. Supported: kk",
    )
    assert body["document_type"] == "ktp"
    assert stub_guardrails.checked == [] and stub_ekstraksi.submitted == []


def test_the_projection_of_the_fixture_household_in_full():
    """Nilai literal, tidak diturunkan dari fixture. Semua uji di atas membandingkan data hasil
    generate dengan data hasil generate, yang tidak bisa menangkap generator yang berubah di
    bawahnya. Snapshot ini dibarui HANYA ketika kontraknya memang sengaja berubah.
    """
    one, zero = 1, 0
    assert _projected() == {
        "no_kk": {"value": "9924187486671285", "confidence": one},
        "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "confidence": one},
        "anggota_keluarga": [
            {
                "nama_lengkap": {"value": "BUDI SANTOSO", "confidence": one},
                "nik": {"value": "9908680101601956", "confidence": one},
                "pendidikan": {"value": "SD/SEDERAJAT", "confidence": one},
                "jenis_pekerjaan": {"value": "KARYAWAN SWASTA", "confidence": one},
                "status_hubungan_dalam_rumah_tangga": {"value": "KEPALA KELUARGA", "confidence": one},
                "ayah": {"value": "RIZKY SANTOSO", "confidence": one},
                "ibu": {"value": "NURUL PRATAMA", "confidence": one},
            },
            {
                "nama_lengkap": {"value": "SITI SANTOSO", "confidence": one},
                "nik": {"value": "9908114806713444", "confidence": one},
                "pendidikan": {"value": "D-III", "confidence": one},
                "jenis_pekerjaan": {"value": "PELAJAR/MAHASISWA", "confidence": zero},
                "status_hubungan_dalam_rumah_tangga": {"value": "ISTRI", "confidence": one},
                "ayah": {"value": "INDAH SANTOSO", "confidence": one},
                "ibu": {"value": "HENDRA PRATAMA", "confidence": one},
            },
        ],
    }
