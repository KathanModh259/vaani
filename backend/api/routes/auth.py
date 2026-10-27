"""Accounts: signup, sessions, password reset, email verification, API keys.

Sessions are a short access token (JWT_ACCESS_TOKEN_EXPIRE_MINUTES) plus a
long refresh token. Both are stateless JWTs; revocation works through
users.password_changed_at - any token issued before it is refused - so a
password change or reset signs the account out everywhere.

Browsers get both as httpOnly cookies, so script injected into a page cannot
read a session. Cookies are SameSite=Lax (not sent on cross-site POSTs) and
cookie-authenticated writes must also carry X-Requested-With, which a
cross-site form cannot set - two independent CSRF defences. API clients can
keep sending "Authorization: Bearer", which needs neither.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db import APIKeySQL, BusinessSQL, UserSQL, get_db
from db.db_models.models import (
    APIKeyCreate,
    APIKeyFullResponse,
    APIKeyResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    RefreshRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
)
from scripts.config import settings
from services.auth import (
    authenticate_user,
    create_access_token,
    create_api_key,
    create_purpose_token,
    create_refresh_token,
    create_user,
    decode_token,
    get_password_hash,
    get_user_by_id,
    password_fingerprint,
    token_is_current,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

ACCESS_COOKIE = "vaani_at"
REFRESH_COOKIE = "vaani_rt"
HINT_COOKIE = "vaani_signed_in"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

RESET_TOKEN_MINUTES = 30
VERIFY_TOKEN_MINUTES = 60 * 48


# ---------------------------------------------------------------- helpers

def _user_out(user: UserSQL) -> UserResponse:
    return UserResponse(
        id=user.id, email=user.email, full_name=user.full_name, phone=user.phone,
        business_id=user.business_id, role=user.role, is_active=user.is_active,
        is_verified=user.is_verified, created_at=user.created_at,
    )


def _secure_cookies() -> bool:
    return (settings.PUBLIC_BASE_URL.startswith("https://")
            or settings.ENVIRONMENT.lower() in ("production", "prod"))


def _tokens(user: UserSQL, status_code: int = 200) -> JSONResponse:
    access = create_access_token(
        user.id, user.email, user.role, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM,
        settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )
    refresh = create_refresh_token(user.id, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM,
                                   settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    body = TokenResponse(access_token=access, refresh_token=refresh, user=_user_out(user))
    resp = JSONResponse(content=jsonable_encoder(body), status_code=status_code)
    secure = _secure_cookies()
    resp.set_cookie(ACCESS_COOKIE, access, max_age=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                    httponly=True, secure=secure, samesite="lax", path="/")
    # Only the auth endpoints ever see the long-lived token.
    resp.set_cookie(REFRESH_COOKIE, refresh, max_age=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 86400,
                    httponly=True, secure=secure, samesite="lax", path="/api/auth")
    # Readable by page script, carries no secret: it only tells pages a
    # session might exist, so signed-out visitors skip the /me round trip.
    resp.set_cookie(HINT_COOKIE, "1", max_age=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 86400,
                    httponly=False, secure=secure, samesite="lax", path="/")
    return resp


def session_token(request: Request) -> Optional[str]:
    """The access token from the Authorization header, else the cookie."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    return request.cookies.get(ACCESS_COOKIE)


