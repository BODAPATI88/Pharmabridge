"""
pharmabridge/gateway/auth.py  ·  v4.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Authentication and RBAC for PharmaBridge.

Auth flow
─────────
1. POST /auth/register  → phone + password → User(role=PATIENT, unverified)
2. POST /auth/send-otp  → phone → OTP in Redis (TTL 5 min)
3. POST /auth/verify-otp→ phone + otp → is_verified=True
4. POST /auth/login     → phone + password → access_token + refresh_token
5. POST /auth/refresh   → refresh_token → new access_token
6. POST /auth/logout    → revokes refresh_token

Bootstrap
─────────
POST /auth/bootstrap  → one-time endpoint, disabled after first use.
Promotes a phone number to ADMIN using BOOTSTRAP_ADMIN_PHONE env var.
Alternatively, migration 0004 auto-promotes on first run if the env var
is set in the Alembic Job's environment.

Role hierarchy
──────────────
  PATIENT  – base role; can search, upload prescriptions, place orders
  OPERATOR – can review prescriptions in the operator queue
  ADMIN    – all of the above + user management + analytics

FastAPI dependencies
────────────────────
  require_auth           – any valid token (any role)
  require_verified_auth  – valid token + is_verified=True
  require_operator       – role in (OPERATOR, ADMIN)
  require_admin          – role == ADMIN

Token design
────────────
Access token  – JWT HS256, 15-min TTL, stateless.
                Contains: sub, phone, role, is_verified, jti.
Refresh token – opaque 64-char hex, stored in Redis pb:refresh:{token}.
                One active refresh token per user (re-login rotates it).
"""

from __future__ import annotations

import logging
import os
import random
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("pharmabridge.auth")

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────

JWT_SECRET        = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_K8S_SECRET")
JWT_ALGORITHM     = "HS256"
ACCESS_TOKEN_TTL  = int(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "15"))
REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL_DAYS",   "7"))
OTP_TTL_SECONDS   = int(os.getenv("OTP_TTL_SECONDS",          "300"))
OTP_MAX_ATTEMPTS  = int(os.getenv("OTP_MAX_ATTEMPTS",         "5"))

# Bootstrap: phone number of the first admin.
# Read at runtime by the bootstrap endpoint; also read by migration 0004.
BOOTSTRAP_ADMIN_PHONE = os.getenv("BOOTSTRAP_ADMIN_PHONE", "").strip()

# Bootstrap can only be used once.  After first use, the used flag is set
# in Redis so the endpoint becomes a no-op even if called again.
_BOOTSTRAP_USED_KEY = "pb:bootstrap:used"


# ─────────────────────────────────────────────────────────
# Password hashing
# ─────────────────────────────────────────────────────────

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


# ─────────────────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    phone_number: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")
    password    : str = Field(..., min_length=8, max_length=72)
    full_name   : Optional[str] = Field(None, max_length=200)
    pincode     : Optional[str] = Field(None, pattern=r"^\d{6}$")

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v) or not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter and one number.")
        return v


class RegisterResponse(BaseModel):
    user_id     : str
    phone_number: str
    message     : str = "Registration successful. Please verify your phone number."


class SendOTPRequest(BaseModel):
    phone_number: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")


class VerifyOTPRequest(BaseModel):
    phone_number: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")
    otp         : str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class LoginRequest(BaseModel):
    phone_number: str = Field(..., pattern=r"^\+91[6-9]\d{9}$")
    password    : str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token : str
    refresh_token: str
    token_type   : str = "bearer"
    expires_in   : int = ACCESS_TOKEN_TTL * 60


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=10)


class TokenPayload(BaseModel):
    """Decoded JWT payload — returned by auth dependencies."""
    sub         : str    # user_id
    phone       : str
    role        : str    # PATIENT | OPERATOR | ADMIN
    jti         : str
    exp         : int
    is_verified : bool = False


class MessageResponse(BaseModel):
    message: str


class BootstrapRequest(BaseModel):
    """One-time admin bootstrap.  Disabled after first successful use."""
    phone_number: str = Field(..., pattern=r"^\+91[6-9]\d{9}$",
                              description="Phone number to promote to ADMIN role.")


# ─────────────────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────────────────

