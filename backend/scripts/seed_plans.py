#!/usr/bin/env python3
"""Seed the subscription plans the pricing page sells.

Without rows in `plans`, GET /api/plans returns [] and every subscribe attempt
404s, so this must run once per environment.

Prices are in paise (INR minor units) to match Stripe's integer convention:
299900 renders as Rs 2,999. Edit PLANS below and re-run - existing rows are
updated in place by id, so pricing changes are safe to re-apply.

    python -m scripts.seed_plans
"""

import asyncio
import logging
import sys

from sqlalchemy import select

from db import DatabaseManager, PlanSQL
from scripts.config import settings

logger = logging.getLogger("seed_plans")

PLANS = [
    {
        "id": "starter",
        "name": "Starter",
        "description": "For solo practices and single-location businesses.",
        "price_monthly": 299900,
        "price_yearly": 2999000,   # two months free
        "currency": "inr",
        "max_businesses": 1,
        "max_phone_numbers": 1,
        "max_minutes_monthly": 500,
        "features": [
            "1 business phone number",
            "500 answered minutes / month",
            "24x7 call answering",
            "Appointment booking + SMS confirmations",
            "Hindi, English and Gujarati",
            "Call transcripts and summaries",
            "Email support",
        ],
    },
    {
        "id": "growth",
        "name": "Growth",
        "description": "For multi-location teams that cannot miss a call.",
        "price_monthly": 799900,
        "price_yearly": 7999000,
        "currency": "inr",
        "max_businesses": 3,
        "max_phone_numbers": 3,
        "max_minutes_monthly": 2000,
        "features": [
            "3 business phone numbers",
            "2,000 answered minutes / month",
            "Everything in Starter",
            "Live transfer to a human agent",
            "Calendar and CRM integrations",
            "Custom voice and greeting",
            "Analytics dashboard",
            "Priority support",
        ],
    },
    {
        "id": "enterprise",
        "name": "Enterprise",
        "description": "For groups, franchises and BPOs at volume.",
        "price_monthly": 0,        # 0 => rendered as "Custom"
        "price_yearly": 0,
        "currency": "inr",
        "max_businesses": 0,       # 0 => unlimited
        "max_phone_numbers": 0,
        "max_minutes_monthly": 0,
        # Only list what you can actually deliver today. "99.9% uptime SLA",
        # "SSO" and "data residency" were advertised here while none of them
        # existed - promising them to an enterprise buyer is a contractual
        # commitment, not marketing copy. Anything genuinely negotiated per
        # deal is labelled as such rather than implied to be built.
        "features": [
            "Unlimited numbers and locations",
            "Volume minute pricing",
            "Everything in Growth",
            "Custom voice and greeting per location",
            "Priority onboarding and migration",
            "Dedicated success manager",
            "Security review and DPA on request",
            "Custom SLA and integrations scoped per contract",
        ],
    },
]


async def upsert_plans(session, only_if_empty: bool = False) -> tuple:
    """Create or update PLANS. With only_if_empty, do nothing if any plan
    exists - the app calls it that way at boot so a fresh deploy has pricing,
    while prices an operator edited in the database are never overwritten."""
    if only_if_empty and (await session.execute(select(PlanSQL.id).limit(1))).first():
        return (0, 0)
    created = updated = 0
    for spec in PLANS:
        existing = (
            await session.execute(select(PlanSQL).where(PlanSQL.id == spec["id"]))
        ).scalar_one_or_none()
        if existing:
            for key, value in spec.items():
                setattr(existing, key, value)
            existing.is_active = True
            updated += 1
        else:
            session.add(PlanSQL(is_active=True, **spec))
            created += 1
    await session.commit()
    return (created, updated)


async def seed():
    db = DatabaseManager(settings.DATABASE_URL)
    await db.initialize()
    session = await db.get_session()

    created = updated = 0
    try:
        created, updated = await upsert_plans(session)
    finally:
        await session.close()
        await db.close()

    logger.info("Plans seeded: %d created, %d updated", created, updated)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        asyncio.run(seed())
    except Exception as e:
        logger.error("Seeding failed: %s", e)
        sys.exit(1)
