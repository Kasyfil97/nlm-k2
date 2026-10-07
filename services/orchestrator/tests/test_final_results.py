"""The final answer of every request is kept in ocr_results, one row per request_id, the latest answer winning, and
a write that fails never fails the request."""

import pytest
from sqlalchemy import MetaData, select

from ocr_common.pipeline.database import dispose_engines, get_engine
from ocr_common.pipeline.tables import final_results_table

from app.services.final_results import SqlFinalResults
from app.services.pipeline_waiter import WaitOutcome
from tests.conftest import JPEG

RID = "OCR_final_results"


def _submit(client, auth, filename="kk.jpg", **form):
    return client.post(
        "/v1/extract-ocr",
        headers=auth,
        data={"request_id": RID, **form},
        files={"file": (filename, JPEG, "image/jpeg")},
    )


# --- what the service keeps --------------------------------------------------------------------------


def test_the_answer_of_the_post_is_kept(client, auth, final_results):
    response = _submit(client, auth)

    assert response.status_code == 200
    assert final_results.saved[RID] == response.json()


def test_a_rejection_and_a_refusal_are_kept_too(client, auth, final_results):
    rejected = _submit(client, auth, filename="notkk.jpg")
    assert final_results.saved[RID] == rejected.json()
    assert (rejected.status_code, rejected.json()["guardrails"]) == (400, 1)

    refused = _submit(client, auth, document_type="ktp")
    assert final_results.saved[RID] == refused.json()
    assert refused.status_code == 400


def test_the_get_overwrites_a_202_with_the_finished_answer(client, auth, stub_waiter, final_results):
    stub_waiter.outcome = WaitOutcome("STRUCTURING", "PROCESSING")
    assert _submit(client, auth).status_code == 202
    assert final_results.saved[RID]["status_code"] == 202

    finished = client.get(f"/v1/extract-ocr/{RID}", headers=auth)

    assert finished.status_code == 200
    assert final_results.saved[RID] == finished.json()


# --- the table -------------------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'final.db'}"
    metadata = MetaData()
    table = final_results_table(metadata)
    async with get_engine(url).begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield url, table
    await dispose_engines()


async def _rows(url, table):
    async with get_engine(url).connect() as conn:
        return (await conn.execute(select(table))).mappings().all()


PROCESSING = {
    "status_code": 202,
    "status_desc": "Accepted",
    "message": "Processing",
    "data": None,
    "errors": None,
    "request_id": RID,
}
COMPLETED = {
    "status_code": 200,
    "status_desc": "OK",
    "message": "Completed",
    "data": {"no_kk": {"value": "9901012609260001", "confidence": 0.97}},
    "errors": None,
    "request_id": RID,
    "guardrails": 0,
}


async def test_one_row_per_request_the_latest_answer_winning(database):
    url, table = database
    results = SqlFinalResults(url)

    await results.save(RID, PROCESSING)
    [first] = await _rows(url, table)
    await results.save(RID, COMPLETED)
    [row] = await _rows(url, table)

    assert (row["request_id"], row["status_code"], row["status_desc"], row["message"]) == (RID, 200, "OK", "Completed")
    assert (row["data"], row["errors"], row["guardrails"]) == (COMPLETED["data"], None, 0)
    assert row["created_at"] == first["created_at"]
    assert row["updated_at"] >= first["updated_at"]
    assert first["guardrails"] is None
    assert len(row["ds"]) == 8


async def test_a_write_that_fails_is_logged_and_does_not_raise(tmp_path, caplog):
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"  # no table: the upsert fails

    await SqlFinalResults(url).save(RID, COMPLETED)

    assert "final result of OCR_final_results not recorded" in caplog.text
    await dispose_engines()


def test_the_live_and_testing_endpoints_write_their_own_tables():
    assert SqlFinalResults("sqlite+aiosqlite://")._table.name == "nilam_ocr_results"
    assert SqlFinalResults("sqlite+aiosqlite://", table_prefix="testing_")._table.name == "nilam_testing_ocr_results"
