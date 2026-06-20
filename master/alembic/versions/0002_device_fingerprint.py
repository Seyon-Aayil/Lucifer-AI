"""device fingerprint column

Revision ID: 0002_device_fingerprint
Revises: 0001_initial_schema
Create Date: 2026-06-01

Adds the nullable ``devices.fingerprint`` column. As with 0001, the
canonical DDL lives in ``infra/postgres/migrations/`` and this wrapper
executes it so ``alembic upgrade head`` stays the single workflow.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_device_fingerprint"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SQL_FILE = _REPO_ROOT / "infra" / "postgres" / "migrations" / "0002_device_fingerprint.sql"


def upgrade() -> None:
    op.execute(_SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS fingerprint;")
