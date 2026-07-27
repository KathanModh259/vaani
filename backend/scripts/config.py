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
    ELEVENLABS_VOICE_ID: str = Field(default_factory=lambda: _env("ELEVENLABS_VOICE_ID"))
    
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
    FRONTEND_URL: str = Field(default_factory=lambda: _env("FRONTEND_URL", "http://localhost:3000"))
    CORS_ORIGINS: list = Field(default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:3000,http://localhost:8080").split(","))


try:
    settings = Settings()
except Exception as e:
    print(f"Error loading settings: {e}")
    raise
