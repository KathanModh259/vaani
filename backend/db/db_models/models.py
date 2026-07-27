from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field
from datetime import datetime, date, timezone
import uuid

class BusinessStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class AppointmentStatus(str, Enum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class CallOutcome(str, Enum):
    BOOKED = "booked"
    FAILED = "failed"
    ESCALATED = "escalated"
    COMPLETED_FAQ = "completed_faq"
    TRANSFERRED = "transferred"


class UserRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class InvoiceStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class BaseDBModel(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Business(BaseDBModel):
    name: str
    phone: str
    email: str
    status: BusinessStatus = BusinessStatus.ACTIVE
    time_zone: str = "UTC"
    currency: str = "USD"


class Service(BaseDBModel):
    business_id: str
    name: str
    description: Optional[str] = None
    duration_minutes: int = 30
    price: float = 0.0
    buffer_minutes: int = 5


class Appointment(BaseDBModel):
    business_id: str
    service_id: str
    customer_name: str
    customer_phone: str
    start_time: datetime
    end_time: datetime
    status: AppointmentStatus = AppointmentStatus.SCHEDULED
    notes: Optional[str] = None


class CallLog(BaseDBModel):
    business_id: str
    call_sid: str
    start_time: datetime
    end_time: datetime
    transcript: List[Dict[str, Any]]
    outcome: CallOutcome
    duration_seconds: int
    customer_phone: str
    agent_confirmed: bool = False
    escalation_requested: bool = False
    metadata: Dict[str, Any] = {}


class BusinessHours(BaseDBModel):
    business_id: str
    day_of_week: int
    start_time: str
    end_time: str
    is_open: bool = True


class BusinessHolidays(BaseDBModel):
    business_id: str
    date: date
    reason: Optional[str] = None


class User(BaseDBModel):
    email: str
    password_hash: str
    full_name: str
    phone: Optional[str] = None
    business_id: Optional[str] = None
    role: UserRole = UserRole.OWNER
    is_active: bool = True
    is_verified: bool = False


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    phone: Optional[str] = None
    business_name: Optional[str] = None


class UserLogin(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    phone: Optional[str] = None
    business_id: Optional[str] = None
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class APIKeyCreate(BaseModel):
    name: str
    permissions: List[str] = []


class APIKeyResponse(BaseDBModel):
    user_id: str
    key_prefix: str
    name: str
    permissions: List[str]
    is_active: bool
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class APIKeyFullResponse(APIKeyResponse):
    key: str


class Plan(BaseDBModel):
    name: str
    description: Optional[str] = None
    price_monthly: int = 0
    price_yearly: int = 0
    currency: str = "usd"
    max_businesses: int = 1
    max_phone_numbers: int = 1
    max_minutes_monthly: int = 0
    features: List[str] = []
    is_active: bool = True


class Subscription(BaseDBModel):
    user_id: str
    plan_id: str
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    current_period_start: datetime
    current_period_end: datetime
    trial_end: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    stripe_subscription_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None


class Invoice(BaseDBModel):
    subscription_id: str
    amount: int
    currency: str = "usd"
    status: InvoiceStatus = InvoiceStatus.PENDING
    stripe_invoice_id: Optional[str] = None
    paid_at: Optional[datetime] = None
    due_date: datetime
