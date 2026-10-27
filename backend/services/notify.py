"""Messages to callers: booking confirmations and day-before reminders.

Two channels, chosen per business (businesses.message_channel):

  sms       Twilio SMS. India: commercial SMS needs DLT registration - a
            registered sender header and pre-approved templates (Jio, Airtel
            or Vi portal). Register BOOKING_SMS_TEMPLATE / REMINDER_SMS_TEMPLATE
            word for word and put the Messaging Service carrying that sender
            in TWILIO_MESSAGING_SERVICE_SID.
  whatsapp  Twilio WhatsApp. Business-initiated messages must use a
            Meta-approved template: create them in Twilio Content Builder and
            set WHATSAPP_CONFIRMATION_CONTENT_SID / WHATSAPP_REMINDER_CONTENT_SID.
            Template variables: {{1}} business, {{2}} service, {{3}} when,
            {{4}} reference (confirmation) or phone (reminder).
            If WhatsApp is not configured, the message goes by SMS instead.

Best-effort by design: a message failing must never fail a committed booking,
and sending happens in a background task after the caller has their answer.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional, Set

from sqlalchemy import select

from db import PhoneNumberSQL
from scripts.config import settings
from services.booking import BookedAppointment
from services.business_profile import BusinessProfile

logger = logging.getLogger(__name__)

# Must match the DLT-registered templates word for word (variables aside).
BOOKING_SMS_TEMPLATE = (
    "{business}: your {service} is booked for {when}. "
    "To change it, call us on {phone}. Ref {ref}"
)
REMINDER_SMS_TEMPLATE = (
    "{business}: reminder of your {service} on {when}. "
    "To change it, call us on {phone}."
)

# Strong references so fire-and-forget tasks are not garbage-collected mid-flight.
_background: Set[asyncio.Task] = set()


def sms_available() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)


def whatsapp_available(kind: str) -> bool:
    sid = (settings.WHATSAPP_CONFIRMATION_CONTENT_SID if kind == "confirmation"
           else settings.WHATSAPP_REMINDER_CONTENT_SID)
    return sms_available() and bool(settings.TWILIO_WHATSAPP_FROM and sid)


def _when(dt: datetime) -> str:
    return dt.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


async def sender_for(session, profile: BusinessProfile) -> Optional[str]:
    """The business's own number reads as legitimate to the caller; fall back
    to the platform number."""
    row = (await session.execute(
        select(PhoneNumberSQL.e164).where(
            PhoneNumberSQL.business_id == profile.id, PhoneNumberSQL.is_active,
        ).limit(1)
    )).scalar_one_or_none()
    return row or settings.TWILIO_PHONE_NUMBER or None


def render_booking_sms(profile: BusinessProfile, booked: BookedAppointment, reply_phone: str) -> str:
    return BOOKING_SMS_TEMPLATE.format(
        business=profile.name, service=booked.service.name, when=_when(booked.start),
        phone=reply_phone or profile.phone or "", ref=booked.id[:6].upper(),
    )


def _send_blocking(to: str, from_: Optional[str], body: Optional[str],
                   whatsapp: bool = False, content_sid: Optional[str] = None,
                   variables: Optional[Dict[str, str]] = None) -> str:
    from twilio.rest import Client

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    if whatsapp:
        kwargs = {"to": f"whatsapp:{to}", "from_": f"whatsapp:{settings.TWILIO_WHATSAPP_FROM}",
                  "content_sid": content_sid, "content_variables": json.dumps(variables or {})}
    else:
        kwargs = {"to": to, "body": body}
        if settings.TWILIO_MESSAGING_SERVICE_SID:
            kwargs["messaging_service_sid"] = settings.TWILIO_MESSAGING_SERVICE_SID
        else:
            kwargs["from_"] = from_
    return client.messages.create(**kwargs).sid


async def _send(label: str, **kwargs) -> bool:
    try:
        sid = await asyncio.to_thread(_send_blocking, **kwargs)
        logger.info("%s sent (%s)", label, sid)
        return True
    except Exception as e:
        logger.warning("%s failed: %s", label, e)
        return False


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _prepare(session, profile: BusinessProfile, kind: str, to: Optional[str],
                   sms_text, variables: Dict[str, str]) -> Optional[dict]:
    """Pick the channel and build the send arguments, or None if we cannot send."""
    if not (to and sms_available()):
        return None
    if profile.message_channel == "whatsapp" and whatsapp_available(kind):
        sid = (settings.WHATSAPP_CONFIRMATION_CONTENT_SID if kind == "confirmation"
               else settings.WHATSAPP_REMINDER_CONTENT_SID)
        return {"to": to, "from_": None, "body": None, "whatsapp": True,
                "content_sid": sid, "variables": variables}
    sender = await sender_for(session, profile)
    if not sender and not settings.TWILIO_MESSAGING_SERVICE_SID:
        logger.warning("No SMS sender configured for business %s", profile.id)
        return None
    return {"to": to, "from_": sender, "body": sms_text(sender or profile.phone)}


async def queue_booking_confirmation(session, profile: BusinessProfile,
                                     booked: BookedAppointment) -> bool:
    """Schedule the confirmation; returns whether one was queued."""
    if not profile.sms_confirmations:
        return False
    try:
        args = await _prepare(
            session, profile, "confirmation", booked.customer_phone,
            lambda reply: render_booking_sms(profile, booked, reply),
            {"1": profile.name, "2": booked.service.name, "3": _when(booked.start), "4": booked.id[:6].upper()},
        )
    except Exception as e:
        logger.warning("Could not prepare confirmation for %s: %s", booked.id, e)
        return False
    if args is None:
        return False
    _spawn(_send(f"Booking confirmation for {booked.id}", **args))
    return True


async def send_reminder(session, profile: BusinessProfile, appointment_id: str, to: str,
                        service_name: str, start: datetime) -> bool:
    """Awaited (the reminder job wants to know); never raises."""
    try:
        args = await _prepare(
            session, profile, "reminder", to,
            lambda reply: REMINDER_SMS_TEMPLATE.format(
                business=profile.name, service=service_name, when=_when(start), phone=reply or ""),
            {"1": profile.name, "2": service_name, "3": _when(start), "4": profile.phone or ""},
        )
    except Exception as e:
        logger.warning("Could not prepare reminder for %s: %s", appointment_id, e)
        return False
    if args is None:
        return False
    return await _send(f"Reminder for {appointment_id}", **args)
