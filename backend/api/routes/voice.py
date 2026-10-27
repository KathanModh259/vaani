"""Twilio voice webhooks.

Twilio expects TwiML (XML) back from these endpoints, not JSON - returning
JSON makes the carrier drop the call. The flow is:

    /incoming         -> route by dialled number, greet, <Gather> speech
    /respond          -> run the utterance through the agent; speak, then
                         gather again / <Dial> a human / hang up
    /dial-status      -> a live transfer ended; unanswered = callback owed
    /status-callback  -> Twilio's call-ended event; records billable duration

Configure each Twilio number with:
    A call comes in:     POST https://<PUBLIC_BASE_URL>/api/voice/incoming
    Call status changes: POST https://<PUBLIC_BASE_URL>/api/voice/status-callback

Every request must carry a valid X-Twilio-Signature, or anyone on the
internet could inject fake calls into a client's dashboard and spend the
LLM budget.
"""

import logging
from typing import Dict, Optional
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from db import BusinessSQL, PhoneNumberSQL, get_db
from scripts.config import settings
from services.language import reply_language
from services.phone import normalize_phone

logger = logging.getLogger(__name__)
router = APIRouter()

FALLBACK = "Sorry, I did not catch that. Could you say it once more?"
TROUBLE = "Sorry, I'm having trouble right now. Please call back in a few minutes."
NOT_IN_SERVICE = "Sorry, this number is not in service. Goodbye."
NO_ANSWER = "Sorry, nobody could take the call right now. I've passed on your message and someone will call you back."
STILL_THERE = "Are you still there? Please tell me how I can help."
SILENT_GOODBYE = "I can't hear anything, so I'll end the call now. Please call back any time. Goodbye."
TOO_LONG = "I'll ask a member of the team to call you back to finish this. Thank you for calling, goodbye."

# Consecutive empty <Gather> results before hanging up. A silent line (pocket
# dial, dropped audio) otherwise re-prompts forever, billing every minute.
MAX_SILENCES = 3
# Caller utterances per call before handing off to a human. Bounds cost and
# catches loops where the caller and the agent keep misunderstanding.
MAX_CALLER_TURNS = 40

# Numbers Twilio substitutes when caller ID is withheld. Treating them as a
# real number would file bookings under "+266696687".
ANONYMOUS_CALLERS = {"+266696687", "+86282452253", "+7378742833", "+2562533", "+8656696"}

# <Say> voices per Gather language. Polly Aditi is bilingual Hindi/Indian
# English; Polly has no Gujarati voice, so Gujarati uses Google's.
VOICES = {
    "en-IN": ("Polly.Aditi", "en-IN"),
    "hi-IN": ("Polly.Aditi", "hi-IN"),
    "gu-IN": ("Google.gu-IN-Standard-A", "gu-IN"),
}


def _voice(language: str):
    return VOICES.get(language or "en-IN", VOICES["en-IN"])


def _twiml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )


def _say(text: str, language: str = "en-IN") -> str:
    """Speak `text` in the voice for its script: a Hindi reply to an English
    business is read by a Hindi voice, not mangled by an English one."""
    voice, lang = _voice(reply_language(text, language))
    return f"<Say voice={quoteattr(voice)} language={quoteattr(lang)}>{escape(text)}</Say>"


def _gather(prompt: str, language: str = "en-IN", action: str = "/api/voice/respond") -> str:
    """Speak a prompt and listen for the caller's reply; if they stay silent,
    Twilio posts an empty SpeechResult to `action` (actionOnEmptyResult).

    The recogniser follows the prompt's language: when the conversation has
    moved to Hindi, the caller's next sentence is transcribed as Hindi."""
    listen = reply_language(prompt, language)
    return (
        f'<Gather input="speech" action={quoteattr(action)} method="POST" '
        f'speechTimeout="auto" language={quoteattr(listen)} actionOnEmptyResult="true">'
        f"{_say(prompt, language)}</Gather>"
    )


# ------------------------------------------------------------ signatures

def _public_url(request: Request) -> str:
    """The URL Twilio signed. Behind a TLS proxy the app sees its internal
    address, so prefer the configured public origin."""
    path_qs = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL + path_qs
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        return f"{proto}://{host}{path_qs}"
    return str(request.url)


