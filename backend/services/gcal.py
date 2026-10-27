"""Google Calendar two-way sync.

  * read:  the owner's busy time closes those slots (freeBusy API)
  * write: each booking Vaani makes appears as an event; cancelling removes it

OAuth: the owner connects once from the dashboard; we keep only the refresh
token, encrypted like transcripts. Access tokens are short-lived and cached in
memory per business.

Latency matters - availability is checked while a caller waits - so reads
use a 3-second budget and fail open (services/booking.py treats an error as
"no calendar busy time"). Writes run in the background after the booking is
already committed and confirmed to the caller.

Needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from a Google Cloud OAuth
client (type "Web application") whose redirect URI is
    {PUBLIC_BASE_URL}/api/integrations/google/callback
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

import aiohttp
from sqlalchemy import select

from scripts.config import settings

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"
READ_TIMEOUT = aiohttp.ClientTimeout(total=3)
WRITE_TIMEOUT = aiohttp.ClientTimeout(total=10)

_access: Dict[str, Tuple[str, float]] = {}   # business_id -> (token, expires_at)
_background: Set[asyncio.Task] = set()


class CalendarError(Exception):
    pass


def configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.PUBLIC_BASE_URL)


def redirect_uri() -> str:
    return f"{settings.PUBLIC_BASE_URL}/api/integrations/google/callback"


def consent_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # we need a refresh token
        "prompt": "consent",        # ...and Google only re-issues one with consent
        "state": state,
    })


async def _post_token(data: dict, timeout=WRITE_TIMEOUT) -> dict:
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.post(TOKEN_URL, data=data) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise CalendarError(f"token endpoint {r.status}: {body.get('error', body)}")
            return body


async def exchange_code(code: str) -> str:
    """Authorization code -> refresh token."""
    body = await _post_token({
        "code": code, "client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
    })
    if not body.get("refresh_token"):
        raise CalendarError("Google did not return a refresh token")
    return body["refresh_token"]


def seal(refresh_token: str) -> str:
    from services.crypto import encrypt_json
    return encrypt_json({"refresh_token": refresh_token})


def _unseal(stored: Optional[str]) -> Optional[str]:
    from services.crypto import decrypt_json
    if not stored:
        return None
    data = decrypt_json(stored, default=None)
    return data.get("refresh_token") if isinstance(data, dict) else None


async def _access_token(session, business_id: str, timeout) -> Tuple[str, str]:
    """(access token, calendar id), refreshing when the cached token is stale."""
    from db import BusinessSQL

    row = (await session.execute(
        select(BusinessSQL.gcal_token, BusinessSQL.gcal_calendar_id).where(BusinessSQL.id == business_id)
    )).first()
    if not row or not row[0]:
        raise CalendarError("calendar not connected")
    calendar_id = row[1] or "primary"

    cached = _access.get(business_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0], calendar_id
    refresh = _unseal(row[0])
    if not refresh:
        raise CalendarError("stored calendar token unreadable")
    body = await _post_token({
        "refresh_token": refresh, "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET, "grant_type": "refresh_token",
    }, timeout=timeout)
    _access[business_id] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"], calendar_id


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def busy(session, profile, lo: datetime, hi: datetime) -> List[Tuple[datetime, datetime]]:
    token, cal = await _access_token(session, profile.id, READ_TIMEOUT)
    async with aiohttp.ClientSession(timeout=READ_TIMEOUT) as http:
        async with http.post(f"{API}/freeBusy", headers={"Authorization": f"Bearer {token}"},
                             json={"timeMin": _iso(lo), "timeMax": _iso(hi), "items": [{"id": cal}]}) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise CalendarError(f"freeBusy {r.status}")
    periods = body.get("calendars", {}).get(cal, {}).get("busy", [])
    return [(_parse(p["start"]), _parse(p["end"])) for p in periods]


async def _create_event(appointment_id: str) -> None:
    from db import AppointmentSQL, BusinessSQL, ServiceSQL, get_manager

    session = await (await get_manager()).get_session()
    try:
        appt = (await session.execute(select(AppointmentSQL).where(AppointmentSQL.id == appointment_id))).scalar_one_or_none()
        if appt is None or appt.external_event_id:
            return
        service = (await session.execute(select(ServiceSQL.name).where(ServiceSQL.id == appt.service_id))).scalar_one_or_none()
        tz = (await session.execute(select(BusinessSQL.time_zone).where(BusinessSQL.id == appt.business_id))).scalar_one_or_none()
        token, cal = await _access_token(session, appt.business_id, WRITE_TIMEOUT)
        start = appt.start_time if appt.start_time.tzinfo else appt.start_time.replace(tzinfo=timezone.utc)
        end = appt.end_time if appt.end_time.tzinfo else appt.end_time.replace(tzinfo=timezone.utc)
        event = {
            "summary": f"{service or 'Appointment'} - {appt.customer_name}",
            "description": f"Booked by Vaani.\nPhone: {appt.customer_phone or 'n/a'}\nRef: {appt.id[:6].upper()}",
            "start": {"dateTime": _iso(start), "timeZone": tz or "Asia/Kolkata"},
            "end": {"dateTime": _iso(end), "timeZone": tz or "Asia/Kolkata"},
        }
        async with aiohttp.ClientSession(timeout=WRITE_TIMEOUT) as http:
            async with http.post(f"{API}/calendars/{cal}/events", headers={"Authorization": f"Bearer {token}"},
                                 json=event) as r:
                body = await r.json(content_type=None)
                if r.status not in (200, 201):
                    raise CalendarError(f"insert event {r.status}")
        appt.external_event_id = body.get("id")
        await session.commit()
    finally:
        await session.close()


async def _delete_event(business_id: str, event_id: str) -> None:
    from db import get_manager

    session = await (await get_manager()).get_session()
    try:
        token, cal = await _access_token(session, business_id, WRITE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=WRITE_TIMEOUT) as http:
            async with http.delete(f"{API}/calendars/{cal}/events/{event_id}",
                                   headers={"Authorization": f"Bearer {token}"}) as r:
                if r.status not in (200, 204, 404, 410):
                    raise CalendarError(f"delete event {r.status}")
    finally:
        await session.close()


def _spawn(coro, what: str) -> None:
    async def guarded():
        try:
            await coro
        except Exception as e:
            logger.warning("Calendar %s failed: %s", what, e)

    task = asyncio.create_task(guarded())
    _background.add(task)
    task.add_done_callback(_background.discard)


def queue_create(profile, appointment_id: str) -> None:
    if profile.gcal_connected and configured():
        _spawn(_create_event(appointment_id), f"create for {appointment_id}")


def queue_delete(profile, event_id: Optional[str]) -> None:
    if event_id and profile.gcal_connected and configured():
        _spawn(_delete_event(profile.id, event_id), f"delete {event_id}")


def forget(business_id: str) -> None:
    _access.pop(business_id, None)
