import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db import get_db, UserSQL, APIKeySQL, BusinessSQL
from db.db_models.models import (
    UserCreate,
    UserLogin,
    UserResponse,
    TokenResponse,
    APIKeyCreate,
    APIKeyResponse,
    APIKeyFullResponse,
)
from scripts.config import settings
from services.auth import (
    authenticate_user,
    create_user,
    create_access_token,
    decode_token,
    create_api_key,
    get_user_by_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


async def get_current_user(
    request: Request,
    db_session: AsyncSession = Depends(get_db),
) -> UserSQL:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid token")
    token = auth_header[7:]
    payload = decode_token(token, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    user = await get_user_by_id(db_session, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


async def get_current_user_optional(
    request: Request,
    db_session: AsyncSession = Depends(get_db),
) -> Optional[UserSQL]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        api_key = request.headers.get("X-API-Key", "")
        if api_key:
            from services.auth import validate_api_key
            api_key_obj = await validate_api_key(db_session, api_key)
            if api_key_obj:
                user = await get_user_by_id(db_session, api_key_obj.user_id)
                return user
        return None
    token = auth_header[7:]
    payload = decode_token(token, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
    if payload is None or payload.get("type") != "access":
        return None
    user = await get_user_by_id(db_session, payload["sub"])
    return user if user and user.is_active else None


@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: UserCreate, db_session: AsyncSession = Depends(get_db)):
    result = await db_session.execute(select(UserSQL).where(UserSQL.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    business_id = None
    if body.business_name:
        business = BusinessSQL(
            id=__import__("uuid").uuid4().hex,
            name=body.business_name,
            phone=body.phone or "",
            email=body.email,
        )
        db_session.add(business)
        await db_session.flush()
        business_id = business.id

    user = await create_user(
        db_session,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        phone=body.phone,
        business_id=business_id,
    )

    access_token = create_access_token(
        user.id, user.email, user.role,
        settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM,
        settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )

    return TokenResponse(
        access_token=access_token,
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            phone=user.phone,
            business_id=user.business_id,
            role=user.role,
            is_active=user.is_active,
            is_verified=user.is_verified,
            created_at=user.created_at,
        ),
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: UserLogin, db_session: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db_session, body.email, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    access_token = create_access_token(
        user.id, user.email, user.role,
        settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM,
        settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )

    return TokenResponse(
        access_token=access_token,
        user=UserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            phone=user.phone,
            business_id=user.business_id,
            role=user.role,
            is_active=user.is_active,
            is_verified=user.is_verified,
            created_at=user.created_at,
        ),
    )


@router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: UserSQL = Depends(get_current_user)):
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        phone=current_user.phone,
        business_id=current_user.business_id,
        role=current_user.role,
        is_active=current_user.is_active,
        is_verified=current_user.is_verified,
        created_at=current_user.created_at,
    )


@router.post("/auth/api-keys", response_model=APIKeyFullResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key_route(
    body: APIKeyCreate,
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    api_key, raw_key = await create_api_key(
        db_session, current_user.id, body.name, body.permissions,
    )
    return APIKeyFullResponse(
        id=api_key.id,
        user_id=api_key.user_id,
        key_prefix=api_key.key_prefix,
        name=api_key.name,
        permissions=api_key.permissions or [],
        is_active=api_key.is_active,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        created_at=api_key.created_at,
        key=raw_key,
    )


@router.get("/auth/api-keys", response_model=list[APIKeyResponse])
async def list_api_keys(
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    result = await db_session.execute(
        select(APIKeySQL).where(APIKeySQL.user_id == current_user.id)
    )
    keys = result.scalars().all()
    return [
        APIKeyResponse(
            id=k.id,
            user_id=k.user_id,
            key_prefix=k.key_prefix,
            name=k.name,
            permissions=k.permissions or [],
            is_active=k.is_active,
            last_used_at=k.last_used_at,
            expires_at=k.expires_at,
            created_at=k.created_at,
        )
        for k in keys
    ]


@router.delete("/auth/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: str,
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    result = await db_session.execute(
        select(APIKeySQL).where(
            APIKeySQL.id == key_id,
            APIKeySQL.user_id == current_user.id,
        )
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    await db_session.delete(api_key)
    await db_session.commit()
