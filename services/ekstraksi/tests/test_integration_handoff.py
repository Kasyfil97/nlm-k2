"""The three things no unit test of this stage can show: the job transaction, the at-least-once
hand-off, and that a slow read does not look stale.

**How the database is provided.** SQLite through `aiosqlite`, in `tmp_path`, which is the convention
`libs/ocr_common/tests/test_outbox.py` and `test_outcomes.py` already follow -- the SQL repository,
the outbox and the outcome table are all exercised as real tables and real transactions, with no
Docker and no Postgres. What that does **not** cover is named explicitly rather than implied: the
PostgreSQL-only parts of the claim (`INSERT ... ON CONFLICT DO NOTHING` under concurrent writers,
`FOR UPDATE SKIP LOCKED` in the outbox claim) behave differently on a single-writer file, so
cross-replica atomicity is verified by construction here, not by execution. That gap belongs to the
compose gate of R24, which cannot run in this environment.
"""

import asyncio
import time
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from ocr_common.pipeline import (
    STAGE_OCR,
    STAGE_STRUCTURING,
    STATUS_DONE,
    STATUS_PROCESSING,
    StagePipeline,
    database,
    handoff_message,
)
from ocr_common.pipeline.outbox import OutboxRelay
from ocr_common.pipeline.outbox_sql import SqlOutbox
from ocr_common.pipeline.outcomes import OrchestrationOutcome
from ocr_common.pipeline.repository_sql import SqlJobRepository
from ocr_common.pipeline.tables import orchestration_outcome_table
from ocr_common.testing import RecordingCallback
from ocr_common.types import OcrEngineResult

from app.config import get_settings
from app.ml.mock import MockOcrEngine
from app.services.ekstraksi_service import EkstraksiService
from app.services.job_service import EkstraksiJobService
from tests.conftest import GUARDRAILS, JPEG

RID = "REQ_integration"
#: A job claimed and then abandoned by nobody in particular. It is the positive control of the two
#: lease tests: without it, "the reaper took nothing" could equally mean the reaper never ran.
SENTINEL = "REQ_abandoned"
OUTCOME_TABLE = "orchestration_extract_ocr"
UPLOAD = (JPEG, "kk.jpg", "image/jpeg")


class Stage:
    """One stage on a real database: jobs, results, the outbox and the orchestrator's outcome row."""

    def __init__(self, url: str, prefix: str, stage: str, *, lease: float = 300.0, **pipeline_kwargs: Any):
        self.outbox = SqlOutbox(url)
        self.outcome_table = orchestration_outcome_table(OUTCOME_TABLE)
        self.repository = SqlJobRepository(
            url,
            prefix,
            lease_seconds=lease,
            outcome=OrchestrationOutcome(self.outcome_table, stage=stage),
            outbox=self.outbox,
            stage=stage,
        )
        self.callback = RecordingCallback()
        self.pipeline = StagePipeline(
            stage=stage,
            repository=self.repository,
            callback=self.callback,
            outbox=self.outbox,
            # No ORCHESTRATION_URL, so `settings.callbacks_enabled` is false and the only outbox
            # message a finished job queues is the hand-off. That is the deployment §2.6 describes:
            # the outcome row is how the caller learns, not a callback.
            callbacks=False,
            **pipeline_kwargs,
        )

    async def create_tables(self) -> None:
        async with self.repository.engine.begin() as conn:
            await conn.run_sync(self.repository.metadata.create_all)
            await conn.run_sync(self.outbox.table.metadata.create_all)
            await conn.run_sync(self.outcome_table.metadata.create_all)

    async def rows(self, table) -> list[dict]:
        async with self.repository.engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(select(table))).mappings().all()]

    async def outbox_rows(self) -> list[dict]:
        return await self.rows(self.outbox.table)

    async def outcome_rows(self) -> list[dict]:
        return await self.rows(self.outcome_table)

    async def result_rows(self) -> list[dict]:
        return await self.rows(self.repository._results)


