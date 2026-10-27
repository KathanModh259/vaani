"""Tools the receptionist LLM may call, and what they actually do.

The LLM decides *what* to do; these functions are the only way anything
happens. Each one records its effect on the TurnContext, and the voice/chat
routes act on that record - never on what the model's reply text claims. So
a model that says "you're booked" without calling book_appointment has booked
nothing, and the call log shows it.

Results are small JSON dicts written for the model to read aloud from.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from services import booking
from services.booking import BookedAppointment, BookingError
from services.business_profile import BusinessProfile, next_open_days
from services.escalation import MESSAGE, TRANSFER, escalation_route

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    session: Any
    profile: BusinessProfile
    channel: str = "phone"                 # phone | web
    caller_phone: Optional[str] = None     # caller ID on phone; None on web
    call_log_id: Optional[str] = None

    # Effects of this turn, filled in by the tools.
    booked: List[BookedAppointment] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str = ""
    transfer_to: Optional[str] = None
    end_call: bool = False


def _when(dt: datetime) -> str:
    return dt.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")


def _slots(dts: List[datetime]) -> List[str]:
    return [dt.strftime("%Y-%m-%d %H:%M") for dt in dts]


# ---------------------------------------------------------------- schemas

TOOL_SPECS: List[Dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "check_availability",
        "description": "List bookable start times for a service on a date. Call before offering any time.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD, in the business's local time"},
            "service_name": {"type": "string", "description": "Service as listed in the business facts"},
        }, "required": ["date", "service_name"]},
    }},
    {"type": "function", "function": {
        "name": "book_appointment",
        "description": "Book an appointment. Only tell the caller it is booked if this returns booked=true.",
        "parameters": {"type": "object", "properties": {
            "service_name": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "24-hour HH:MM"},
            "customer_name": {"type": "string"},
            "customer_phone": {"type": "string", "description": "Only if different from the caller's own number"},
        }, "required": ["service_name", "date", "time", "customer_name"]},
    }},
    {"type": "function", "function": {
        "name": "find_my_appointments",
        "description": "List the caller's upcoming appointments (needed before cancelling or rescheduling).",
        "parameters": {"type": "object", "properties": {
            "customer_phone": {"type": "string", "description": "Only on web chat, where there is no caller ID"},
        }},
    }},
    {"type": "function", "function": {
        "name": "cancel_appointment",
        "description": "Cancel one of the caller's appointments by id from find_my_appointments.",
        "parameters": {"type": "object", "properties": {
            "appointment_id": {"type": "string"},
            "reason": {"type": "string"},
            "customer_phone": {"type": "string"},
        }, "required": ["appointment_id"]},
    }},
    {"type": "function", "function": {
        "name": "reschedule_appointment",
        "description": "Move one of the caller's appointments to a new date/time.",
        "parameters": {"type": "object", "properties": {
            "appointment_id": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "time": {"type": "string", "description": "24-hour HH:MM"},
            "customer_phone": {"type": "string"},
        }, "required": ["appointment_id", "date", "time"]},
    }},
    {"type": "function", "function": {
        "name": "transfer_to_human",
        "description": "Hand the caller to a person: they asked for one, are upset, or need something you cannot do.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Short summary for the staff member"},
        }, "required": ["reason"]},
    }},
    {"type": "function", "function": {
        "name": "end_call",
        "description": "The caller has finished. Call after saying goodbye.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


# ---------------------------------------------------------------- handlers

async def _check_availability(ctx: TurnContext, args: Dict[str, Any]) -> Dict[str, Any]:
    p = ctx.profile
    service = p.find_service(args.get("service_name"))
    if not service:
        return {"error": "unknown_service", "services": [s.name for s in p.services]}
    try:
        day = date.fromisoformat(str(args.get("date", "")).strip())
    except ValueError:
        return {"error": "bad_date", "hint": "Use YYYY-MM-DD"}

    if not p.hours_on(day):
        return {"date": day.isoformat(), "open": False,
                "next_open_days": [d.isoformat() for d in next_open_days(p, day)]}

    slots = await booking.free_slots(ctx.session, p, service, day)
    out = {"date": day.isoformat(), "open": True, "service": service.name, "slots": _slots(slots)}
    if not slots:
        out["next_available"] = _slots(await booking.next_available(ctx.session, p, service, day))
    return out


async def _book(ctx: TurnContext, args: Dict[str, Any]) -> Dict[str, Any]:
    p = ctx.profile
    service = p.find_service(args.get("service_name"))
    if not service:
        return {"booked": False, "reason": "unknown_service", "services": [s.name for s in p.services]}

    phone = args.get("customer_phone") or ctx.caller_phone
    if not phone:
        return {"booked": False, "reason": "missing_phone", "say": "Ask for a contact phone number."}

    try:
        start = booking.parse_local(p, str(args.get("date", "")), str(args.get("time", "")))
        booked = await booking.book(
            ctx.session, p, service, start,
            customer_name=str(args.get("customer_name", "")),
            customer_phone=phone,
            source=ctx.channel,
            call_log_id=ctx.call_log_id,
        )
    except BookingError as e:
        return {"booked": False, "reason": e.code, "say": e.message, "alternatives": _slots(e.alternatives)}

    ctx.booked.append(booked)
    ctx.events.append(f"Booked {service.name} for {booked.customer_name} on {_when(booked.start)} (ref {booked.id[:6].upper()})")

    sms = False
    try:
        from services.notify import queue_booking_confirmation
        sms = await queue_booking_confirmation(ctx.session, p, booked)
    except Exception as e:  # never let a notification fail a committed booking
        logger.warning("SMS queue failed for %s: %s", booked.id, e)

    return {
        "booked": True, "appointment_id": booked.id, "service": service.name,
        "when": _when(booked.start), "reference": booked.id[:6].upper(),
        "price": p.format_price(service.price) if service.price else None,
        "sms_confirmation_sent": sms,
    }


async def _find(ctx: TurnContext, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = ctx.caller_phone or args.get("customer_phone")
    if not phone:
        return {"error": "missing_phone", "say": "Ask for the phone number the booking was made with."}
    appts = await booking.upcoming_for_phone(ctx.session, ctx.profile, phone)
    names = {s.id: s.name for s in ctx.profile.services}
    return {"appointments": [
        {"appointment_id": a.id, "service": names.get(a.service_id, "appointment"),
         "when": _when(booking._aware(a.start_time).astimezone(ctx.profile.tz)),
         "name": a.customer_name}
        for a in appts
    ]}


async def _cancel(ctx: TurnContext, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = ctx.caller_phone or args.get("customer_phone")
    try:
        appt = await booking.cancel(ctx.session, ctx.profile, str(args.get("appointment_id", "")),
                                    phone, reason=args.get("reason"))
    except BookingError as e:
        return {"cancelled": False, "reason": e.code, "say": e.message}
    ctx.events.append(f"Cancelled appointment ref {appt.id[:6].upper()} for {appt.customer_name}")
    return {"cancelled": True}


async def _reschedule(ctx: TurnContext, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = ctx.caller_phone or args.get("customer_phone")
    try:
        start = booking.parse_local(ctx.profile, str(args.get("date", "")), str(args.get("time", "")))
        booked = await booking.reschedule(ctx.session, ctx.profile, str(args.get("appointment_id", "")),
                                          phone, start)
    except BookingError as e:
        return {"rescheduled": False, "reason": e.code, "say": e.message,
                "alternatives": _slots(e.alternatives)}
    ctx.booked.append(booked)
    ctx.events.append(f"Rescheduled to {_when(booked.start)} (new ref {booked.id[:6].upper()})")
    return {"rescheduled": True, "when": _when(booked.start), "reference": booked.id[:6].upper()}


async def _transfer(ctx: TurnContext, args: Dict[str, Any]) -> Dict[str, Any]:
    reason = str(args.get("reason") or "caller asked for a person").strip()
    ctx.escalated = True
    ctx.escalation_reason = reason

    route = escalation_route(ctx.profile, ctx.profile.now(), reason)
    # Web chat cannot bridge a phone call, whatever the policy says.
    if route == TRANSFER and ctx.profile.transfer_phone and ctx.channel == "phone":
        ctx.transfer_to = ctx.profile.transfer_phone
        ctx.events.append(f"Transferred to staff: {reason}")
        return {"action": "transfer", "say": "Tell the caller you are connecting them now."}

    ctx.events.append(f"Callback requested: {reason}")
    return {"action": MESSAGE,
            "say": "Tell the caller a team member will call them back, and check the best number and time."}


async def _end(ctx: TurnContext, args: Dict[str, Any]) -> Dict[str, Any]:
    ctx.end_call = True
    return {"ok": True}


HANDLERS = {
    "check_availability": _check_availability,
    "book_appointment": _book,
    "find_my_appointments": _find,
    "cancel_appointment": _cancel,
    "reschedule_appointment": _reschedule,
    "transfer_to_human": _transfer,
    "end_call": _end,
}


async def dispatch(ctx: TurnContext, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool {name}"}
    try:
        return await handler(ctx, args or {})
    except Exception as e:
        # A tool bug must degrade the call ("let me take a message"), not end it.
        logger.exception("Tool %s failed: %s", name, e)
        if name == "transfer_to_human":
            ctx.escalated = True
            ctx.events.append(f"Callback requested: {args.get('reason', '')}".strip())
            return {"action": MESSAGE, "say": "Tell the caller a team member will call them back."}
        return {"error": "internal_error", "say": "Apologise and offer to take a message."}
