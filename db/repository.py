"""
pharmabridge/db/repository.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Repository pattern: one class per aggregate, encapsulating all
database queries.  Route handlers call repositories, never raw SQLAlchemy.

Why repository pattern?
───────────────────────
• Routes stay thin: they validate input, call a repository method, return output.
• Queries are testable in isolation with a test database session.
• When we add read replicas, we swap the session in the repository, not in 10 routes.

All methods are async and accept an AsyncSession from the FastAPI dependency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select, update, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from db.orm_models import (
    Order,
    Prescription,
    Search,
    SearchVendorResult,
    User,
    VendorPriceHistory,
)

logger = logging.getLogger("pharmabridge.db.repository")


# ─────────────────────────────────────────────────────────
# UserRepository
# ─────────────────────────────────────────────────────────

class UserRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        phone_number: str,
        password_hash: Optional[str] = None,
        full_name   : Optional[str]  = None,
        email       : Optional[str]  = None,
        pincode     : Optional[str]  = None,
    ) -> User:
        user = User(
            phone_number = phone_number,
            password_hash = password_hash,
            full_name    = full_name,
            email        = email,
            pincode      = pincode,
        )
        self.db.add(user)
        await self.db.flush()   # populate id without committing
        logger.info("Created user %s (%s)", user.id[:8], phone_number)
        return user

    async def get_by_id(self, user_id: str) -> Optional[User]:
        result = await self.db.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone_number: str) -> Optional[User]:
        result = await self.db.execute(
            select(User).where(
                User.phone_number == phone_number,
                User.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def mark_verified(self, user_id: str) -> None:
        await self.db.execute(
            update(User)
            .where(User.id == user_id)
            .values(is_verified=True, updated_at=func.now())
        )

    async def soft_delete(self, user_id: str) -> None:
        await self.db.execute(
            update(User)
            .where(User.id == user_id)
            .values(deleted_at=datetime.now(timezone.utc), is_active=False)
        )


# ─────────────────────────────────────────────────────────
# PrescriptionRepository
# ─────────────────────────────────────────────────────────

class PrescriptionRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        user_id          : str,
        storage_key      : str,
        original_filename: Optional[str] = None,
        mime_type        : Optional[str] = None,
        file_size_bytes  : Optional[int] = None,
    ) -> Prescription:
        presc = Prescription(
            user_id           = user_id,
            storage_key       = storage_key,
            original_filename = original_filename,
            mime_type         = mime_type,
            file_size_bytes   = file_size_bytes,
            verification_status = "PENDING",
        )
        self.db.add(presc)
        await self.db.flush()
        return presc

    async def get_by_id(self, prescription_id: str) -> Optional[Prescription]:
        result = await self.db.execute(
            select(Prescription).where(Prescription.id == prescription_id)
        )
        return result.scalar_one_or_none()

    async def get_user_prescriptions(
        self,
        user_id: str,
        limit  : int = 20,
        offset : int = 0,
    ) -> Sequence[Prescription]:
        result = await self.db.execute(
            select(Prescription)
            .where(Prescription.user_id == user_id)
            .order_by(desc(Prescription.created_at))
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def approve(
        self,
        prescription_id: str,
        verified_by    : str,
        expires_at     : Optional[datetime] = None,
    ) -> None:
        await self.db.execute(
            update(Prescription)
            .where(Prescription.id == prescription_id)
            .values(
                verification_status = "APPROVED",
                verified_by         = verified_by,
                verified_at         = datetime.now(timezone.utc),
                expires_at          = expires_at,
                updated_at          = func.now(),
            )
        )

    async def reject(self, prescription_id: str, reason: str, verified_by: str) -> None:
        await self.db.execute(
            update(Prescription)
            .where(Prescription.id == prescription_id)
            .values(
                verification_status = "REJECTED",
                rejection_reason    = reason,
                verified_by         = verified_by,
                verified_at         = datetime.now(timezone.utc),
                updated_at          = func.now(),
            )
        )

    async def update_extracted_data(
        self,
        prescription_id  : str,
        extracted_medicines: dict,
        doctor_name      : Optional[str] = None,
        doctor_reg_number: Optional[str] = None,
        patient_name     : Optional[str] = None,
        issued_date      : Optional[datetime] = None,
    ) -> None:
        await self.db.execute(
            update(Prescription)
            .where(Prescription.id == prescription_id)
            .values(
                extracted_medicines  = extracted_medicines,
                doctor_name          = doctor_name,
                doctor_reg_number    = doctor_reg_number,
                patient_name         = patient_name,
                issued_date          = issued_date,
                updated_at           = func.now(),
            )
        )


# ─────────────────────────────────────────────────────────
# SearchRepository
# ─────────────────────────────────────────────────────────

class SearchRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        search_id          : str,
        pincode            : str,
        medicines_requested: list[dict],
        vendors_queried    : list[str],
        user_id            : Optional[str] = None,
        prescription_id    : Optional[str] = None,
        prefer_generic     : bool          = False,
    ) -> Search:
        search = Search(
            id                  = search_id,
            user_id             = user_id,
            prescription_id     = prescription_id,
            pincode             = pincode,
            medicines_requested = medicines_requested,
            vendors_queried     = vendors_queried,
            prefer_generic      = prefer_generic,
            status              = "pending",
        )
        self.db.add(search)
        await self.db.flush()
        return search

    async def get_by_id(self, search_id: str) -> Optional[Search]:
        result = await self.db.execute(
            select(Search).where(Search.id == search_id)
        )
        return result.scalar_one_or_none()

    async def complete(
        self,
        search_id         : str,
        smart_split_result: dict,
        grand_total       : float,
        total_saving      : float,
        duration_ms       : float,
        cache_hit         : bool = False,
    ) -> None:
        await self.db.execute(
            update(Search)
            .where(Search.id == search_id)
            .values(
                status             = "done",
                smart_split_result = smart_split_result,
                grand_total        = grand_total,
                total_saving       = total_saving,
                duration_ms        = duration_ms,
                cache_hit          = cache_hit,
                updated_at         = func.now(),
            )
        )

    async def mark_failed(self, search_id: str, reason: str = "") -> None:
        await self.db.execute(
            update(Search)
            .where(Search.id == search_id)
            .values(status="failed", updated_at=func.now())
        )

    async def get_user_history(
        self,
        user_id: str,
        limit  : int = 20,
        offset : int = 0,
    ) -> Sequence[Search]:
        result = await self.db.execute(
            select(Search)
            .where(Search.user_id == user_id, Search.status == "done")
            .order_by(desc(Search.created_at))
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def upsert_vendor_result(
        self,
        search_id  : str,
        vendor     : str,
        status     : str,
        raw_results: dict,
        duration_ms: Optional[float] = None,
        error_msg  : Optional[str]   = None,
    ) -> None:
        """
        Insert a vendor result row, replacing any previous result for
        the same (search_id, vendor) pair (handles worker retries).
        """
        # Check if exists
        existing = await self.db.execute(
            select(SearchVendorResult).where(
                SearchVendorResult.search_id == search_id,
                SearchVendorResult.vendor    == vendor,
            )
        )
        row = existing.scalar_one_or_none()

        if row:
            row.status      = status
            row.raw_results = raw_results
            row.duration_ms = duration_ms
            row.error_msg   = error_msg
            row.scraped_at  = datetime.now(timezone.utc)
        else:
            row = SearchVendorResult(
                search_id   = search_id,
                vendor      = vendor,
                status      = status,
                raw_results = raw_results,
                duration_ms = duration_ms,
                error_msg   = error_msg,
            )
            self.db.add(row)

        await self.db.flush()


# ─────────────────────────────────────────────────────────
# OrderRepository
# ─────────────────────────────────────────────────────────

class OrderRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        order_number    : str,
        user_id         : str,
        search_id       : str,
        cart_json       : dict,
        medicine_total  : float,
        delivery_total  : float,
        grand_total     : float,
        total_saving    : float,
        prescription_id : Optional[str] = None,
        delivery_address: Optional[dict] = None,
    ) -> Order:
        order = Order(
            order_number     = order_number,
            user_id          = user_id,
            search_id        = search_id,
            prescription_id  = prescription_id,
            cart_json        = cart_json,
            medicine_total   = medicine_total,
            delivery_total   = delivery_total,
            grand_total      = grand_total,
            total_saving     = total_saving,
            delivery_address = delivery_address,
            status           = "PENDING_PAYMENT",
        )
        self.db.add(order)
        await self.db.flush()
        logger.info("Created order %s for user %s", order_number, user_id[:8])
        return order

    async def get_by_id(self, order_id: str) -> Optional[Order]:
        result = await self.db.execute(
            select(Order).where(Order.id == order_id, Order.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_order_number(self, order_number: str) -> Optional[Order]:
        result = await self.db.execute(
            select(Order).where(Order.order_number == order_number)
        )
        return result.scalar_one_or_none()

    async def update_status(self, order_id: str, status: str) -> None:
        await self.db.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(status=status, updated_at=func.now())
        )

    async def get_user_orders(
        self,
        user_id: str,
        limit  : int = 20,
        offset : int = 0,
    ) -> Sequence[Order]:
        result = await self.db.execute(
            select(Order)
            .where(Order.user_id == user_id, Order.deleted_at.is_(None))
            .order_by(desc(Order.created_at))
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def get_user_stats(self, user_id: str) -> dict:
        """Return aggregate spending/saving stats for a user."""
        result = await self.db.execute(
            select(
                func.count(Order.id).label("order_count"),
                func.sum(Order.grand_total).label("total_spent"),
                func.sum(Order.total_saving).label("total_saved"),
            )
            .where(
                Order.user_id    == user_id,
                Order.status     == "DELIVERED",
                Order.deleted_at.is_(None),
            )
        )
        row = result.one()
        total_spent = float(row.total_spent or 0)
        total_saved = float(row.total_saved or 0)
        return {
            "order_count" : row.order_count or 0,
            "total_spent" : total_spent,
            "total_saved" : total_saved,
            "avg_discount_pct": round(
                total_saved / (total_spent + total_saved) * 100, 1
            ) if (total_spent + total_saved) > 0 else 0.0,
        }


# ─────────────────────────────────────────────────────────
# VendorPriceHistoryRepository
# ─────────────────────────────────────────────────────────

class VendorPriceHistoryRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def bulk_insert(self, rows: list[dict]) -> int:
        """
        Insert multiple price records in one statement.
        Used by the gateway after every completed search.

        Returns number of rows inserted.
        """
        objects = [VendorPriceHistory(**row) for row in rows]
        self.db.add_all(objects)
        await self.db.flush()
        return len(objects)

    async def get_price_trend(
        self,
        medicine_name: str,
        vendor       : str,
        pincode      : str,
        days         : int = 30,
        limit        : int = 100,
    ) -> Sequence[VendorPriceHistory]:
        """
        Fetch price history for a medicine at a vendor over the last N days.
        Used by the operator console trend charts.
        """
        from sqlalchemy import text
        result = await self.db.execute(
            select(VendorPriceHistory)
            .where(
                func.lower(VendorPriceHistory.medicine_name).contains(medicine_name.lower()),
                VendorPriceHistory.vendor  == vendor,
                VendorPriceHistory.pincode == pincode,
                VendorPriceHistory.recorded_at >= func.now() - text(f"interval '{days} days'"),
            )
            .order_by(desc(VendorPriceHistory.recorded_at))
            .limit(limit)
        )
        return result.scalars().all()

    async def get_cheapest_vendor_today(
        self,
        medicine_name: str,
        pincode      : str,
    ) -> Optional[VendorPriceHistory]:
        """Latest cheapest vendor for a medicine in a given PIN area."""
        result = await self.db.execute(
            select(VendorPriceHistory)
            .where(
                func.lower(VendorPriceHistory.medicine_name).contains(medicine_name.lower()),
                VendorPriceHistory.pincode   == pincode,
                VendorPriceHistory.in_stock  == True,
                VendorPriceHistory.recorded_at >= func.now() - text("interval '24 hours'"),
            )
            .order_by(VendorPriceHistory.price_per_unit.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()
