"""The OCR stage's tables become `nilam_ocr_extraction_*`; `nilam_ocr_results` becomes the orchestrator's final result

Revision ID: 0004_ocr_extraction_naming
Revises: 0003_nilam_naming
Create Date: 2026-10-07

`nilam_ocr_jobs` / `nilam_ocr_results` (the OCR stage, written by extraction) are renamed in place to
`nilam_ocr_extraction_jobs` / `nilam_ocr_extraction_results`, the testing lane alike, with the names derived from
them (indexes, primary keys, the foreign key) following, as 0003 did. Only the catalog changes; the rows stay.

The freed name is then taken by a new table: `nilam_ocr_results` (and `nilam_testing_ocr_results`) holds the final
envelope the orchestrator answers on `/v1/extract-ocr`, one row per request_id, upserted on every answer.

Pods on an image that still addresses `nilam_ocr_jobs` fail their queries between this migration and their
replacement -- and an old extraction pod would find a `nilam_ocr_results` of another shape -- so roll the
services out together with it. Anything outside this repository that reads the OCR stage's tables has to use the
new names from then on.

The names are written out rather than taken from `tables.py`, so this revision keeps acting on the names it was
written for.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ocr_extraction_naming"
down_revision: str | None = "0003_nilam_naming"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

SCHEMA = "nilam_ocr_kk"
LANES = ("nilam_", "nilam_testing_")

RENAMES = {
    f"{lane}{old}": f"{lane}{new}"
    for lane in LANES
    for old, new in (("ocr_jobs", "ocr_extraction_jobs"), ("ocr_results", "ocr_extraction_results"))
}

FINAL_TABLES = tuple(f"{lane}ocr_results" for lane in LANES)

FINAL_STATEMENTS = (
    """
    CREATE TABLE "{schema}"."{name}" (
        request_id      TEXT PRIMARY KEY,
        status_code     INT NOT NULL,
        status_desc     TEXT,
        message         TEXT,
        data            JSONB,
        errors          JSONB,
        guardrails      INT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        ds              TEXT NOT NULL
    )
    """,
    'CREATE INDEX "idx_{name}_ds" ON "{schema}"."{name}" (ds)',
)

# Every object named after the table: its indexes (renaming the index of a primary key or unique constraint renames
# the constraint), its other constraints (the foreign keys) and the sequences it owns. As in 0003.
DERIVED_NAMES = sa.text(
    """
    SELECT 'INDEX' AS kind, i.relname AS name
      FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
     WHERE x.indrelid = CAST(:table AS regclass)
    UNION ALL
    SELECT 'CONSTRAINT', c.conname
      FROM pg_constraint c
     WHERE c.conrelid = CAST(:table AS regclass) AND c.contype NOT IN ('p', 'u', 'x')
    UNION ALL
    SELECT 'SEQUENCE', s.relname
      FROM pg_depend d JOIN pg_class s ON s.oid = d.objid
     WHERE d.refobjid = CAST(:table AS regclass) AND s.relkind = 'S' AND d.deptype IN ('a', 'i')
    """
)


def _rename_derived(table: str, old: str, new: str) -> None:
    """Puts `new` in place of `old` in the names of the objects named after the table, now called `table`."""
    conn = op.get_bind()
    for kind, name in conn.execute(DERIVED_NAMES, {"table": f'"{SCHEMA}"."{table}"'}).all():
        if old not in name:
            continue
        renamed = name.replace(old, new, 1)
        if kind == "CONSTRAINT":
            op.execute(sa.text(f'ALTER TABLE "{SCHEMA}"."{table}" RENAME CONSTRAINT "{name}" TO "{renamed}"'))
        else:
            op.execute(sa.text(f'ALTER {kind} "{SCHEMA}"."{name}" RENAME TO "{renamed}"'))


def _rename(names: dict[str, str]) -> None:
    """Renames every table after `names` (old name -> new name) inside `SCHEMA`."""
    conn = op.get_bind()
    for old, new in names.items():
        if conn.execute(sa.text("SELECT to_regclass(:name)"), {"name": f'"{SCHEMA}"."{new}"'}).scalar():
            raise RuntimeError(
                f"{SCHEMA}.{new} already exists next to {SCHEMA}.{old}; compare the two by hand before running "
                "this migration again"
            )
        op.execute(sa.text(f'ALTER TABLE "{SCHEMA}"."{old}" RENAME TO "{new}"'))
        _rename_derived(new, old, new)
        rows = conn.execute(sa.text(f'SELECT count(*) FROM "{SCHEMA}"."{new}"')).scalar()
        log.info("renamed %s.%s to %s (%s rows)", SCHEMA, old, new, rows)


def upgrade() -> None:
    _rename(RENAMES)
    for name in FINAL_TABLES:
        for statement in FINAL_STATEMENTS:
            op.execute(statement.format(schema=SCHEMA, name=name))


def downgrade() -> None:
    for name in FINAL_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}"."{name}"')
    _rename({new: old for old, new in RENAMES.items()})
