import pytest

from ocr_common.errors import ServiceError
from ocr_common.pipeline import STAGE_OCR, database
from ocr_common.pipeline.repository_sql import SqlJobRepository
from ocr_common.pipeline.results import load_upstream
from ocr_common.pipeline.results_sql import SqlStageResults
from ocr_common.pipeline.tables import OCR_STAGE_TABLE_PREFIX

RID = "REQ_ref"
OCR = {
    "engine": "mock",
    "texts": [{"text": "3273012345678901", "score": 0.9, "poly": [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]]}],
}


@pytest.fixture
async def stored(tmp_path):
    await database.dispose_engines()
    url = f"sqlite+aiosqlite:///{tmp_path / 'results.db'}"
    repository = SqlJobRepository(url, OCR_STAGE_TABLE_PREFIX, stage=STAGE_OCR)
    async with repository.engine.begin() as conn:
        await conn.run_sync(repository.metadata.create_all)
    await repository.claim(RID)
    await repository.complete(RID, OCR)
    yield SqlStageResults(url)
    await database.dispose_engines()


async def test_reads_what_the_earlier_stage_stored(stored):
    assert await stored.get(OCR_STAGE_TABLE_PREFIX, RID) == OCR
    assert await load_upstream(stored, OCR_STAGE_TABLE_PREFIX, RID) == OCR


async def test_unknown_request_is_none_and_fails_the_job_when_required(stored):
    assert await stored.get(OCR_STAGE_TABLE_PREFIX, "REQ_other") is None
    with pytest.raises(ServiceError, match="no ocr_extraction result stored for REQ_other"):
        await load_upstream(stored, OCR_STAGE_TABLE_PREFIX, "REQ_other")


async def test_without_a_database_the_reference_cannot_be_followed():
    with pytest.raises(ServiceError, match="no DATABASE_URL"):
        await load_upstream(None, OCR_STAGE_TABLE_PREFIX, RID)
