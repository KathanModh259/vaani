"""Transcript retention: "Retention is yours to set" (landing page).

Each business may set retention_days; DEFAULT_RETENTION_DAYS covers the rest.
Expired calls are scrubbed, not deleted: the transcript and caller number go,
the outcome and duration stay, so the owner's statistics survive while the
personal data does not.

Runs every RETENTION_INTERVAL inside the app (idempotent, so several replicas
running it is harmless) and on demand:  python -m services.retention
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import null, select, update

from db import BusinessSQL, CallLogSQL
from scripts.config import settings

logger = logging.getLogger(__name__)

RETENTION_INTERVAL = timedelta(hours=6)


async def purge_expired(session, now: Optional[datetime] = None) -> int:
    """Scrub transcripts past each business's retention. Returns calls scrubbed."""
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(BusinessSQL.id, BusinessSQL.retention_days))).all()
    total = 0
    for business_id, days in rows:
        days = days or settings.DEFAULT_RETENTION_DAYS
        if not days:
            continue
        cutoff = now - timedelta(days=days)
        result = await session.execute(
            update(CallLogSQL)
            .where(CallLogSQL.business_id == business_id,
                   CallLogSQL.start_time < cutoff,
                   CallLogSQL.transcript.is_not(None))
            # null(), not None: the JSON type would store None as the JSON
            # literal 'null', which is not SQL NULL and would match forever.
            .values(transcript=null(), customer_phone=None)
            # Do not re-evaluate the WHERE against objects loaded in this
            # session: that runs in Python and trips over SQLite's naive
            # datetimes. Callers re-read what they need.
            .execution_options(synchronize_session=False)
        )
        total += result.rowcount or 0
    await session.commit()
    if total:
        logger.info("Retention: scrubbed %d expired call transcripts", total)
    return total


async def retention_loop() -> None:
    from db import get_manager

    while True:
        try:
            session = await (await get_manager()).get_session()
            try:
                await purge_expired(session)
            finally:
                await session.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Retention run failed: %s", e)
        await asyncio.sleep(RETENTION_INTERVAL.total_seconds())


if __name__ == "__main__":
    async def _once():
        from db import close_manager, get_manager
        session = await (await get_manager()).get_session()
        try:
            print(f"Scrubbed {await purge_expired(session)} call transcripts")
        finally:
            await session.close()
            await close_manager()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_once())
