"""Bring the database schema up to date with Alembic.

Three starting states have to work:

  * empty database           -> run every migration
  * built by create_all()    -> the app used to do this at boot, so these have
                                the baseline tables but no alembic_version row.
                                Stamp them as 0001, then upgrade.
  * already under Alembic    -> upgrade to head

Called from the app lifespan when AUTO_MIGRATE is on (the default), and usable
as a release step: `python -m db.migrate`. With several app replicas, turn
AUTO_MIGRATE off and run it once per deploy instead, so replicas do not race.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASELINE_REVISION = "0001"


def _alembic_config(url: Optional[str] = None) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # Do not let env.py reconfigure logging inside a running app.
    cfg.attributes["configure_logger"] = False
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
        cfg.attributes["url_pinned"] = True
    return cfg


def _table_names(url: str) -> set:
    """Inspect through the async driver the app already depends on, so no
    sync driver (psycopg2) has to be installed just for this check."""

    async def _names():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        finally:
            await engine.dispose()

    return asyncio.run(_names())


def upgrade_to_head(url: Optional[str] = None) -> None:
    """Synchronous on purpose: env.py calls asyncio.run(), so run this in a
    worker thread (asyncio.to_thread) when calling from async code."""
    from scripts.config import settings

    url = url or settings.DATABASE_URL
    cfg = _alembic_config(url)

    tables = _table_names(url)
    if tables and "alembic_version" not in tables:
        logger.info("Existing schema without Alembic history - stamping baseline %s", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)

    command.upgrade(cfg, "head")
    logger.info("Database schema is at head")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    upgrade_to_head()
