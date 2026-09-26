"""Baseline: the whole schema nlm-k2 owns, in one revision

nilam reached this shape through seven revisions, three of which existed only to dismantle an
earlier design (legacy per-stage schemas, the old synchronous request table). Copying that chain
would make `make db-upgrade` build and then demolish structures this repository never had. The
baseline here is therefore the end state directly.

What it does NOT create, on purpose: `orchestration_extract_ocr` and `ocr.orchestration_api_events`.
Those belong to Orkestrasi pusat and live in the same database; `db/external/*.sql` holds a local
stand-in so the flow can be exercised without them. `include_object` in `env.py` is what keeps
autogenerate from proposing to drop them.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-26

"""

from collections.abc import Sequence

from alembic import op

from ocr_common.pipeline.tables import PIPELINE_TABLE_PREFIXES
from ocr_common.testing_endpoints import TESTING_TABLE_PREFIX

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One pair per stage. `input` carries what a later run needs besides the earlier stages' stored
# results (document_type, guardrails report, file_url), so a job left behind by a dead process can
# be run again; the stale-job reaper hands it back to `resume`.
STAGE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS {prefix}_jobs (
        request_id      TEXT PRIMARY KEY,
        status          TEXT NOT NULL,
        error_message   TEXT,
        attempts        INT NOT NULL DEFAULT 1,
        input           JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        ds              TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_{prefix}_jobs_status ON {prefix}_jobs (status)",
    "CREATE INDEX IF NOT EXISTS idx_{prefix}_jobs_ds ON {prefix}_jobs (ds)",
    """
    CREATE TABLE IF NOT EXISTS {prefix}_results (
        request_id      TEXT PRIMARY KEY REFERENCES {prefix}_jobs (request_id),
        result          JSONB NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        ds              TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_{prefix}_results_ds ON {prefix}_results (ds)",
)

# The transactional outbox, one row per undelivered callback or hand-off. A row that fails
# permanently stays as a dead letter (`failed_at` + `last_error`), is never picked up again, and is
# released by hand. The two partial indexes keep the relay's "due" scan off the dead letters.
OUTBOX_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS {prefix}pipeline_outbox (
        id              BIGSERIAL PRIMARY KEY,
        request_id      TEXT NOT NULL,
        stage           TEXT NOT NULL,
        kind            TEXT NOT NULL,
        payload         JSONB NOT NULL,
        attempts        INT NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        failed_at       TIMESTAMPTZ,
        last_error      TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        ds              TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_{prefix}pipeline_outbox_due
        ON {prefix}pipeline_outbox (stage, next_attempt_at, id) WHERE failed_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_{prefix}pipeline_outbox_dead
        ON {prefix}pipeline_outbox (stage) WHERE failed_at IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_{prefix}pipeline_outbox_request_id
        ON {prefix}pipeline_outbox (request_id)
    """,
)

# Both lanes: the live tables, and their `testing_` twins for the ML team's load tests
# (TESTING_ENDPOINTS). The twins are exact copies and inherit every access rule the live ones have.
LANES = ("", TESTING_TABLE_PREFIX)


def upgrade() -> None:
    for lane in LANES:
        for prefix in PIPELINE_TABLE_PREFIXES:
            for statement in STAGE_STATEMENTS:
                op.execute(statement.format(prefix=f"{lane}{prefix}"))
        for statement in OUTBOX_STATEMENTS:
            op.execute(statement.format(prefix=lane))


def downgrade() -> None:
    for lane in LANES:
        op.execute(f"DROP TABLE IF EXISTS {lane}pipeline_outbox")
        for prefix in PIPELINE_TABLE_PREFIXES:
            op.execute(f"DROP TABLE IF EXISTS {lane}{prefix}_results")
            op.execute(f"DROP TABLE IF EXISTS {lane}{prefix}_jobs")
