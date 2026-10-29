"""`nilam_ocr_results` takes the envelope's shape: an `id` key, `request_id` unique, `pipeline_last_stage`; `ds` goes

Revision ID: 0005_final_results_shape
Revises: 0005_drop_ocr_results_updated_at
Create Date: 2026-10-09

0004 created `nilam_ocr_results` (and `nilam_testing_ocr_results`) keyed by `request_id`, with `updated_at` and `ds`;
0005_drop_ocr_results_updated_at dropped `updated_at`. The table now holds the envelope as the orchestrator answers
it: a surrogate `id` is the primary key, `request_id` is unique, `pipeline_last_stage` names the service the answer
comes from, and `ds` is dropped. The rows stay; the new `id` is a BIGSERIAL, as in the outbox and the guardrails
verdicts, and numbers them as it is added.

This revision was written next to 0005_drop_ocr_results_updated_at, both on top of 0004, which left two heads. It now
follows it and keeps its revision id, so a database that already ran it is still recognised. Such a database never
ran 0005_drop_ocr_results_updated_at, but this revision dropped `updated_at` itself, so it lacks nothing: the drop
here is `IF EXISTS` for that reason.

The downgrade puts `ds` back from `created_at` (UTC, as the service wrote it); `updated_at` is put back by the
downgrade of 0005_drop_ocr_results_updated_at.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_final_results_shape"
down_revision: str | None = "0005_drop_ocr_results_updated_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "nilam_ocr_kk"
TABLES = ("nilam_ocr_results", "nilam_testing_ocr_results")


def upgrade() -> None:
    for name in TABLES:
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" DROP CONSTRAINT "{name}_pkey"')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" ADD COLUMN id BIGSERIAL NOT NULL')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" ADD PRIMARY KEY (id)')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" ADD CONSTRAINT "uq_{name}_request_id" UNIQUE (request_id)')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" ADD COLUMN pipeline_last_stage TEXT')
        op.execute(f'DROP INDEX "{SCHEMA}"."idx_{name}_ds"')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" DROP COLUMN IF EXISTS updated_at, DROP COLUMN ds')


def downgrade() -> None:
    for name in TABLES:
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" ADD COLUMN ds TEXT')
        op.execute(f'UPDATE "{SCHEMA}"."{name}" SET ds = to_char(created_at AT TIME ZONE \'UTC\', \'YYYYMMDD\')')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" ALTER COLUMN ds SET NOT NULL')
        op.execute(f'CREATE INDEX "idx_{name}_ds" ON "{SCHEMA}"."{name}" (ds)')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" DROP COLUMN pipeline_last_stage')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" DROP CONSTRAINT "uq_{name}_request_id"')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" DROP CONSTRAINT "{name}_pkey"')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" DROP COLUMN id')
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" ADD PRIMARY KEY (request_id)')
