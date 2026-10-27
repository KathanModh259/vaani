"""Alembic environment configuration."""
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.config import settings
from db import Base

# this is the Alembic Config object
config = context.config

# Interpret the config file for Python logging - but only from the CLI. When
# the app migrates itself at boot (db.migrate), fileConfig would disable every
# logger the app already configured and silence it for the process lifetime.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# Set the database URL from settings unless the caller pinned one (tests).
if not config.attributes.get("url_pinned"):
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# target_metadata for autogenerate
target_metadata = Base.metadata


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=_is_sqlite(url),
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with the given connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the
        # table instead. A no-op on Postgres.
        render_as_batch=connection.dialect.name == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in async mode."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    import asyncio
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
