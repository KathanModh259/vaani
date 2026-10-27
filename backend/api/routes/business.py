"""The owner's side of the receptionist: what Vaani knows and may do.

Everything a caller can be told - hours, services, prices, FAQs, where to
transfer - is edited here. All routes are scoped to the signed-in user's own
business; ids from other businesses 404 exactly like missing ones.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.auth import get_current_user
from db import (
    AppointmentSQL, BusinessHolidaysSQL, BusinessHoursSQL, BusinessSQL, FAQSQL,
    PhoneNumberSQL, ServiceSQL, UserSQL, get_db,
)
from services.phone import normalize_phone

router = APIRouter(tags=["business"])

LANGUAGES = {"en-IN", "hi-IN", "gu-IN"}
_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


def current_business_id(user: UserSQL = Depends(get_current_user)) -> str:
    if not user.business_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="No business is linked to this account yet.")
    return user.business_id


async def _business(session: AsyncSession, business_id: str) -> BusinessSQL:
    b = (await session.execute(select(BusinessSQL).where(BusinessSQL.id == business_id))).scalar_one_or_none()
    if b is None:
        raise HTTPException(status_code=404, detail="Business not found")
    return b


async def _owned(session: AsyncSession, model, row_id: str, business_id: str):
    row = (await session.execute(
        select(model).where(model.id == row_id, model.business_id == business_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return row


# ------------------------------------------------------------------ schemas

class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    phone: Optional[str] = Field(None, max_length=32)
    email: Optional[str] = Field(None, max_length=200)
    greeting: Optional[str] = Field(None, max_length=300)
    about: Optional[str] = Field(None, max_length=2000)
    transfer_phone: Optional[str] = Field(None, max_length=32)
    language: Optional[str] = None
    time_zone: Optional[str] = None
    booking_enabled: Optional[bool] = None
    sms_confirmations: Optional[bool] = None
    slot_minutes: Optional[int] = Field(None, ge=5, le=240)
    retention_days: Optional[int] = Field(None, ge=1, le=3650)
    capacity: Optional[int] = Field(None, ge=1, le=50)
    message_channel: Optional[str] = None
    reminders_enabled: Optional[bool] = None
    forward_from: Optional[str] = Field(None, max_length=32)
    monthly_minute_cap: Optional[int] = Field(None, ge=10, le=100000)

    @field_validator("message_channel")
    @classmethod
    def _channel(cls, v):
        if v is not None and v not in ("sms", "whatsapp"):
            raise ValueError("must be sms or whatsapp")
        return v

    @field_validator("language")
    @classmethod
    def _lang(cls, v):
        if v is not None and v not in LANGUAGES:
            raise ValueError(f"must be one of {sorted(LANGUAGES)}")
        return v

    @field_validator("time_zone")
    @classmethod
    def _tz(cls, v):
        if v is not None:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("unknown time zone")
        return v

    @field_validator("transfer_phone", "forward_from")
    @classmethod
    def _phone(cls, v):
        if v is None or not v.strip():
            return v
        n = normalize_phone(v)
        if not n or not n.startswith("+") or len(n) < 11:
            raise ValueError("enter a full phone number, e.g. +91 98765 43210")
        return n


class DayHoursIn(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6)  # Monday = 0
    is_open: bool
    opens: Optional[str] = Field(None, pattern=_HHMM)
    closes: Optional[str] = Field(None, pattern=_HHMM)


class ServiceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    duration_minutes: int = Field(30, ge=5, le=480)
    price: int = Field(0, ge=0, description="Minor units (paise)")
    buffer_minutes: int = Field(0, ge=0, le=120)


class FAQIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=300)
    answer: str = Field(..., min_length=1, max_length=1000)


class HolidayIn(BaseModel):
    date: date
    reason: Optional[str] = Field(None, max_length=120)


class AppointmentUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in {"scheduled", "confirmed", "completed", "cancelled", "no_show"}:
            raise ValueError("invalid status")
        return v


def _profile_out(b: BusinessSQL) -> dict:
    return {
        "id": b.id, "name": b.name, "phone": b.phone, "email": b.email,
        "greeting": b.greeting, "about": b.about, "transfer_phone": b.transfer_phone,
        "language": b.language, "time_zone": b.time_zone, "currency": b.currency,
        "booking_enabled": b.booking_enabled, "sms_confirmations": b.sms_confirmations,
        "slot_minutes": b.slot_minutes, "retention_days": b.retention_days,
        "capacity": b.capacity, "message_channel": b.message_channel,
        "reminders_enabled": b.reminders_enabled, "forward_from": b.forward_from,
        "monthly_minute_cap": b.monthly_minute_cap,
        "google_calendar": {"connected": bool(b.gcal_token), "calendar_id": b.gcal_calendar_id},
    }


def _service_out(s: ServiceSQL) -> dict:
    return {"id": s.id, "name": s.name, "description": s.description,
            "duration_minutes": s.duration_minutes, "price": s.price, "buffer_minutes": s.buffer_minutes}


# ------------------------------------------------------------------ profile

@router.get("/business")
async def get_business(business_id: str = Depends(current_business_id),
                       session: AsyncSession = Depends(get_db)):
    """The whole receptionist configuration in one call (the settings page)."""
    b = await _business(session, business_id)
    q = lambda m: select(m).where(m.business_id == business_id)  # noqa: E731
    hours = (await session.execute(q(BusinessHoursSQL))).scalars().all()
    services = (await session.execute(q(ServiceSQL).order_by(ServiceSQL.name))).scalars().all()
    faqs = (await session.execute(q(FAQSQL).order_by(FAQSQL.created_at))).scalars().all()
    holidays = (await session.execute(q(BusinessHolidaysSQL).order_by(BusinessHolidaysSQL.date))).scalars().all()
    numbers = (await session.execute(q(PhoneNumberSQL))).scalars().all()
    by_day = {h.day_of_week: h for h in hours}
    return {
        "profile": _profile_out(b),
        "hours": [
            {"day_of_week": d, "is_open": bool(by_day[d].is_open) if d in by_day else False,
             "opens": by_day[d].start_time if d in by_day else None,
             "closes": by_day[d].end_time if d in by_day else None}
            for d in range(7)
        ],
        "services": [_service_out(s) for s in services],
        "faqs": [{"id": f.id, "question": f.question, "answer": f.answer} for f in faqs],
        "holidays": [{"id": h.id, "date": h.date.isoformat(), "reason": h.reason} for h in holidays if h.date],
        "phone_numbers": [{"id": n.id, "e164": n.e164, "is_active": n.is_active} for n in numbers],
    }


@router.put("/business")
async def update_business(body: ProfileUpdate, business_id: str = Depends(current_business_id),
                          session: AsyncSession = Depends(get_db)):
    b = await _business(session, business_id)
    clearable = {"greeting", "about", "transfer_phone", "retention_days", "forward_from", "monthly_minute_cap"}
    for field_name, value in body.model_dump(exclude_unset=True).items():
        if isinstance(value, str):
            value = value.strip() or None
        # Required columns keep their value when sent blank or null.
        if value is None and field_name not in clearable:
            continue
        setattr(b, field_name, value)
    await session.commit()
    return _profile_out(b)


@router.put("/business/hours")
async def set_hours(days: List[DayHoursIn], business_id: str = Depends(current_business_id),
                    session: AsyncSession = Depends(get_db)):
    seen = set()
    for d in days:
        if d.day_of_week in seen:
            raise HTTPException(status_code=422, detail="Each day may appear only once")
        seen.add(d.day_of_week)
        if d.is_open and (not d.opens or not d.closes or d.opens >= d.closes):
            raise HTTPException(status_code=422,
                                detail=f"Day {d.day_of_week}: opening time must be before closing time")

    await session.execute(delete(BusinessHoursSQL).where(BusinessHoursSQL.business_id == business_id))
    for d in days:
        session.add(BusinessHoursSQL(
            id=uuid.uuid4().hex, business_id=business_id, day_of_week=d.day_of_week,
            is_open=d.is_open, start_time=d.opens if d.is_open else None,
            end_time=d.closes if d.is_open else None,
        ))
    await session.commit()
    return {"ok": True}


# ----------------------------------------------------------------- services

@router.post("/business/services", status_code=201)
async def add_service(body: ServiceIn, business_id: str = Depends(current_business_id),
                      session: AsyncSession = Depends(get_db)):
    s = ServiceSQL(id=uuid.uuid4().hex, business_id=business_id, **body.model_dump())
    session.add(s)
    await session.commit()
    return _service_out(s)


@router.put("/business/services/{service_id}")
async def update_service(service_id: str, body: ServiceIn,
                         business_id: str = Depends(current_business_id),
                         session: AsyncSession = Depends(get_db)):
    s = await _owned(session, ServiceSQL, service_id, business_id)
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    await session.commit()
    return _service_out(s)


@router.delete("/business/services/{service_id}", status_code=204)
async def delete_service(service_id: str, business_id: str = Depends(current_business_id),
                         session: AsyncSession = Depends(get_db)):
    s = await _owned(session, ServiceSQL, service_id, business_id)
    in_use = (await session.execute(
        select(AppointmentSQL.id).where(AppointmentSQL.service_id == s.id).limit(1)
    )).first()
    if in_use:
        # Appointments reference it; deleting would orphan the owner's history.
        raise HTTPException(status_code=409,
                            detail="This service has appointments. Rename it instead of deleting it.")
    await session.delete(s)
    await session.commit()


# --------------------------------------------------------------------- faqs

@router.post("/business/faqs", status_code=201)
async def add_faq(body: FAQIn, business_id: str = Depends(current_business_id),
                  session: AsyncSession = Depends(get_db)):
    f = FAQSQL(id=uuid.uuid4().hex, business_id=business_id,
               question=body.question.strip(), answer=body.answer.strip())
    session.add(f)
    await session.commit()
    return {"id": f.id, "question": f.question, "answer": f.answer}


@router.put("/business/faqs/{faq_id}")
async def update_faq(faq_id: str, body: FAQIn, business_id: str = Depends(current_business_id),
                     session: AsyncSession = Depends(get_db)):
    f = await _owned(session, FAQSQL, faq_id, business_id)
    f.question, f.answer = body.question.strip(), body.answer.strip()
    await session.commit()
    return {"id": f.id, "question": f.question, "answer": f.answer}


@router.delete("/business/faqs/{faq_id}", status_code=204)
async def delete_faq(faq_id: str, business_id: str = Depends(current_business_id),
                     session: AsyncSession = Depends(get_db)):
    f = await _owned(session, FAQSQL, faq_id, business_id)
    await session.delete(f)
    await session.commit()


# ----------------------------------------------------------------- holidays

@router.post("/business/holidays", status_code=201)
async def add_holiday(body: HolidayIn, business_id: str = Depends(current_business_id),
                      session: AsyncSession = Depends(get_db)):
    h = BusinessHolidaysSQL(id=uuid.uuid4().hex, business_id=business_id, date=body.date, reason=body.reason)
    session.add(h)
    await session.commit()
    return {"id": h.id, "date": h.date.isoformat(), "reason": h.reason}


@router.delete("/business/holidays/{holiday_id}", status_code=204)
async def delete_holiday(holiday_id: str, business_id: str = Depends(current_business_id),
                         session: AsyncSession = Depends(get_db)):
    h = await _owned(session, BusinessHolidaysSQL, holiday_id, business_id)
    await session.delete(h)
    await session.commit()


# ------------------------------------------------------------- appointments

@router.get("/appointments")
async def list_appointments(
    days_back: int = Query(1, ge=0, le=365),
    days_ahead: int = Query(60, ge=0, le=365),
    business_id: str = Depends(current_business_id),
    session: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(AppointmentSQL, ServiceSQL.name)
        .join(ServiceSQL, ServiceSQL.id == AppointmentSQL.service_id, isouter=True)
        .where(
            AppointmentSQL.business_id == business_id,
            AppointmentSQL.start_time >= now - timedelta(days=days_back),
            AppointmentSQL.start_time <= now + timedelta(days=days_ahead),
        )
        .order_by(AppointmentSQL.start_time)
    )).all()

    def iso(dt):
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()

    return [
        {"id": a.id, "service": svc, "customer_name": a.customer_name, "customer_phone": a.customer_phone,
         "start_time": iso(a.start_time), "end_time": iso(a.end_time), "status": a.status,
         "source": a.source, "call_log_id": a.call_log_id, "notes": a.notes}
        for a, svc in rows
    ]


@router.patch("/appointments/{appointment_id}")
async def update_appointment(appointment_id: str, body: AppointmentUpdate,
                             business_id: str = Depends(current_business_id),
                             session: AsyncSession = Depends(get_db)):
    a = await _owned(session, AppointmentSQL, appointment_id, business_id)
    was_active = a.status in ("scheduled", "confirmed")
    a.status = body.status
    await session.commit()
    if was_active and body.status == "cancelled" and a.external_event_id:
        from services import gcal
        from services.business_profile import load_profile
        gcal.queue_delete(await load_profile(session, business_id), a.external_event_id)
    return {"id": a.id, "status": a.status}
