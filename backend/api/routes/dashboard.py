"""Dashboard analytics, computed from real call logs.

Everything here is scoped to the authenticated user's business, so one
customer can never read another's calls. Days are bucketed in the business's
own time zone - a 2 AM call in Ahmedabad belongs to that day, not to the
previous UTC one.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.business import current_business_id
from db import AppointmentSQL, CallLogSQL, ServiceSQL, get_db
from services.business_profile import load_profile
from services.call_logger import transcript_of

logger = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"])

# Outcomes that mean the AI finished the job without a human.
_RESOLVED = {"booked", "completed_faq"}


def _aware(dt):
    """SQLite returns naive datetimes; they were written as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _open_callbacks_query(business_id: str):
    """Callers promised a callback who have not had one. Transferred calls
    reached a person; everything else escalated is still owed."""
    return select(CallLogSQL).where(
        CallLogSQL.business_id == business_id,
        CallLogSQL.escalation_requested.is_(True),
        CallLogSQL.follow_up_done_at.is_(None),
        CallLogSQL.outcome != "transferred",
    )


def _callback_reason(call) -> str:
    for t in reversed(transcript_of(call)):
        text = str(t.get("text", ""))
        if t.get("speaker") == "event" and text.startswith("Callback requested:"):
            return text.split(":", 1)[1].strip()
    return ""


def _preview(call) -> str:
    return next((t.get("text") for t in transcript_of(call) if t.get("speaker") == "caller"), "")[:110]


@router.get("/dashboard/stats")
async def dashboard_stats(
    days: int = Query(14, ge=1, le=90),
    business_id: str = Depends(current_business_id),
    db_session: AsyncSession = Depends(get_db),
):
    profile = await load_profile(db_session, business_id)
    tz = profile.tz if profile else timezone.utc
    now = datetime.now(timezone.utc)
    today_local = now.astimezone(tz).date()
    first_day = today_local - timedelta(days=days)
    since = datetime.combine(first_day, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)

    total_calls = (await db_session.execute(
        select(func.count()).select_from(CallLogSQL).where(CallLogSQL.business_id == business_id)
    )).scalar_one()

    windowed = (await db_session.execute(
        select(CallLogSQL).where(CallLogSQL.business_id == business_id, CallLogSQL.start_time >= since)
    )).scalars().all()

    def local_day(c):
        return (_aware(c.start_time) or now).astimezone(tz).date()

    booked = sum(1 for c in windowed if c.outcome == "booked")
    resolved = sum(1 for c in windowed if c.outcome in _RESOLVED)
    escalated = sum(1 for c in windowed if c.escalation_requested or c.outcome in ("escalated", "transferred"))
    durations = [c.duration_seconds or 0 for c in windowed]

    # Dense day series so the chart has no gaps on quiet days.
    by_day = Counter(local_day(c) for c in windowed)
    calls_by_day = [
        {"date": (first_day + timedelta(days=i)).isoformat(), "calls": by_day.get(first_day + timedelta(days=i), 0)}
        for i in range(days + 1)
    ]
    outcomes = Counter(c.outcome or "unknown" for c in windowed)

    recent = (await db_session.execute(
        select(CallLogSQL).where(CallLogSQL.business_id == business_id)
        .order_by(CallLogSQL.start_time.desc()).limit(12)
    )).scalars().all()

    upcoming = (await db_session.execute(
        select(AppointmentSQL).where(
            AppointmentSQL.business_id == business_id,
            AppointmentSQL.status.in_(["scheduled", "confirmed"]),
            AppointmentSQL.start_time >= now,
        ).order_by(AppointmentSQL.start_time.asc()).limit(5)
    )).scalars().all()

    open_callbacks = (await db_session.execute(
        select(func.count()).select_from(_open_callbacks_query(business_id).subquery())
    )).scalar_one()

    # The case for keeping Vaani, in the owner's own numbers: calls nobody
    # would have answered, and what the bookings from them are worth.
    after_hours = sum(1 for c in windowed if not c.call_sid.startswith("web_")
                      and profile and not profile.is_open_at(_aware(c.start_time) or now))
    booked_value = (await db_session.execute(
        select(func.coalesce(func.sum(ServiceSQL.price), 0))
        .select_from(AppointmentSQL)
        .join(ServiceSQL, ServiceSQL.id == AppointmentSQL.service_id)
        .where(AppointmentSQL.business_id == business_id,
               AppointmentSQL.source.in_(("phone", "web")),
               AppointmentSQL.status.in_(("scheduled", "confirmed", "completed")),
               AppointmentSQL.created_at >= since)
    )).scalar_one()
    from services import usage
    month_usage = await usage.summary(db_session, profile) if profile else None

    return {
        "window_days": days,
        "open_callbacks": open_callbacks,
        "after_hours_calls": after_hours,
        "booked_value": int(booked_value or 0),   # minor units (paise)
        "usage": month_usage,
        "time_zone": str(tz),
        "total_calls": total_calls,
        "window_calls": len(windowed),
        "today_calls": by_day.get(today_local, 0),
        "bookings": booked,
        "escalations": escalated,
        "booking_rate": round(booked / len(windowed) * 100, 1) if windowed else 0.0,
        "resolution_rate": round(resolved / len(windowed) * 100, 1) if windowed else 0.0,
        "avg_duration": round(sum(durations) / len(durations)) if durations else 0,
        "minutes_used": round(sum(durations) / 60, 1),
        "calls_by_day": calls_by_day,
        "outcome_breakdown": [{"outcome": k, "count": v} for k, v in outcomes.most_common()],
        "recent_calls": [
            {
                "id": c.id,
                "started_at": (_aware(c.start_time) or now).isoformat(),
                "duration_seconds": c.duration_seconds or 0,
                "outcome": c.outcome or "unknown",
                "customer_phone": c.customer_phone,
                "escalated": bool(c.escalation_requested),
                "turns": sum(1 for t in transcript_of(c) if t.get("speaker") in ("caller", "agent")),
                "preview": _preview(c),
            }
            for c in recent
        ],
        "upcoming_appointments": [
            {
                "id": a.id,
                "customer_name": a.customer_name,
                "customer_phone": a.customer_phone,
                "start_time": (_aware(a.start_time) or now).isoformat(),
                "status": a.status,
            }
            for a in upcoming
        ],
    }


