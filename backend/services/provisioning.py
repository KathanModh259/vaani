"""Self-serve phone numbers: search, buy, point at Vaani, release.

Buying a number spends real money on the platform's Twilio account, so every
purchase passes four gates: the platform flag (SELF_SERVE_NUMBERS, off by
default), a verified owner email, the plan's number limit, and an active
business. The twilio SDK is synchronous; every call runs in a worker thread
so a slow Twilio API never blocks the event loop other calls are served on.

India: numbers can only be bought with a regulatory bundle and an address
registered in the Twilio console (TWILIO_BUNDLE_SID, TWILIO_ADDRESS_SID).
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import func, select

from db import PhoneNumberSQL, PlanSQL, SubscriptionSQL
from scripts.config import settings
from services.phone import normalize_phone

logger = logging.getLogger(__name__)

DEFAULT_NUMBER_LIMIT = 1


class ProvisioningError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class AvailableNumber:
    e164: str
    friendly: str
    locality: str


def twilio_configured() -> bool:
    return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)


def enabled() -> bool:
    return settings.SELF_SERVE_NUMBERS and twilio_configured() and bool(settings.PUBLIC_BASE_URL)


def _client():
    from twilio.rest import Client
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def webhook_config() -> dict:
    base = settings.PUBLIC_BASE_URL
    return {
        "voice_url": f"{base}/api/voice/incoming",
        "voice_method": "POST",
        "status_callback": f"{base}/api/voice/status-callback",
        "status_callback_method": "POST",
    }


async def _run(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ProvisioningError:
        raise
    except Exception as e:
        logger.warning("Twilio provisioning call failed: %s", e)
        # Twilio's message (e.g. "number no longer available") is safe and useful to show.
        raise ProvisioningError(f"Phone provider error: {getattr(e, 'msg', None) or e}", status=502)


# ---------------------------------------------------------------- limits

async def number_limit(session, user_id: str) -> int:
    """0 means unlimited (the Enterprise convention in seed_plans)."""
    row = (await session.execute(
        select(PlanSQL.max_phone_numbers)
        .join(SubscriptionSQL, SubscriptionSQL.plan_id == PlanSQL.id)
        .where(SubscriptionSQL.user_id == user_id,
               SubscriptionSQL.status.in_(["active", "trialing", "past_due"]))
        .order_by(SubscriptionSQL.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    return DEFAULT_NUMBER_LIMIT if row is None else row


async def numbers_in_use(session, business_id: str) -> int:
    return (await session.execute(
        select(func.count()).select_from(PhoneNumberSQL)
        .where(PhoneNumberSQL.business_id == business_id, PhoneNumberSQL.is_active)
    )).scalar_one()


# ------------------------------------------------------------- operations

async def search(contains: Optional[str] = None, limit: int = 10) -> List[AvailableNumber]:
    country = settings.TWILIO_NUMBER_COUNTRY or "IN"

    def _search():
        c = _client().available_phone_numbers(country)
        kwargs = {"voice_enabled": True, "limit": limit}
        if contains:
            kwargs["contains"] = contains
        found = []
        # India sells few "local" numbers; mobile ranges are where the stock is.
        for kind in (c.local, c.mobile):
            try:
                found = kind.list(**kwargs)
            except Exception as e:
                logger.info("Number search (%s) failed: %s", kind, e)
                found = []
            if found:
                break
        return found

    return [AvailableNumber(n.phone_number, n.friendly_name or n.phone_number, n.locality or "")
            for n in await _run(_search)]


async def purchase(session, business_id: str, business_name: str, e164: str) -> PhoneNumberSQL:
    e164 = normalize_phone(e164)
    if not e164 or not e164.startswith("+"):
        raise ProvisioningError("Choose a number from the search results.")

    def _buy():
        kwargs = dict(phone_number=e164, friendly_name=f"Vaani - {business_name}"[:64], **webhook_config())
        if settings.TWILIO_BUNDLE_SID:
            kwargs["bundle_sid"] = settings.TWILIO_BUNDLE_SID
        if settings.TWILIO_ADDRESS_SID:
            kwargs["address_sid"] = settings.TWILIO_ADDRESS_SID
        return _client().incoming_phone_numbers.create(**kwargs)

    bought = await _run(_buy)
    row = PhoneNumberSQL(id=uuid.uuid4().hex, business_id=business_id, e164=bought.phone_number,
                         provider="twilio", provider_sid=bought.sid, is_active=True)
    session.add(row)
    try:
        await session.commit()
    except Exception:
        # We paid for it but could not record it: release rather than leak a
        # number that bills monthly and routes nowhere.
        await session.rollback()
        await release_at_provider(bought.sid)
        raise
    logger.info("Provisioned %s for business %s", bought.phone_number, business_id)
    return row


async def configure_existing(e164: str) -> Optional[str]:
    """Point an already-owned number's webhooks at this deployment. Returns its SID."""
    def _configure():
        c = _client()
        matches = c.incoming_phone_numbers.list(phone_number=e164, limit=1)
        if not matches:
            return None
        c.incoming_phone_numbers(matches[0].sid).update(**webhook_config())
        return matches[0].sid

    return await _run(_configure)


async def release_at_provider(provider_sid: Optional[str]) -> None:
    if not provider_sid or not twilio_configured():
        return
    await _run(lambda: _client().incoming_phone_numbers(provider_sid).delete())
