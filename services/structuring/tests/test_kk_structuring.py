"""Stub structuring: the §7.4 validity gate, the variable member list, and the two endpoints.

Written fresh rather than adapted from the previous pipeline's suite, because what matters here
has no counterpart there: a rejection that is a `DONE` job, and a member list whose length varies.
"""

from ocr_common.kk import (
    DOC_CELL_FEATURES,
    DOC_FIELDS,
    MEMBER_CELL_FEATURES,
    MEMBER_FIELDS,
    SCORED_DOC_FIELDS,
    SCORED_MEMBER_FIELDS,
)
from ocr_common.testing import wait_for_job

from app.ml.mock import NO_TEXT, NOT_A_KK

JOBS = "/v1/structuring/jobs"
SYNC = "/v1/ocr_postprocess"


def box(text: str) -> dict:
    return {"text": text, "score": 0.99, "poly": [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]]}


def job_body(request_id: str, *texts: str, **ocr_extra) -> dict:
    return {
        "request_id": request_id,
        "document_type": "kk",
        "ocr": {"engine": "mock", "texts": [box(t) for t in texts], **ocr_extra},
    }


def result_of(client, auth, request_id: str) -> dict:
    """The finished job. `wait_for_job` polls: the stage answers 202 and works in the background."""
    return wait_for_job(client, f"{JOBS}/{request_id}")


# --- bentuk §7.3 ---------------------------------------------------------------------------


def test_the_result_is_flat_with_thirteen_top_level_keys(client, auth):
    assert client.post(JOBS, json=job_body("REQ_flat", "KARTU KELUARGA"), headers=auth).status_code == 202

    result = result_of(client, auth, "REQ_flat")["result"]
    assert set(result) == set(DOC_FIELDS) | {"anggota_keluarga", "reject_reason"}
    assert "fields" not in result and "flag" not in result and "document_type" not in result


def test_every_field_carries_two_scores_and_a_string_value(client, auth):
    client.post(JOBS, json=job_body("REQ_scores", "KARTU KELUARGA"), headers=auth)

    result = result_of(client, auth, "REQ_scores")["result"]
    for name in DOC_FIELDS:
        assert set(result[name]) == {"value", "ocr_conf", "crf_conf", "features"}
        assert isinstance(result[name]["value"], str)
        assert result[name]["crf_conf"] is None, "field dokumen tidak pernah lewat Viterbi"
    for member in result["anggota_keluarga"]:
        assert set(member) == set(MEMBER_FIELDS)


def test_only_the_nine_scored_fields_carry_a_feature_vector(client, auth):
    """`features` adalah vektor masukan trust model, dan hanya sembilan field kontrak yang diskor.

    Kelengkapannya yang mengikat, bukan nilainya: scoring membaca setiap nama di
    `kk.MEMBER_CELL_FEATURES` / `kk.DOC_CELL_FEATURES`, dan nama yang hilang menjadi NaN pada
    model yang tidak pernah dilatih membacanya sebagai "tidak ada". Delapan field anggota lain
    diextraction tapi tidak pernah diskor, jadi mengarang fitur untuk mereka akan menyesatkan.
    """
    client.post(JOBS, json=job_body("REQ_features", "KARTU KELUARGA"), headers=auth)
    result = result_of(client, auth, "REQ_features")["result"]

    for name in SCORED_DOC_FIELDS:
        assert set(result[name]["features"]) == set(DOC_CELL_FEATURES)
    for name in set(DOC_FIELDS) - set(SCORED_DOC_FIELDS):
        assert result[name]["features"] is None, f"{name} tidak diskor"

    for member in result["anggota_keluarga"]:
        for name in SCORED_MEMBER_FIELDS:
            assert set(member[name]["features"]) == set(MEMBER_CELL_FEATURES)
        for name in set(MEMBER_FIELDS) - set(SCORED_MEMBER_FIELDS):
            assert member[name]["features"] is None, f"{name} tidak diskor"


def test_no_two_cells_share_a_feature_vector(client, auth):
    """Vektor yang identik antar sel membuat pergeseran indeks tak terlihat -- kegagalan yang
    justru harus terekspos oleh daftar anggota yang panjangnya berubah."""
    client.post(JOBS, json=job_body("REQ_distinct", "MOCK:members=3"), headers=auth)
    result = result_of(client, auth, "REQ_distinct")["result"]
    vektor = [
        tuple(sorted(member[name]["features"].items()))
        for member in result["anggota_keluarga"]
        for name in SCORED_MEMBER_FIELDS
    ]
    assert len(set(vektor)) == len(vektor)


# --- daftar anggota yang panjangnya berubah -------------------------------------------------


