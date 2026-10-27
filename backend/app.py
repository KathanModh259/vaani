#!/usr/bin/env python3

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import text

from db import close_manager, get_manager
from api.routes.voice import router as voice_router
from api.routes.chat import router as chat_router
from api.routes.dashboard import router as dashboard_router
from api.routes.speech import router as speech_router
from api.routes.auth import router as auth_router
from api.routes.subscriptions import router as subscriptions_router
from api.routes.business import router as business_router
from api.routes.numbers import router as numbers_router
from api.routes.account import router as account_router
from api.routes.integrations import router as integrations_router
from api.routes.relay import router as relay_router
from api.core.exception_handlers import register_exception_handlers
from api.core.observability import configure_logging, init_sentry
from api.core.middleware import (
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from scripts.config import settings

configure_logging()
logger = logging.getLogger(__name__)
if init_sentry():
    logger.info("Sentry error tracking enabled")


def _is_production() -> bool:
    return settings.ENVIRONMENT.lower() in ("production", "prod")


def check_production_readiness():
    """Refuse to start a production deploy with insecure defaults.

    The JWT secret signs every session token: shipping the placeholder means
    anyone who reads this repo can mint an admin token. Failing loudly at boot
    is far better than discovering it after launch.
    """
    if not _is_production():
        if settings.JWT_SECRET_KEY == "change-me-in-production":
            logger.warning(
                "JWT_SECRET_KEY is the default placeholder - fine for local dev, "
                "but set a real one before deploying (see DEPLOYMENT.md)."
            )
        return

    problems = []
    if settings.JWT_SECRET_KEY == "change-me-in-production" or len(settings.JWT_SECRET_KEY) < 32:
        problems.append("JWT_SECRET_KEY must be set to a random value of 32+ chars")
    if settings.DEBUG:
        problems.append("DEBUG must be false in production")
    # The site promises transcripts are encrypted at rest; without a key they
    # would silently be written in plaintext.
    if not settings.ENCRYPTION_KEY or len(settings.ENCRYPTION_KEY) < 16:
        problems.append("ENCRYPTION_KEY must be set (16+ chars) - call transcripts are encrypted with it")
    # A configured key with no cipher behind it is worse than no key: it looks
    # encrypted and is not. This is how a missing `cryptography` dependency
    # reached production once already.
    from services import crypto
    if settings.ENCRYPTION_KEY and not crypto.backend_available():
        problems.append("transcript encryption is unavailable (the 'cryptography' package "
                        "is not installed) - transcripts would be stored in plaintext")
    if settings.DATABASE_URL.startswith("sqlite"):
        problems.append("DATABASE_URL must point at Postgres, not SQLite")

    if any("localhost" in o or "127.0.0.1" in o for o in settings.CORS_ORIGINS):
        logger.warning("CORS_ORIGINS still contains localhost entries in production")
    from llm_agent import providers
    model = providers.resolve_config()
    if model is None:
        logger.warning("No LLM provider configured - calls use the deterministic booking "
                       "flow, which books in English only")
    elif not model.open_weights:
        logger.warning("LLM provider %s (%s) does not publish its weights; OpenRouter, "
                       "Groq, Together or Ollama are the open-weight options",
                       model.provider, model.model)
    if not settings.TWILIO_AUTH_TOKEN:
        logger.warning("TWILIO_AUTH_TOKEN not set - all Twilio webhooks will be rejected")
    elif not settings.PUBLIC_BASE_URL:
        logger.warning("PUBLIC_BASE_URL not set - Twilio signature checks will fail behind a proxy")

    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure configuration:\n  - "
            + "\n  - ".join(problems)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI Phone Receptionist...")
    check_production_readiness()

    if settings.AUTO_MIGRATE:
        try:
            from db.migrate import upgrade_to_head

            # env.py calls asyncio.run(), which cannot nest inside this loop.
            async with asyncio.timeout(settings.DB_CONNECTION_TIMEOUT + 60):
                await asyncio.to_thread(upgrade_to_head)
        except Exception as e:
            if _is_production():
                # Serving against a half-migrated schema corrupts data; stop.
                raise RuntimeError(f"Database migration failed: {e}") from e
            logger.error("Database migration failed (continuing in dev): %s", e)

    app.state.db_manager = await get_manager()

    # A fresh deploy should have pricing without a manual seeding step; plans
    # an operator already edited are left alone.
    try:
        from scripts.seed_plans import upsert_plans
        session = await app.state.db_manager.get_session()
        try:
            created, _ = await upsert_plans(session, only_if_empty=True)
            if created:
                logger.info("Seeded %d pricing plans", created)
        finally:
            await session.close()
    except Exception as e:
        logger.warning("Plan seeding skipped: %s", e)

    from services.reminders import reminder_loop
    from services.retention import retention_loop
    background = [asyncio.create_task(retention_loop()), asyncio.create_task(reminder_loop())]

    # Load the Piper voice now so the first visitor does not pay the
    # multi-hundred-megabyte model load.
    try:
        from tts import piper_tts
        if await piper_tts.warmup():
            logger.info("Piper voice warm: %s", piper_tts.status().get("default"))
    except Exception as e:
        logger.warning(f"Piper warmup skipped: {e}")

    yield

    logger.info("Shutting down AI Phone Receptionist...")
    # Cancel and *await* the loops: returning from the lifespan while they are
    # still unwinding leaves "Task was destroyed but it is pending" noise and
    # can cut a reminder write in half during a rolling deploy.
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await close_manager()
    logger.info("Shutdown complete")


app = FastAPI(
    title="AI Phone Receptionist API",
    description="Production-grade AI phone receptionist for small businesses",
    version="0.1.0",
    lifespan=lifespan,
    # The docs map every endpoint for anyone probing the service; off in
    # production unless DOCS_ENABLED.
    docs_url="/docs" if settings.DOCS_ENABLED else None,
    redoc_url="/redoc" if settings.DOCS_ENABLED else None,
    openapi_url="/openapi.json" if settings.DOCS_ENABLED else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Reply-Text", "X-Reply-Language", "X-Reply-Emotion", "X-RateLimit-Remaining"],
)

register_exception_handlers(app)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth_router, prefix="/api")
app.include_router(subscriptions_router, prefix="/api")
app.include_router(voice_router, prefix="/api/voice")
app.include_router(chat_router, prefix="/api")
app.include_router(speech_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(business_router, prefix="/api")
app.include_router(numbers_router, prefix="/api")
app.include_router(account_router, prefix="/api")
app.include_router(integrations_router, prefix="/api")
app.include_router(relay_router, prefix="/api/voice")


@app.get("/api/info")
async def api_info():
    return {
        "name": "AI Phone Receptionist API",
        "version": "0.1.0",
        "description": "Production-grade AI phone receptionist for small businesses",
        "status": "running",
    }


@app.get("/api/llm/status")
async def llm_status():
    """Which model answers the phone, and whether its weights are open.

    Configuration only: no network call, so the dashboard can poll this
    without spending provider quota or stalling on a rate limit.
    """
    from llm_agent import llm_provider

    return llm_provider.status()


@app.get("/health")
async def health_check():
    # The timestamp used to be a hardcoded constant, so any monitor watching
    # it saw a clock frozen in the past.
    from datetime import datetime, timezone

    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health/ready")
async def readiness_check():
    """Readiness: can we actually serve a call? /health only proves the
    process is up; a load balancer should route on this one.

    The schema revision is checked too, because `AUTO_MIGRATE=false` (the
    right setting for several replicas) means the release step might have been
    skipped. Serving a call against a missing table is a 500 in front of a
    caller; reporting it here keeps traffic away until the migration runs.
    """
    try:
        manager = await get_manager()
        async with asyncio.timeout(3):
            session = await manager.get_session()
            try:
                await session.execute(text("SELECT 1"))
                revision = (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
            finally:
                await session.close()
    except Exception as e:
        logger.warning("Readiness check failed: %s", e)
        detail = "database" if "alembic_version" not in str(e) else "schema not migrated"
        return JSONResponse(status_code=503,
                            content={"status": "unavailable", "database": "down",
                                     "detail": detail})
    return {"status": "ready", "database": "ok", "schema": revision}


# Serve the Vaani frontend directly from the API server:
#   /     -> landing.html  (marketing site, pricing, signup)
#   /app  -> index.html    (interactive particle-avatar demo)
# Declared before the catch-all mount so these win; the mount then covers
# lib/, assets/ and anything else. /health, /api/* and /docs are registered
# earlier still, so they always take precedence.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
LANDING_FILE = FRONTEND_DIR / "landing.html"
APP_FILE = FRONTEND_DIR / "index.html"
DASHBOARD_FILE = FRONTEND_DIR / "dashboard.html"
LEGAL_FILE = FRONTEND_DIR / "legal.html"
RESET_FILE = FRONTEND_DIR / "reset.html"

if FRONTEND_DIR.is_dir():
    from fastapi import Request
    from api.core.pages import NoncedStaticFiles, legal_substitutions, render_page

    # Browsers heuristically cache HTML that carries no Cache-Control, which
    # means edits to these pages silently do not show up on refresh.
    HTML_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

    def _page(path: Path, request: Request, **kwargs):
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return render_page(path, request, **kwargs)

    @app.get("/", include_in_schema=False)
    async def serve_landing(request: Request):
        # Fall back to the demo if the marketing page has not been added yet.
        return _page(LANDING_FILE if LANDING_FILE.is_file() else APP_FILE, request, headers=HTML_HEADERS)

    @app.get("/app", include_in_schema=False)
    async def serve_app(request: Request):
        return _page(APP_FILE, request, headers=HTML_HEADERS)

    @app.get("/dashboard", include_in_schema=False)
    async def serve_dashboard(request: Request):
        return _page(DASHBOARD_FILE, request, headers=HTML_HEADERS)

    @app.get("/reset-password", include_in_schema=False)
    async def serve_reset(request: Request):
        # The page reads a one-time token from the URL; do not leak it onward.
        return _page(RESET_FILE, request, headers={**HTML_HEADERS, "Referrer-Policy": "no-referrer"})

    # Privacy/terms live on one page; /privacy and /terms are the URLs people
    # (and Stripe/Twilio onboarding forms) expect to be able to link to.
    @app.get("/legal", include_in_schema=False)
    @app.get("/privacy", include_in_schema=False)
    @app.get("/terms", include_in_schema=False)
    async def serve_legal(request: Request):
        return _page(LEGAL_FILE, request, headers=HTML_HEADERS, substitutions=legal_substitutions())

    @app.get("/robots.txt", include_in_schema=False)
    async def robots():
        base = settings.FRONTEND_URL.rstrip("/")
        return PlainTextResponse(
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /app\n"
            "Disallow: /dashboard\n"
            "Disallow: /api/\n"
            "Disallow: /docs\n"
            "Disallow: /reset-password\n"
            f"Sitemap: {base}/sitemap.xml\n"
        )

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap():
        base = settings.FRONTEND_URL.rstrip("/")
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f"  <url><loc>{base}/</loc><priority>1.0</priority></url>\n"
                "</urlset>\n"
            ),
            media_type="application/xml",
        )

    app.mount("/", NoncedStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