_warned_unsigned = False


async def twilio_params(request: Request) -> Dict[str, str]:
    """Parse Twilio's form body and verify its signature. Used as a dependency."""
    global _warned_unsigned
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    if not settings.TWILIO_VALIDATE_SIGNATURE:
        return params

    if not settings.TWILIO_AUTH_TOKEN:
        if settings.ENVIRONMENT.lower() in ("production", "prod"):
            logger.error("Rejecting Twilio webhook: TWILIO_AUTH_TOKEN not configured")
            raise HTTPException(status_code=403, detail="Webhook signing not configured")
        if not _warned_unsigned:
            _warned_unsigned = True
            logger.warning("TWILIO_AUTH_TOKEN not set - accepting unsigned voice webhooks (dev only)")
        return params

    from twilio.request_validator import RequestValidator

    signature = request.headers.get("X-Twilio-Signature", "")
    if not RequestValidator(settings.TWILIO_AUTH_TOKEN).validate(_public_url(request), params, signature):
        logger.warning("Rejected Twilio webhook with bad signature for %s", request.url.path)
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return params


def _caller(raw: Optional[str]) -> Optional[str]:
    n = normalize_phone(raw)
    return None if not n or n in ANONYMOUS_CALLERS else n


# --------------------------------------------------------------- routing

async def resolve_business(session, dialled: Optional[str], forwarded_from: Optional[str] = None) -> Optional[str]:
    """Which client does this call belong to?

    1. the dialled number, if it is one of ours;
    2. call forwarding: carriers that pass the original number (Twilio's
       ForwardedFrom) identify the client's own public line;
    3. outside production only, the demo clinic.
    """
    e164 = normalize_phone(dialled)
    if e164:
        business_id = (await session.execute(
            select(PhoneNumberSQL.business_id).where(
                PhoneNumberSQL.e164 == e164, PhoneNumberSQL.is_active,
            )
        )).scalar_one_or_none()
        if business_id:
            return business_id

    fwd = normalize_phone(forwarded_from)
    if fwd:
        business_id = (await session.execute(
            select(BusinessSQL.id).where(BusinessSQL.forward_from == fwd).limit(1)
        )).scalar_one_or_none()
        if business_id:
            return business_id

    # Outside production, an unassigned number (e.g. a developer's Twilio
    # trial number) reaches the demo clinic. In production it must not:
    # a misconfigured number would silently file one client's calls
    # under the demo business.
    if settings.ENVIRONMENT.lower() not in ("production", "prod"):
        from services.call_logger import get_or_create_demo_business
        return await get_or_create_demo_business(session)
    logger.error("Inbound call to unassigned number %s", dialled)
    return None


async def _route(session, request: Request, params: Dict[str, str]):
    """(business_id, caller, test_bid) for this webhook.

    Owner test calls are outbound: Twilio calls the owner from our number, so
    `To` is the owner and routing by dialled number cannot work. Those carry
    ?bid= in the webhook URL - covered by Twilio's signature, so it cannot be
    forged - and are only honoured on outbound calls.
    """
    bid = request.query_params.get("bid")
    if bid and params.get("Direction", "").startswith("outbound"):
        return bid, _caller(params.get("To")), bid
    business_id = await resolve_business(session, params.get("To"), params.get("ForwardedFrom"))
    return business_id, _caller(params.get("From")), None


def _respond_action(test_bid: Optional[str]) -> str:
    return "/api/voice/respond" + (f"?bid={test_bid}" if test_bid else "")


# -------------------------------------------------------------- webhooks

@router.get("/status")
async def voice_status():
    from api.routes.relay import relay_enabled
    return {
        "status": "voice service running",
        "twilio_configured": bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN),
        "signature_validation": settings.TWILIO_VALIDATE_SIGNATURE and bool(settings.TWILIO_AUTH_TOKEN),
        "mode": "relay" if relay_enabled() else "gather",
        "webhook": "/api/voice/incoming",
    }