def test_the_member_count_is_controllable(client, auth):
    """Panjang yang berubah-ubah adalah perbedaan struktural terbesar KK terhadap dokumen
    bernilai tunggal; stub bernilai tetap justru membekukan dimensi yang paling mungkin pecah."""
    for count in (1, 2, 5):
        request_id = f"REQ_members_{count}"
        client.post(JOBS, json=job_body(request_id, f"MOCK:members={count}"), headers=auth)
        assert len(result_of(client, auth, request_id)["result"]["anggota_keluarga"]) == count


def test_members_do_not_share_scores(client, auth):
    """Angka yang sama membuat pergeseran indeks tak terlihat, padahal itu justru yang hendak
    diungkap oleh panjang yang berubah-ubah."""
    client.post(JOBS, json=job_body("REQ_distinct", "MOCK:members=3"), headers=auth)

    members = result_of(client, auth, "REQ_distinct")["result"]["anggota_keluarga"]
    niks = [m["nik"]["value"] for m in members]
    scores = [m["nik"]["ocr_conf"] for m in members]
    assert len(set(niks)) == 3
    assert len(set(scores)) == 3


# --- gerbang validitas §7.4 -----------------------------------------------------------------


def test_no_text_boxes_is_the_first_rule(client, auth):
    """`texts` kosong memenuhi ketiga kondisi sekaligus, jadi urutanlah yang membuat kasus ini
    deterministik."""
    client.post(JOBS, json=job_body("REQ_empty"), headers=auth)

    result = result_of(client, auth, "REQ_empty")["result"]
    assert result["reject_reason"] == NO_TEXT
    assert result["anggota_keluarga"] == []
    assert all(result[name]["value"] == "" for name in DOC_FIELDS), "§7.3: objek tetap lengkap"


def test_a_missing_kk_number_is_the_second_rule(client, auth):
    client.post(JOBS, json=job_body("REQ_nokk", "MOCK:blank_kk=1"), headers=auth)

    assert result_of(client, auth, "REQ_nokk")["result"]["reject_reason"] == NOT_A_KK


def test_zero_members_is_the_third_rule_and_is_told_apart_by_content(client, auth):
    """Aturan kedua dan ketiga berbagi pesan yang sama persis ("idem" di tabel §7.4), jadi yang
    membedakan keduanya adalah isi hasilnya: nomor KK terisi, daftar anggota kosong."""
    client.post(JOBS, json=job_body("REQ_nomembers", "MOCK:members=0"), headers=auth)

    result = result_of(client, auth, "REQ_nomembers")["result"]
    assert result["reject_reason"] == NOT_A_KK
    assert result["nomor_kk"]["value"], "aturan ketiga, bukan kedua: nomornya terbaca"
    assert result["anggota_keluarga"] == []


def test_a_rejected_document_is_a_done_job_whose_result_stays_readable(client, auth):
    """Penolakan bukan kegagalan tahap. Job-nya `DONE`, hasilnya tetap terbaca, dan orchestrator
    yang tanpa state membaca `reject_reason` dari sini untuk menjadikannya 400."""
    client.post(JOBS, json=job_body("REQ_readable"), headers=auth)

    data = result_of(client, auth, "REQ_readable")
    assert data["status"] == "DONE"
    assert data["result"]["reject_reason"] == NO_TEXT


def test_an_accepted_document_has_no_reason(client, auth):
    client.post(JOBS, json=job_body("REQ_ok", "KARTU KELUARGA"), headers=auth)

    assert result_of(client, auth, "REQ_ok")["result"]["reject_reason"] is None


# --- kedua endpoint menerima masukan yang berbeda -------------------------------------------


def test_the_job_endpoint_accepts_an_empty_texts_list(client, auth):
    """§7.1: gambar tanpa teks harus SAMPAI ke aturan untuk ditolak di sana."""
    assert client.post(JOBS, json=job_body("REQ_accept_empty"), headers=auth).status_code == 202


def test_the_legacy_sync_endpoint_still_refuses_an_empty_texts_list(client, auth):
    """§11 mempertahankan `min_length=1` justru di sini, dan hanya di sini."""
    assert client.post(SYNC, json={"texts": []}, headers=auth).status_code == 422
    assert client.post(SYNC, json={"texts": [box("KARTU KELUARGA")]}, headers=auth).status_code == 200


def test_the_job_endpoint_accepts_a_body_without_an_ocr_block(client, auth):
    """Serah-terima lewat referensi (dianjurkan untuk KK): tahap ini membaca `ocr_extraction_results` sendiri.
    Tanpa DATABASE_URL di uji ini, jawabannya 422 yang menjelaskan sebabnya -- bukan 500."""
    response = client.post(JOBS, json={"request_id": "REQ_byref", "document_type": "kk"}, headers=auth)
    assert response.status_code == 422
    assert "DATABASE_URL" in response.json()["message"]