async def _user_from_token(db_session, token: str, expected_type: str) -> Optional[UserSQL]:
    payload = decode_token(token, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
    if payload is None or payload.get("type") != expected_type:
        return None
    user = await get_user_by_id(db_session, payload.get("sub", ""))
    if not user or not user.is_active or not token_is_current(payload, user):
        return None
    return user


def _send_verification(user: UserSQL) -> None:
    from services.email import public_url, queue_email

    token = create_purpose_token(user, "verify", settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM,
                                 VERIFY_TOKEN_MINUTES)
    queue_email(user.email, "Confirm your email for Vaani", (
        f"Hi {user.full_name},\n\n"
        f"Confirm this is your email address so we can send you booking and callback alerts:\n\n"
        f"{public_url('/api/auth/verify-email?token=' + token)}\n\n"
        f"The link works for 48 hours. If you did not sign up for Vaani, ignore this email.\n"
    ))


# ------------------------------------------------------------ dependencies

async def get_current_user(
    request: Request,
    db_session: AsyncSession = Depends(get_db),
) -> UserSQL:
    bearer = request.headers.get("Authorization", "").startswith("Bearer ")
    token = session_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid token")
    if not bearer and request.method not in SAFE_METHODS and not request.headers.get("x-requested-with"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing X-Requested-With header")
    user = await _user_from_token(db_session, token, "access")
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    return user


async def get_current_user_optional(
    request: Request,
    db_session: AsyncSession = Depends(get_db),
) -> Optional[UserSQL]:
    api_key = request.headers.get("X-API-Key", "")
    if api_key:
        from services.auth import validate_api_key
        api_key_obj = await validate_api_key(db_session, api_key)
        return await get_user_by_id(db_session, api_key_obj.user_id) if api_key_obj else None
    token = session_token(request)
    return await _user_from_token(db_session, token, "access") if token else None


# ------------------------------------------------------------ signup/login

@router.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: UserCreate, db_session: AsyncSession = Depends(get_db)):
    result = await db_session.execute(select(UserSQL).where(UserSQL.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    # Every account gets a business: without one the dashboard and settings
    # have nothing to show, and self-serve signup dead-ends at "no business
    # linked". Default hours make the receptionist usable immediately.
    from services.business_profile import seed_default_hours

    business = BusinessSQL(
        id=uuid.uuid4().hex,
        name=(body.business_name or "").strip() or f"{body.full_name}'s business",
        phone=body.phone or "",
        email=body.email,
    )
    db_session.add(business)
    await db_session.flush()
    seed_default_hours(db_session, business.id)

    try:
        user = await create_user(
            db_session, email=body.email, password=body.password, full_name=body.full_name,
            phone=body.phone, business_id=business.id,
        )
    except IntegrityError:
        # Lost a race with a concurrent signup for the same email.
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    _send_verification(user)
    return _tokens(user, status_code=status.HTTP_201_CREATED)


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: UserLogin, db_session: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db_session, body.email, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    return _tokens(user)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(request: Request, body: Optional[RefreshRequest] = None,
                  db_session: AsyncSession = Depends(get_db)):
    token = (body.refresh_token if body else None) or request.cookies.get(REFRESH_COOKIE)
    user = await _user_from_token(db_session, token, "refresh") if token else None
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired. Please sign in again.")
    return _tokens(user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout():
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    resp.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    resp.delete_cookie(HINT_COOKIE, path="/")
    return resp


@router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: UserSQL = Depends(get_current_user)):
    return _user_out(current_user)


# --------------------------------------------------------------- passwords

@router.post("/auth/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: ForgotPasswordRequest, db_session: AsyncSession = Depends(get_db)):
    """Always 202, whether or not the email exists - the response must not
    tell an attacker which addresses have accounts."""
    from services.email import public_url, queue_email

    user = (await db_session.execute(select(UserSQL).where(UserSQL.email == body.email))).scalar_one_or_none()
    if user and user.is_active:
        token = create_purpose_token(user, "reset", settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM,
                                     RESET_TOKEN_MINUTES)
        queue_email(user.email, "Reset your Vaani password", (
            f"Hi {user.full_name},\n\n"
            f"Someone asked to reset the password for this account. If it was you, open:\n\n"
            f"{public_url('/reset-password?token=' + token)}\n\n"
            f"The link works once, for {RESET_TOKEN_MINUTES} minutes. If you did not ask for this, "
            f"ignore this email - your password has not changed.\n"
        ))
    return {"ok": True, "detail": "If that email has an account, a reset link is on its way."}


@router.post("/auth/reset-password", response_model=TokenResponse)
async def reset_password(body: ResetPasswordRequest, db_session: AsyncSession = Depends(get_db)):
    invalid = HTTPException(status_code=400, detail="This reset link is invalid or has expired. Request a new one.")
    payload = decode_token(body.token, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
    if payload is None or payload.get("type") != "reset":
        raise invalid
    user = await get_user_by_id(db_session, payload.get("sub", ""))
    # Fingerprint mismatch = the password changed since the link was issued,
    # so the link has already been used (or superseded).
    if not user or not user.is_active or payload.get("fp") != password_fingerprint(user.password_hash):
        raise invalid

    user.password_hash = get_password_hash(body.password)
    user.password_changed_at = datetime.now(timezone.utc)
    user.is_verified = True  # they just proved they read this inbox
    await db_session.commit()
    return _tokens(user)


@router.post("/auth/change-password", response_model=TokenResponse)
async def change_password(body: ChangePasswordRequest,
                          current_user: UserSQL = Depends(get_current_user),
                          db_session: AsyncSession = Depends(get_db)):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current_user.password_hash = get_password_hash(body.new_password)
    current_user.password_changed_at = datetime.now(timezone.utc)
    await db_session.commit()
    # Every other session is now invalid; hand this one fresh tokens.
    return _tokens(current_user)


# ----------------------------------------------------------- verification

@router.post("/auth/send-verification", status_code=status.HTTP_202_ACCEPTED)
async def send_verification(current_user: UserSQL = Depends(get_current_user)):
    if current_user.is_verified:
        return {"ok": True, "detail": "Already verified"}
    _send_verification(current_user)
    return {"ok": True, "detail": "Verification email sent"}


@router.get("/auth/verify-email", include_in_schema=False)
async def verify_email(token: str = Query(...), db_session: AsyncSession = Depends(get_db)):
    """Target of the link in the verification email; lands on the dashboard."""
    payload = decode_token(token, settings.JWT_SECRET_KEY, settings.JWT_ALGORITHM)
    user = None
    if payload and payload.get("type") == "verify":
        user = await get_user_by_id(db_session, payload.get("sub", ""))
    # The link binds the address it was sent to; a changed email needs a new link.
    if not user or user.email != payload.get("email"):
        return RedirectResponse("/dashboard?verified=0", status_code=303)
    if not user.is_verified:
        user.is_verified = True
        await db_session.commit()
    return RedirectResponse("/dashboard?verified=1", status_code=303)


# ---------------------------------------------------------------- API keys

@router.post("/auth/api-keys", response_model=APIKeyFullResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key_route(
    body: APIKeyCreate,
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    api_key, raw_key = await create_api_key(db_session, current_user.id, body.name, body.permissions)
    return APIKeyFullResponse(
        id=api_key.id, user_id=api_key.user_id, key_prefix=api_key.key_prefix, name=api_key.name,
        permissions=api_key.permissions or [], is_active=api_key.is_active,
        last_used_at=api_key.last_used_at, expires_at=api_key.expires_at,
        created_at=api_key.created_at, key=raw_key,
    )


@router.get("/auth/api-keys", response_model=list[APIKeyResponse])
async def list_api_keys(
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    keys = (await db_session.execute(
        select(APIKeySQL).where(APIKeySQL.user_id == current_user.id)
    )).scalars().all()
    return [
        APIKeyResponse(
            id=k.id, user_id=k.user_id, key_prefix=k.key_prefix, name=k.name,
            permissions=k.permissions or [], is_active=k.is_active,
            last_used_at=k.last_used_at, expires_at=k.expires_at, created_at=k.created_at,
        )
        for k in keys
    ]


@router.delete("/auth/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: str,
    current_user: UserSQL = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db),
):
    api_key = (await db_session.execute(
        select(APIKeySQL).where(APIKeySQL.id == key_id, APIKeySQL.user_id == current_user.id)
    )).scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    await db_session.delete(api_key)
    await db_session.commit()
