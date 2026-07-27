import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db import get_db, PlanSQL, SubscriptionSQL, UserSQL
from db.db_models.models import Plan
from api.routes.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["subscriptions"])


@router.get("/plans", response_model=list[Plan])
async def list_plans(db_session: AsyncSession = Depends(get_db)):
    result = await db_session.execute(
        select(PlanSQL).where(PlanSQL.is_active)
    )
    plans = result.scalars().all()
    return [
        Plan(
            id=p.id,
            name=p.name,
            description=p.description,
            price_monthly=p.price_monthly,
            price_yearly=p.price_yearly,
            currency=p.currency,
            max_businesses=p.max_businesses,
            max_phone_numbers=p.max_phone_numbers,
            max_minutes_monthly=p.max_minutes_monthly,
            features=p.features or [],
            is_active=p.is_active,
            created_at=p.created_at,
        )
        for p in plans
    ]


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def create_subscription(
    plan_id: str,
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    result = await db_session.execute(select(PlanSQL).where(PlanSQL.id == plan_id, PlanSQL.is_active))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    existing = await db_session.execute(
        select(SubscriptionSQL).where(
            SubscriptionSQL.user_id == current_user.id,
            SubscriptionSQL.status.in_(["active", "trialing", "past_due"]),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already has an active subscription")

    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=14)

    sub = SubscriptionSQL(
        id=__import__("uuid").uuid4().hex,
        user_id=current_user.id,
        plan_id=plan_id,
        status="trialing",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        trial_end=trial_end,
    )
    db_session.add(sub)
    await db_session.commit()
    await db_session.refresh(sub)

    return {
        "id": sub.id,
        "status": sub.status,
        "trial_end": sub.trial_end.isoformat(),
        "current_period_end": sub.current_period_end.isoformat(),
    }


@router.get("/subscriptions/current")
async def get_current_subscription(
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    result = await db_session.execute(
        select(SubscriptionSQL).where(
            SubscriptionSQL.user_id == current_user.id,
        ).order_by(SubscriptionSQL.created_at.desc())
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return None

    plan_result = await db_session.execute(select(PlanSQL).where(PlanSQL.id == sub.plan_id))
    plan = plan_result.scalar_one_or_none()

    return {
        "id": sub.id,
        "status": sub.status,
        "plan": {
            "id": plan.id,
            "name": plan.name,
            "price_monthly": plan.price_monthly,
            "features": plan.features or [],
        } if plan else None,
        "current_period_start": sub.current_period_start.isoformat(),
        "current_period_end": sub.current_period_end.isoformat(),
        "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
        "canceled_at": sub.canceled_at.isoformat() if sub.canceled_at else None,
    }


@router.post("/subscriptions/cancel")
async def cancel_subscription(
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    result = await db_session.execute(
        select(SubscriptionSQL).where(
            SubscriptionSQL.user_id == current_user.id,
            SubscriptionSQL.status.in_(["active", "trialing", "past_due"]),
        )
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription found")

    sub.status = "canceled"
    sub.canceled_at = datetime.now(timezone.utc)
    await db_session.commit()

    return {"status": "canceled", "canceled_at": sub.canceled_at.isoformat()}
