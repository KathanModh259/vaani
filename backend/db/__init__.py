from typing import AsyncGenerator, Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, DateTime, Date, Enum, Integer, Boolean, Text, JSON, ForeignKey, Index, func

from db.db_models.models import (
    BaseDBModel as BaseDBModel,
    Business as Business,
    Service as Service,
    Appointment as Appointment,
    CallLog as CallLog,
    BusinessHours as BusinessHours,
    BusinessHolidays as BusinessHolidays,
    User as User,
    APIKeyCreate as APIKeyCreate,
    APIKeyResponse as APIKeyResponse,
    APIKeyFullResponse as APIKeyFullResponse,
    Plan as Plan,
    Subscription as Subscription,
    Invoice as Invoice,
)

Base = declarative_base()

# SQLAlchemy model classes
class BusinessSQL(Base):
    __tablename__ = "businesses"
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String, nullable=False)
    status = Column(Enum("active", "inactive", "suspended", name="business_status"), nullable=False, default="active")
    time_zone = Column(String, default="Asia/Kolkata")
    currency = Column(String, default="INR")

    # What the receptionist says and knows. Everything a caller can be told
    # about this business comes from these columns plus services/hours/faqs -
    # never from text baked into the code.
    greeting = Column(Text, nullable=True)
    about = Column(Text, nullable=True)
    # Where escalations go. Without it Vaani takes a message instead of
    # promising a transfer she cannot make.
    transfer_phone = Column(String, nullable=True)
    # Twilio <Gather> speech language: en-IN, hi-IN, gu-IN.
    language = Column(String, nullable=False, default="en-IN", server_default="en-IN")
    booking_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    sms_confirmations = Column(Boolean, nullable=False, default=False, server_default="0")
    # Granularity of offered appointment start times.
    slot_minutes = Column(Integer, nullable=False, default=30, server_default="30")
    # Transcripts older than this are purged. NULL keeps them indefinitely.
    retention_days = Column(Integer, nullable=True)
    # Appointments that may overlap: chairs in a salon, doctors in a clinic.
    capacity = Column(Integer, nullable=False, default=1, server_default="1")
    # How confirmations and reminders reach the caller: sms | whatsapp.
    message_channel = Column(String, nullable=False, default="sms", server_default="sms")
    reminders_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    # The business's own public number, when calls reach Vaani by carrier
    # call forwarding rather than by customers dialling a new number.
    forward_from = Column(String, nullable=True)
    # Google Calendar: encrypted OAuth refresh token + calendar to sync with.
    gcal_token = Column(Text, nullable=True)
    gcal_calendar_id = Column(String, nullable=True)
    # Owner-set monthly cap on answered minutes (NULL = plan allowance).
    monthly_minute_cap = Column(Integer, nullable=True)
    # Last usage alert sent, e.g. "2026-09:80", so each threshold fires once a month.
    usage_alert_state = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class PhoneNumberSQL(Base):
    """A dialable number and the business it belongs to.

    Inbound calls are routed by the number the caller dialled (Twilio's `To`),
    so this table is what keeps one client's callers out of another client's
    dashboard.
    """
    __tablename__ = "phone_numbers"

    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False, index=True)
    e164 = Column(String, nullable=False, unique=True, index=True)
    provider = Column(String, nullable=False, default="twilio", server_default="twilio")
    provider_sid = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), default=func.now())


class FAQSQL(Base):
    __tablename__ = "faqs"

    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False, index=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class ServiceSQL(Base):
    __tablename__ = "services"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    duration_minutes = Column(Integer, default=30)
    price = Column(Integer, default=0)
    buffer_minutes = Column(Integer, default=5)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class AppointmentSQL(Base):
    __tablename__ = "appointments"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    service_id = Column(String, ForeignKey("services.id"), nullable=False)
    customer_name = Column(String, nullable=False)
    customer_phone = Column(String, nullable=False)
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    status = Column(Enum("scheduled", "confirmed", "completed", "cancelled", "no_show", name="appointment_status"), default="scheduled")
    notes = Column(Text)
    # phone | web | dashboard - lets the owner see what the AI booked.
    source = Column(String, nullable=True)
    call_log_id = Column(String, ForeignKey("call_logs.id", name="fk_appointments_call_log_id"), nullable=True)
    # Set when the day-before reminder is claimed; claiming is an atomic
    # UPDATE so two replicas never both send one.
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    # The mirrored Google Calendar event, if the business syncs one.
    external_event_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    __table_args__ = (
        # Every availability check is "appointments for business X around time T".
        Index("ix_appointments_business_start", "business_id", "start_time"),
    )


