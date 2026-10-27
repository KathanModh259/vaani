"""Day-before appointment reminders - the cheapest cure for no-shows.

Every REMINDER_INTERVAL the app looks for active appointments starting
REMINDER_AHEAD from now (a window wide enough that a missed run still catches
them) at businesses with reminders on, and messages each caller once.

"Once" survives several replicas: each appointment is claimed with an atomic
UPDATE ... WHERE reminder_sent_at IS NULL, and only the process whose update
changed a row sends. A send that fails after claiming is not retried - one
missed reminder beats a customer getting three.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update

from db import AppointmentSQL, BusinessSQL, ServiceSQL

logger = logging.getLogger(__name__)

REMINDER_INTERVAL = timedelta(minutes=10)
WINDOW_START = timedelta(hours=20)
WINDOW_END = timedelta(hours=26)


async def send_due(session, now: Optional[datetime] = None) -> int:
    from services.business_profile import load_profile
    from services.notify import send_reminder

    now = now or datetime.now(timezone.utc)
    due = (await session.execute(
        select(AppointmentSQL.id, AppointmentSQL.business_id, AppointmentSQL.customer_phone,
               AppointmentSQL.start_time, ServiceSQL.name)
        .join(BusinessSQL, BusinessSQL.id == AppointmentSQL.business_id)
        .join(ServiceSQL, ServiceSQL.id == AppointmentSQL.service_id, isouter=True)
        .where(
            BusinessSQL.reminders_enabled.is_(True),
            AppointmentSQL.status.in_(("scheduled", "confirmed")),
            AppointmentSQL.reminder_sent_at.is_(None),
            AppointmentSQL.start_time >= now + WINDOW_START,
            AppointmentSQL.start_time <= now + WINDOW_END,
        )
    )).all()

    sent, profiles = 0, {}
    for appt_id, business_id, phone, start, service_name in due:
        claimed = await session.execute(
            update(AppointmentSQL)
            .where(AppointmentSQL.id == appt_id, AppointmentSQL.reminder_sent_at.is_(None))
            .values(reminder_sent_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if claimed.rowcount != 1 or not phone:
            continue  # another replica has it, or there is nobody to text
        if business_id not in profiles:
            profiles[business_id] = await load_profile(session, business_id)
        profile = profiles[business_id]
        if profile is None:
            continue
        start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        if await send_reminder(session, profile, appt_id, phone, service_name or "appointment",
                               start.astimezone(profile.tz)):
            sent += 1
    if sent:
        logger.info("Sent %d appointment reminders", sent)
    return sent


async def reminder_loop() -> None:
    from db import get_manager

    while True:
        try:
            session = await (await get_manager()).get_session()
            try:
                await send_due(session)
            finally:
                await session.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Reminder run failed: %s", e)
        await asyncio.sleep(REMINDER_INTERVAL.total_seconds())
