#!/usr/bin/env python3
"""Attach a Twilio number to a client's business.

Inbound calls are routed by the dialled number, so a number must be assigned
before a client's callers reach their own receptionist. Until self-serve
number provisioning ships, onboard a client with:

    python -m scripts.assign_number owner@clinic.in +919876543210
    python -m scripts.assign_number owner@clinic.in +919876543210 --release

Then point that number's Twilio webhooks at:
    A call comes in:     POST {PUBLIC_BASE_URL}/api/voice/incoming
    Call status changes: POST {PUBLIC_BASE_URL}/api/voice/status-callback
"""

import argparse
import asyncio
import sys
import uuid
from typing import Optional

from sqlalchemy import select

from db import PhoneNumberSQL, UserSQL, get_manager, close_manager
from services.phone import normalize_phone


class AssignError(Exception):
    pass


async def assign(session, business_id: str, number: str, provider_sid: Optional[str] = None) -> PhoneNumberSQL:
    e164 = normalize_phone(number)
    if not e164 or not e164.startswith("+"):
        raise AssignError(f"{number!r} is not a full international number (e.g. +919876543210)")

    existing = (await session.execute(select(PhoneNumberSQL).where(PhoneNumberSQL.e164 == e164))).scalar_one_or_none()
    if existing and existing.business_id != business_id and existing.is_active:
        raise AssignError(f"{e164} already belongs to business {existing.business_id}")
    if existing:
        existing.business_id, existing.is_active = business_id, True
        existing.provider_sid = provider_sid or existing.provider_sid
        row = existing
    else:
        row = PhoneNumberSQL(id=uuid.uuid4().hex, business_id=business_id, e164=e164,
                             provider="twilio", provider_sid=provider_sid, is_active=True)
        session.add(row)
    await session.commit()
    return row


async def release(session, number: str) -> bool:
    row = (await session.execute(
        select(PhoneNumberSQL).where(PhoneNumberSQL.e164 == normalize_phone(number))
    )).scalar_one_or_none()
    if not row:
        return False
    row.is_active = False
    await session.commit()
    return True


async def _main(email: str, number: str, do_release: bool) -> int:
    session = await (await get_manager()).get_session()
    try:
        if do_release:
            print("Released" if await release(session, number) else "Number not found")
            return 0
        user = (await session.execute(select(UserSQL).where(UserSQL.email == email.strip().lower()))).scalar_one_or_none()
        if not user or not user.business_id:
            print(f"No account with a business for {email}", file=sys.stderr)
            return 1
        row = await assign(session, user.business_id, number)
        print(f"{row.e164} -> business {row.business_id}")
        return 0
    except AssignError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        await session.close()
        await close_manager()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email")
    ap.add_argument("number")
    ap.add_argument("--release", action="store_true", help="stop routing this number")
    a = ap.parse_args()
    sys.exit(asyncio.run(_main(a.email, a.number, a.release)))