def create_access_token(user_id: str, phone: str, role: str, is_verified: bool) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub"        : user_id,
        "phone"      : phone,
        "role"       : role,
        "is_verified": is_verified,
        "jti"        : secrets.token_hex(16),
        "iat"        : int(now.timestamp()),
        "exp"        : int((now + timedelta(minutes=ACCESS_TOKEN_TTL)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenPayload:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return TokenPayload(**payload)
    except JWTError as exc:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = f"Invalid or expired token: {exc}",
            headers     = {"WWW-Authenticate": "Bearer"},
        )


# ─────────────────────────────────────────────────────────
# Refresh token helpers
# ─────────────────────────────────────────────────────────

def _refresh_key(token: str) -> str:      return f"pb:refresh:{token}"
def _user_refresh_key(uid: str) -> str:   return f"pb:refresh:user:{uid}"
def _otp_key(phone: str) -> str:          return f"pb:otp:{phone}"
def _otp_attempts_key(phone: str) -> str: return f"pb:otp:attempts:{phone}"


async def create_refresh_token(redis: aioredis.Redis, user_id: str) -> str:
    token = secrets.token_hex(32)
    ttl   = REFRESH_TOKEN_TTL * 86400
    old   = await redis.get(_user_refresh_key(user_id))
    if old:
        await redis.delete(_refresh_key(old))
    pipe = redis.pipeline()
    await pipe.set(_refresh_key(token),       user_id, ex=ttl)
    await pipe.set(_user_refresh_key(user_id), token,  ex=ttl)
    await pipe.execute()
    return token


async def validate_refresh_token(redis: aioredis.Redis, token: str) -> str:
    user_id = await redis.get(_refresh_key(token))
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Refresh token is invalid or has expired.")
    return user_id


async def revoke_refresh_token(redis: aioredis.Redis, token: str) -> None:
    user_id = await redis.get(_refresh_key(token))
    if user_id:
        await redis.delete(_refresh_key(token))
        await redis.delete(_user_refresh_key(user_id))


# ─────────────────────────────────────────────────────────
# OTP helpers
# ─────────────────────────────────────────────────────────

async def generate_and_store_otp(redis: aioredis.Redis, phone: str) -> str:
    otp = "".join(random.choices(string.digits, k=6))
    await redis.set(_otp_key(phone), otp, ex=OTP_TTL_SECONDS)
    await redis.delete(_otp_attempts_key(phone))
    # TODO: replace log line with MSG91 / Twilio SMS call in production
    logger.info("[OTP][DEV] %s → %s", phone, otp)
    return otp


async def verify_otp(redis: aioredis.Redis, phone: str, submitted: str) -> bool:
    attempts = int(await redis.get(_otp_attempts_key(phone)) or 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many OTP attempts. Request a new OTP.")
    stored = await redis.get(_otp_key(phone))
    if not stored:
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")
    if stored != submitted:
        await redis.incr(_otp_attempts_key(phone))
        await redis.expire(_otp_attempts_key(phone), OTP_TTL_SECONDS)
        return False
    await redis.delete(_otp_key(phone))
    await redis.delete(_otp_attempts_key(phone))
    return True


# ─────────────────────────────────────────────────────────
# FastAPI dependencies  — the four RBAC gates
# ─────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=True)
_optional_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> TokenPayload:
    """Any valid token — no role restriction."""
    return decode_access_token(credentials.credentials)


async def require_verified_auth(
    token: TokenPayload = Depends(require_auth),
) -> TokenPayload:
    """Valid token + phone verified."""
    if not token.is_verified:
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Phone number not verified. Complete OTP verification first.",
        )
    return token


async def require_operator(
    token: TokenPayload = Depends(require_verified_auth),
) -> TokenPayload:
    """
    OPERATOR or ADMIN role required.
    Use on all prescription review endpoints.
    """
    if token.role not in ("OPERATOR", "ADMIN"):
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Operator or Admin role required.",
        )
    return token


async def require_admin(
    token: TokenPayload = Depends(require_verified_auth),
) -> TokenPayload:
    """
    ADMIN role required.
    Use on user management and system configuration endpoints.
    """
    if token.role != "ADMIN":
        raise HTTPException(
            status_code = status.HTTP_403_FORBIDDEN,
            detail      = "Admin role required.",
        )
    return token


async def optional_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
) -> Optional[TokenPayload]:
    """Decode token if present; return None for anonymous callers."""
    if credentials is None:
        return None
    try:
        return decode_access_token(credentials.credentials)
    except HTTPException:
        return None


# ─────────────────────────────────────────────────────────
# Bootstrap helper
# ─────────────────────────────────────────────────────────

async def run_bootstrap(redis: aioredis.Redis, phone_number: str, db) -> dict:
    """
    One-time promotion of a phone number to ADMIN role.

    Guards:
    1. BOOTSTRAP_ADMIN_PHONE env var must match the requested phone.
    2. The Redis key pb:bootstrap:used must not exist (one-time use).
    3. A user with that phone must exist in the DB.

    After success, sets pb:bootstrap:used in Redis (no TTL — permanent flag).
    Returns a dict with {promoted: bool, message: str}.
    """
    if not BOOTSTRAP_ADMIN_PHONE:
        raise HTTPException(
            status_code = 400,
            detail      = "BOOTSTRAP_ADMIN_PHONE not configured. Set it in the K8s Secret.",
        )

    if phone_number != BOOTSTRAP_ADMIN_PHONE:
        raise HTTPException(
            status_code = 403,
            detail      = "Phone number does not match BOOTSTRAP_ADMIN_PHONE.",
        )

    # One-time check
    already_used = await redis.get(_BOOTSTRAP_USED_KEY)
    if already_used:
        raise HTTPException(
            status_code = 409,
            detail      = "Bootstrap already used. Remove BOOTSTRAP_ADMIN_PHONE from K8s Secret.",
        )

    # Promote user
    from db.repository import UserRepository
    repo = UserRepository(db)
    user = await repo.get_by_phone(phone_number)
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Register first.")

    user.role = "ADMIN"
    await db.flush()

    # Mark as used (permanent — no TTL)
    await redis.set(_BOOTSTRAP_USED_KEY, "1")

    logger.info("[BOOTSTRAP] %s promoted to ADMIN. Remove BOOTSTRAP_ADMIN_PHONE from Secret.", phone_number)
    return {
        "promoted": True,
        "user_id" : user.id,
        "message" : (
            "User promoted to ADMIN. "
            "Remove BOOTSTRAP_ADMIN_PHONE from your K8s Secret immediately."
        ),
    }
