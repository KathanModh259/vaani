import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from passlib.context import CryptContext

from db import UserSQL, APIKeySQL

logger = logging.getLogger(__name__)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(
    user_id: str,
    email: str,
    role: str,
    secret_key: str,
    algorithm: str = "HS256",
    expires_minutes: int = 60,
) -> str:
    from datetime import timezone
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, secret_key, algorithm=algorithm)


def create_refresh_token(
    user_id: str,
    secret_key: str,
    algorithm: str = "HS256",
    expires_days: int = 30,
) -> str:
    from datetime import timezone
    expire = datetime.now(timezone.utc) + timedelta(days=expires_days)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
    }
    return jwt.encode(payload, secret_key, algorithm=algorithm)


def decode_token(token: str, secret_key: str, algorithm: str = "HS256") -> Optional[dict]:
    try:
        payload = jwt.decode(token, secret_key, algorithms=[algorithm])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token: {e}")
        return None


def generate_api_key() -> tuple[str, str, str]:
    key = f"vaani_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    key_prefix = key[:12]
    return key, key_hash, key_prefix


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def authenticate_user(db_session, email: str, password: str) -> Optional[UserSQL]:
    from sqlalchemy import select
    result = await db_session.execute(select(UserSQL).where(UserSQL.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


async def get_user_by_id(db_session, user_id: str) -> Optional[UserSQL]:
    from sqlalchemy import select
    result = await db_session.execute(select(UserSQL).where(UserSQL.id == user_id))
    return result.scalar_one_or_none()


async def create_user(
    db_session,
    email: str,
    password: str,
    full_name: str,
    phone: Optional[str] = None,
    business_id: Optional[str] = None,
    role: str = "owner",
) -> UserSQL:
    user = UserSQL(
        id=__import__("uuid").uuid4().hex,
        email=email,
        password_hash=get_password_hash(password),
        full_name=full_name,
        phone=phone,
        business_id=business_id,
        role=role,
        is_active=True,
        is_verified=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def create_api_key(
    db_session,
    user_id: str,
    name: str,
    permissions: list = None,
) -> tuple[APIKeySQL, str]:
    key, key_hash, key_prefix = generate_api_key()
    api_key = APIKeySQL(
        id=__import__("uuid").uuid4().hex,
        user_id=user_id,
        key_prefix=key_prefix,
        key_hash=key_hash,
        name=name,
        permissions=permissions or [],
        is_active=True,
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return api_key, key


async def validate_api_key(db_session, key: str) -> Optional[APIKeySQL]:
    from sqlalchemy import select
    key_hash = hash_api_key(key)
    result = await db_session.execute(
        select(APIKeySQL).where(
            APIKeySQL.key_hash == key_hash,
            APIKeySQL.is_active,
        )
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        return None
    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
        return None
    api_key.last_used_at = datetime.now(timezone.utc)
    await db_session.commit()
    return api_key
