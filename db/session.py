"""
pharmabridge/db/session.py
━━━━━━━━━━━━━━━━━━━━━━━━━━
Async SQLAlchemy engine + session factory.

Design choices
──────────────
• Uses asyncpg driver (fastest PostgreSQL async driver for Python).
• Connection pool sized for the expected concurrent load:
    - Gateway: 2 pods × 2 workers × pool_size=10 → max 40 connections
    - Workers: stateless, use their own thin session if needed
    - Total stays well within PostgreSQL's default max_connections=100
• NullPool is used in Alembic migration context (synchronous) to avoid
  the "can't reuse asyncpg connection in sync context" error.
• get_db() is a FastAPI dependency that yields an AsyncSession and
  commits/rolls back automatically.

Usage in a route
────────────────
    from db.session import get_db
    from sqlalchemy.ext.asyncio import AsyncSession

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(Order).where(Order.id == order_id))
        return result.scalar_one_or_none()
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

logger = logging.getLogger("pharmabridge.db")

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://pharmabridge:pharmabridge@postgres-svc:5432/pharmabridge",
)

# Echo SQL for debug mode only – never in production (leaks patient data to logs)
ECHO_SQL: bool = os.getenv("ECHO_SQL", "false").lower() == "true"

POOL_SIZE       = int(os.getenv("DB_POOL_SIZE",        "10"))
MAX_OVERFLOW    = int(os.getenv("DB_MAX_OVERFLOW",     "5"))
POOL_TIMEOUT    = int(os.getenv("DB_POOL_TIMEOUT",     "30"))
POOL_RECYCLE    = int(os.getenv("DB_POOL_RECYCLE",     "1800"))   # 30 min
POOL_PRE_PING   = os.getenv("DB_POOL_PRE_PING", "true").lower() == "true"


# ─────────────────────────────────────────────────────────
# Engine factory
# ─────────────────────────────────────────────────────────

def _make_engine(*, nullpool: bool = False):
    """
    Create the async engine.

    nullpool=True is used by Alembic's env.py to get a synchronous-
    compatible engine for running migrations.
    """
    kwargs: dict = {
        "echo"          : ECHO_SQL,
        "pool_pre_ping" : POOL_PRE_PING,
    }
    if nullpool:
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update({
            "pool_size"    : POOL_SIZE,
            "max_overflow" : MAX_OVERFLOW,
            "pool_timeout" : POOL_TIMEOUT,
            "pool_recycle" : POOL_RECYCLE,
        })

    return create_async_engine(DATABASE_URL, **kwargs)


# Module-level engine and session factory (created once at import time)
_engine  = _make_engine()
_session_factory = async_sessionmaker(
    _engine,
    expire_on_commit = False,   # Don't expire objects after commit (safe for async)
    class_           = AsyncSession,
)


def get_engine():
    """Return the module-level async engine (for health checks etc.)."""
    return _engine


# ─────────────────────────────────────────────────────────
# FastAPI dependency
# ─────────────────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an AsyncSession.

    Commits on clean exit, rolls back on exception, always closes.

    Example:
        @app.post("/orders")
        async def create_order(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        # Session closes automatically via async context manager


# ─────────────────────────────────────────────────────────
# Context manager for non-FastAPI use (workers, scripts)
# ─────────────────────────────────────────────────────────

@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for use outside FastAPI dependency injection.

    Usage:
        async with db_session() as db:
            db.add(record)
            await db.commit()
    """
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ─────────────────────────────────────────────────────────
# Alembic helper (used in migrations/env.py)
# ─────────────────────────────────────────────────────────

def make_sync_engine_for_alembic():
    """
    Return a synchronous engine string that Alembic can use.
    Converts asyncpg URL to psycopg2 URL for Alembic's sync runner.
    """
    sync_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    from sqlalchemy import create_engine
    return create_engine(sync_url, poolclass=NullPool)
