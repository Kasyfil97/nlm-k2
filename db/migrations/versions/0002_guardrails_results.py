"""guardrails_results: every guardrails verdict, the rejected documents included

Revision ID: 0002_guardrails_results
Revises: 0001_baseline
Create Date: 2026-09-29

Ported from nilam (its 0008). The orchestrator writes one row per guardrails check: passed or not,
the document confidence, the threshold that decided and whose it was, the page count, the request's
pipeline_name_sequence, and the full report (`probability_bad` included). Before this, a document
rejected by guardrails left no trace in this database, and a guardrails-only request could not be
read back. Append-only: sending the same request_id again judges it again.
`testing_guardrails_results` is the same table for the `-test` endpoints.

The only table the orchestrator owns; it is otherwise stateless.

`IF NOT EXISTS`, like the baseline and unlike nilam's 0008: a database that already has the tables
but no version row (CI's "adopt a database" step) must upgrade cleanly.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_guardrails_results"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("guardrails_results", "testing_guardrails_results")

STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS {name} (
        id                      BIGSERIAL PRIMARY KEY,
        request_id              TEXT NOT NULL,
        passed                  BOOLEAN NOT NULL,
        verdict                 TEXT,
        confidence              DOUBLE PRECISION,
        threshold               DOUBLE PRECISION,
        threshold_target        TEXT,
        threshold_source        TEXT NOT NULL,
        n_pages                 INT,
        reason                  TEXT,
        pipeline_name_sequence  JSONB,
        report                  JSONB NOT NULL,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        ds                      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_{name}_request_id ON {name} (request_id)",
    "CREATE INDEX IF NOT EXISTS idx_{name}_ds ON {name} (ds)",
)


def upgrade() -> None:
    for name in TABLES:
        for statement in STATEMENTS:
            op.execute(statement.format(name=name))


def downgrade() -> None:
    for name in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name}")
