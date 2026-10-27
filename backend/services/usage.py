"""Monthly minute usage against the allowance, with owner alerts.

Allowance: the business's own monthly_minute_cap if set, else its plan's
max_minutes_monthly (0 = unlimited), else unlimited. Only phone calls count;
the web demo is free.

At 80% and 100% the owner gets one email each per month. With
HARD_MINUTE_CAP on, calls over the allowance are answered with a short
message and logged as callbacks instead of running the AI - a cost backstop
until billing can charge overage.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import func, select

from db import BusinessSQL, CallLogSQL, PlanSQL, SubscriptionSQL, UserSQL
from scripts.config import settings

logger = logging.getLogger(__name__)

THRESHOLDS = (80, 100)


def _month_start(profile) -> Tuple[datetime, str]:
    local = datetime.now(profile.tz)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), local.strftime("%Y-%m")


async def minutes_this_month(session, profile) -> float:
    since, _ = _month_start(profile)
    seconds = (await session.execute(
        select(func.coalesce(func.sum(CallLogSQL.duration_seconds), 0)).where(
            CallLogSQL.business_id == profile.id,
            CallLogSQL.start_time >= since,
            ~CallLogSQL.call_sid.startswith("web_", autoescape=True),
        )
    )).scalar_one()
    return round((seconds or 0) / 60, 1)


async def allowance(session, profile) -> Optional[int]:
    if profile.monthly_minute_cap:
        return profile.monthly_minute_cap
    plan_minutes = (await session.execute(
        select(PlanSQL.max_minutes_monthly)
        .join(SubscriptionSQL, SubscriptionSQL.plan_id == PlanSQL.id)
        .join(UserSQL, UserSQL.id == SubscriptionSQL.user_id)
        .where(UserSQL.business_id == profile.id,
               SubscriptionSQL.status.in_(["active", "trialing", "past_due"]))
        .order_by(SubscriptionSQL.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    return plan_minutes or None


async def summary(session, profile) -> dict:
    used = await minutes_this_month(session, profile)
    limit = await allowance(session, profile)
    return {"minutes_used": used, "minutes_allowed": limit,
            "percent": round(used / limit * 100) if limit else None,
            "hard_cap": settings.HARD_MINUTE_CAP}


async def over_hard_cap(session, profile) -> bool:
    if not settings.HARD_MINUTE_CAP:
        return False
    limit = await allowance(session, profile)
    return bool(limit) and await minutes_this_month(session, profile) >= limit


async def check_and_alert(session, profile) -> Optional[int]:
    """Email the owner the first time each threshold is crossed this month.
    Returns the threshold alerted, if any."""
    from services.email import public_url, queue_email
    from services.owner_alerts import _alertable

    limit = await allowance(session, profile)
    if not limit or not _alertable(profile):
        return None
    used = await minutes_this_month(session, profile)
    pct = used / limit * 100
    _, month = _month_start(profile)
    crossed = max((t for t in THRESHOLDS if pct >= t), default=None)
    if crossed is None:
        return None

    b = (await session.execute(select(BusinessSQL).where(BusinessSQL.id == profile.id))).scalar_one()
    prior = b.usage_alert_state or ""
    if prior.startswith(month + ":") and int(prior.split(":")[1]) >= crossed:
        return None
    b.usage_alert_state = f"{month}:{crossed}"
    await session.commit()

    what = ("has used its full monthly allowance" if crossed >= 100
            else f"has used {crossed}% of its monthly allowance")
    after = (" New calls are now answered with a short message and logged as callbacks."
             if crossed >= 100 and settings.HARD_MINUTE_CAP else "")
    queue_email(profile.email, f"Vaani usage: {crossed}% of this month's minutes", (
        f"{profile.name} {what}: {used:.0f} of {limit} minutes.{after}\n\n"
        f"Usage: {public_url('/dashboard')}\n"
    ))
    return crossed
