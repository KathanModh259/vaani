"""Clear the demo business back to a clean slate before recording.

Deletes appointments and call logs; keeps the business, hours, services and
FAQs, because those are what make the demo meaningful. Run it before every
take so the calendar is empty and the first booking is the *first* booking.

    python -m scripts.demo_reset --yes
    python -m scripts.demo_reset --business <id> --yes
"""

import argparse
import asyncio
import sys

from sqlalchemy import delete, func, select

from db import AppointmentSQL, CallLogSQL, DatabaseManager
from scripts.config import settings


async def reset(business_id: str) -> None:
    manager = DatabaseManager(settings.DATABASE_URL)
    await manager.initialize()
    session = await manager.get_session()
    try:
        appointments = (await session.execute(
            select(func.count()).select_from(AppointmentSQL)
            .where(AppointmentSQL.business_id == business_id)
        )).scalar() or 0
        calls = (await session.execute(
            select(func.count()).select_from(CallLogSQL)
            .where(CallLogSQL.business_id == business_id)
        )).scalar() or 0

        # Appointments reference call_logs, so they go first.
        await session.execute(delete(AppointmentSQL).where(AppointmentSQL.business_id == business_id))
        await session.execute(delete(CallLogSQL).where(CallLogSQL.business_id == business_id))
        await session.commit()

        print(f"cleared {appointments} appointment(s) and {calls} call log(s) "
              f"for {business_id}")
        print("services, hours and FAQs are untouched")
    finally:
        await session.close()
        await manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset the demo business before a recording.")
    parser.add_argument("--business", default="demo-business",
                        help="business id (default: the public demo clinic)")
    parser.add_argument("--yes", action="store_true", help="required; this deletes rows")
    args = parser.parse_args()

    if not args.yes:
        print("This deletes every appointment and call log for "
              f"{args.business}. Re-run with --yes if that is what you want.", file=sys.stderr)
        raise SystemExit(2)

    asyncio.run(reset(args.business))


if __name__ == "__main__":
    main()