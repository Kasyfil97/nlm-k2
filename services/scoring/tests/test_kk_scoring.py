"""Stub scoring: the §8.3 shape, positional alignment with structuring, and the shared projection.

The alignment tests are the point of this file. `contract_fields` is called from two places -- here
and in the orchestrator -- and §8.5 requires them to agree exactly, so a member list that drifts by
one would produce a 200 that looks right while carrying another person's confidence.
"""

import pytest

from ocr_common.kk import SCORED_DOC_FIELDS, SCORED_MEMBER_FIELDS, contract_fields
from ocr_common.testing import wait_for_job

JOBS = "/v1/scoring/jobs"
SYNC = "/v1/scoring/confidence"


def field(value: str, ocr_conf: float | None = 0.99, crf_conf: float | None = 0.97) -> dict:
    if not value:
        return {"value": "", "ocr_conf": None, "crf_conf": None}
    return {"value": value, "ocr_conf": ocr_conf, "crf_conf": crf_conf}


def member(name: str, nik: str, **overrides) -> dict:
    base = {
        "nama_lengkap": field(name),
        "nik": field(nik),
        "jenis_kelamin": field("LAKI-LAKI"),
        "tempat_lahir": field("BANDUNG"),
        "tanggal_lahir": field("01-01-1990"),
        "agama": field("ISLAM"),
        "pendidikan": field("S1"),
        "jenis_pekerjaan": field("KARYAWAN SWASTA"),
        "golongan_darah": field("O"),
        "status_perkawinan": field("KAWIN"),
        "tanggal_perkawinan": field("08-08-2015"),
        "status_hubungan_dalam_keluarga": field("KEPALA KELUARGA"),
        "kewarganegaraan": field("WNI"),
        "ayah": field("SUTRISNO"),
        "ibu": field("SITI AMINAH"),
    }
    return {**base, **overrides}


def structuring(*members: dict, nomor_kk: str = "9924187486671285") -> dict:
    document = {
        "nomor_kk": field(nomor_kk),
        "nama_kepala_keluarga": field("BUDI SANTOSO"),
        "alamat": field("JL. MERDEKA NO. 12"),
        "desa_kelurahan": field("CIHAPIT"),
        "rt": field("003"),
        "rw": field("007"),
        "kecamatan": field("BANDUNG WETAN"),
        "kabupaten_kota": field("KOTA BANDUNG"),
        "provinsi": field("JAWA BARAT"),
        "kode_pos": field("40114"),
        "tanggal_dikeluarkan": field("12-03-2019"),
        "anggota_keluarga": list(members),
        "reject_reason": None,
    }
    return document


def job_body(request_id: str, *members: dict) -> dict:
    return {
        "request_id": request_id,
        "document_type": "kk",
        "structuring": structuring(*members) if members else structuring(member("BUDI SANTOSO", "9908680101601956")),
    }


# --- bentuk §8.3 ---------------------------------------------------------------------------


def test_the_result_scores_only_the_nine_contract_fields(client, auth):
    assert client.post(JOBS, json=job_body("REQ_nine"), headers=auth).status_code == 202

    result = wait_for_job(client, f"{JOBS}/REQ_nine")["result"]
    assert set(result["fields"]) == set(SCORED_DOC_FIELDS)
    assert set(result["anggota_keluarga"][0]) == set(SCORED_MEMBER_FIELDS)


def test_the_keys_are_internal_names_not_contract_names(client, auth):
    """Penggantian nama terjadi di orchestrator, bukan di sini."""
    client.post(JOBS, json=job_body("REQ_internal"), headers=auth)

    result = wait_for_job(client, f"{JOBS}/REQ_internal")["result"]
    assert "nomor_kk" in result["fields"] and "no_kk" not in result["fields"]
    assert "status_hubungan_dalam_keluarga" in result["anggota_keluarga"][0]
    assert "status_hubungan_dalam_rumah_tangga" not in result["anggota_keluarga"][0]


