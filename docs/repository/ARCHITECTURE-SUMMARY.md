=== MAIN APPLICATION ===
"""
pharmabridge/gateway/main.py  ·  v3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Changes from v2
───────────────
+ JWT authentication via /auth/* routes (register, login, refresh, logout)
+ Redis rate limiting on all write endpoints (sliding window)
+ Structured JSON logging with per-request context (request_id, user_id)
+ Security headers middleware (HSTS, X-Frame-Options, CSP etc.)
+ user_id attached to search records when caller is authenticated
+ Vendors table used for delivery rules (DB-driven, not hardcoded)
+ Resource-light: all middleware is async, no blocking I/O in handlers

Redis key additions (v3)
────────────────────────
  pb:rl:ip:{group}:{ip}       Rate limit sliding window (IP)
  pb:rl:user:{group}:{uid}    Rate limit sliding window (user)
  pb:refresh:{token}          Refresh token → user_id (TTL 7 days)
  pb:refresh:user:{uid}       user_id → current refresh token
  pb:otp:{phone}              OTP value (TTL 5 min)
  pb:otp:attempts:{phone}     OTP attempt counter
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

# Make db/ importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.session import get_db, get_engine
from db.repository import SearchRepository, VendorPriceHistoryRepository

from auth import TokenPayload, require_auth, require_verified_auth
from logging_config import RequestContextMiddleware, configure_logging, get_logger
from models import (
    GatewayHealthResponse,
    SearchInitResponse,
    SearchRequest,
    SearchResultResponse,
    SmartSplitResult,
    TaskStatus,
    VendorBatchResult,
    VendorName,
)
from smart_split import DEFAULT_DELIVERY_RULES, VendorDeliveryRule, compute_smart_split

# Auth router
from routers.auth_routes import router as auth_router
from routers.prescription_routes import router as prescription_router

# ─────────────────────────────────────────────────────────
# Logging (must be before any logger usage)
# ─────────────────────────────────────────────────────────

JSON_LOGS = os.getenv("JSON_LOGS", "true").lower() == "true"
configure_logging(level=os.getenv("LOG_LEVEL", "INFO"), json_output=JSON_LOGS)
logger = get_logger("pharmabridge.gateway")

# ─────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────

REDIS_URL       = os.getenv("REDIS_URL",        "redis://redis-svc:6379/0")
RESULT_TTL_S    = int(os.getenv("RESULT_TTL_S", "3600"))
CACHE_TTL_S     = int(os.getenv("CACHE_TTL_S",  "300"))
HEARTBEAT_TTL_S = int(os.getenv("HEARTBEAT_TTL_S", "90"))
FRONTEND_DIR    = Path(os.getenv("FRONTEND_DIR", "/app/frontend"))
GATEWAY_VERSION = os.getenv("GATEWAY_VERSION",  "1.0.0")

ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost,http://localhost:8000,"
        "https://pharmabridge.bvrinfra.in,"
        "https://pharmabridge-ops.bvrinfra.in",
    ).split(",") if o.strip()
]

# ─────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────

app = FastAPI(
    title       = "PharmaBridge API",
    description = "Medicine price aggregation with Smart-Split cart optimisation.",
    version     = GATEWAY_VERSION,
    docs_url    = "/api/docs",
    redoc_url   = "/api/redoc",
)

# ── Middleware (order matters: outermost = first to run) ─
app.add_middleware(RequestContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_credentials = True,
    allow_methods     = ["GET", "POST", "OPTIONS"],
    allow_headers     = ["*"],
)

# ── Security headers middleware ──────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers to every response."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]   = "nosniff"
        response.headers["X-Frame-Options"]          = "SAMEORIGIN"
        response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"]         = "1; mode=block"
        response.headers["Permissions-Policy"]       = "geolocation=(), microphone=()"
        # HSTS – only add when TLS is terminated at Traefik/Cloudflare
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# ── Static files ─────────────────────────────────────────
if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")

# ── Auth router ──────────────────────────────────────────
app.include_router(auth_router)
app.include_router(prescription_router)

# ─────────────────────────────────────────────────────────
# Redis connection pool
# ─────────────────────────────────────────────────────────

_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            REDIS_URL,
            encoding        = "utf-8",
            decode_responses= True,
            max_connections = 50,
        )
    return _redis


# ─────────────────────────────────────────────────────────
# Startup / shutdown
# ─────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event() -> None:
    logger.info("starting", version=GATEWAY_VERSION)

    r = await get_redis()
    try:
        await r.ping()
        logger.info("redis_ok", url=REDIS_URL)
    except Exception as exc:
        logger.error("redis_failed", error=str(exc))

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("postgres_ok")
    except Exception as exc:
        logger.error("postgres_failed", error=str(exc))


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        logger.info("redis_closed")


# ─────────────────────────────────────────────────────────
# Helper: load delivery rules from DB (with hardcoded fallback)
# ─────────────────────────────────────────────────────────

async def _load_delivery_rules() -> dict[VendorName, VendorDeliveryRule]:
    """
    Fetch active vendor delivery rules from PostgreSQL.
    Falls back to DEFAULT_DELIVERY_RULES if DB is unreachable.
    Results are NOT cached here — the gateway's Redis cache on the
    SmartSplitResult means this is called at most once per 5-minute window
    per unique search.
    """
    try:
        from db.orm_models import Vendor
        from sqlalchemy import select
        engine = get_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(
                select(
                    Vendor.name, Vendor.flat_fee, Vendor.free_above, Vendor.min_order
                ).where(Vendor.is_active == True)
            )).fetchall()

        if not rows:
            return DEFAULT_DELIVERY_RULES

        rules: dict[VendorName, VendorDeliveryRule] = {}
        for row in rows:
            try:
                vendor = VendorName(row.name)
                rules[vendor] = VendorDeliveryRule(
                    vendor    = vendor,
                    flat_fee  = row.flat_fee,
                    free_above= row.free_above,
                    min_order = row.min_order,
                )
            except ValueError:
                pass  # Unknown vendor name in DB — skip

        return rules or DEFAULT_DELIVERY_RULES

    except Exception as exc:
        logger.warning("delivery_rules_db_fallback", error=str(exc))
        return DEFAULT_DELIVERY_RULES


# ─────────────────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_consumer_frontend() -> FileResponse:
    html = FRONTEND_DIR / "pharmabridge-demo.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="Frontend file not found.")
    return FileResponse(str(html), media_type="text/html")


@app.get("/operator", include_in_schema=False)
async def serve_operator_console() -> FileResponse:
    html = FRONTEND_DIR / "pharmabridge-v2-operator.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="Operator console file not found.")
    return FileResponse(str(html), media_type="text/html")


# ─────────────────────────────────────────────────────────
# Health  &  Operations
# ─────────────────────────────────────────────────────────

@app.get("/api/v1/health", response_model=GatewayHealthResponse, tags=["Operations"])
async def health_check() -> GatewayHealthResponse:
    r = await get_redis()
    redis_ok = False
    try:
        await r.ping()
        redis_ok = True
    except Exception as exc:
        logger.error("health_redis_fail", error=str(exc))

    workers_alive: dict[str, bool] = {}
    for vendor in VendorName:
        try:
            val = await r.get(f"pb:heartbeat:{vendor.value}")
            workers_alive[vendor.value] = val is not None
        except Exception:
            workers_alive[vendor.value] = False

    return GatewayHealthResponse(
        status        = "ok" if redis_ok else "degraded",
        redis_ok      = redis_ok,
        workers_alive = workers_alive,
        version       = GATEWAY_VERSION,
        timestamp     = datetime.now(timezone.utc),
    )


@app.get("/api/v1/workers", tags=["Operations"])
async def worker_status() -> dict:
    r = await get_redis()
    output = {}
    for vendor in VendorName:
        try:
            raw = await r.get(f"pb:heartbeat:{vendor.value}")
            output[vendor.value] = json.loads(raw) if raw else None
        except Exception:
            output[vendor.value] = None
    return {"workers": output, "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/api/v1/queue-depth", tags=["Operations"])
async def queue_depth() -> dict:
    r = await get_redis()
    depths = {}
    for vendor in VendorName:
        try:
            depths[vendor.value] = await r.llen(f"pb:queue:{vendor.value}")
        except Exception:
            depths[vendor.value] = -1
    return {"queues": depths, "timestamp": datetime.now(timezone.utc).isoformat()}


# ─────────────────────────────────────────────────────────
# Search  –  POST /api/v1/search
# ─────────────────────────────────────────────────────────

from rate_limit import rate_limit_ip, rate_limit_user



# Override with properly typed implementation
from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_optional_bearer = HTTPBearer(auto_error=False)


async def _optional_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
) -> Optional[TokenPayload]:
    """Decode token if present; return None if absent (anonymous)."""
    if credentials is None:
        return None
    try:
        from auth import decode_access_token
        return decode_access_token(credentials.credentials)
    except HTTPException:
        return None


# Remove the placeholder and define the real route
## app.routes = [r for r in app.routes if getattr(r, "path", None) != "/api/v1/search"]
#
#
@app.post(
    "/api/v1/search",
    response_model = SearchInitResponse,
    status_code    = status.HTTP_202_ACCEPTED,
    tags           = ["Search"],
    summary        = "Submit a prescription search; returns task_id for polling",
    dependencies   = [Depends(rate_limit_ip("search"))],
)
async def submit_search(  # noqa: F811
    req  : SearchRequest,
    token: Optional[TokenPayload] = Depends(_optional_token),
) -> SearchInitResponse:
    """
    Fan-out search to all requested vendor queues.
    Returns a task_id immediately (202 Accepted).
    Poll GET /api/v1/result/{task_id} for results.
    """
    return await _submit_search_impl(req, user_id=token.sub if token else None)


async def _submit_search_impl(
    req    : SearchRequest,
    user_id: Optional[str],
) -> SearchInitResponse:
    r         = await get_redis()
    task_id   = str(uuid.uuid4())
    queued_at = datetime.now(timezone.utc)

    # Bind task_id into logging context for this request
    from logging_config import set_request_context
    set_request_context(task_id=task_id, user_id=user_id or "anonymous")

    # Cache check
    cache_key = _build_cache_key(req)
    cached    = await r.get(f"pb:cache:{cache_key}")

    if cached:
        await _write_task_meta(r, task_id, req, queued_at, from_cache=True)
        await r.set(f"pb:task:{task_id}:cached_key", cache_key, ex=RESULT_TTL_S)
        logger.info("cache_hit", cache_key=cache_key[:12])
        return SearchInitResponse(
            task_id      = task_id,
            status       = TaskStatus.CACHED,
            vendor_count = len(req.vendors),
            message      = "Results served from cache. Poll /api/v1/result/{task_id}.",
            queued_at    = queued_at,
        )

    await _write_task_meta(r, task_id, req, queued_at, from_cache=False, user_id=user_id)

    # Fan-out to vendor queues
    async with r.pipeline(transaction=True) as pipe:
        for vendor in req.vendors:
            job = {
                "task_id"        : task_id,
                "vendor"         : vendor.value,
                "medicines"      : [m.model_dump() for m in req.medicines],
                "pincode"        : req.pincode,
                "prefer_generic" : req.prefer_generic,
                "queued_at"      : queued_at.isoformat(),
            }
            await pipe.rpush(f"pb:queue:{vendor.value}", json.dumps(job))
        await pipe.execute()

    logger.info(
        "search_queued",
        vendors=[v.value for v in req.vendors],
        medicine_count=len(req.medicines),
        pincode=req.pincode,
    )

    # Persist to DB best-effort
    try:
        from db.session import db_session
        async with db_session() as db:
            repo = SearchRepository(db)
            await repo.create(
                search_id           = task_id,
                pincode             = req.pincode,
                medicines_requested = [m.model_dump() for m in req.medicines],
                vendors_queried     = [v.value for v in req.vendors],
                user_id             = user_id,
                prefer_generic      = req.prefer_generic,
            )
    except Exception as exc:
        logger.warning("db_persist_failed", task_id=task_id, error=str(exc))

    return SearchInitResponse(
        task_id      = task_id,
        status       = TaskStatus.PENDING,
        vendor_count = len(req.vendors),
        queued_at    = queued_at,
    )


# ─────────────────────────────────────────────────────────
# Result  –  GET /api/v1/result/{task_id}
# ─────────────────────────────────────────────────────────

@app.get(
    "/api/v1/result/{task_id}",
    response_model = SearchResultResponse,
    tags           = ["Search"],
    summary        = "Poll for search results; Smart-Split runs when all vendors respond",
    dependencies   = [Depends(rate_limit_ip("general"))],
)
async def get_result(task_id: str) -> SearchResultResponse:
    r = await get_redis()

    meta_raw = await r.hgetall(f"pb:task:{task_id}:meta")
    if not meta_raw:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    from logging_config import set_request_context
    set_request_context(task_id=task_id)

    vendors_requested: list[VendorName] = [VendorName(v) for v in json.loads(meta_raw["vendors"])]
    medicines_raw    : list[dict]       = json.loads(meta_raw["medicines"])
    medicine_quantities: dict[str, int] = {m["name"]: m["quantity"] for m in medicines_raw}

    # Cache hit path
    cached_key = await r.get(f"pb:task:{task_id}:cached_key")
    if cached_key:
        raw_split = await r.get(f"pb:cache:{cached_key}")
        if raw_split:
            smart_split = SmartSplitResult.model_validate_json(raw_split)
            smart_split.task_id = task_id
            return SearchResultResponse(
                task_id     = task_id,
                status      = TaskStatus.DONE,
                vendors_done= vendors_requested,
                smart_split = smart_split,
                cache_hit   = True,
            )

    # Collect vendor results
    batch_results  : list[VendorBatchResult] = []
    vendors_done   : list[VendorName]        = []
    vendors_pending: list[VendorName]        = []
    vendors_failed : list[VendorName]        = []

    for vendor in vendors_requested:
        raw = await r.get(f"pb:task:{task_id}:result:{vendor.value}")
        if raw is None:
            vendors_pending.append(vendor)
        else:
            batch = VendorBatchResult.model_validate_json(raw)
            batch_results.append(batch)
            (vendors_done if batch.status == TaskStatus.DONE else vendors_failed).append(vendor)

