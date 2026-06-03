"""initial schema (telemetry, budgets, oauth, audit, artifacts, devices)

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-31

Baseline migration. To avoid duplicating DDL, this revision executes the
canonical SQL file that already lives in ``infra/postgres/migrations/``.
That file remains the single source of truth for the initial schema; this
wrapper makes it reachable through the standard ``alembic upgrade head``
workflow documented in the Makefile and README.

The downgrade drops everything the SQL file creates, in reverse dependency
order, so a full ``alembic downgrade base`` returns a clean database.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Canonical DDL lives in infra/postgres/migrations/ — resolve relative to repo root.
# alembic.ini sets prepend_sys_path = .. so cwd is master/; the repo root is one up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SQL_FILE = _REPO_ROOT / "infra" / "postgres" / "migrations" / "0001_initial_schema.sql"


def upgrade() -> None:
    sql = _SQL_FILE.read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_oauth_tokens_updated_at ON oauth_tokens;
        DROP TRIGGER IF EXISTS trg_agent_budgets_updated_at ON agent_budgets;
        DROP FUNCTION IF EXISTS update_updated_at();
        DROP TABLE IF EXISTS refresh_tokens;
        DROP TABLE IF EXISTS devices;
        DROP TABLE IF EXISTS artifacts;
        DROP TABLE IF EXISTS audit_log;
        DROP TABLE IF EXISTS oauth_tokens;
        DROP TABLE IF EXISTS daily_spend;
        DROP TABLE IF EXISTS agent_budgets;
        DROP TABLE IF EXISTS telemetry_events;
        DROP TYPE IF EXISTS surface;
        DROP TYPE IF EXISTS agent_status;
        DROP TYPE IF EXISTS risk_tier;
        DROP TYPE IF EXISTS data_classification;
        """
    )
