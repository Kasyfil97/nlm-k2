"""Every answer to POST /v1/extract-ocr is appended to ocr_results, one row per answer (the GET that polls is not
logged), and a write that fails never fails the request."""

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
    assert final_results.saved[RID] == [response.json()]


def test_a_rejection_is_kept_too(client, auth, final_results):
    rejected = _submit(client, auth, filename="notkk.jpg")
    assert final_results.saved[RID] == [rejected.json()]
    assert (rejected.status_code, rejected.json()["guardrails"]) == (400, 1)


def test_the_get_is_not_logged(client, auth, stub_waiter, final_results):
    stub_waiter.outcome = WaitOutcome("STRUCTURING", "PROCESSING")
    assert _submit(client, auth).status_code == 202
    assert [answer["status_code"] for answer in final_results.saved[RID]] == [202]

    assert client.get(f"/v1/extract-ocr/{RID}", headers=auth).status_code == 200

    assert [answer["status_code"] for answer in final_results.saved[RID]] == [202]


def test_a_request_id_run_again_is_logged_again(client, auth, final_results):
    first, second = _submit(client, auth), _submit(client, auth)

    assert final_results.saved[RID] == [first.json(), second.json()]


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
    "pipeline_last_stage": "scoring",
    "guardrails": 0,
}


async def test_every_answer_is_another_row_in_the_order_given(database):
    url, table = database
    results = SqlFinalResults(url)

    await results.save(RID, PROCESSING)
    await results.save(RID, COMPLETED)
    first, last = await _rows(url, table)

    assert (first["request_id"], first["status_code"], first["status_desc"], first["message"]) == (
        RID,
        202,
        "Accepted",
        "Processing",
    )
    assert (first["guardrails"], first["pipeline_last_stage"]) == (None, None)
    assert (last["request_id"], last["status_code"], last["status_desc"], last["message"]) == (
        RID,
        200,
        "OK",
        "Completed",
    )
    assert (last["data"], last["errors"], last["pipeline_last_stage"], last["guardrails"]) == (
        COMPLETED["data"],
        None,
        "scoring",
        0,
    )
    assert last["id"] > first["id"]


async def test_status_desc_follows_the_status_code(database):
    url, table = database

    await SqlFinalResults(url).save(RID, {"status_code": 500, "errors": "INTERNAL_ERROR"})
    await SqlFinalResults(url).save(RID, {"status_code": 599})
    first, unknown = await _rows(url, table)

    assert (first["status_desc"], first["errors"]) == ("Internal Server Error", "INTERNAL_ERROR")
    assert unknown["status_desc"] == "Error"


def test_the_table_has_the_columns_of_the_sibling_pipeline():
    table = final_results_table(MetaData())

    assert [column.name for column in table.columns] == [
        "id",
        "request_id",
        "status_code",
        "status_desc",
        "message",
        "data",
        "errors",
        "pipeline_last_stage",
        "guardrails",
        "created_at",
    ]
    assert not table.c.request_id.unique
    assert any(index.name == "idx_nilam_ocr_results_request_id" for index in table.indexes)


async def test_a_write_that_fails_is_logged_and_does_not_raise(tmp_path, caplog):
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"  # no table: the insert fails

    await SqlFinalResults(url).save(RID, COMPLETED)

    assert "final result of OCR_final_results not recorded" in caplog.text
    await dispose_engines()


def test_the_live_and_testing_endpoints_write_their_own_tables():
    assert SqlFinalResults("sqlite+aiosqlite://")._table.name == "nilam_ocr_results"
    assert SqlFinalResults("sqlite+aiosqlite://", table_prefix="testing_")._table.name == "nilam_testing_ocr_results"
