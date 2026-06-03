"""
master.alembic.env
==================
Alembic migration environment for Lucifer AI.

The database URL is pulled from ``master.core.config.get_settings()`` so there
is a single source of truth for connection settings — never hardcode it in
``alembic.ini``. Migrations run against the async asyncpg engine via
``connection.run_sync``.

Lucifer's schema is currently managed with explicit, hand-written migrations
(see ``alembic/versions/``); autogenerate is intentionally not wired to ORM
metadata because the canonical DDL lives in ``infra/postgres/migrations/``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from master.core.config import get_settings
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Alembic Config object — provides access to alembic.ini values.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM metadata: migrations are explicit. Autogenerate is not used.
target_metadata = None


def _database_url() -> str:
    """Return the async (asyncpg) DSN from application settings."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Create an async engine and run migrations within a sync-bridged context."""
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database using the async engine."""
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
