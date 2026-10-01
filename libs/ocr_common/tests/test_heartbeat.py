"""Detak job dan batas atas umurnya (Unit 3, bagian kedua).

Detak menukar satu masalah dengan masalah lain kalau tidak hati-hati, dan kedua sisinya diuji di
sini: task yang hidup lebih lama dari jobnya akan menjaga lease job mati tetap hangat dan justru
MELUMPUHKAN reaper, dan job yang berdetak selamanya tidak akan pernah mencapai keadaan akhir.
"""

import asyncio

import pytest
from pydantic import ValidationError

from ocr_common.config import PipelineSettings
from ocr_common.pipeline import STAGE_OCR, STATUS_FAILED, InMemoryJobRepository, StagePipeline
from ocr_common.testing import RecordingCallback

RID = "REQ_heartbeat"


async def job(repo) -> dict:
    """Baris job-nya, dipastikan ada. `get` mengembalikan Optional, dan setiap pemakaian di berkas ini
    terjadi setelah job diklaim -- jadi None di sini adalah kegagalan uji, bukan cabang yang perlu
    ditangani setiap kali."""
    row = await repo.get(RID)
    assert row is not None, "job hilang"
    return row


def pipeline(repo, **overrides) -> StagePipeline:
    return StagePipeline(stage=STAGE_OCR, repository=repo, callback=RecordingCallback(), **overrides)


# --- saklar: mati secara bawaan ------------------------------------------------------------


def settings(**overrides) -> PipelineSettings:
    base = {"api_key": "a-real-key", "environment": "local", "_env_file": None}
    # Soal penekanan di baris terakhir: sebaran **dict ke pydantic-settings membuat ty mencocokkan
    # dict itu terhadap SETIAP parameter kata-kunci privatnya (_env_file, _cli_*, _secrets_dir,
    # ~40 buah) dan mengeluh sekali per parameter -- satu baris ini sendiri menghasilkan ~50
    # diagnostik. Itu batas pemeriksa tipe terhadap sebaran, bukan cacat: setiap kunci di sini
    # harfiah dan diuji nilainya oleh uji di bawahnya.
    return PipelineSettings(**{**base, **overrides})  # ty: ignore[invalid-argument-type]


def test_the_heartbeat_is_off_by_default():
    """Mekanismenya dipasang sekarang karena permukaannya beku setelah gerbang R6, tetapi
    intervalnya tidak dikarang: ia disetel oleh batch yang menjalankan model sungguhan, ketika
    durasi job pertama kali bisa diukur."""
    assert settings().pipeline_heartbeat_seconds == 0.0
    assert settings().pipeline_job_max_runtime_seconds == 0.0


def test_a_heartbeat_without_a_ceiling_is_refused():
    """Detak menghapus satu-satunya batas atas yang dulu ada. Tanpa penggantinya, job yang macet
    tetapi prosesnya hidup akan berdetak selamanya dan tidak pernah menulis keadaan akhir."""
    with pytest.raises(ValidationError, match="PIPELINE_JOB_MAX_RUNTIME_SECONDS"):
        settings(pipeline_heartbeat_seconds=30)
    settings(pipeline_heartbeat_seconds=30, pipeline_job_max_runtime_seconds=600)


def test_a_heartbeat_slower_than_the_lease_is_refused():
    with pytest.raises(ValidationError, match="well below PIPELINE_JOB_LEASE_SECONDS"):
        settings(pipeline_heartbeat_seconds=300, pipeline_job_max_runtime_seconds=600, pipeline_job_lease_seconds=300)


# --- perilaku detak ------------------------------------------------------------------------


async def test_a_running_job_keeps_its_lease_fresh():
    seen: list[str] = []

    repo = InMemoryJobRepository(lease_seconds=60)
    stage = pipeline(repo, heartbeat_seconds=0.01, max_runtime_seconds=5)

    async def work():
        seen.append((await job(repo))["updated_at"])
        await asyncio.sleep(0.08)  # beberapa kali interval detak
        seen.append((await job(repo))["updated_at"])
        return {"texts": []}

    await stage.submit(RID, work)
    await stage.runner.drain(5)

    start, later = seen
    assert later > start, "lease harus bergerak SELAGI job berjalan, bukan hanya saat selesai"


async def test_the_beat_stops_with_the_job_and_cannot_keep_a_dead_lease_warm():
    """Sifat yang paling mudah salah. Kalau detaknya dimulai di luar `try/finally` milik job, ia
    akan terus berdetak setelah jobnya selesai -- dan job yang benar-benar terlantar tidak akan
    pernah dipanen reaper, yaitu kebalikan dari tujuan unit ini."""
    repo = InMemoryJobRepository(lease_seconds=60)
    stage = pipeline(repo, heartbeat_seconds=0.01, max_runtime_seconds=5)

    await stage.submit(RID, _slow_done)
    await stage.runner.drain(5)

    settled = (await job(repo))["updated_at"]
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("heartbeat:")]
    await asyncio.sleep(0.05)
    assert (await job(repo))["updated_at"] == settled, "tidak ada detak setelah job selesai"


async def test_the_beat_stops_when_the_job_fails_too():
    repo = InMemoryJobRepository(lease_seconds=60)
    stage = pipeline(repo, heartbeat_seconds=0.01, max_runtime_seconds=5)

    async def boom():
        raise RuntimeError("model exploded")

    await stage.submit(RID, boom)
    await stage.runner.drain(5)

    assert (await job(repo))["status"] == STATUS_FAILED
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("heartbeat:")]


async def test_touch_never_resurrects_a_settled_job():
    """Sebuah detak yang sudah terbang tidak boleh menggerakkan `updated_at` pada job yang sementara
    itu sudah selesai atau gagal."""
    repo = InMemoryJobRepository(lease_seconds=60)
    await repo.claim(RID)  # langsung ke repository, bukan lewat submit(), jadi jobnya dibuat di sini
    await repo.complete(RID, {"texts": []})
    settled = (await job(repo))["updated_at"]

    await repo.touch(RID)

    assert (await job(repo))["updated_at"] == settled


async def test_touch_on_an_unknown_request_is_harmless():
    await InMemoryJobRepository().touch("REQ_never_seen")


# --- batas atas umur job -------------------------------------------------------------------


async def test_a_job_past_the_ceiling_still_reaches_a_terminal_state():
    repo = InMemoryJobRepository(lease_seconds=60)
    stage = pipeline(repo, heartbeat_seconds=0.01, max_runtime_seconds=0.05)

    async def forever():
        await asyncio.sleep(30)
        return {"texts": []}

    await stage.submit(RID, forever)
    await stage.runner.drain(5)

    record = await job(repo)
    assert record["status"] == STATUS_FAILED
    assert "PIPELINE_JOB_MAX_RUNTIME_SECONDS" in record["error_message"]
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("heartbeat:")]


async def test_without_a_ceiling_the_work_is_not_wrapped():
    repo = InMemoryJobRepository()
    stage = pipeline(repo)

    await stage.submit(RID, lambda: _done())
    await stage.runner.drain(5)

    assert (await job(repo))["result"] == {"texts": []}


async def _done():
    return {"texts": []}


async def _slow_done():
    """Cukup lama untuk berdetak sekali, supaya ujinya benar-benar punya detak untuk dihentikan."""
    await asyncio.sleep(0.03)
    return {"texts": []}
