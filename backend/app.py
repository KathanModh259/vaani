#!/usr/bin/env python3

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from db import Base, DatabaseManager
from api.routes.voice import router as voice_router
from api.routes.chat import router as chat_router
from api.routes.dashboard import router as dashboard_router
from api.routes.auth import router as auth_router
from api.routes.subscriptions import router as subscriptions_router
from api.core.exception_handlers import register_exception_handlers
from api.core.middleware import RateLimitMiddleware, RequestLoggingMiddleware
from scripts.config import settings
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AI Phone Receptionist...")

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

app.include_router(auth_router, prefix="/api")
app.include_router(subscriptions_router, prefix="/api")
app.include_router(voice_router, prefix="/api/voice")
app.include_router(chat_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")


@app.get("/")
async def root():
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
