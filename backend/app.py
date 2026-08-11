#!/usr/bin/env python3

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db import Base, DatabaseManager
from api.routes.voice import router as voice_router
from api.routes.chat import router as chat_router
from api.routes.dashboard import router as dashboard_router
from api.routes.speech import router as speech_router
from api.routes.auth import router as auth_router
from api.routes.subscriptions import router as subscriptions_router
from api.core.exception_handlers import register_exception_handlers
from api.core.middleware import (
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from scripts.config import settings
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def check_production_readiness():
    """Refuse to start a production deploy with insecure defaults.

    The JWT secret signs every session token: shipping the placeholder means
    anyone who reads this repo can mint an admin token. Failing loudly at boot
    is far better than discovering it after launch.
    """
    if settings.ENVIRONMENT.lower() not in ("production", "prod"):
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
    if any("localhost" in o or "127.0.0.1" in o for o in settings.CORS_ORIGINS):
        logger.warning("CORS_ORIGINS still contains localhost entries in production")

    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure configuration:\n  - "
            + "\n  - ".join(problems)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI Phone Receptionist...")
    check_production_readiness()

    try:
        async with asyncio.timeout(settings.DB_CONNECTION_TIMEOUT):
            await create_db_tables()
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.warning(f"Database init skipped: {e}")

    try:
        await initialize_twilio_connections()
        logger.info("Twilio connections initialized")
    except Exception as e:
        logger.warning(f"Twilio init skipped: {e}")

    try:
        await initialize_stt_engines()
        logger.info("STT engines initialized")
    except Exception as e:
        logger.warning(f"STT init skipped: {e}")

    try:
        await initialize_tts_engines()
        logger.info("TTS engines initialized")
    except Exception as e:
        logger.warning(f"TTS init skipped: {e}")

    # Load the Piper voice now so the first visitor does not pay the
    # multi-hundred-megabyte model load.
    try:
        from tts import piper_tts
        if await piper_tts.warmup():
            logger.info("Piper voice warm: %s", piper_tts.status().get("default"))
    except Exception as e:
        logger.warning(f"Piper warmup skipped: {e}")

    try:
        db_manager = DatabaseManager(settings.DATABASE_URL)
        await db_manager.initialize()
        app.state.db_manager = db_manager
        logger.info("Database manager initialized")
    except Exception as e:
        logger.warning(f"Database manager init skipped: {e}")
        app.state.db_manager = None

    try:
        from llm_agent.agent import initialize_llm_agent
        if app.state.db_manager:
            await initialize_llm_agent(app.state.db_manager)
            logger.info("LLM agent initialized")
        else:
            logger.warning("LLM agent init skipped: no db_manager available")
    except Exception as e:
        logger.warning(f"LLM agent init skipped: {e}")

    yield

    logger.info("Shutting down AI Phone Receptionist...")
    await cleanup_resources(app)
    logger.info("Shutdown complete")


app = FastAPI(
    title="AI Phone Receptionist API",
    description="Production-grade AI phone receptionist for small businesses",
    version="0.1.0",
    lifespan=lifespan,
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


@app.get("/api/info")
async def api_info():
    return {
        "name": "AI Phone Receptionist API",
        "version": "0.1.0",
        "description": "Production-grade AI phone receptionist for small businesses",
        "status": "running",
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": "2026-07-20T17:12:00Z",
    }


async def create_db_tables():
    from db import DatabaseManager
    from sqlalchemy import create_engine

    if settings.DATABASE_URL.startswith("sqlite"):
        sync_url = settings.DATABASE_URL.replace("sqlite+aiosqlite", "sqlite")
        sync_engine = create_engine(sync_url)
        Base.metadata.create_all(bind=sync_engine)
        sync_engine.dispose()
    else:
        engine = create_async_engine(settings.DATABASE_URL)
        await DatabaseManager.create_tables(engine)
        await engine.dispose()


async def initialize_twilio_connections():
    try:
        from telephony.twilio_engine import twilio_engine
        await twilio_engine.initialize()
    except Exception as e:
        logger.warning(f"Twilio init skipped: {e}")


async def initialize_stt_engines():
    for mod_name in ["deepgram_stt", "whisper_stt"]:
        try:
            mod = __import__(f"stt.{mod_name}", fromlist=["stt"])
            if hasattr(mod, "initialize"):
                await mod.initialize()
        except Exception as e:
            logger.warning(f"STT {mod_name} init skipped: {e}")


async def initialize_tts_engines():
    for mod_name in ["elevenlabs_tts", "azure_tts"]:
        try:
            mod = __import__(f"tts.{mod_name}", fromlist=["tts"])
            if hasattr(mod, "initialize"):
                await mod.initialize()
        except Exception as e:
            logger.warning(f"TTS {mod_name} init skipped: {e}")


async def cleanup_resources(app: FastAPI):
    try:
        from telephony.twilio_engine import twilio_engine
        await twilio_engine.cleanup()
    except Exception as e:
        logger.warning(f"Twilio cleanup skipped: {e}")
    try:
        db_manager = getattr(app.state, 'db_manager', None)
        if db_manager:
            await db_manager.close()
    except Exception as e:
        logger.warning(f"DB cleanup skipped: {e}")


# Serve the Vaani frontend directly from the API server:
#   /     -> landing.html  (marketing site, pricing, signup)
#   /app  -> index.html    (interactive particle-avatar demo)
# Declared before the catch-all mount so these win; the mount then covers
# lib/, assets/ and anything else. /health, /api/* and /docs are registered
# earlier still, so they always take precedence.
from pathlib import Path

from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
LANDING_FILE = FRONTEND_DIR / "landing.html"
APP_FILE = FRONTEND_DIR / "index.html"
DASHBOARD_FILE = FRONTEND_DIR / "dashboard.html"

if FRONTEND_DIR.is_dir():

    # Browsers heuristically cache HTML that carries no Cache-Control, which
    # means edits to these pages silently do not show up on refresh. "no-cache"
    # still allows a 304 via ETag, so this costs a revalidation, not bandwidth.
    HTML_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/", include_in_schema=False)
    async def serve_landing():
        # Fall back to the demo if the marketing page has not been added yet.
        target = LANDING_FILE if LANDING_FILE.is_file() else APP_FILE
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Frontend not found")
        return FileResponse(target, headers=HTML_HEADERS)

    @app.get("/app", include_in_schema=False)
    async def serve_app():
        if not APP_FILE.is_file():
            raise HTTPException(status_code=404, detail="Demo app not found")
        return FileResponse(APP_FILE, headers=HTML_HEADERS)

    @app.get("/dashboard", include_in_schema=False)
    async def serve_dashboard():
        if not DASHBOARD_FILE.is_file():
            raise HTTPException(status_code=404, detail="Dashboard not found")
        return FileResponse(DASHBOARD_FILE, headers=HTML_HEADERS)

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

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
