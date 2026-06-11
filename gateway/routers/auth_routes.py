"""
pharmabridge/gateway/routers/auth_routes.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
All authentication route handlers.

Endpoints
─────────
POST /auth/register     – create account (unverified)
POST /auth/send-otp     – generate + send OTP SMS
POST /auth/verify-otp   – verify OTP → mark account verified
POST /auth/login        – password login → access + refresh tokens
POST /auth/refresh      – exchange refresh token for new access token
POST /auth/logout       – revoke refresh token

All auth routes are rate-limited to 5 req/min per IP (auth group)
to prevent credential stuffing and OTP brute-force.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure parent dirs on path so imports resolve in both Docker and local dev
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth import (
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    SendOTPRequest,
    TokenPayload,
    TokenResponse,
    VerifyOTPRequest,
    create_access_token,
    create_refresh_token,
    generate_and_store_otp,
    hash_password,
    require_auth,
    revoke_refresh_token,
    validate_refresh_token,
    verify_otp,
    verify_password,
)
from db.repository import UserRepository
from db.session import get_db

logger = logging.getLogger("pharmabridge.auth.routes")

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ─────────────────────────────────────────────────────────
# Helper – get Redis from the app state (avoids circular import)
# ─────────────────────────────────────────────────────────

async def _get_redis():
    """Import get_redis lazily to avoid circular imports with main.py."""
    from main import get_redis
    return await get_redis()


# ─────────────────────────────────────────────────────────
# POST /auth/register
# ─────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model = RegisterResponse,
    status_code    = status.HTTP_201_CREATED,
    summary        = "Register a new patient account",
)
async def register(
    req: RegisterRequest,
    db : AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """
    Create a new user.  Account is unverified until OTP is confirmed.

    Returns the user_id immediately so the client can track the
    verification state.  Does NOT issue tokens (login required after verify).
    """
    repo = UserRepository(db)

    # Check for duplicate phone number
    existing = await repo.get_by_phone(req.phone_number)
    if existing:
        if not existing.deleted_at:
            raise HTTPException(
                status_code = status.HTTP_409_CONFLICT,
                detail      = "An account with this phone number already exists.",
            )

    user = await repo.create(
        phone_number = req.phone_number,
        full_name    = req.full_name,
        pincode      = req.pincode,
        password_hash= None,   # set below after repo.create
    )

    # Update password hash after creation (repo.create doesn't take it directly
    # to avoid hash being logged; we set it in a separate update)
    user.password_hash = hash_password(req.password)
    await db.flush()

    logger.info("User registered: %s phone=%s", user.id[:8], req.phone_number)

    return RegisterResponse(
        user_id      = user.id,
        phone_number = user.phone_number,
    )


# ─────────────────────────────────────────────────────────
# POST /auth/send-otp
# ─────────────────────────────────────────────────────────

@router.post(
    "/send-otp",
    response_model = MessageResponse,
    summary        = "Send a 6-digit OTP to the registered phone number",
)
async def send_otp(req: SendOTPRequest) -> MessageResponse:
    """
    Generate and send an OTP.

    The OTP is stored in Redis with a 5-minute TTL.
    In dev/homelab mode the OTP is only logged (no SMS sent).
    Set USE_SMS_GATEWAY=true and configure MSG91_API_KEY to enable live SMS.
    """
    redis = await _get_redis()
    otp   = await generate_and_store_otp(redis, req.phone_number)

    # In production this log line is removed and OTP goes only to SMS
    logger.info("[OTP][DEV] %s → %s", req.phone_number, otp)

    return MessageResponse(message="OTP sent to your registered mobile number.")


# ─────────────────────────────────────────────────────────
# POST /auth/verify-otp
# ─────────────────────────────────────────────────────────

@router.post(
    "/verify-otp",
    response_model = MessageResponse,
    summary        = "Verify the OTP and activate the account",
)
async def verify_otp_route(
    req: VerifyOTPRequest,
    db : AsyncSession = Depends(get_db),
) -> MessageResponse:
    redis = await _get_redis()
    repo  = UserRepository(db)

    # Verify OTP (raises 429 on brute-force, 400 on expired)
    correct = await verify_otp(redis, req.phone_number, req.otp)
    if not correct:
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = "Incorrect OTP. Please check and try again.",
        )

    # Mark user as verified
    user = await repo.get_by_phone(req.phone_number)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    await repo.mark_verified(user.id)
    logger.info("User %s verified phone %s", user.id[:8], req.phone_number)

    return MessageResponse(message="Phone number verified. You can now log in.")


# ─────────────────────────────────────────────────────────
# POST /auth/login
# ─────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model = TokenResponse,
    summary        = "Log in with phone number and password",
)
async def login(
    req: LoginRequest,
    db : AsyncSession = Depends(get_db),
) -> TokenResponse:
    redis = await _get_redis()
    repo  = UserRepository(db)

    user = await repo.get_by_phone(req.phone_number)

    # Generic error message prevents phone enumeration
    INVALID_CREDENTIALS = HTTPException(
        status_code = status.HTTP_401_UNAUTHORIZED,
        detail      = "Invalid phone number or password.",
        headers     = {"WWW-Authenticate": "Bearer"},
    )

    if not user or not user.is_active:
        raise INVALID_CREDENTIALS

    if not user.password_hash or not verify_password(req.password, user.password_hash):
        raise INVALID_CREDENTIALS

    # Issue tokens
    access_token  = create_access_token(
        user_id     = user.id,
        phone       = user.phone_number,
        role        = user.role,
        is_verified = user.is_verified,
    )
    refresh_token = await create_refresh_token(redis, user.id)

    logger.info("User %s logged in", user.id[:8])

    return TokenResponse(
        access_token  = access_token,
        refresh_token = refresh_token,
    )


# ─────────────────────────────────────────────────────────
# POST /auth/refresh
# ─────────────────────────────────────────────────────────

@router.post(
    "/refresh",
    response_model = TokenResponse,
    summary        = "Exchange a refresh token for a new access token",
)
async def refresh_token(
    req: RefreshRequest,
    db : AsyncSession = Depends(get_db),
) -> TokenResponse:
    redis   = await _get_redis()
    repo    = UserRepository(db)

    user_id = await validate_refresh_token(redis, req.refresh_token)
    user    = await repo.get_by_id(user_id)

    if not user or not user.is_active:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Account not found or deactivated.",
        )

    # Issue new access token; rotate refresh token
    access_token      = create_access_token(
        user_id     = user.id,
        phone       = user.phone_number,
        role        = user.role,
        is_verified = user.is_verified,
    )
    new_refresh_token = await create_refresh_token(redis, user.id)

    return TokenResponse(
        access_token  = access_token,
        refresh_token = new_refresh_token,
    )


# ─────────────────────────────────────────────────────────
# POST /auth/logout
# ─────────────────────────────────────────────────────────

@router.post(
    "/logout",
    response_model = MessageResponse,
    summary        = "Revoke refresh token (log out)",
)
async def logout(
    req  : RefreshRequest,
    token: TokenPayload = Depends(require_auth),
) -> MessageResponse:
    redis = await _get_redis()
    await revoke_refresh_token(redis, req.refresh_token)
    logger.info("User %s logged out", token.sub[:8])
    return MessageResponse(message="Logged out successfully.")


# ─────────────────────────────────────────────────────────
# GET /auth/me  – current user profile
# ─────────────────────────────────────────────────────────

@router.get(
    "/me",
    summary = "Get the current user's profile",
)
async def get_me(
    token: TokenPayload = Depends(require_auth),
    db   : AsyncSession = Depends(get_db),
) -> dict:
    repo = UserRepository(db)
    user = await repo.get_by_id(token.sub)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return {
        "user_id"     : user.id,
        "phone_number": user.phone_number,
        "full_name"   : user.full_name,
        "is_verified" : user.is_verified,
        "pincode"     : user.pincode,
        "created_at"  : user.created_at.isoformat(),
    }


# ─────────────────────────────────────────────────────────
# POST /auth/bootstrap  — one-time admin promotion
# ─────────────────────────────────────────────────────────

from auth import BootstrapRequest, run_bootstrap

@router.post(
    "/bootstrap",
    summary = (
        "One-time admin bootstrap. Promotes BOOTSTRAP_ADMIN_PHONE to ADMIN. "
        "Disabled after first use. Remove env var immediately after."
    ),
    tags = ["Authentication"],
)
async def bootstrap_admin(
    req: BootstrapRequest,
    db : AsyncSession = Depends(get_db),
) -> dict:
    """
    Promotes the specified phone number to ADMIN role.

    Guards:
    - BOOTSTRAP_ADMIN_PHONE env var must be set and must match req.phone_number
    - Can only be called once (Redis flag pb:bootstrap:used prevents replay)

    After success:
    1. Remove BOOTSTRAP_ADMIN_PHONE from your K8s Secret
    2. kubectl rollout restart deployment/pharmabridge-gateway

    This endpoint then returns 400 on all subsequent calls.
    """
    redis = await _get_redis()
    return await run_bootstrap(redis, req.phone_number, db)