def test_the_payload_is_kept_as_an_audit_trail(client, auth):
    """§8.3: angkanya harus bisa direproduksi tanpa menjalankan ulang pipeline, yang hanya benar
    kalau apa yang diskor disimpan bersama apa yang keluar."""
    client.post(JOBS, json=job_body("REQ_audit"), headers=auth)

    assert wait_for_job(client, f"{JOBS}/REQ_audit")["result"]["payload"]["structuring"]


def test_an_empty_field_scores_null(client, auth):
    blank = member("SITI NURHALIZA", "9908114806713444", golongan_darah=field(""), jenis_pekerjaan=field(""))
    client.post(JOBS, json=job_body("REQ_null", blank), headers=auth)

    scored = wait_for_job(client, f"{JOBS}/REQ_null")["result"]["anggota_keluarga"][0]
    assert scored["jenis_pekerjaan"] is None
    assert scored["nik"] is not None


# --- penyelarasan posisional ----------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 5])
def test_the_member_list_comes_back_the_same_length(client, auth, count):
    members = [member(f"ORANG {i}", f"990868010160{i:04d}") for i in range(count)]
    request_id = f"REQ_align_{count}"
    client.post(JOBS, json=job_body(request_id, *members), headers=auth)

    result = wait_for_job(client, f"{JOBS}/{request_id}")["result"]
    assert len(result["anggota_keluarga"]) == count


def test_members_do_not_share_scores(client, auth):
    """Angka yang sama akan membuat pergeseran satu indeks tak terlihat."""
    members = [member(f"ORANG {i}", f"990868010160{i:04d}") for i in range(3)]
    client.post(JOBS, json=job_body("REQ_distinct", *members), headers=auth)

    scored = wait_for_job(client, f"{JOBS}/REQ_distinct")["result"]["anggota_keluarga"]
    assert len({m["nik"] for m in scored}) == 3


# --- proyeksi bersama §3.3.1 ----------------------------------------------------------------


def test_the_projection_is_the_same_function_the_orchestrator_calls(client, auth):
    """R7a(c): stub ini memanggil `contract_fields()` dari `ocr_common.kk`, fungsi yang sama persis
    dengan yang dipakai orchestrator. Kalau keduanya berbeda, baris outcome dan respons
    `extract-ocr` bisa berbeda untuk request yang sama (§8.5)."""
    members = [member("BUDI SANTOSO", "9908680101601956"), member("SITI NURHALIZA", "9908114806713444")]
    client.post(JOBS, json=job_body("REQ_projection", *members), headers=auth)

    scored = wait_for_job(client, f"{JOBS}/REQ_projection")["result"]
    data = contract_fields(structuring(*members), scored, 0.5)

    assert set(data) == {"no_kk", "nama_kepala_keluarga", "anggota_keluarga"}
    assert len(data["anggota_keluarga"]) == 2
    assert data["anggota_keluarga"][0]["nik"]["value"] == "9908680101601956"
    assert data["anggota_keluarga"][1]["nik"]["value"] == "9908114806713444"
    assert "status_hubungan_dalam_rumah_tangga" in data["anggota_keluarga"][0]


def test_a_shorter_score_list_is_refused_rather_than_truncated(client, auth):
    members = [member("BUDI SANTOSO", "9908680101601956"), member("SITI NURHALIZA", "9908114806713444")]
    client.post(JOBS, json=job_body("REQ_mismatch", *members), headers=auth)
    scored = wait_for_job(client, f"{JOBS}/REQ_mismatch")["result"]

    short = {**scored, "anggota_keluarga": scored["anggota_keluarga"][:1]}
    with pytest.raises(ValueError, match="anggota_keluarga"):
        contract_fields(structuring(*members), short, 0.5)


# --- endpoint sinkron -----------------------------------------------------------------------


def test_the_sync_endpoint_scores_the_same_shape(client, auth):
    body = {"structuring": structuring(member("BUDI SANTOSO", "9908680101601956")), "guardrail_probability": 0.0287}
    response = client.post(SYNC, json=body, headers=auth)

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data["fields"]) == set(SCORED_DOC_FIELDS)
    assert len(data["anggota_keluarga"]) == 1
