import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

def _env(key: str, default: str = ""):
    return os.getenv(key, default)

# A blank line in .env ("DOCS_ENABLED=") means "use the default", not
# false/zero - otherwise copying .env.example silently flips defaults, and
# a blank integer crashes the app at import.
def _env_int(key: str, default: int):
    raw = os.getenv(key, "").strip()
    return int(raw) if raw else default

def _env_bool(key: str, default: bool = False):
    raw = os.getenv(key, "").strip().lower()
    return raw in ("true", "1", "yes", "on") if raw else default


def _database_url() -> str:
    """The DATABASE_URL, with the async driver made explicit.

    Managed hosts (Render, Heroku, Railway) hand out `postgres://` or
    `postgresql://`, neither of which SQLAlchemy can open asynchronously.
    Rewriting the scheme here means the value from the dashboard can be pasted
    in as-is instead of silently failing at boot with "greenlet_spawn has not
    been called".
    """
    raw = _env("DATABASE_URL", "sqlite+aiosqlite:///phone_receptionist.db").strip()
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+asyncpg://" + raw[len(prefix):]
    return raw


class Settings(BaseModel):
    # API Configuration
    HOST: str = Field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    PORT: int = Field(default_factory=lambda: _env_int("PORT", 8000))
    DEBUG: bool = Field(default_factory=lambda: _env_bool("DEBUG", False))
    ENVIRONMENT: str = Field(default_factory=lambda: _env("ENVIRONMENT", "development"))
    LOG_LEVEL: str = Field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    
    # Database Configuration
    DATABASE_URL: str = Field(default_factory=_database_url)
    DB_CONNECTION_TIMEOUT: int = Field(default_factory=lambda: _env_int("DB_CONNECTION_TIMEOUT", 30))
    DB_POOL_SIZE: int = Field(default_factory=lambda: _env_int("DB_POOL_SIZE", 10))
    DB_MAX_OVERFLOW: int = Field(default_factory=lambda: _env_int("DB_MAX_OVERFLOW", 20))
    
    # Run Alembic migrations at startup. Turn off when running several
    # replicas and migrate once per deploy with `python -m db.migrate`.
    AUTO_MIGRATE: bool = Field(default_factory=lambda: _env_bool("AUTO_MIGRATE", True))

    # Public origin Twilio reaches us on, e.g. https://vaani.example.com.
    # Twilio signs the exact URL it called; behind a proxy the app sees
    # http://127.0.0.1:8000 instead, so signatures cannot be checked without it.
    PUBLIC_BASE_URL: str = Field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))

    # Twilio Configuration
    TWILIO_ACCOUNT_SID: str = Field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    TWILIO_AUTH_TOKEN: str = Field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    TWILIO_PHONE_NUMBER: str = Field(default_factory=lambda: _env("TWILIO_PHONE_NUMBER"))
    # Reject webhooks without a valid X-Twilio-Signature. Only disable for
    # local testing with tools that cannot sign requests.
    TWILIO_VALIDATE_SIGNATURE: bool = Field(default_factory=lambda: _env_bool("TWILIO_VALIDATE_SIGNATURE", True))
    # SMS in India goes through a DLT-registered sender; a Messaging Service
    # carries that registration. Falls back to sending from the business number.
    TWILIO_MESSAGING_SERVICE_SID: str = Field(default_factory=lambda: _env("TWILIO_MESSAGING_SERVICE_SID"))
    
    # Deepgram Configuration
    DEEPGRAM_API_KEY: str = Field(default_factory=lambda: _env("DEEPGRAM_API_KEY"))
    DEEPGRAM_MODEL: str = Field(default_factory=lambda: _env("DEEPGRAM_MODEL", "nova-2"))
    
    # ElevenLabs Configuration
    ELEVENLABS_API_KEY: str = Field(default_factory=lambda: _env("ELEVENLABS_API_KEY"))
    ELEVENLABS_VOICE_ID: str = Field(default_factory=lambda: _env("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"))
    ELEVENLABS_MODEL: str = Field(default_factory=lambda: _env("ELEVENLABS_MODEL", "eleven_flash_v2_5"))

    # Speech engine selection. "auto" picks the best configured provider and
    # degrades to the browser's built-in speech when nothing is installed,
    # so a demo works with zero keys and zero downloads.
    #   TTS: auto | sarvam | elevenlabs | piper | none
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
    
    # LLM Configuration.
    #
    # Vaani talks to any OpenAI-compatible endpoint, which is what every
    # open-weight host speaks. LLM_PROVIDER=auto picks the first configured
    # provider in this order: custom LLM_BASE_URL -> OpenRouter -> Groq ->
    # Together. OpenAI stays supported for older deployments but is reported
    # as closed weights, because the default story is open models.
    #
    # Leaving LLM_MODEL blank means "use the provider's default model", so
    # switching provider does not silently send a model name it does not host.
    LLM_PROVIDER: str = Field(default_factory=lambda: _env("LLM_PROVIDER", "auto").lower())
    LLM_MODEL: str = Field(default_factory=lambda: _env("LLM_MODEL"))
    LLM_BASE_URL: str = Field(default_factory=lambda: _env("LLM_BASE_URL"))
    LLM_API_KEY: str = Field(default_factory=lambda: _env("LLM_API_KEY"))
    LLM_TIMEOUT_SECONDS: float = Field(default_factory=lambda: float(_env("LLM_TIMEOUT_SECONDS") or 10.0))

    OPENAI_API_KEY: str = Field(default_factory=lambda: _env("OPENAI_API_KEY"))
    OPENROUTER_API_KEY: str = Field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    GROQ_API_KEY: str = Field(default_factory=lambda: _env("GROQ_API_KEY"))
    TOGETHER_API_KEY: str = Field(default_factory=lambda: _env("TOGETHER_API_KEY"))
    
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
    
    # Self-serve phone numbers. Off by default: buying a number spends real
    # money on the platform's Twilio account, and there is no billing yet to
    # recover it. Turn on once payments exist (or for trusted accounts).
    SELF_SERVE_NUMBERS: bool = Field(default_factory=lambda: _env_bool("SELF_SERVE_NUMBERS", False))
    TWILIO_NUMBER_COUNTRY: str = Field(default_factory=lambda: _env("TWILIO_NUMBER_COUNTRY", "IN"))
    # India requires a regulatory bundle and address on every number purchase.
    TWILIO_BUNDLE_SID: str = Field(default_factory=lambda: _env("TWILIO_BUNDLE_SID"))
    TWILIO_ADDRESS_SID: str = Field(default_factory=lambda: _env("TWILIO_ADDRESS_SID"))

    # Email (password resets, verification, owner notifications). With no
    # SMTP_HOST, emails are logged instead of sent - fine for development.
    SMTP_HOST: str = Field(default_factory=lambda: _env("SMTP_HOST"))
    SMTP_PORT: int = Field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    SMTP_USERNAME: str = Field(default_factory=lambda: _env("SMTP_USERNAME"))
    SMTP_PASSWORD: str = Field(default_factory=lambda: _env("SMTP_PASSWORD"))
    SMTP_FROM: str = Field(default_factory=lambda: _env("SMTP_FROM", "Vaani <no-reply@localhost>"))
    SMTP_STARTTLS: bool = Field(default_factory=lambda: _env_bool("SMTP_STARTTLS", True))

    # Transcripts older than this are scrubbed for businesses that have not
    # set their own retention. Empty = keep until the business sets one.
    DEFAULT_RETENTION_DAYS: int = Field(default_factory=lambda: _env_int("DEFAULT_RETENTION_DAYS", 0))

    # Observability
    SENTRY_DSN: str = Field(default_factory=lambda: _env("SENTRY_DSN"))
    LOG_FORMAT: str = Field(default_factory=lambda: _env("LOG_FORMAT", "text").lower())  # text | json
    # Interactive API docs (/docs). Off in production unless asked for: they
    # map every endpoint for anyone probing the service.
    DOCS_ENABLED: bool = Field(default_factory=lambda: _env_bool(
        "DOCS_ENABLED", _env("ENVIRONMENT", "development").lower() not in ("production", "prod")))

    # Phone conversation engine:
    #   gather - request/response TwiML (<Gather>/<Say>). Simple, robust.
    #   relay  - Twilio ConversationRelay over a WebSocket: streaming speech
    #            recognition and TTS, barge-in, "one moment" fillers. Much
    #            lower perceived latency. Needs a wss:// PUBLIC_BASE_URL.
    VOICE_MODE: str = Field(default_factory=lambda: _env("VOICE_MODE", "gather").lower())
    RELAY_TTS_PROVIDER: str = Field(default_factory=lambda: _env("RELAY_TTS_PROVIDER", "Google"))
    RELAY_TRANSCRIPTION_PROVIDER: str = Field(default_factory=lambda: _env("RELAY_TRANSCRIPTION_PROVIDER", "Deepgram"))

    # WhatsApp (via Twilio). Business-initiated WhatsApp messages must use a
    # Meta-approved template; these are Twilio Content SIDs (HX...).
    TWILIO_WHATSAPP_FROM: str = Field(default_factory=lambda: _env("TWILIO_WHATSAPP_FROM"))
    WHATSAPP_CONFIRMATION_CONTENT_SID: str = Field(default_factory=lambda: _env("WHATSAPP_CONFIRMATION_CONTENT_SID"))
    WHATSAPP_REMINDER_CONTENT_SID: str = Field(default_factory=lambda: _env("WHATSAPP_REMINDER_CONTENT_SID"))

    # Google Calendar sync (OAuth "Web application" client).
    GOOGLE_CLIENT_ID: str = Field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    GOOGLE_CLIENT_SECRET: str = Field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))

    # Sarvam AI - Indian-language TTS for the web demo (Hindi, Gujarati, ...).
    SARVAM_API_KEY: str = Field(default_factory=lambda: _env("SARVAM_API_KEY"))
    SARVAM_SPEAKER: str = Field(default_factory=lambda: _env("SARVAM_SPEAKER", "priya"))
    SARVAM_MODEL: str = Field(default_factory=lambda: _env("SARVAM_MODEL", "bulbul:v3"))

    # Second LLM, used when the primary errors or times out. Any
    # OpenAI-compatible endpoint: Azure OpenAI, Groq, Together, a local vLLM.
    LLM_FALLBACK_BASE_URL: str = Field(default_factory=lambda: _env("LLM_FALLBACK_BASE_URL"))
    LLM_FALLBACK_API_KEY: str = Field(default_factory=lambda: _env("LLM_FALLBACK_API_KEY"))
    LLM_FALLBACK_MODEL: str = Field(default_factory=lambda: _env("LLM_FALLBACK_MODEL"))

    # When a business passes its monthly minute allowance: alert only (false),
    # or also stop answering with the AI and take messages (true).
    HARD_MINUTE_CAP: bool = Field(default_factory=lambda: _env_bool("HARD_MINUTE_CAP", False))

    # Filled into the [PLACEHOLDERS] of /legal. Have a lawyer review the text.
    LEGAL_ENTITY_NAME: str = Field(default_factory=lambda: _env("LEGAL_ENTITY_NAME"))
    LEGAL_ADDRESS: str = Field(default_factory=lambda: _env("LEGAL_ADDRESS"))
    LEGAL_CITY: str = Field(default_factory=lambda: _env("LEGAL_CITY"))
    LEGAL_JURISDICTION: str = Field(default_factory=lambda: _env("LEGAL_JURISDICTION"))
    PRIVACY_EMAIL: str = Field(default_factory=lambda: _env("PRIVACY_EMAIL"))
    SUPPORT_EMAIL: str = Field(default_factory=lambda: _env("SUPPORT_EMAIL"))
    CLOUD_HOST: str = Field(default_factory=lambda: _env("CLOUD_HOST"))
    RETENTION_PERIOD_TEXT: str = Field(default_factory=lambda: _env("RETENTION_PERIOD_TEXT"))

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = Field(default_factory=lambda: _env_bool("RATE_LIMIT_ENABLED", True))
    RATE_LIMIT_REQUESTS: int = Field(default_factory=lambda: _env_int("RATE_LIMIT_REQUESTS", 100))
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default_factory=lambda: _env_int("RATE_LIMIT_WINDOW_SECONDS", 60))
    # Share limits across replicas. Without it each process counts alone.
    REDIS_URL: str = Field(default_factory=lambda: _env("REDIS_URL"))
    
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