class CallLogSQL(Base):
    __tablename__ = "call_logs"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    call_sid = Column(String, unique=True)
    start_time = Column(DateTime(timezone=True), default=func.now())
    end_time = Column(DateTime(timezone=True))
    transcript = Column(JSON)
    outcome = Column(Enum("booked", "failed", "escalated", "completed_faq", "transferred", name="call_outcome"))
    duration_seconds = Column(Integer)
    customer_phone = Column(String)
    agent_confirmed = Column(Boolean, default=False)
    escalation_requested = Column(Boolean, default=False)
    call_metadata = Column("metadata", JSON)
    # Set when the owner marks a promised callback as done. An escalated call
    # with this NULL is a caller still waiting to hear back.
    follow_up_done_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_call_logs_business_start", "business_id", "start_time"),
    )


class BusinessHoursSQL(Base):
    __tablename__ = "business_hours"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    day_of_week = Column(Integer)
    start_time = Column(String)
    end_time = Column(String)
    is_open = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class BusinessHolidaysSQL(Base):
    __tablename__ = "business_holidays"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    date = Column(Date)
    reason = Column(String)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class UserSQL(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    phone = Column(String)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=True)
    role = Column(String, default="owner")
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    # Tokens issued before this instant are refused, so a password change or
    # reset signs the account out everywhere (including a thief's session).
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class APIKeySQL(Base):
    __tablename__ = "api_keys"
    
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    key_prefix = Column(String(8), nullable=False)
    key_hash = Column(String, nullable=False)
    name = Column(String, nullable=False)
    permissions = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())


class PlanSQL(Base):
    __tablename__ = "plans"
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    price_monthly = Column(Integer, default=0)
    price_yearly = Column(Integer, default=0)
    currency = Column(String, default="usd")
    max_businesses = Column(Integer, default=1)
    max_phone_numbers = Column(Integer, default=1)
    max_minutes_monthly = Column(Integer, default=0)
    features = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=func.now())


class SubscriptionSQL(Base):
    __tablename__ = "subscriptions"
    
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    plan_id = Column(String, ForeignKey("plans.id"), nullable=False)
    status = Column(Enum("active", "trialing", "past_due", "canceled", "incomplete", name="subscription_status"), default="active")
    current_period_start = Column(DateTime(timezone=True), nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=False)
    trial_end = Column(DateTime(timezone=True), nullable=True)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    stripe_subscription_id = Column(String, unique=True, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class InvoiceSQL(Base):
    __tablename__ = "invoices"
    
    id = Column(String, primary_key=True)
    subscription_id = Column(String, ForeignKey("subscriptions.id"), nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String, default="usd")
    status = Column(Enum("pending", "paid", "failed", "refunded", name="invoice_status"), default="pending")
    stripe_invoice_id = Column(String, unique=True, nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    due_date = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())


class DatabaseManager:
    def __init__(self, database_url: str, pool_size: int = 10, max_overflow: int = 20):
        self.database_url = database_url
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.engine = None
        self.async_session = None
    
    async def initialize(self):
        """Initialize database connection."""
        kwargs = {"echo": False}
        if not self.database_url.startswith("sqlite"):
            kwargs.update(
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                # Drop connections the server (or a proxy like PgBouncer) has
                # closed behind our back instead of failing the next request.
                pool_pre_ping=True,
                pool_recycle=1800,
            )
        self.engine = create_async_engine(self.database_url, **kwargs)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
    
    async def close(self):
        """Close database connection."""
        if self.engine:
            await self.engine.dispose()
            self.engine = None
            self.async_session = None
    
    async def get_session(self) -> AsyncSession:
        """Get an async database session."""
        if self.async_session is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self.async_session()
    
    @classmethod
    async def create_tables(cls, engine):
        """Create all tables."""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    @classmethod
    async def drop_tables(cls, engine):
        """Drop all tables."""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    
    async def __aenter__(self):
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# One engine (and so one connection pool) per process. This used to be built
# fresh inside get_db(), which opened and tore down a whole pool on every
# request - fine on SQLite, connection exhaustion on Postgres.
_manager: Optional[DatabaseManager] = None


async def get_manager() -> DatabaseManager:
    global _manager
    if _manager is None or _manager.engine is None:
        from scripts.config import settings

        _manager = DatabaseManager(
            settings.DATABASE_URL,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
        )
        await _manager.initialize()
    return _manager


async def close_manager():
    global _manager
    if _manager is not None:
        await _manager.close()
        _manager = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for FastAPI to get database session."""
    manager = await get_manager()
    session = await manager.get_session()
    try:
        yield session
    finally:
        await session.close()
