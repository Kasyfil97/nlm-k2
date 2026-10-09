"""`nilam_ocr_results` loses its `updated_at` column

Revision ID: 0005_drop_ocr_results_updated_at
Revises: 0004_ocr_extraction_naming
Create Date: 2026-10-09

The final-result table of the orchestrator (`nilam_ocr_results`, and `nilam_testing_ocr_results` of the testing
lane) no longer carries `updated_at`: the column is dropped. `created_at` stays. No other table is touched; the
`updated_at` of the job, outbox and stage tables is what the lease, the heartbeat and the reaper read.

An orchestrator pod on an image that still upserts `updated_at` fails its write to this table (best-effort, the
answer is still given) between this migration and its replacement, so roll the orchestrator out together with it.

Downgrade adds the column back as NOT NULL DEFAULT now(): the dropped values are not recoverable, existing rows
get the time of the downgrade.

The names are written out rather than taken from `tables.py`, so this revision keeps acting on the names it was
written for.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_drop_ocr_results_updated_at"
down_revision: str | None = "0004_ocr_extraction_naming"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "nilam_ocr_kk"
FINAL_TABLES = ("nilam_ocr_results", "nilam_testing_ocr_results")


def upgrade() -> None:
    for name in FINAL_TABLES:
        op.execute(f'ALTER TABLE "{SCHEMA}"."{name}" DROP COLUMN IF EXISTS updated_at')


def downgrade() -> None:
    for name in FINAL_TABLES:
        op.execute(
            f'ALTER TABLE "{SCHEMA}"."{name}" ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()'
        )