def _service(stage: Stage) -> EkstraksiJobService:
    """The real job service on the real pipeline: the mock backend, through the real threadpool hop."""
    return EkstraksiJobService(
        stage.pipeline,
        EkstraksiService(MockOcrEngine(), get_settings()),
        max_upload_bytes=5 * 1024 * 1024,
        handoff_by_reference=False,
    )


@pytest.fixture
async def ocr(tmp_path):
    await database.dispose_engines()
    stage = Stage(f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}", "ocr", STAGE_OCR)
    await stage.create_tables()
    yield stage
    await database.dispose_engines()


# --- one transaction: result + hand-off + outcome row --------------------------------------


async def test_finishing_a_job_writes_the_result_the_handoff_and_the_outcome_row(ocr):
    """§6.3 step 4 and §2.6: the outcome row is written *in the same transaction* as the result, so
    there is never a state where the result is stored and the caller cannot learn it, or the other
    way round."""
    await _service(ocr).submit(RID, "kk", GUARDRAILS, UPLOAD)
    await ocr.pipeline.runner.drain(5)

    [job] = await ocr.rows(ocr.repository._jobs)
    assert job["status"] == STATUS_DONE

    [result] = await ocr.result_rows()
    assert result["result"]["text_regions_count"] == len(result["result"]["texts"]) > 0
    assert all(len(box["poly"]) == 4 for box in result["result"]["texts"])

    [handoff] = [row for row in await ocr.outbox_rows() if row["kind"] == "handoff"]
    assert handoff["payload"]["next_stage"] == STAGE_STRUCTURING
    body = handoff["payload"]["body"]
    assert body["request_id"] == RID
    assert body["guardrails"] == GUARDRAILS
    assert body["ocr"]["texts"] == result["result"]["texts"]

    [outcome] = await ocr.outcome_rows()
    assert (outcome["downstream_status"], outcome["downstream_stage"], outcome["status_code"]) == (
        "processing",
        STAGE_OCR,
        202,
    )


async def test_the_three_writes_are_one_transaction_and_roll_back_together(ocr):
    """Driven by breaking one of the three. If they were three transactions, the result would
    survive the outbox failing and the pipeline would have a stored result nobody is told about --
    exactly the state §2.6 exists to rule out."""
    await ocr.repository.claim(RID)
    async with ocr.repository.engine.begin() as conn:
        await conn.run_sync(ocr.outbox.table.drop)

    with pytest.raises(OperationalError):
        await ocr.repository.complete(
            RID,
            {"texts": [], "text_regions_count": 0},
            messages=[handoff_message(STAGE_STRUCTURING, {"request_id": RID})],
        )

    assert (await ocr.repository.get(RID))["status"] == STATUS_PROCESSING, "not DONE: the whole thing rolled back"
    assert await ocr.result_rows() == []


async def test_the_handoff_carries_no_ocr_payload_by_reference(tmp_path):
    """`PIPELINE_HANDOFF_BY_REFERENCE` is recommended for KK because one card is ~180 boxes and the
    outbox row survives 24 hours as a dead letter. The row must then not contain the card's text."""
    await database.dispose_engines()
    stage = Stage(f"sqlite+aiosqlite:///{tmp_path / 'byref.db'}", "ocr", STAGE_OCR)
    await stage.create_tables()
    service = EkstraksiJobService(
        stage.pipeline,
        EkstraksiService(MockOcrEngine(), get_settings()),
        max_upload_bytes=5 * 1024 * 1024,
        handoff_by_reference=True,
    )

    await service.submit(RID, "kk", GUARDRAILS, UPLOAD)
    await stage.pipeline.runner.drain(5)

    [handoff] = [row for row in await stage.outbox_rows() if row["kind"] == "handoff"]
    assert "ocr" not in handoff["payload"]["body"]
    assert (await stage.result_rows())[0]["result"]["texts"], "the text is stored, just not shipped"
    await database.dispose_engines()


# --- at-least-once: the same hand-off delivered twice ---------------------------------------


