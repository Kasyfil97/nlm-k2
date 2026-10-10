"""Logs every answer to `POST /v1/extract-ocr` in `nilam_ocr_results`: the envelope as it was given, one append-only
row per answer. The `GET` that polls a request is not logged: the outcome of a request answered `202` reaches the
table from the stage that ends it, with its result callback (`ocr_common.pipeline.ocr_results_sql`). A request_id's
newest row (highest `id`) is the last answer the central orchestrator got.

Best-effort, as `guardrails_log`: a write that fails or takes longer than `timeout` is logged, and the request is
answered all the same, because the entry point must not go down with its record.
"""

import asyncio
import logging
from typing import Any, Protocol

from sqlalchemy import MetaData

from ocr_common.pipeline.database import get_engine
from ocr_common.pipeline.ocr_results_sql import write_ocr_result
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
            await asyncio.wait_for(self._insert(request_id, body), self._timeout)
        except Exception:  # noqa: BLE001 - best-effort, see the module docstring
            logger.exception("final result of %s not recorded", request_id)

    async def _insert(self, request_id: str, body: dict[str, Any]) -> None:
        async with get_engine(self._url).begin() as conn:
            await write_ocr_result(
                conn,
                self._table,
                request_id,
                body["status_code"],
                body.get("message"),
                data=body.get("data"),
                errors=body.get("errors"),
                guardrails=body.get("guardrails"),
                pipeline_last_stage=body.get("pipeline_last_stage"),
            )