@router.get("/dashboard/calls/{call_id}")
async def call_detail(
    call_id: str,
    business_id: str = Depends(current_business_id),
    db_session: AsyncSession = Depends(get_db),
):
    """Full transcript for one call, including what the system did."""
    call = (
        await db_session.execute(select(CallLogSQL).where(CallLogSQL.id == call_id))
    ).scalar_one_or_none()

    # Same 404 whether it is missing or someone else's - do not leak existence.
    if not call or call.business_id != business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")

    return {
        "id": call.id,
        "started_at": (_aware(call.start_time) or datetime.now(timezone.utc)).isoformat(),
        "duration_seconds": call.duration_seconds or 0,
        "outcome": call.outcome or "unknown",
        "customer_phone": call.customer_phone,
        "escalated": bool(call.escalation_requested),
        "transcript": transcript_of(call),
    }


@router.get("/dashboard/callbacks")
async def open_callbacks(
    business_id: str = Depends(current_business_id),
    db_session: AsyncSession = Depends(get_db),
):
    """Every caller still waiting on a promised callback, oldest first."""
    calls = (await db_session.execute(
        _open_callbacks_query(business_id).order_by(CallLogSQL.start_time.asc()).limit(100)
    )).scalars().all()
    now = datetime.now(timezone.utc)
    return [
        {
            "id": c.id,
            "started_at": (_aware(c.start_time) or now).isoformat(),
            "customer_phone": c.customer_phone,
            "reason": _callback_reason(c),
            "preview": _preview(c),
        }
        for c in calls
    ]


class FollowUp(BaseModel):
    done: bool = True


@router.post("/dashboard/calls/{call_id}/follow-up")
async def set_follow_up(
    call_id: str,
    body: FollowUp,
    business_id: str = Depends(current_business_id),
    db_session: AsyncSession = Depends(get_db),
):
    call = (await db_session.execute(select(CallLogSQL).where(CallLogSQL.id == call_id))).scalar_one_or_none()
    if not call or call.business_id != business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")
    call.follow_up_done_at = datetime.now(timezone.utc) if body.done else None
    await db_session.commit()
    return {"id": call.id, "follow_up_done": body.done}