class Structuring:
    """The next stage, claiming on a real database exactly as the structuring service does.

    A double rather than the real service, because the property under test belongs to the *claim*:
    `POST /v1/structuring/jobs` answers 202 with `duplicate: true` for a request_id it already has,
    and a 202 is a successful delivery, so the relay deletes the message instead of retrying it.
    """

    def __init__(self, stage: Stage):
        self._stage = stage
        self.received: list[dict] = []
        self.runs = 0

    async def send(self, body: dict) -> None:
        self.received.append(body)

        async def work() -> dict:
            self.runs += 1
            return {"nomor_kk": {"value": "", "ocr_conf": None, "crf_conf": None}}

        await self._stage.pipeline.submit(body["request_id"], work)
        await self._stage.pipeline.runner.drain(5)


async def test_a_resent_handoff_is_refused_as_a_duplicate_and_still_counts_as_delivered(ocr, tmp_path):
    """The outbox is at-least-once by construction: a relay that sends and then dies before marking
    the row delivered sends it again. This is correct today and is pinned so it stays correct -- the
    second delivery must not produce a second structuring job, and must not become a dead letter."""
    structuring = Stage(f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}", "structuring", STAGE_STRUCTURING)
    await structuring.create_tables()
    receiver = Structuring(structuring)
    relay = OutboxRelay(ocr.outbox, stage=STAGE_OCR, callback=ocr.callback, next_stage=receiver, callbacks=False)

    await _service(ocr).submit(RID, "kk", GUARDRAILS, UPLOAD)
    await ocr.pipeline.runner.drain(5)
    [queued] = [row for row in await ocr.outbox_rows() if row["kind"] == "handoff"]

    assert await relay.deliver_due() == 1
    assert receiver.runs == 1

    # The at-least-once re-send: the identical message, queued again, as a relay that could not
    # record its own success would produce.
    async with ocr.repository.engine.begin() as conn:
        await ocr.outbox.add(conn, RID, STAGE_OCR, [handoff_message(STAGE_STRUCTURING, queued["payload"]["body"])])

    assert await relay.deliver_due() == 1, "a duplicate is a successful delivery, not a retry"
    assert len(receiver.received) == 2
    assert receiver.runs == 1, "the second hand-off claimed nothing, so the work did not run again"
    assert await ocr.outbox_rows() == [], "delivered and removed; no dead letter"
    assert [row["status"] for row in await structuring.rows(structuring.repository._jobs)] == [STATUS_DONE]


# --- a slow read is not a stale job ----------------------------------------------------------


#: Long enough that the lease below expires three times over while one read is in flight.
READ_SECONDS = 0.9
LEASE_SECONDS = 0.3
#: A read that holds the loop still lets exactly one beat through -- the heartbeat task gets the
#: loop back after the read returns and fires once before `_run_bound`'s `finally` cancels it. That
#: beat is too late to be worth anything, so it is the ceiling for "missed every beat", not zero.
MISSED_BEATS = 1


