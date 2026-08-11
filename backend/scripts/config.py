import os
from typing import Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

def _env(key: str, default: str = ""):
    return os.getenv(key, default)

def _env_int(key: str, default: int):
    return int(os.getenv(key, str(default)))

def _env_bool(key: str, default: bool = False):
    return os.getenv(key, str(default)).lower() == "true"


class Settings(BaseModel):
    # API Configuration
    HOST: str = Field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    PORT: int = Field(default_factory=lambda: _env_int("PORT", 8000))
    DEBUG: bool = Field(default_factory=lambda: _env_bool("DEBUG", False))
    ENVIRONMENT: str = Field(default_factory=lambda: _env("ENVIRONMENT", "development"))
    LOG_LEVEL: str = Field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    
    # Database Configuration
    DATABASE_URL: str = Field(default_factory=lambda: _env("DATABASE_URL", "sqlite+aiosqlite:///phone_receptionist.db"))
    DB_CONNECTION_TIMEOUT: int = Field(default_factory=lambda: _env_int("DB_CONNECTION_TIMEOUT", 30))
    DB_POOL_SIZE: int = Field(default_factory=lambda: _env_int("DB_POOL_SIZE", 10))
    DB_MAX_OVERFLOW: int = Field(default_factory=lambda: _env_int("DB_MAX_OVERFLOW", 20))
    
    # Twilio Configuration
    TWILIO_ACCOUNT_SID: str = Field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    TWILIO_AUTH_TOKEN: str = Field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    TWILIO_PHONE_NUMBER: str = Field(default_factory=lambda: _env("TWILIO_PHONE_NUMBER"))
    
    # Deepgram Configuration
    DEEPGRAM_API_KEY: str = Field(default_factory=lambda: _env("DEEPGRAM_API_KEY"))
    
    # ElevenLabs Configuration
    ELEVENLABS_API_KEY: str = Field(default_factory=lambda: _env("ELEVENLABS_API_KEY"))
    ELEVENLABS_VOICE_ID: str = Field(default_factory=lambda: _env("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"))
    ELEVENLABS_MODEL: str = Field(default_factory=lambda: _env("ELEVENLABS_MODEL", "eleven_flash_v2_5"))

    # Speech engine selection. "auto" picks the best configured provider and
    # degrades to the browser's built-in speech when nothing is installed,
    # so a demo works with zero keys and zero downloads.
    #   TTS: auto | elevenlabs | piper | none
    #   STT: auto | deepgram | faster_whisper | none
    TTS_PROVIDER: str = Field(default_factory=lambda: _env("TTS_PROVIDER", "auto").lower())
    STT_PROVIDER: str = Field(default_factory=lambda: _env("STT_PROVIDER", "auto").lower())

    # Piper - free, offline, MIT-licensed neural TTS.
    # PIPER_VOICE pins one extra .onnx path; any voice in backend/models/piper
    # is picked up automatically. PIPER_DEFAULT_VOICE names which to use when
    # a request does not ask for one (else the first English voice wins).
    PIPER_VOICE: str = Field(default_factory=lambda: _env("PIPER_VOICE"))
    PIPER_DEFAULT_VOICE: str = Field(default_factory=lambda: _env("PIPER_DEFAULT_VOICE"))

    # faster-whisper - free, offline, MIT-licensed STT
    WHISPER_MODEL_SIZE: str = Field(default_factory=lambda: _env("WHISPER_MODEL_SIZE", "base"))
    WHISPER_DEVICE: str = Field(default_factory=lambda: _env("WHISPER_DEVICE", "cpu"))
    WHISPER_COMPUTE_TYPE: str = Field(default_factory=lambda: _env("WHISPER_COMPUTE_TYPE", "int8"))
    
    # LLM Configuration (OpenAI)
    OPENAI_API_KEY: str = Field(default_factory=lambda: _env("OPENAI_API_KEY"))
    LLM_MODEL: str = Field(default_factory=lambda: _env("LLM_MODEL", "gpt-4o-mini"))
    
    # Security
    ENCRYPTION_KEY: str = Field(default_factory=lambda: _env("ENCRYPTION_KEY"))
    JWT_SECRET_KEY: str = Field(default_factory=lambda: _env("JWT_SECRET_KEY", "change-me-in-production"))
    JWT_ALGORITHM: str = Field(default_factory=lambda: _env("JWT_ALGORITHM", "HS256"))
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default_factory=lambda: _env_int("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 60))
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default_factory=lambda: _env_int("JWT_REFRESH_TOKEN_EXPIRE_DAYS", 30))
    
    # Stripe Configuration
    STRIPE_SECRET_KEY: str = Field(default_factory=lambda: _env("STRIPE_SECRET_KEY"))
    STRIPE_WEBHOOK_SECRET: str = Field(default_factory=lambda: _env("STRIPE_WEBHOOK_SECRET"))
    STRIPE_PRICE_MONTHLY: str = Field(default_factory=lambda: _env("STRIPE_PRICE_MONTHLY"))
    STRIPE_PRICE_YEARLY: str = Field(default_factory=lambda: _env("STRIPE_PRICE_YEARLY"))
    
    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = Field(default_factory=lambda: _env_bool("RATE_LIMIT_ENABLED", True))
    RATE_LIMIT_REQUESTS: int = Field(default_factory=lambda: _env_int("RATE_LIMIT_REQUESTS", 100))
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default_factory=lambda: _env_int("RATE_LIMIT_WINDOW_SECONDS", 60))
    
    # CORS
    # The API serves the frontend same-origin, so these only matter for split
    # deploys and the standalone static server. 127.0.0.1 is a distinct origin
    # from localhost to the browser, so both spellings must be listed.
    FRONTEND_URL: str = Field(default_factory=lambda: _env("FRONTEND_URL", "http://localhost:3000"))
    CORS_ORIGINS: list = Field(default_factory=lambda: [
        o.strip() for o in _env(
            "CORS_ORIGINS",
            "http://localhost:3000,http://localhost:8080,http://localhost:8000,"
            "http://127.0.0.1:3000,http://127.0.0.1:8080,http://127.0.0.1:8000",
        ).split(",") if o.strip()
    ])


try:
    settings = Settings()
except Exception as e:
    print(f"Error loading settings: {e}")
    raise
