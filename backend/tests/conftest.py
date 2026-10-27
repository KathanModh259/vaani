import os
import shutil
import sys
from pathlib import Path

# Pin the environment BEFORE anything imports scripts.config (which reads it
# once, at import). Empty strings count as "set", so load_dotenv() cannot
# fill them from a developer's .env - a real OPENAI_API_KEY or Twilio token
# there must never turn the test suite into live API calls.
os.environ.update({
    "ENVIRONMENT": "test",
    "OPENAI_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "GROQ_API_KEY": "",
    "TOGETHER_API_KEY": "",
    "LLM_PROVIDER": "auto",
    "LLM_MODEL": "",
    "LLM_BASE_URL": "",
    "LLM_API_KEY": "",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "TWILIO_PHONE_NUMBER": "",
    "DEEPGRAM_API_KEY": "",
    "ELEVENLABS_API_KEY": "",
    "SARVAM_API_KEY": "",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "LLM_FALLBACK_BASE_URL": "",
    "LLM_FALLBACK_MODEL": "",
    "SMTP_HOST": "",
    "SENTRY_DSN": "",
    "REDIS_URL": "",
    "PUBLIC_BASE_URL": "",
    "VOICE_MODE": "gather",
    "RATE_LIMIT_ENABLED": "false",
    "AUTO_MIGRATE": "false",
    "JWT_SECRET_KEY": "test-secret-key-that-is-long-enough-for-hs256",
})
os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-unit-tests-only")

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402


# Set TEST_DATABASE_URL=postgresql+asyncpg://... to run the whole suite on
# Postgres (CI does). Everything production-specific - timestamptz, row
# locks, enum types - only shows up there.
PG_URL = os.environ.get("TEST_DATABASE_URL", "")


def _reset_postgres(url):
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    async def go():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(go())


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory):
    """Run the real Alembic migrations once; each test copies the result.
    Exercises the migrations on every run without paying for them per test."""
    from db.migrate import upgrade_to_head

    if PG_URL:
        _reset_postgres(PG_URL)
        upgrade_to_head(PG_URL)
        return None
    path = tmp_path_factory.mktemp("template") / "template.db"
    upgrade_to_head(f"sqlite+aiosqlite:///{path.as_posix()}")
    return path


async def _truncate_all(url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from db import Base

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture
async def db_url(tmp_path, migrated_template, monkeypatch):
    import db
    from scripts.config import settings
    from services import call_logger

    if PG_URL:
        await _truncate_all(PG_URL)
        url = PG_URL
    else:
        target = tmp_path / "test.db"
        shutil.copy(migrated_template, target)
        url = f"sqlite+aiosqlite:///{target.as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    await db.close_manager()
    call_logger._demo_ready = False
    yield url
    await db.close_manager()


@pytest.fixture
async def session(db_url):
    import db

    manager = await db.get_manager()
    s = await manager.get_session()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
async def client(db_url):
    """An API client that authenticates only by explicit headers: it drops
    every cookie the server sets, so tests acting as several users in turn do
    not silently inherit the last signup's session cookie."""
    import httpx
    from app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        async def forget_cookies(response):
            c.cookies.clear()
        c.event_hooks["response"] = [forget_cookies]
        yield c


@pytest.fixture
async def browser(db_url):
    """A client that keeps cookies, like a real browser session."""
    import httpx
    from app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