class BlockingEngine:
    """An in-process backend (`OcrRecognizer`) that really blocks its thread, the way a detector does."""

    name = "blocking"

    def read(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult:
        time.sleep(READ_SECONDS)
        return {"texts": [], "model": "blocking"}


class LoopBlockingEngine:
    """The same read, mistakenly reachable through the async branch: it blocks the event loop.

    This is precisely the shape the design forbids -- an in-process model awaited directly instead
    of handed to the threadpool -- written out so the control below exercises the real code path
    rather than a hand-made job.
    """

    name = "loop-blocking"

    async def extract(self, filename: str, content: bytes, content_type: str | None = None) -> OcrEngineResult:
        time.sleep(READ_SECONDS)
        return {"texts": [], "model": "loop-blocking"}

    async def aclose(self) -> None:
        pass


def _counting_touch(stage: Stage) -> list[str]:
    """Record every beat. The beat is the mechanism under test, so counting it is the measurement;
    watching `updated_at` would only show its effect and would be racy at these timings."""
    beats: list[str] = []
    original = stage.repository.touch

    async def touch(request_id: str) -> None:
        beats.append(request_id)
        await original(request_id)

    stage.repository.touch = touch  # ty: ignore[invalid-assignment]
    return beats


async def _lease_stage(tmp_path, name: str) -> Stage:
    await database.dispose_engines()
    stage = Stage(
        f"sqlite+aiosqlite:///{tmp_path / name}",
        "ocr",
        STAGE_OCR,
        lease=LEASE_SECONDS,
        heartbeat_seconds=0.05,
        max_runtime_seconds=10,
    )
    await stage.create_tables()
    return stage


async def test_a_read_that_outlasts_the_lease_is_not_reclaimed_because_it_runs_off_the_loop(tmp_path):
    """The point of the whole `OcrRecognizer`-is-synchronous design, proved rather than asserted.

    The lease is 0.3 s and the read takes 0.9 s, so without a beat this job is stale three times
    over before it finishes. The beat is an ordinary event-loop task, so it can only fire while
    `work()` leaves the loop free -- which it does exactly because `EkstraksiService` puts the
    in-process backend in the threadpool. Another replica's reaper is simulated by polling
    `reclaim_stale` throughout, which is all a reaper does.

    `SENTINEL` is a job claimed and then abandoned, and it is what makes the negative result mean
    something: the reaper demonstrably did run, the lease demonstrably did expire at these
    timings, and it took the abandoned job -- and left the beating one alone.
    """
    stage = await _lease_stage(tmp_path, "offloop.db")
    beats = _counting_touch(stage)
    await stage.repository.claim(SENTINEL)
    service = EkstraksiJobService(
        stage.pipeline, EkstraksiService(BlockingEngine(), get_settings()), max_upload_bytes=5 * 1024 * 1024
    )

    await service.submit(RID, "kk", GUARDRAILS, UPLOAD)
    reclaimed = await _reap_while_running(stage)

    # 0.9 s of work at one beat per 0.05 s: many beats, and all of them during the read.
    assert len(beats) > MISSED_BEATS, "the heartbeat kept firing, which it can only do if the loop was free"
    assert SENTINEL in reclaimed, "the reaper was awake and the lease really does expire at these timings"
    assert RID not in reclaimed, "and the job that was running fine was left alone"
    record = await stage.repository.get(RID)
    assert record is not None and record["status"] == STATUS_DONE
    await database.dispose_engines()


async def test_the_control_the_same_read_on_the_loop_misses_every_beat(tmp_path):
    """Without this the test above proves nothing: it would pass just as well if the beat were
    broken and the lease simply never expired.

    Same lease, same 0.9 s of work, same pipeline, same service -- only the branch differs. Held
    on the loop, the heartbeat task never gets a turn, so for 0.9 s against a 0.3 s lease this job
    looks exactly as abandoned as `SENTINEL`, which the reaper duly takes. Nothing reclaims the job
    itself *here* only because the poller cannot run either while the loop is held; in production
    the reaper is in another pod and is not blocked by this one.
    """
    stage = await _lease_stage(tmp_path, "onloop.db")
    beats = _counting_touch(stage)
    await stage.repository.claim(SENTINEL)
    service = EkstraksiJobService(
        stage.pipeline, EkstraksiService(LoopBlockingEngine(), get_settings()), max_upload_bytes=5 * 1024 * 1024
    )

    await service.submit(RID, "kk", GUARDRAILS, UPLOAD)
    await stage.pipeline.runner.drain(5)

    # At most one beat, and it is worthless: the heartbeat task only gets the loop back once the
    # read has already returned, so its `touch` lands after the job is over.
    assert len(beats) <= MISSED_BEATS, "a beat cannot fire while the loop is held; one late one may"
    assert [job.request_id for job in await stage.repository.reclaim_stale(10)] == [SENTINEL], (
        "an unbeaten job of this age is reclaimable -- which is what the job above was, the whole time it ran"
    )
    await database.dispose_engines()


async def _reap_while_running(stage: Stage, *, every: float = 0.02) -> set[str]:
    """Poll `reclaim_stale` until the job task finishes, and report what it took."""
    [task] = [task for task in stage.pipeline.runner._tasks]
    taken: set[str] = set()
    while not task.done():
        taken.update(job.request_id for job in await stage.repository.reclaim_stale(10))
        await asyncio.sleep(every)
    await stage.pipeline.runner.drain(5)
    return taken
