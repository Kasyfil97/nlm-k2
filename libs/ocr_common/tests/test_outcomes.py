import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from ocr_common.config import PipelineSettings
from ocr_common.pipeline import STAGE_SCORING, StagePipeline, database
from ocr_common.pipeline.outcomes import OrchestrationOutcome, build_stage_outcome
from ocr_common.pipeline.repository_sql import SqlJobRepository
from ocr_common.pipeline.tables import orchestration_outcome_table
from ocr_common.testing import RecordingCallback

TABLE = "orchestration_extract_ocr"
RID = "REQ_outcome"
DATA = {
    "no_kk": {"value": "3273012345678901", "confidence": 1},
    "nama_kepala_keluarga": {"value": "BUDI SANTOSO", "confidence": 0},
    "anggota_keluarga": [],
}


@pytest.fixture
async def repository(tmp_path):
    await database.dispose_engines()
    table = orchestration_outcome_table(TABLE)
    repo = SqlJobRepository(
        f"sqlite+aiosqlite:///{tmp_path / 'outcome.db'}",
        "scoring",
        outcome=OrchestrationOutcome(table, stage=STAGE_SCORING),
    )
    async with repo.engine.begin() as conn:
        await conn.run_sync(repo.metadata.create_all)
        await conn.run_sync(table.metadata.create_all)
    yield repo, table
    await database.dispose_engines()


async def _row(repository) -> dict:
    repo, table = repository
    async with repo.engine.connect() as conn:
        rows = (await conn.execute(select(table))).mappings().all()
    assert len(rows) == 1
    return dict(rows[0])


async def test_claiming_a_job_marks_the_request_processing_at_that_stage(repository):
    repo, _ = repository
    assert await repo.claim(RID) is True

    row = await _row(repository)
    assert (row["request_id"], row["document_type"]) == (RID, "kk")
    assert (row["status_code"], row["downstream_status"], row["downstream_stage"]) == (202, "processing", "SCORING")
    assert (row["result_data"], row["error_code"], row["error_message"]) == (None, None, None)
    assert row["ds"]


async def test_completing_with_data_marks_the_request_completed(repository):
    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"fields": {"nomor_kk": 0.7}, "anggota_keluarga": []}, outcome_data=DATA)

    row = await _row(repository)
    assert (row["status_code"], row["downstream_status"], row["downstream_stage"]) == (200, "completed", "SCORING")
    assert row["result_data"] == DATA
    assert (row["error_code"], row["error_message"]) == (None, None)


async def test_completing_without_data_leaves_the_request_processing(repository):
    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"blocks": []})

    row = await _row(repository)
    assert (row["status_code"], row["downstream_status"]) == (202, "processing")
    assert row["result_data"] is None


async def test_failing_records_the_stage_that_failed(repository):
    repo, _ = repository
    await repo.claim(RID)
    await repo.fail(RID, "No text lines to structure")

    row = await _row(repository)
    assert (row["status_code"], row["downstream_status"], row["downstream_stage"]) == (422, "failed", "SCORING")
    assert (row["error_code"], row["error_message"]) == ("SCORING_FAILED", "No text lines to structure")


async def test_a_rejection_marks_the_request_failed_with_400_and_the_reason(repository):
    repo, _ = repository
    reason = "Dokumen tidak dikenali sebagai Kartu Keluarga atau hasil ekstraksi tidak lengkap"
    await repo.claim(RID)
    await repo.complete(RID, {"reject_reason": reason}, outcome_data=DATA, rejection=reason)

    row = await _row(repository)
    assert (row["status_code"], row["downstream_status"], row["downstream_stage"]) == (400, "failed", "SCORING")
    assert (row["error_code"], row["error_message"]) == ("DOWNSTREAM_VALIDATION_ERROR", reason)
    assert row["result_data"] is None


async def test_a_second_run_of_the_same_request_id_overwrites_its_row(repository):
    repo, _ = repository
    await repo.claim(RID)
    await repo.fail(RID, "boom")
    await repo.claim(RID)
    await repo.complete(RID, {"fields": {"nomor_kk": 0.7}, "anggota_keluarga": []}, outcome_data=DATA)

    row = await _row(repository)
    assert (row["downstream_status"], row["error_code"]) == ("completed", None)


async def test_the_row_and_the_job_are_written_in_one_transaction(repository):
    repo, table = repository
    await repo.claim(RID)
    async with repo.engine.begin() as conn:
        await conn.run_sync(table.drop)

    with pytest.raises(OperationalError):
        await repo.complete(RID, {"fields": {"nomor_kk": 0.7}, "anggota_keluarga": []}, outcome_data=DATA)

    record = await repo.get(RID)
    assert record is not None
    assert record["status"] == "PROCESSING"
    assert record["result"] is None


async def test_the_pipeline_passes_the_row_data_of_the_last_stage(repository):
    repo, _ = repository
    callback = RecordingCallback()
    pipeline = StagePipeline(stage=STAGE_SCORING, repository=repo, callback=callback)

    async def work():
        return {"fields": {"nomor_kk": 0.7296, "nama_kepala_keluarga": 0.41}, "anggota_keluarga": []}

    await pipeline.submit(RID, work, outcome_data=lambda result: DATA)
    await pipeline.runner.drain(5)

    row = await _row(repository)
    assert (row["downstream_status"], row["result_data"]) == ("completed", DATA)


