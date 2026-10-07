"""Keeps the final answer of every request in `nilam_ocr_results`: the `/v1/extract-ocr` envelope as it was last
given, one row per request_id. Every POST and GET that answers a request upserts its row, so a request answered
`202` and later polled ends with its `200`/`400`/`422`; `created_at` stays the first answer's, `updated_at` is the
last one's.

Best-effort, as `guardrails_log`: a write that fails or takes longer than `timeout` is logged, and the request is
answered all the same, because the entry point must not go down with its record.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import MetaData
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ocr_common.pipeline.database import get_engine
from ocr_common.pipeline.tables import final_results_table

logger = logging.getLogger(__name__)


class FinalResults(Protocol):
    async def save(self, request_id: str, body: dict[str, Any]) -> None: ...


class NoFinalResults:
    """Without DATABASE_URL (local runs, tests): nothing is kept."""

    async def save(self, request_id: str, body: dict[str, Any]) -> None:
        return None


class SqlFinalResults:
    def __init__(self, database_url: str, *, table_prefix: str = "", timeout: float = 2.0):
        self._url = database_url
        self._table = final_results_table(MetaData(), table_prefix)
        self._timeout = timeout

    async def save(self, request_id: str, body: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(self._upsert(request_id, body), self._timeout)
        except Exception:  # noqa: BLE001 - best-effort, see the module docstring
            logger.exception("final result of %s not recorded", request_id)

    async def _upsert(self, request_id: str, body: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        answer = {
            "status_code": body["status_code"],
            "status_desc": body.get("status_desc"),
            "message": body.get("message"),
            "data": body.get("data"),
            "errors": body.get("errors"),
            "guardrails": body.get("guardrails"),
            "updated_at": now,
        }
        async with get_engine(self._url).begin() as conn:
            insert = postgresql_insert if conn.dialect.name == "postgresql" else sqlite_insert
            statement = insert(self._table).values(
                request_id=request_id, created_at=now, ds=now.strftime("%Y%m%d"), **answer
            )
            await conn.execute(statement.on_conflict_do_update(index_elements=[self._table.c.request_id], set_=answer))