@router.post("/incoming")
async def handle_incoming_call(request: Request, params: Dict[str, str] = Depends(twilio_params),
                               session=Depends(get_db)):
    """First leg of a call: greet and start listening."""
    from api.routes.relay import relay_enabled, relay_twiml
    from services import owner_alerts, usage
    from services.business_profile import load_profile
    from services.call_logger import mark_callback, start_call, turn

    call_sid = params.get("CallSid", "")
    logger.info("Incoming call %s to %s", call_sid or "?", params.get("To") or "?")

    business_id, caller, test_bid = await _route(session, request, params)
    profile = await load_profile(session, business_id) if business_id else None
    if profile is None or profile.status != "active":
        return _twiml(_say(NOT_IN_SERVICE) + "<Hangup/>")

    if call_sid and await usage.over_hard_cap(session, profile):
        # Allowance used up and the platform stops AI answering at the cap:
        # still never drop a customer silently - log a callback.
        await start_call(session, call_sid, profile.id, customer_phone=caller)
        row = await mark_callback(session, call_sid, "monthly minutes used up")
        owner_alerts.callback_alert(profile, caller, "monthly minutes used up", row.id if row else None)
        return _twiml(_say(f"Thank you for calling {profile.name}. We can't take your call right now, "
                           "but we have your number and will call you back soon. Goodbye.",
                           profile.language) + "<Hangup/>")

    greeting = profile.spoken_greeting()
    if call_sid:
        await start_call(session, call_sid, profile.id, customer_phone=caller,
                         opening_turns=[turn("agent", greeting)])
    if relay_enabled():
        return _twiml(relay_twiml(profile, greeting, test_bid))
    # If the caller never speaks, <Gather> falls through to the goodbye.
    return _twiml(_gather(greeting, profile.language, _respond_action(test_bid))
                  + _say("Thanks for calling. Goodbye.", profile.language))


def _dial(reply: str, lang: str, transfer_to: str, caller_id: str) -> Response:
    return _twiml(
        _say(reply, lang)
        # Twilio posts the outcome to /dial-status, which decides whether
        # the caller is now owed a callback.
        + f'<Dial timeout="25" action="/api/voice/dial-status" method="POST" '
          f'callerId={quoteattr(caller_id)}>{escape(transfer_to)}</Dial>'
    )


@router.post("/respond")
async def respond(request: Request, params: Dict[str, str] = Depends(twilio_params), session=Depends(get_db)):
    """Caller said something: answer it and keep the conversation going."""
    from services import owner_alerts
    from services.business_profile import load_profile
    from services.call_logger import bump_meta, find_call, mark_callback, start_call, transcript_of
    from services.call_session import UnknownBusiness, run_turn

    call_sid = params.get("CallSid", "")
    said = (params.get("SpeechResult") or "").strip()

    business_id, caller, test_bid = await _route(session, request, params)
    profile = await load_profile(session, business_id) if business_id else None
    if profile is None or profile.status != "active" or not call_sid:
        return _twiml(_say(NOT_IN_SERVICE) + "<Hangup/>")
    lang = profile.language
    action = _respond_action(test_bid)
    await start_call(session, call_sid, profile.id, customer_phone=caller)

    if not said:
        silences = await bump_meta(session, call_sid, "silences")
        if silences >= MAX_SILENCES:
            return _twiml(_say(SILENT_GOODBYE, lang) + "<Hangup/>")
        prompt = FALLBACK if silences == 1 else STILL_THERE
        return _twiml(_gather(prompt, lang, action) + _say("Thanks for calling. Goodbye.", lang))
    await bump_meta(session, call_sid, "silences", reset=True)

    caller_turns = sum(1 for t in transcript_of(await find_call(session, call_sid)) if t.get("speaker") == "caller")
    if caller_turns >= MAX_CALLER_TURNS:
        row = await mark_callback(session, call_sid, "call ran long - finish with the caller")
        owner_alerts.callback_alert(profile, caller, "call ran long", row.id if row else None)
        return _twiml(_say(TOO_LONG, lang) + "<Hangup/>")

    try:
        result = await run_turn(session, profile.id, call_sid, said, channel="phone", caller_phone=caller)
    except UnknownBusiness:
        return _twiml(_say(NOT_IN_SERVICE) + "<Hangup/>")
    except Exception as e:
        logger.exception("Voice turn failed for %s: %s", call_sid, e)
        return _twiml(_say(TROUBLE, lang) + "<Hangup/>")

    if result.transfer_to:
        # callerId must be a number we own: the dialled number inbound, our
        # number (From) on an outbound test call.
        caller_id = normalize_phone(params.get("From" if test_bid else "To")) or ""
        return _dial(result.reply, lang, result.transfer_to, caller_id)
    if result.end_call:
        return _twiml(_say(result.reply, lang) + "<Hangup/>")
    return _twiml(_gather(result.reply, lang, action) + _say("Thanks for calling. Goodbye.", lang))


