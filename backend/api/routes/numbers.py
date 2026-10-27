"""Self-serve phone numbers for the signed-in owner. See services/provisioning.py."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.auth import get_current_user
from db import BusinessSQL, PhoneNumberSQL, UserSQL, get_db
from services import provisioning
from services.provisioning import ProvisioningError

router = APIRouter(tags=["numbers"])


class BuyRequest(BaseModel):
    phone_number: str


def _fail(e: ProvisioningError):
    raise HTTPException(status_code=e.status, detail=e.message)


async def _gate(user: UserSQL, session: AsyncSession) -> BusinessSQL:
    if not provisioning.enabled():
        raise HTTPException(status_code=403, detail="Self-serve numbers are not enabled. Contact support to get a number.")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Verify your email address before adding a phone number.")
    b = (await session.execute(select(BusinessSQL).where(BusinessSQL.id == user.business_id))).scalar_one_or_none()
    if b is None:
        raise HTTPException(status_code=409, detail="No business is linked to this account yet.")
    if b.status != "active":
        raise HTTPException(status_code=403, detail="This business is not active.")
    return b


@router.get("/numbers/config")
async def numbers_config(user: UserSQL = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    limit = await provisioning.number_limit(session, user.id)
    used = await provisioning.numbers_in_use(session, user.business_id) if user.business_id else 0
    return {"enabled": provisioning.enabled(), "verified": bool(user.is_verified),
            "limit": limit, "used": used}


@router.get("/numbers/available")
async def available_numbers(contains: Optional[str] = Query(None, max_length=10, pattern=r"^\d*$"),
                            user: UserSQL = Depends(get_current_user),
                            session: AsyncSession = Depends(get_db)):
    await _gate(user, session)
    try:
        found = await provisioning.search(contains or None)
    except ProvisioningError as e:
        _fail(e)
    return [{"e164": n.e164, "friendly": n.friendly, "locality": n.locality} for n in found]


@router.post("/numbers", status_code=201)
async def buy_number(body: BuyRequest, user: UserSQL = Depends(get_current_user),
                     session: AsyncSession = Depends(get_db)):
    business = await _gate(user, session)
    limit = await provisioning.number_limit(session, user.id)
    if limit and await provisioning.numbers_in_use(session, business.id) >= limit:
        raise HTTPException(status_code=403,
                            detail=f"Your plan includes {limit} number{'s' if limit != 1 else ''}. Release one or upgrade.")
    try:
        row = await provisioning.purchase(session, business.id, business.name, body.phone_number)
    except ProvisioningError as e:
        _fail(e)
    return {"id": row.id, "e164": row.e164, "is_active": row.is_active}


@router.delete("/numbers/{number_id}", status_code=204)
async def release_number(number_id: str, user: UserSQL = Depends(get_current_user),
                         session: AsyncSession = Depends(get_db)):
    row = (await session.execute(
        select(PhoneNumberSQL).where(PhoneNumberSQL.id == number_id,
                                     PhoneNumberSQL.business_id == user.business_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        await provisioning.release_at_provider(row.provider_sid)
    except ProvisioningError as e:
        _fail(e)
    # Stop routing immediately; keep the row so old call logs still make sense.
    row.is_active = False
    await session.commit()
