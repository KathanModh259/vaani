"""Dashboard analytics, computed from real call logs.

Everything here is scoped to the authenticated user's business, so one
customer can never read another's calls.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.auth import get_current_user
from db import AppointmentSQL, CallLogSQL, UserSQL, get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"])

# Outcomes that mean the AI finished the job without a human.
_RESOLVED = {"booked", "completed_faq"}


def _require_business(user: UserSQL) -> str:
    if not user.business_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No business is linked to this account yet.",
        )
    return user.business_id


def _aware(dt):
    """SQLite returns naive datetimes; compare everything in UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/dashboard/stats")
async def dashboard_stats(
    days: int = Query(14, ge=1, le=90),
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    business_id = _require_business(current_user)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    calls = (
        await db_session.execute(
            select(CallLogSQL)
            .where(CallLogSQL.business_id == business_id)
            .order_by(CallLogSQL.start_time.desc())
        )
    ).scalars().all()

    windowed = [c for c in calls if (_aware(c.start_time) or now) >= since]
    today = now.date()
    todays = [c for c in calls if (_aware(c.start_time) or now).date() == today]

    booked = sum(1 for c in windowed if c.outcome == "booked")
    resolved = sum(1 for c in windowed if c.outcome in _RESOLVED)
    escalated = sum(1 for c in windowed if c.escalation_requested or c.outcome == "escalated")
    durations = [c.duration_seconds or 0 for c in windowed]

    # Dense day series so the chart has no gaps on quiet days.
    by_day = Counter((_aware(c.start_time) or now).date().isoformat() for c in windowed)
    calls_by_day = [
        {
            "date": (since + timedelta(days=i)).date().isoformat(),
            "calls": by_day.get((since + timedelta(days=i)).date().isoformat(), 0),
        }
        for i in range(days + 1)
    ]

    outcomes = Counter(c.outcome or "unknown" for c in windowed)

    upcoming = (
        await db_session.execute(
            select(AppointmentSQL)
            .where(
                AppointmentSQL.business_id == business_id,
                AppointmentSQL.status.in_(["scheduled", "confirmed"]),
            )
            .order_by(AppointmentSQL.start_time.asc())
            .limit(5)
        )
    ).scalars().all()

    return {
        "window_days": days,
        "total_calls": len(calls),
        "window_calls": len(windowed),
        "today_calls": len(todays),
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
                "turns": len(c.transcript or []),
                "preview": next(
                    (t.get("text") for t in (c.transcript or []) if t.get("speaker") == "caller"),
                    "",
                )[:110],
            }
            for c in calls[:12]
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
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    """Full transcript for one call."""
    business_id = _require_business(current_user)

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
        "transcript": call.transcript or [],
    }
