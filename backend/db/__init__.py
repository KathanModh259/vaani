from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, DateTime, Date, Enum, Integer, Boolean, Text, JSON, ForeignKey, func

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
    time_zone = Column(String, default="UTC")
    currency = Column(String, default="USD")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class ServiceSQL(Base):
    __tablename__ = "services"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    duration_minutes = Column(Integer, default=30)
    price = Column(Integer, default=0)
    buffer_minutes = Column(Integer, default=5)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class AppointmentSQL(Base):
    __tablename__ = "appointments"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    service_id = Column(String, ForeignKey("services.id"), nullable=False)
    customer_name = Column(String, nullable=False)
    customer_phone = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(Enum("scheduled", "confirmed", "completed", "cancelled", "no_show", name="appointment_status"), default="scheduled")
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class CallLogSQL(Base):
    __tablename__ = "call_logs"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    call_sid = Column(String, unique=True)
    start_time = Column(DateTime, default=func.now())
    end_time = Column(DateTime)
    transcript = Column(JSON)
    outcome = Column(Enum("booked", "failed", "escalated", "completed_faq", "transferred", name="call_outcome"))
    duration_seconds = Column(Integer)
    customer_phone = Column(String)
    agent_confirmed = Column(Boolean, default=False)
    escalation_requested = Column(Boolean, default=False)
    call_metadata = Column("metadata", JSON)


class BusinessHoursSQL(Base):
    __tablename__ = "business_hours"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    day_of_week = Column(Integer)
    start_time = Column(String)
    end_time = Column(String)
    is_open = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class BusinessHolidaysSQL(Base):
    __tablename__ = "business_holidays"
    
    id = Column(String, primary_key=True)
    business_id = Column(String, ForeignKey("businesses.id"), nullable=False)
    date = Column(Date)
    reason = Column(String)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


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
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class APIKeySQL(Base):
    __tablename__ = "api_keys"
    
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    key_prefix = Column(String(8), nullable=False)
    key_hash = Column(String, nullable=False)
    name = Column(String, nullable=False)
    permissions = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())


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
    created_at = Column(DateTime, default=func.now())


class SubscriptionSQL(Base):
    __tablename__ = "subscriptions"
    
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    plan_id = Column(String, ForeignKey("plans.id"), nullable=False)
    status = Column(Enum("active", "trialing", "past_due", "canceled", "incomplete", name="subscription_status"), default="active")
    current_period_start = Column(DateTime, nullable=False)
    current_period_end = Column(DateTime, nullable=False)
    trial_end = Column(DateTime, nullable=True)
    canceled_at = Column(DateTime, nullable=True)
    stripe_subscription_id = Column(String, unique=True, nullable=True)
    stripe_customer_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class InvoiceSQL(Base):
    __tablename__ = "invoices"
    
    id = Column(String, primary_key=True)
    subscription_id = Column(String, ForeignKey("subscriptions.id"), nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String, default="usd")
    status = Column(Enum("pending", "paid", "failed", "refunded", name="invoice_status"), default="pending")
    stripe_invoice_id = Column(String, unique=True, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    due_date = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())


class DatabaseManager:
    def __init__(self, database_url: str, pool_size: int = 10, max_overflow: int = 20):
        self.database_url = database_url
        self.pool_size = pool_size
        self.max_overflow = max_overflow
        self.engine = None
        self.async_session = None
    
    async def initialize(self):
        """Initialize database connection."""
        self.engine = create_async_engine(
            self.database_url,
            pool_size=self.pool_size,
            max_overflow=self.max_overflow,
            echo=False,
        )
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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for FastAPI to get database session."""
    from scripts.config import settings
    
    db_manager = DatabaseManager(settings.DATABASE_URL)
    await db_manager.initialize()
    try:
        session = await db_manager.get_session()
        yield session
    finally:
        await session.close()
        await db_manager.close()
