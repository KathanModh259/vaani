"""Data rights: export everything, or delete everything.

The privacy policy promises access, portability and deletion (GDPR, and
India's DPDP Act). These are the endpoints that keep that promise without
someone running SQL by hand.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.auth import get_current_user
from db import (
    APIKeySQL, AppointmentSQL, BusinessHolidaysSQL, BusinessHoursSQL, BusinessSQL, CallLogSQL,
    FAQSQL, InvoiceSQL, PhoneNumberSQL, ServiceSQL, SubscriptionSQL, UserSQL, get_db,
)
from services.auth import verify_password
from services.call_logger import transcript_of

logger = logging.getLogger(__name__)
router = APIRouter(tags=["account"])


def _row(obj, exclude=()) -> dict:
    # Walk mapper attributes, not table columns: CallLogSQL.call_metadata is
    # stored in a column named "metadata", so column names are not attributes.
    out = {}
    for attr in sa_inspect(obj).mapper.column_attrs:
        if attr.key in exclude:
            continue
        v = getattr(obj, attr.key)
        out[attr.key] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


@router.get("/account/export")
async def export_account(user: UserSQL = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    """Everything stored for this account, as one JSON download."""
    bid = user.business_id
    q = lambda m: select(m).where(m.business_id == bid)  # noqa: E731
    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": _row(user, exclude=("password_hash",)),
    }
    if bid:
        b = (await session.execute(select(BusinessSQL).where(BusinessSQL.id == bid))).scalar_one_or_none()
        data["business"] = _row(b) if b else None
        for key, model in (("hours", BusinessHoursSQL), ("holidays", BusinessHolidaysSQL),
                           ("services", ServiceSQL), ("faqs", FAQSQL), ("phone_numbers", PhoneNumberSQL),
                           ("appointments", AppointmentSQL)):
            data[key] = [_row(r) for r in (await session.execute(q(model))).scalars().all()]
        calls = (await session.execute(q(CallLogSQL).order_by(CallLogSQL.start_time))).scalars().all()
        # Transcripts are decrypted: an export the owner cannot read is not an export.
        data["calls"] = [{**_row(c, exclude=("transcript",)), "transcript": transcript_of(c)} for c in calls]

    body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return Response(content=body, media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="vaani-export.json"',
        "Cache-Control": "no-store",
    })


class DeleteRequest(BaseModel):
    password: str
    confirm: str


@router.delete("/account", status_code=204)
async def delete_account(body: DeleteRequest, user: UserSQL = Depends(get_current_user),
                         session: AsyncSession = Depends(get_db)):
    """Permanently delete the account, its business and every call. Requires
    the password and the literal word DELETE - there is no undo."""
    if body.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='Type DELETE to confirm.')
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Password is incorrect")

    bid = user.business_id
    if bid:
        # Stop paying for (and routing) numbers first; a failure here must not
        # leave a deleted business still answering calls.
        from services import provisioning
        numbers = (await session.execute(select(PhoneNumberSQL).where(PhoneNumberSQL.business_id == bid))).scalars().all()
        for n in numbers:
            try:
                await provisioning.release_at_provider(n.provider_sid)
            except Exception as e:
                logger.warning("Could not release %s at provider during deletion: %s", n.e164, e)

        # Children before parents (appointments reference call logs and services).
        for model in (AppointmentSQL, CallLogSQL, FAQSQL, ServiceSQL, BusinessHoursSQL,
                      BusinessHolidaysSQL, PhoneNumberSQL):
            await session.execute(delete(model).where(model.business_id == bid))

    sub_ids = select(SubscriptionSQL.id).where(SubscriptionSQL.user_id == user.id)
    await session.execute(delete(InvoiceSQL).where(InvoiceSQL.subscription_id.in_(sub_ids)))
    await session.execute(delete(SubscriptionSQL).where(SubscriptionSQL.user_id == user.id))
    await session.execute(delete(APIKeySQL).where(APIKeySQL.user_id == user.id))
    await session.execute(delete(UserSQL).where(UserSQL.id == user.id))
    if bid:
        # Another user could still reference this business in a future
        # multi-user model; today accounts are single-owner.
        await session.execute(delete(BusinessSQL).where(BusinessSQL.id == bid))
    await session.commit()
    logger.info("Deleted account %s and business %s", user.id, bid)
