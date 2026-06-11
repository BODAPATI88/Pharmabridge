"""
pharmabridge/gateway/rate_limit.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Redis-based rate limiting via the sliding window counter algorithm.

Why sliding window vs token bucket?
─────────────────────────────────────
Token bucket allows instantaneous bursts up to the bucket capacity.
Sliding window smooths traffic more aggressively: the window always
covers exactly the last N seconds, so a burst at t=0 will still count
against requests made at t=59 in a 60-second window.

For a medicine price API this is the right trade-off: we care more about
preventing scraper abuse than about allowing legitimate burst traffic.

Algorithm (per key)
───────────────────
1. ZADD the current timestamp to a sorted set at key pb:rl:{key}
2. ZREMRANGEBYSCORE to drop members older than window_seconds
3. ZCARD to count remaining members
4. If count > limit → reject with 429
5. EXPIRE the key to window_seconds (auto-cleanup)

This is O(log N + M) per request where M = expired members removed.
In practice M is tiny, so this is effectively O(1).

Keys used
─────────
  pb:rl:ip:{ip}              Global per-IP limit
  pb:rl:user:{user_id}       Authenticated user limit (stricter)
  pb:rl:endpoint:{ep}:{ip}   Per-endpoint per-IP limit (for expensive ops)

Limits configured per endpoint group
─────────────────────────────────────
  Search       : 10 req/min per IP, 30 req/min per user
  Auth         : 5  req/min per IP  (prevents credential stuffing)
  General API  : 60 req/min per IP
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, Request, status

logger = logging.getLogger("pharmabridge.rate_limit")


# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RateLimit:
    limit  : int    # max requests
    window : int    # window in seconds


LIMITS = {
    "search"  : RateLimit(limit=10, window=60),    # 10/min per IP; 30/min per user
    "search_user": RateLimit(limit=30, window=60),
    "auth"    : RateLimit(limit=5,  window=60),    # OTP / login brute-force guard
    "general" : RateLimit(limit=60, window=60),
}


# ─────────────────────────────────────────────────────────
# Core sliding-window check
# ─────────────────────────────────────────────────────────

async def _check(
    redis     : aioredis.Redis,
    key       : str,
    rate_limit: RateLimit,
) -> tuple[bool, int, int]:
    """
    Perform a sliding window rate limit check.

    Returns (allowed: bool, current_count: int, reset_in_seconds: int).
    Uses a Lua script for atomicity — ZADD + ZREMRANGE + ZCARD in one
    round-trip, preventing TOCTOU race conditions under concurrent load.
    """
    now    = time.time()
    cutoff = now - rate_limit.window

    # Atomic Lua script: removes expired members, adds current, counts
    lua_script = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local window = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
redis.call('ZADD', key, now, now .. math.random())
local count = redis.call('ZCARD', key)
redis.call('EXPIRE', key, window)
return count
"""
    count = await redis.eval(lua_script, 1, key, now, cutoff, rate_limit.window)
    count = int(count)
    allowed = count <= rate_limit.limit
    reset_in = rate_limit.window   # conservative estimate

    if not allowed:
        logger.warning("Rate limit exceeded on key=%s count=%d limit=%d",
                       key, count, rate_limit.limit)

    return allowed, count, reset_in


# ─────────────────────────────────────────────────────────
# Public FastAPI dependency factories
# ─────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    """
    Extract the real client IP, respecting Cloudflare / Traefik forwarded headers.
    Cloudflare sets CF-Connecting-IP, which is more reliable than X-Forwarded-For
    (which can be spoofed by clients) because CF-Connecting-IP is set by Cloudflare
    itself at the edge.
    """
    # Cloudflare tunnel: CF-Connecting-IP is set by CF, not forwardable by clients
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()

    # Traefik / nginx: X-Forwarded-For (first entry is client)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()

    # Direct connection fallback
    return request.client.host if request.client else "unknown"


def rate_limit_ip(group: str = "general"):
    """
    FastAPI dependency that rate-limits by client IP.

    Usage:
        @app.post("/api/v1/search", dependencies=[Depends(rate_limit_ip("search"))])
        async def search(...): ...
    """
    from gateway.main import get_redis   # Late import to avoid circular dependency

    async def _dependency(request: Request) -> None:
        r    = await get_redis()
        ip   = _get_client_ip(request)
        rl   = LIMITS.get(group, LIMITS["general"])
        key  = f"pb:rl:ip:{group}:{ip}"

        allowed, count, reset_in = await _check(r, key, rl)
        if not allowed:
            raise HTTPException(
                status_code = status.HTTP_429_TOO_MANY_REQUESTS,
                detail      = f"Rate limit exceeded. Try again in {reset_in} seconds.",
                headers     = {
                    "X-RateLimit-Limit"     : str(rl.limit),
                    "X-RateLimit-Remaining" : "0",
                    "X-RateLimit-Reset"     : str(reset_in),
                    "Retry-After"           : str(reset_in),
                },
            )

    return _dependency


def rate_limit_user(group: str = "search_user"):
    """
    FastAPI dependency that rate-limits by authenticated user_id.
    Falls back to IP limiting if no auth token is present.

    Usage:
        @app.post("/api/v1/search", dependencies=[Depends(rate_limit_user())])
        async def search(...): ...
    """
    from gateway.main import get_redis
    from gateway.auth import decode_access_token

    async def _dependency(request: Request) -> None:
        r     = await get_redis()
        rl    = LIMITS.get(group, LIMITS["search_user"])
        ip    = _get_client_ip(request)

        # Try to extract user_id from Bearer token (optional – no 401 if absent)
        user_id: Optional[str] = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from gateway.auth import decode_access_token
                payload   = decode_access_token(auth_header[7:])
                user_id   = payload.sub
            except Exception:
                pass

        if user_id:
            key = f"pb:rl:user:{group}:{user_id}"
        else:
            key = f"pb:rl:ip:{group}:{ip}"

        allowed, count, reset_in = await _check(r, key, rl)
        if not allowed:
            raise HTTPException(
                status_code = status.HTTP_429_TOO_MANY_REQUESTS,
                detail      = f"Rate limit exceeded. Try again in {reset_in} seconds.",
                headers     = {
                    "X-RateLimit-Limit"    : str(rl.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset"    : str(reset_in),
                    "Retry-After"          : str(reset_in),
                },
            )

    return _dependency
