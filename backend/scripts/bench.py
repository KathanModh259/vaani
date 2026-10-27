"""Latency numbers for the claims in the README.

    python -m scripts.bench
    python -m scripts.bench --turns 40

Measured in-process through the real ASGI app against a throwaway migrated
database, so the numbers are the application's own cost: no network, no TLS,
no model provider. That makes them comparable between changes and honest about
what they exclude.

The voice path adds transcription, the model and synthesis on top of this; the
point of measuring the turn itself is that a slow turn is the one thing a
caller hears as "the robot is thinking".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import statistics
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")


def _prepare() -> str:
    tmp = Path(tempfile.mkdtemp(prefix="vaani-bench-"))
    url = f"sqlite+aiosqlite:///{(tmp / 'bench.db').as_posix()}"
    os.environ.update({
        "ENVIRONMENT": "test",
        "DATABASE_URL": url,
        "AUTO_MIGRATE": "true",
        "RATE_LIMIT_ENABLED": "false",
        "JWT_SECRET_KEY": "bench-secret-key-that-is-long-enough-1234",
        "ENCRYPTION_KEY": "bench-encryption-key-32-chars-abcdef",
        "PUBLIC_BASE_URL": "",
        "OPENAI_API_KEY": "", "OPENROUTER_API_KEY": "", "GROQ_API_KEY": "",
        "TOGETHER_API_KEY": "", "LLM_BASE_URL": "", "LLM_PROVIDER": "auto",
        "LLM_MODEL": "", "SMTP_HOST": "", "REDIS_URL": "", "SENTRY_DSN": "",
    })
    return url


def _bookable_day(offset: int = 30) -> str:
    d = datetime.now(IST).date() + timedelta(days=offset)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


def _pct(values: List[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return ordered[index]


async def run(turns: int) -> None:
    url = _prepare()

    import httpx

    from app import app

    rows: List[tuple[str, int, float, float, float]] = []

    async def timeit(label: str, n: int, call: Callable) -> None:
        samples: List[float] = []
        for _ in range(n):
            started = time.perf_counter()
            await call()
            samples.append((time.perf_counter() - started) * 1000)
        rows.append((label, n, statistics.median(samples),
                     _pct(samples, 95), max(samples)))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
            async def chat() -> None:
                await client.post("/api/chat", json={"message": "what are your opening hours?"})

            await timeit("chat turn (rules path)", turns, chat)

            day = _bookable_day()
            counter = {"n": 0}

            async def book() -> None:
                counter["n"] += 1
                minute = counter["n"] % 2
                await client.post("/api/chat", json={
                    "message": (f"dental check-up on {day} at "
                                f"{9 + counter['n'] // 40}:{minute * 30:02d}, "
                                f"name Bench {counter['n']}, 9000000{counter['n']:03d}"),
                })

            await timeit("booking write (end to end)", 20, book)

            from db import DatabaseManager
            from services import booking
            from services.business_profile import load_profile
            from services.call_logger import get_or_create_demo_business

            manager = DatabaseManager(url)
            await manager.initialize()
            session = await manager.get_session()
            try:
                bid = await get_or_create_demo_business(session)
                profile = await load_profile(session, bid)
                service = profile.find_service("check-up")

                async def availability() -> None:
                    await booking.free_slots(session, profile, service,
                                             datetime.fromisoformat(day).date())

                await timeit("availability read", 50, availability)
            finally:
                await session.close()
                await manager.close()

    print(f"\nVaani latency  |  {platform.system()} {platform.release()}, "
          f"{os.cpu_count()} cores, in-process ASGI, SQLite")
    print(f"{'':34}{'n':>4}{'p50':>9}{'p95':>9}{'max':>9}")
    for label, n, p50, p95, worst in rows:
        print(f"{label:<34}{n:>4}{p50:>8.1f}ms{p95:>7.1f}ms{worst:>7.1f}ms")
    print("\nExcludes speech recognition, the model provider and synthesis; "
          "those are network-bound and vary by provider.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure turn latency in-process.")
    parser.add_argument("--turns", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(run(args.turns))


if __name__ == "__main__":
    main()