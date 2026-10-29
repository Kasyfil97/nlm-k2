"""`nilam_ocr_results` as in the sibling pipeline: an append-only log of the answers, one row per answer

Revision ID: 0006_final_results_append_only
Revises: 0005_final_results_shape
Create Date: 2026-10-10

Until now `nilam_ocr_results` (and `nilam_testing_ocr_results`) held one row per request_id, upserted by every POST
and GET that answered it. It now has the sibling pipeline's shape: every answer to `POST /v1/extract-ocr` is
another row, so is every result callback a stage delivered, and a trigger refuses UPDATE and DELETE (TRUNCATE stays
possible). The rows already there are kept, each
as the one answer it was. Changes of the columns:

- `request_id` stops being unique and gets an index (`idx_<table>_request_id`);
- `status_desc` is NOT NULL (a NULL becomes the description of its `status_code`, as the envelope gives it);
- `errors` goes from JSONB to TEXT (a JSON string keeps its text, anything else its JSON text);
- `guardrails` goes from INTEGER to DOUBLE PRECISION; the 0 / 1 in it stay as they are;
- the columns are in the order id, request_id, status_code, status_desc, message, data, errors,
  pipeline_last_stage, guardrails, created_at. PostgreSQL cannot reorder columns, so each table is rebuilt: a new
  table in that order, every row copied with its id (the id sequence goes on after them), the old table dropped and
  the new one renamed. All in the migration's one transaction.

Stop the orchestrator before it and deploy right after: the previous code upserts on `request_id`, which needs the
unique key this revision removes. The downgrade keeps the newest row of each request_id and puts the old types and
the unique key back; a probability in `guardrails` is rounded to 0 or 1.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_final_results_append_only"
down_revision: str | None = "0005_final_results_shape"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

SCHEMA = "nilam_ocr_kk"
TABLES = ("nilam_ocr_results", "nilam_testing_ocr_results")
FUNCTION = "nilam_append_only"
# `STATUS_DESC` of `ocr_common.web.envelope`, frozen here: a migration does not follow the code.
_STATUS_DESC = {
    200: "OK",
    202: "Accepted",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}
_WHEN = " ".join(f"WHEN {code} THEN '{desc}'" for code, desc in _STATUS_DESC.items())
_STATUS_DESC_SQL = f"CASE status_code {_WHEN} ELSE 'Error' END"
COLUMNS = "id, request_id, status_code, status_desc, message, data, errors, pipeline_last_stage, guardrails, created_at"


def _trigger(table: str) -> str:
    return (
        f'CREATE TRIGGER "{table}_append_only" BEFORE UPDATE OR DELETE ON "{SCHEMA}"."{table}" '
        f'FOR EACH ROW EXECUTE FUNCTION "{SCHEMA}"."{FUNCTION}"()'
    )


def _rebuild(table: str, columns: str, select: str, unique: bool) -> int:
    """Replaces `table` by a copy with `columns` (the DDL between the parentheses), filled by `select`."""
    conn = op.get_bind()
    new = f"{table}_new"
    op.execute(sa.text(f'CREATE TABLE "{SCHEMA}"."{new}" ({columns})'))
    op.execute(
        sa.text(f'INSERT INTO "{SCHEMA}"."{new}" ({COLUMNS}) SELECT {select} FROM "{SCHEMA}"."{table}" ORDER BY id')
    )
    op.execute(
        sa.text(
            f"SELECT setval(pg_get_serial_sequence('\"{SCHEMA}\".\"{new}\"', 'id'), "
            f'COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM "{SCHEMA}"."{new}"'
        )
    )
    rows = conn.execute(sa.text(f'SELECT count(*) FROM "{SCHEMA}"."{new}"')).scalar() or 0
    op.execute(sa.text(f'DROP TABLE "{SCHEMA}"."{table}"'))  # its trigger, index, key and id sequence with it
    op.execute(sa.text(f'ALTER TABLE "{SCHEMA}"."{new}" RENAME TO "{table}"'))
    op.execute(sa.text(f'ALTER SEQUENCE "{SCHEMA}"."{new}_id_seq" RENAME TO "{table}_id_seq"'))
    op.execute(sa.text(f'ALTER INDEX "{SCHEMA}"."{new}_pkey" RENAME TO "{table}_pkey"'))
    if unique:
        op.execute(
            sa.text(f'ALTER TABLE "{SCHEMA}"."{table}" ADD CONSTRAINT "uq_{table}_request_id" UNIQUE (request_id)')
        )
    return int(rows)


def upgrade() -> None:
    op.execute(
        sa.text(
            f'CREATE OR REPLACE FUNCTION "{SCHEMA}"."{FUNCTION}"() RETURNS trigger LANGUAGE plpgsql AS $$ '
            "BEGIN RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP; END $$"
        )
    )
    for table in TABLES:
        rows = _rebuild(
            table,
            "id BIGSERIAL PRIMARY KEY, "
            "request_id TEXT NOT NULL, "
            "status_code INTEGER NOT NULL, "
            "status_desc TEXT NOT NULL, "
            "message TEXT, "
            "data JSONB, "
            "errors TEXT, "
            "pipeline_last_stage TEXT, "
            "guardrails DOUBLE PRECISION, "
            "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()",
            f"id, request_id, status_code, COALESCE(status_desc, {_STATUS_DESC_SQL}), message, data, "
            "CASE WHEN jsonb_typeof(errors) = 'string' THEN errors #>> '{}' ELSE errors::text END, "
            "pipeline_last_stage, guardrails::double precision, created_at",
            unique=False,
        )
        op.create_index(f"idx_{table}_request_id", table, ["request_id"], schema=SCHEMA)
        op.execute(sa.text(_trigger(table)))
        log.info("rebuilt %s.%s as an append-only log (%s rows)", SCHEMA, table, rows)


def downgrade() -> None:
    for table in TABLES:
        op.execute(sa.text(f'DROP TRIGGER "{table}_append_only" ON "{SCHEMA}"."{table}"'))
        # Back to one row per request_id: its newest one.
        op.execute(
            sa.text(
                f'DELETE FROM "{SCHEMA}"."{table}" t USING "{SCHEMA}"."{table}" newer '
                "WHERE newer.request_id = t.request_id AND newer.id > t.id"
            )
        )
        _rebuild(
            table,
            "id BIGSERIAL PRIMARY KEY, "
            "request_id TEXT NOT NULL, "
            "status_code INTEGER NOT NULL, "
            "status_desc TEXT, "
            "message TEXT, "
            "data JSONB, "
            "errors JSONB, "
            "pipeline_last_stage TEXT, "
            "guardrails INTEGER, "
            "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()",
            "id, request_id, status_code, status_desc, message, data, to_jsonb(errors), "
            "pipeline_last_stage, round(guardrails)::integer, created_at",
            unique=True,
        )
    op.execute(sa.text(f'DROP FUNCTION "{SCHEMA}"."{FUNCTION}"()'))
