"""Third-party connections: Google Calendar, and importing a business profile
from its website."""

import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.auth import get_current_user
from api.routes.business import current_business_id
from db import BusinessSQL, UserSQL, get_db
from scripts.config import settings
from services import gcal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["integrations"])


# ----------------------------------------------------------- Google Calendar

@router.get("/integrations/google/connect")
async def google_connect(user: UserSQL = Depends(get_current_user),
                         business_id: str = Depends(current_business_id)):
    """Returns the Google consent URL; the dashboard navigates to it."""
    if not gcal.configured():
        raise HTTPException(status_code=503, detail="Google Calendar is not configured on this server.")
    # The state ties the callback to this business and cannot be forged or
    # replayed after 10 minutes - it is the OAuth CSRF defence.
    state = jwt.encode({"type": "gcal", "bid": business_id, "sub": user.id,
                        "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
                       settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return {"url": gcal.consent_url(state)}


@router.get("/integrations/google/callback", include_in_schema=False)
async def google_callback(state: str = Query(""), code: str = Query(""), error: str = Query(""),
                          session: AsyncSession = Depends(get_db)):
    try:
        claims = jwt.decode(state, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if claims.get("type") != "gcal":
            raise ValueError("wrong state type")
    except Exception:
        return RedirectResponse("/dashboard?gcal=invalid#settings", status_code=303)
    if error or not code:
        return RedirectResponse("/dashboard?gcal=denied#settings", status_code=303)

    try:
        refresh = await gcal.exchange_code(code)
    except Exception as e:
        logger.warning("Google OAuth exchange failed: %s", e)
        return RedirectResponse("/dashboard?gcal=failed#settings", status_code=303)

    b = (await session.execute(select(BusinessSQL).where(BusinessSQL.id == claims["bid"]))).scalar_one_or_none()
    if b is None:
        return RedirectResponse("/dashboard?gcal=invalid#settings", status_code=303)
    b.gcal_token = gcal.seal(refresh)
    b.gcal_calendar_id = b.gcal_calendar_id or "primary"
    await session.commit()
    gcal.forget(b.id)
    return RedirectResponse("/dashboard?gcal=connected#settings", status_code=303)


@router.post("/integrations/google/disconnect")
async def google_disconnect(business_id: str = Depends(current_business_id),
                            session: AsyncSession = Depends(get_db)):
    b = (await session.execute(select(BusinessSQL).where(BusinessSQL.id == business_id))).scalar_one()
    b.gcal_token = None
    await session.commit()
    gcal.forget(business_id)
    return {"connected": False}


# ----------------------------------------------------------------- test call

class TestCallRequest(BaseModel):
    phone: str = Field("", max_length=32)


@router.post("/business/test-call", status_code=202)
async def test_call(body: TestCallRequest, user: UserSQL = Depends(get_current_user),
                    business_id: str = Depends(current_business_id),
                    session: AsyncSession = Depends(get_db)):
    """Have Vaani ring the owner, so they hear their receptionist exactly as a
    customer would before going live. Costs a real call: verified owners only,
    and rate-limited (api/core/middleware.py)."""
    import asyncio
    from db import PhoneNumberSQL
    from services.phone import normalize_phone
    from services.provisioning import twilio_configured

    if not (twilio_configured() and settings.PUBLIC_BASE_URL):
        raise HTTPException(status_code=503, detail="Phone calls are not configured on this server.")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Verify your email address before placing a test call.")
    to = normalize_phone(body.phone or user.phone)
    if not to or not to.startswith("+"):
        raise HTTPException(status_code=422, detail="Enter the phone number to call, e.g. +91 98765 43210")
    from_ = (await session.execute(select(PhoneNumberSQL.e164).where(
        PhoneNumberSQL.business_id == business_id, PhoneNumberSQL.is_active).limit(1))).scalar_one_or_none()
    if not from_:
        raise HTTPException(status_code=409, detail="Connect a phone number first.")

    def place():
        from twilio.rest import Client
        return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN).calls.create(
            to=to, from_=from_, time_limit=300,
            url=f"{settings.PUBLIC_BASE_URL}/api/voice/incoming?bid={business_id}",
            status_callback=f"{settings.PUBLIC_BASE_URL}/api/voice/status-callback",
        ).sid

    try:
        sid = await asyncio.to_thread(place)
    except Exception as e:
        logger.warning("Test call failed for %s: %s", business_id, e)
        raise HTTPException(status_code=502, detail=f"Could not place the call: {getattr(e, 'msg', None) or e}")
    return {"calling": to, "call_sid": sid}


# ------------------------------------------------------------ website import

class ImportRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=500)


@router.post("/business/import")
async def import_from_website(body: ImportRequest, business_id: str = Depends(current_business_id)):
    """Read the business's website and draft hours, services and FAQs.

    Returns a preview only - nothing is saved. The owner reviews it in the
    dashboard and applies what is right, because a scraped price that is
    wrong would be quoted to real callers.
    """
    from services.site_import import ImportError_, draft_from_website

    try:
        return await draft_from_website(body.url)
    except ImportError_ as e:
        raise HTTPException(status_code=e.status, detail=e.message)