def test_the_outcome_row_is_off_until_the_table_is_configured():
    off = PipelineSettings(api_key="k", environment="local", _env_file=None)
    assert build_stage_outcome(off, stage=STAGE_SCORING) is None

    on = PipelineSettings(api_key="k", environment="local", orchestration_outcome_table=TABLE, _env_file=None)
    writer = build_stage_outcome(on, stage=STAGE_SCORING)
    assert isinstance(writer, OrchestrationOutcome)
    assert writer.table.name == TABLE


async def test_a_failed_handoff_marks_the_request_failed_at_the_next_stage(repository):
    """Tahap yang MENYERAHKAN job tidak pernah menulis `result_data`: hanya scoring yang mengirim
    `outcome_data`, dan scoring tidak punya tahap berikutnya. Jadi barisnya masih `processing` saat
    handoff-nya gagal, dan `422/failed` adalah jawaban yang benar -- bukan penulisan yang ditekan."""
    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"texts": []})

    await repo.handoff_failed(RID, "STRUCTURING", "Handoff to STRUCTURING failed: structuring service is unavailable")

    row = await _row(repository)
    assert (row["status_code"], row["downstream_status"], row["downstream_stage"]) == (422, "failed", "STRUCTURING")
    assert row["error_code"] == "STRUCTURING_FAILED"
    assert row["error_message"] == "Handoff to STRUCTURING failed: structuring service is unavailable"


# --- monotonisitas: baris `completed` tidak boleh mundur (Unit 3) ---------------------------


async def test_a_dead_lettered_handoff_after_completion_does_not_undo_it(repository):
    """Jalur yang BENAR-BENAR bisa dicapai, dan alasan unit ini ada.

    Analisis awal menyebut reaper sebagai jalurnya; itu keliru. `reclaim_stale` hanya memilih baris
    `PROCESSING`, dan `complete()` menyetel job jadi `DONE` di transaksi yang sama dengan penulisan
    baris `completed`, jadi reaper tidak akan pernah melihatnya. Yang bisa terjadi adalah relay
    outbox menyerah melewati `PIPELINE_OUTBOX_MAX_AGE_SECONDS` setelah tahap berikutnya sudah
    selesai -- dan `handoff_failed()` lalu menulis `422/failed` di atas hasil yang sudah benar,
    tepat di tabel yang dibaca tim lain sebagai kanal produksi.
    """
    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"fields": {"nomor_kk": 0.9}, "anggota_keluarga": []}, outcome_data=DATA)

    await repo.handoff_failed(RID, "SCORING", "next stage never accepted the job")

    row = await _row(repository)
    assert row["downstream_status"] == "completed", "baris selesai tidak boleh mundur jadi failed"
    assert row["result_data"] == DATA, "dan hasilnya tidak boleh hilang"


async def test_a_late_failure_from_a_duplicate_run_does_not_undo_completion(repository):
    """Jalur kedua: eksekusi kembar yang ditinggalkan `resume()` terdahulu memanggil `fail()`
    setelah eksekusi asli memanggil `complete()`."""
    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"fields": {}, "anggota_keluarga": []}, outcome_data=DATA)

    await repo.fail(RID, "Internal error in SCORING stage")

    row = await _row(repository)
    assert (row["downstream_status"], row["result_data"]) == ("completed", DATA)


async def test_a_late_rejection_does_not_undo_completion(repository):
    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"fields": {}, "anggota_keluarga": []}, outcome_data=DATA)

    await repo.complete(RID, {"fields": {}, "anggota_keluarga": []}, rejection="ditolak terlambat")

    row = await _row(repository)
    assert (row["downstream_status"], row["status_code"]) == ("completed", 200)


async def test_a_reclaim_after_completion_does_not_blank_the_result(repository):
    """Pertahanan berlapis. Lewat reaper jalur ini tidak bisa dicapai, tetapi `claimed()` memang
    dulu menulis `result_data=None` tanpa syarat, jadi sifatnya tetap ditegaskan."""
    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"fields": {}, "anggota_keluarga": []}, outcome_data=DATA)

    outcome = repo._outcome
    assert outcome is not None
    async with repo.engine.begin() as conn:
        await outcome.claimed(conn, RID)

    row = await _row(repository)
    assert (row["downstream_status"], row["result_data"]) == ("completed", DATA)


async def test_completion_after_a_failure_clears_the_stale_error_columns(repository):
    """`claim()` mengklaim ulang baris `FAILED`, jadi urutan fail -> klaim ulang -> sukses bisa
    terjadi. Tanpa pembersihan eksplisit, barisnya jadi `200/completed` yang masih membawa
    `error_code` lama -- tim Orkestrasi membaca request selesai yang sekaligus melaporkan galat."""
    repo, _ = repository
    await repo.claim(RID)
    await repo.fail(RID, "Internal error in SCORING stage")
    assert await repo.claim(RID) is True

    await repo.complete(RID, {"fields": {}, "anggota_keluarga": []}, outcome_data=DATA)

    row = await _row(repository)
    assert row["downstream_status"] == "completed"
    assert (row["error_code"], row["error_message"]) == (None, None)


async def test_a_suppressed_write_is_logged_and_counted(repository, caplog):
    """Penekanan tidak boleh senyap: kalau balapannya tak terlihat di produksi, tidak ada yang tahu
    seberapa sering ia terjadi -- padahal justru pengukuran itu yang memberi tahu apakah detaknya
    bekerja."""
    import logging

    repo, _ = repository
    await repo.claim(RID)
    await repo.complete(RID, {"fields": {}, "anggota_keluarga": []}, outcome_data=DATA)

    with caplog.at_level(logging.WARNING):
        await repo.handoff_failed(RID, "SCORING", "next stage never accepted the job")

    assert any(RID in record.message and "completed" in record.message.lower() for record in caplog.records)
