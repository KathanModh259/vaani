"""Tell the business owner when something needs them.

When Vaani says "a member of the team will call you back", this is what
makes that true: the owner gets an email with the caller's number and the
reason, and the call sits in the dashboard's callback list until marked
done. Bookings are announced too, so a new appointment is never discovered
only when the customer walks in.

Alerts are fire-and-forget (services.email.queue_email) - a mail outage must
not affect the call.
"""

import logging
from typing import Optional

from services.booking import BookedAppointment
from services.business_profile import BusinessProfile
from services.email import public_url, queue_email

logger = logging.getLogger(__name__)


def _alertable(profile: BusinessProfile) -> bool:
    from services.call_logger import DEMO_BUSINESS_ID
    return profile.id != DEMO_BUSINESS_ID and bool(profile.email)


def _when(dt) -> str:
    return dt.strftime("%A %d %B, %I:%M %p").replace(" 0", " ")


def booking_alert(profile: BusinessProfile, booked: BookedAppointment, channel: str) -> None:
    if not _alertable(profile):
        return
    queue_email(
        profile.email,
        f"New booking: {booked.service.name}, {_when(booked.start)}",
        (
            f"Vaani booked an appointment for {profile.name}.\n\n"
            f"Customer: {booked.customer_name}\n"
            f"Phone:    {booked.customer_phone or 'not given'}\n"
            f"Service:  {booked.service.name}\n"
            f"When:     {_when(booked.start)}\n"
            f"Via:      {'phone call' if channel == 'phone' else 'website chat'}\n"
            f"Ref:      {booked.id[:6].upper()}\n\n"
            f"All appointments: {public_url('/dashboard#appointments')}\n"
        ),
    )


def callback_alert(profile: BusinessProfile, caller_phone: Optional[str], reason: str,
                   call_id: Optional[str]) -> None:
    if not _alertable(profile):
        return
    queue_email(
        profile.email,
        f"Callback needed: {caller_phone or 'caller'}",
        (
            f"A caller to {profile.name} asked to speak to someone, and Vaani promised "
            f"a callback.\n\n"
            f"Call back: {caller_phone or 'number not available - see the transcript'}\n"
            f"Reason:    {reason}\n\n"
            f"Transcript and callback list: {public_url('/dashboard#calls')}\n"
            + (f"Call id: {call_id}\n" if call_id else "")
        ),
    )