@router.post("/relay-ended")
async def relay_ended(request: Request, params: Dict[str, str] = Depends(twilio_params), session=Depends(get_db)):
    """A ConversationRelay session finished. Its handoff says what next; with
    no handoff the socket dropped, so keep the caller on the <Gather> path
    rather than hanging up on them."""
    import json as _json
    from services import owner_alerts
    from services.business_profile import load_profile
    from services.call_logger import mark_callback

    try:
        handoff = _json.loads(params.get("HandoffData") or "{}")
    except ValueError:
        handoff = {}
    business_id, caller, test_bid = await _route(session, request, params)
    profile = await load_profile(session, business_id) if business_id else None
    lang = profile.language if profile else "en-IN"
    action = handoff.get("action")

    if action == "hangup":
        return _twiml("<Hangup/>")
    if action == "transfer" and handoff.get("to"):
        caller_id = normalize_phone(params.get("From" if test_bid else "To")) or ""
        return _dial("", lang, handoff["to"], caller_id)
    if action == "callback":
        row = await mark_callback(session, params.get("CallSid", ""), handoff.get("reason") or "callback")
        if profile:
            owner_alerts.callback_alert(profile, caller, handoff.get("reason") or "callback", row.id if row else None)
        return _twiml(_say(TOO_LONG, lang) + "<Hangup/>")
    if params.get("CallStatus") in ("completed", "canceled", "failed"):
        return _twiml("<Hangup/>")
    return _twiml(_gather("Sorry, the line dropped for a moment. How can I help?", lang, _respond_action(test_bid)))


@router.post("/dial-status")
async def dial_status(params: Dict[str, str] = Depends(twilio_params), session=Depends(get_db)):
    """A live transfer finished. If staff never picked up, the caller was just
    told someone will call back - make that true."""
    from services import owner_alerts
    from services.business_profile import load_profile
    from services.call_logger import mark_callback

    status = params.get("DialCallStatus", "")
    if status == "completed":
        return _twiml("<Hangup/>")

    call_sid = params.get("CallSid", "")
    row = await mark_callback(session, call_sid, f"transfer not answered ({status or 'unknown'})")
    profile = await load_profile(session, row.business_id) if row else None
    lang = profile.language if profile else "en-IN"
    if profile:
        owner_alerts.callback_alert(profile, _caller(params.get("From")),
                                    "live transfer was not answered", row.id)
    return _twiml(_say(NO_ANSWER, lang) + "<Hangup/>")


@router.post("/status-callback")
async def status_callback(params: Dict[str, str] = Depends(twilio_params), session=Depends(get_db)):
    """Final call status from the carrier. Its CallDuration is what minutes
    are billed on, so it overrides the turn-based estimate - and it is the
    moment to check the month's usage against the allowance."""
    from services import usage
    from services.business_profile import load_profile
    from services.call_logger import find_call, finish_call

    if params.get("CallStatus") in ("completed", "busy", "failed", "no-answer", "canceled"):
        duration = params.get("CallDuration")
        call_sid = params.get("CallSid", "")
        await finish_call(session, call_sid, int(duration) if duration and duration.isdigit() else None)
        row = await find_call(session, call_sid)
        if row is not None:
            try:
                profile = await load_profile(session, row.business_id)
                if profile:
                    await usage.check_and_alert(session, profile)
            except Exception as e:
                logger.warning("Usage check failed for %s: %s", call_sid, e)
    return Response(status_code=204)
