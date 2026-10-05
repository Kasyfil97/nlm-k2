"""CI check of migration 0003: a database at 0002, with its rows and its version table in `public`, ends up with
every table and every row in `nilam_ocr_kk` under the `nilam_` names, keeps counting its ids where it left off, and
survives a downgrade and an upgrade again. Ported from nilam.

    DATABASE_URL=postgresql+asyncpg://... python db/check_schema_move.py   # an EMPTY database: it is rebuilt
"""

import asyncio
import os
import subprocess
import sys

import asyncpg

ALEMBIC = [sys.executable, "-m", "alembic", "-c", os.path.join(os.path.dirname(__file__), "alembic.ini")]
SCHEMA = "nilam_ocr_kk"
PREFIX = "nilam_"
VERSION_TABLE = "nilam_ocr_kk_alembic_version"
OLD_VERSION_TABLE = "ocr_kk_alembic_version"
# The names before 0003; from 0003 on each one carries PREFIX.
TABLES = [
    f"{lane}{name}"
    for lane in ("", "testing_")
    for name in (
        *(f"{stage}_{kind}" for stage in ("ocr", "structuring", "scoring") for kind in ("jobs", "results")),
        "pipeline_outbox",
        "guardrails_results",
    )
]
NEW_TABLES = [f"{PREFIX}{table}" for table in TABLES]


def alembic(*args: str) -> None:
    subprocess.run([*ALEMBIC, *args], check=True)


async def connect() -> asyncpg.Connection:
    return await asyncpg.connect(os.environ["DATABASE_URL"].replace("+asyncpg", ""))


async def tables_in(conn: asyncpg.Connection, schema: str) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1", schema)
    return {row["tablename"] for row in rows}


async def put_version_table(conn: asyncpg.Connection, schema: str) -> None:
    """Puts the version table where, and under the name, the migration image of an older revision kept it."""
    await conn.execute(f"ALTER TABLE {SCHEMA}.{VERSION_TABLE} SET SCHEMA {schema}")
    await conn.execute(f"ALTER TABLE {schema}.{VERSION_TABLE} RENAME TO {OLD_VERSION_TABLE}")
    await conn.execute(f"ALTER INDEX {schema}.{VERSION_TABLE}_pkc RENAME TO {OLD_VERSION_TABLE}_pkc")


async def seed(conn: asyncpg.Connection) -> None:
    for lane in ("", "testing_"):
        for stage in ("ocr", "structuring", "scoring"):
            await conn.execute(
                f"INSERT INTO public.{lane}{stage}_jobs (request_id, status, input, ds) "
                "VALUES ('REQ_1', 'DONE', '{\"document_type\": \"kk\"}', '20261005'), "
                "('REQ_2', 'FAILED', NULL, '20261005')"
            )
            await conn.execute(
                f"INSERT INTO public.{lane}{stage}_results (request_id, result, ds) "
                "VALUES ('REQ_1', '{\"ok\": 1}', '20261005')"
            )
        await conn.execute(
            f"INSERT INTO public.{lane}pipeline_outbox (request_id, stage, kind, payload, ds) "
            "VALUES ('REQ_1', 'OCR', 'handoff', '{}', '20261005'), ('REQ_2', 'OCR', 'callback', '{}', '20261005')"
        )
        await conn.execute(
            f"INSERT INTO public.{lane}guardrails_results (request_id, passed, threshold_source, report, ds) "
            "VALUES ('REQ_1', true, 'service', '{}', '20261005')"
        )


async def counts(conn: asyncpg.Connection, schema: str, prefix: str = "") -> dict[str, int]:
    """Rows per table, keyed by the name before 0003 whatever the table is called in `schema`."""
    return {table: int(await conn.fetchval(f'SELECT count(*) FROM "{schema}"."{prefix}{table}"')) for table in TABLES}


async def check_head(before: dict[str, int]) -> None:
    alembic("upgrade", "head")
    alembic("check")
    conn = await connect()
    try:
        old_names = set(TABLES) | set(NEW_TABLES) | {VERSION_TABLE, OLD_VERSION_TABLE}
        assert not old_names & await tables_in(conn, "public"), "tables left in public"
        assert await tables_in(conn, SCHEMA) == set(NEW_TABLES) | {VERSION_TABLE}
        assert await counts(conn, SCHEMA, PREFIX) == before, "rows lost in the move"
        # The id sequences moved and were renamed with their tables, and carry on after the rows already there.
        sequence = await conn.fetchval(f"SELECT pg_get_serial_sequence('{SCHEMA}.{PREFIX}pipeline_outbox', 'id')")
        assert sequence == f"{SCHEMA}.{PREFIX}pipeline_outbox_id_seq", sequence
        last_id = await conn.fetchval(f"SELECT max(id) FROM {SCHEMA}.{PREFIX}pipeline_outbox")
        new_id = await conn.fetchval(
            f"INSERT INTO {SCHEMA}.{PREFIX}pipeline_outbox (request_id, stage, kind, payload, ds) "
            "VALUES ('REQ_3', 'OCR', 'handoff', '{}', '20261005') RETURNING id"
        )
        assert new_id > last_id, (new_id, last_id)
        await conn.execute(f"DELETE FROM {SCHEMA}.{PREFIX}pipeline_outbox WHERE request_id = 'REQ_3'")
        # Every index, constraint and sequence is named after the table it now belongs to.
        stale = await conn.fetch(
            "SELECT relname FROM pg_class WHERE relnamespace = $1::regnamespace AND relkind IN ('i', 'S') "
            "AND strpos(relname, $2) = 0",
            SCHEMA,
            PREFIX,
        )
        assert not stale, [row["relname"] for row in stale]
        constraints = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE connamespace = $1::regnamespace AND strpos(conname, $2) <> 1",
            SCHEMA,
            PREFIX,
        )
        assert not constraints, [row["conname"] for row in constraints]
        # The foreign key moved too: a result without its job is still refused.
        try:
            await conn.execute(
                f"INSERT INTO {SCHEMA}.{PREFIX}ocr_results (request_id, result, ds) VALUES ('NOPE', '{{}}', '')"
            )
        except asyncpg.ForeignKeyViolationError:
            pass
        else:
            raise AssertionError("nilam_ocr_results lost its foreign key to nilam_ocr_jobs")
    finally:
        await conn.close()


async def main() -> None:
    alembic("upgrade", "0002_guardrails_results")
    conn = await connect()
    try:
        assert set(TABLES) <= await tables_in(conn, "public"), "0002 should leave the tables in public"
        await put_version_table(conn, "public")  # where the migration image up to 0002 kept it
        await seed(conn)
        before = await counts(conn, "public")
    finally:
        await conn.close()

    await check_head(before)
    # Back to 0002 (the version table stays where env.py keeps it now), then up again.
    alembic("downgrade", "0002_guardrails_results")
    conn = await connect()
    try:
        assert await counts(conn, "public") == before, "rows lost moving back to public"
    finally:
        await conn.close()
    await check_head(before)
    print(f"0003 moves every table and row from public to {SCHEMA} under {PREFIX}*, and back")


asyncio.run(main())
