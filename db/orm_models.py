"""
pharmabridge/db/orm_models.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SQLAlchemy 2.0 async ORM — v4.1

Table inventory
───────────────
  users                 – registered patients / operators / admins
  prescriptions         – uploaded prescription images + verification state
  prescription_audit    – immutable event log for every state transition
  operator_sessions     – review lock with expiry (claimed_at + 30 min)
  searches              – every price-comparison search request
  search_vendor_results – raw per-vendor scrape outputs
  orders                – confirmed split-cart orders
  vendor_price_history  – time-series medicine prices
  vendors               – vendor registry with delivery rules

State machine (prescriptions.verification_status)
───────────────────────────────────────────────────
  UPLOADED      → file in MinIO, awaiting confirmation from patient
  PENDING_REVIEW → confirmed by patient, in operator queue
  UNDER_REVIEW  → claimed by an operator (session lock active)
  APPROVED      → verified by operator; order-eligible if now() < expires_at
  REJECTED      → rejected by operator; rejection_reason surfaced to patient

  EXPIRED is NOT stored as a state.  It is computed:
    is_order_eligible = (status == APPROVED) AND (now() < expires_at)
  This eliminates the need for a background job to flip rows.

User roles (users.role)
───────────────────────
  PATIENT   – registered user, can upload prescriptions and place orders
  OPERATOR  – pharmacist, can review prescriptions in the operator queue
  ADMIN     – full access, can promote/demote users, view analytics

  Bootstrap: set BOOTSTRAP_ADMIN_PHONE in K8s Secret before migration 0004.
  The migration promotes that phone number to ADMIN on first run.
  Remove the env var after promotion.

Storage key format (prescriptions.storage_key)
──────────────────────────────────────────────
  prescriptions/{year:04d}/{month:02d}/{prescription_id}.{ext}
  e.g. prescriptions/2026/06/3f7a9c1d-4b5e-....pdf

  Defined canonically in gateway/storage.py :: build_storage_key().
  Never construct keys manually.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────
# users
# ─────────────────────────────────────────────────────────

class User(Base):
    """
    Registered accounts.

    role values: PATIENT | OPERATOR | ADMIN
    Bootstrap: migration 0004 promotes BOOTSTRAP_ADMIN_PHONE → ADMIN.
    """
    __tablename__ = "users"

    id           : Mapped[str]           = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    phone_number : Mapped[str]           = mapped_column(String(20),  unique=True, nullable=False, index=True)
    email        : Mapped[str | None]    = mapped_column(String(254), unique=True, nullable=True)
    full_name    : Mapped[str | None]    = mapped_column(String(200), nullable=True)
    password_hash: Mapped[str | None]    = mapped_column(String(128), nullable=True)
    pincode      : Mapped[str | None]    = mapped_column(String(6),   nullable=True)
    role         : Mapped[str]           = mapped_column(String(20),  nullable=False, default="PATIENT",
                                                         comment="PATIENT | OPERATOR | ADMIN")
    is_active    : Mapped[bool]          = mapped_column(Boolean, default=True,  nullable=False)
    is_verified  : Mapped[bool]          = mapped_column(Boolean, default=False, nullable=False)
    created_at   : Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at   : Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at   : Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)

    prescriptions: Mapped[list["Prescription"]] = relationship(
        back_populates="user",
        foreign_keys="Prescription.user_id",
        lazy="select",
    )

    reviewed_prescriptions: Mapped[list["Prescription"]] = relationship(
        back_populates="reviewer",
        foreign_keys="Prescription.reviewer_id",
        lazy="select",
    )

    searches: Mapped[list["Search"]] = relationship(back_populates="user", lazy="select")
    orders: Mapped[list["Order"]] = relationship(back_populates="user", lazy="select")

    def __repr__(self) -> str:
        return f"<User {self.phone_number} role={self.role}>"


# ─────────────────────────────────────────────────────────
# prescriptions
# ─────────────────────────────────────────────────────────

class Prescription(Base):
    """
    Uploaded prescription images and their verification workflow state.

    Storage
    ───────
    Files live in MinIO bucket pharmabridge-prescriptions.
    storage_key format: prescriptions/{year}/{month}/{id}.{ext}
    Defined in gateway/storage.py :: build_storage_key().

    State machine
    ─────────────
    UPLOADED → PENDING_REVIEW → UNDER_REVIEW → APPROVED | REJECTED

    content_type (not mime_type) is the authoritative MIME column.
    mime_type is retained for backward compatibility only.
    """
    __tablename__ = "prescriptions"

    id               : Mapped[str]           = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id          : Mapped[str]           = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Storage
    storage_key      : Mapped[str]           = mapped_column(
        String(512), nullable=False,
        comment="MinIO object key: prescriptions/{year}/{month}/{uuid}.{ext}"
    )
    original_filename: Mapped[str | None]    = mapped_column(String(255), nullable=True)
    content_type     : Mapped[str]           = mapped_column(
        String(50), nullable=False, server_default="application/pdf",
        comment="Authoritative MIME type: image/jpeg | image/png | application/pdf"
    )
    mime_type        : Mapped[str | None]    = mapped_column(
        String(50), nullable=True,
        comment="Deprecated: use content_type. Retained for backward compat."
    )
    file_size_bytes  : Mapped[int | None]    = mapped_column(Integer, nullable=True)

    # Workflow state
    verification_status: Mapped[str]          = mapped_column(
        String(20), nullable=False, default="UPLOADED", index=True,
        comment="UPLOADED | PENDING_REVIEW | UNDER_REVIEW | APPROVED | REJECTED"
    )
    reviewer_id      : Mapped[str | None]    = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        comment="Operator who holds UNDER_REVIEW lock"
    )
    review_started_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Verification outcome
    verified_by      : Mapped[str | None]    = mapped_column(String(200), nullable=True)
    verified_at      : Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason : Mapped[str | None]    = mapped_column(
        Text, nullable=True,
        comment="Illegible | Missing Signature | Expired | Wrong Patient | Schedule Mismatch | Other"
    )
    expires_at       : Mapped[datetime|None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="issued_date + 180 days (Indian Schedule H validity period)"
    )

    # OCR-extracted clinical data
    extracted_medicines : Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    doctor_name         : Mapped[str | None]  = mapped_column(String(200), nullable=True)
    doctor_reg_number   : Mapped[str | None]  = mapped_column(String(50),  nullable=True)
    patient_name        : Mapped[str | None]  = mapped_column(String(200), nullable=True)
    issued_date         : Mapped[datetime|None]= mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(
        back_populates="prescriptions",
        foreign_keys=[user_id]
    )

    reviewer: Mapped["User | None"] = relationship(
        back_populates="reviewed_prescriptions",
        foreign_keys=[reviewer_id]
    )

    orders: Mapped[list["Order"]] = relationship(
        back_populates="prescription",
        lazy="select",
    )

    __table_args__ = (
        Index("ix_prescs_user",     "user_id"),
        Index("ix_prescs_status",   "verification_status"),
        Index("ix_prescs_reviewer", "reviewer_id"),
    )

    def __repr__(self) -> str:
        return f"<Prescription {self.id[:8]} status={self.verification_status}>"


# ─────────────────────────────────────────────────────────
# prescription_audit  (immutable)
# ─────────────────────────────────────────────────────────

class PrescriptionAudit(Base):
    """
    Immutable event log. One row per state transition.

    DB-level protection (migration 0003):
      CREATE RULE prescription_audit_no_update ...
      CREATE RULE prescription_audit_no_delete ...
    DBA-level (manual post-migration):
      REVOKE UPDATE, DELETE ON prescription_audit FROM pharmabridge_app;

    Repository only exposes insert(), never update/delete.

    event_type values:
      UPLOADED | PENDING_REVIEW | REVIEW_CLAIMED | REVIEW_RELEASED |
      LOCK_EXPIRED | APPROVED | REJECTED | DELETED
    """
    __tablename__ = "prescription_audit"

    id              : Mapped[str]         = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    prescription_id : Mapped[str]         = mapped_column(UUID(as_uuid=False), ForeignKey("prescriptions.id", ondelete="CASCADE"), nullable=False)
    actor_id        : Mapped[str | None]  = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_role      : Mapped[str]         = mapped_column(String(20),  nullable=False, comment="patient | operator | system")
    event_type      : Mapped[str]         = mapped_column(String(50),  nullable=False)
    from_status     : Mapped[str | None]  = mapped_column(String(30),  nullable=True)
    to_status       : Mapped[str]         = mapped_column(String(30),  nullable=False)
    rejection_reason: Mapped[str | None]  = mapped_column(Text,        nullable=True)
    notes           : Mapped[str | None]  = mapped_column(Text,        nullable=True)
    ip_address      : Mapped[str | None]  = mapped_column(String(45),  nullable=True)
    user_agent      : Mapped[str | None]  = mapped_column(String(500), nullable=True)
    occurred_at     : Mapped[datetime]    = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_pa_prescription", "prescription_id"),
        Index("ix_pa_actor",        "actor_id"),
        Index("ix_pa_event_type",   "event_type"),
        Index("ix_pa_occurred_at",  "occurred_at"),
    )

    def __repr__(self) -> str:
        return f"<PrescriptionAudit {self.event_type} presc={self.prescription_id[:8]}>"


# ─────────────────────────────────────────────────────────
# operator_sessions  (review lock with expiry)
# ─────────────────────────────────────────────────────────

class OperatorSession(Base):
    """
    Tracks which operator holds the UNDER_REVIEW lock on a prescription.

    Lock lifecycle:
      claim()     → is_active=True, expires_at=now()+30min
      heartbeat() → expires_at=now()+30min  (resets timer while operator is active)
      release()   → is_active=False, released_at=now()
      expire()    → is_active=False by queue query (expires_at < now())

    Queue query ignores expired active sessions:
      WHERE verification_status = 'PENDING_REVIEW'
      OR (verification_status = 'UNDER_REVIEW' AND reviewer.expires_at < now())

    Partial unique index in DB:
      CREATE UNIQUE INDEX uq_active_operator_lock
      ON operator_sessions (prescription_id)
      WHERE is_active = true
    """
    __tablename__ = "operator_sessions"

    id              : Mapped[str]           = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    operator_id     : Mapped[str]           = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    prescription_id : Mapped[str]           = mapped_column(UUID(as_uuid=False), ForeignKey("prescriptions.id", ondelete="CASCADE"), nullable=False)
    claimed_at      : Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at      : Mapped[datetime]      = mapped_column(
        DateTime(timezone=True), nullable=False,
        comment="claimed_at + 30 min; heartbeat resets this"
    )
    released_at     : Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active       : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_os_operator",     "operator_id"),
        Index("ix_os_prescription", "prescription_id"),
        Index("ix_os_expires_at",   "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<OperatorSession op={self.operator_id[:8]} presc={self.prescription_id[:8]} expires={self.expires_at}>"


# ─────────────────────────────────────────────────────────
# searches
# ─────────────────────────────────────────────────────────

class Search(Base):
    __tablename__ = "searches"

    id                 : Mapped[str]          = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id            : Mapped[str | None]   = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    prescription_id    : Mapped[str | None]   = mapped_column(UUID(as_uuid=False), ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True)
    pincode            : Mapped[str]          = mapped_column(String(6),   nullable=False, index=True)
    medicines_requested: Mapped[dict]         = mapped_column(JSONB,       nullable=False)
    vendors_queried    : Mapped[list]         = mapped_column(JSONB,       nullable=False)
    prefer_generic     : Mapped[bool]         = mapped_column(Boolean,     default=False)
    status             : Mapped[str]          = mapped_column(String(20),  default="pending", nullable=False, index=True)
    cache_hit          : Mapped[bool]         = mapped_column(Boolean,     default=False)
    duration_ms        : Mapped[float | None] = mapped_column(Float,       nullable=True)
    smart_split_result : Mapped[dict | None]  = mapped_column(JSONB,       nullable=True)
    grand_total        : Mapped[float | None] = mapped_column(Float,       nullable=True)
    total_saving       : Mapped[float | None] = mapped_column(Float,       nullable=True)
    created_at         : Mapped[datetime]     = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at         : Mapped[datetime]     = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user           : Mapped["User | None"]              = relationship(back_populates="searches")
    vendor_results : Mapped[list["SearchVendorResult"]] = relationship(back_populates="search", cascade="all, delete-orphan")
    orders         : Mapped[list["Order"]]              = relationship(back_populates="search")

    __table_args__ = (
        Index("ix_searches_user_created",    "user_id", "created_at"),
        Index("ix_searches_pincode_created", "pincode", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Search {self.id[:8]} status={self.status}>"


# ─────────────────────────────────────────────────────────
# search_vendor_results
# ─────────────────────────────────────────────────────────

class SearchVendorResult(Base):
    __tablename__ = "search_vendor_results"

    id          : Mapped[str]          = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    search_id   : Mapped[str]          = mapped_column(UUID(as_uuid=False), ForeignKey("searches.id", ondelete="CASCADE"), nullable=False, index=True)
    vendor      : Mapped[str]          = mapped_column(String(30),      nullable=False)
    status      : Mapped[str]          = mapped_column(String(20),      nullable=False)
    raw_results : Mapped[dict]         = mapped_column(JSONB,           nullable=False)
    duration_ms : Mapped[float | None] = mapped_column(Float,           nullable=True)
    error_msg   : Mapped[str | None]   = mapped_column(Text,            nullable=True)
    scraped_at  : Mapped[datetime]     = mapped_column(DateTime(timezone=True), server_default=func.now())

    search: Mapped["Search"] = relationship(back_populates="vendor_results")

    __table_args__ = (
        UniqueConstraint("search_id", "vendor", name="uq_search_vendor"),
        Index("ix_svr_vendor_scraped", "vendor", "scraped_at"),
    )

    def __repr__(self) -> str:
        return f"<SearchVendorResult {self.vendor} search={self.search_id[:8]}>"


# ─────────────────────────────────────────────────────────
# orders
# ─────────────────────────────────────────────────────────

class Order(Base):
    """
    Confirmed split-cart orders.

    status values:
      PENDING_PAYMENT | CONFIRMED | PACKED | OUT_FOR_DELIVERY | DELIVERED |
      CANCELLED | REFUND_INITIATED
    """
    __tablename__ = "orders"

    id              : Mapped[str]           = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    order_number    : Mapped[str]           = mapped_column(String(30),  unique=True, nullable=False, index=True)
    user_id         : Mapped[str]           = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    search_id       : Mapped[str]           = mapped_column(UUID(as_uuid=False), ForeignKey("searches.id", ondelete="RESTRICT"), nullable=False)
    prescription_id : Mapped[str | None]    = mapped_column(UUID(as_uuid=False), ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True)
    medicine_total  : Mapped[float]         = mapped_column(Float, nullable=False)
    delivery_total  : Mapped[float]         = mapped_column(Float, nullable=False, default=0.0)
    grand_total     : Mapped[float]         = mapped_column(Float, nullable=False)
    total_saving    : Mapped[float]         = mapped_column(Float, nullable=False, default=0.0)
    status          : Mapped[str]           = mapped_column(String(30), default="PENDING_PAYMENT", nullable=False, index=True)
    delivery_address: Mapped[dict | None]   = mapped_column(JSONB, nullable=True)
    cart_json       : Mapped[dict]          = mapped_column(JSONB, nullable=False)
    vendor_order_ids: Mapped[dict | None]   = mapped_column(JSONB, nullable=True)
    auto_refill_enabled : Mapped[bool]           = mapped_column(Boolean, default=False)
    auto_refill_interval: Mapped[int | None]     = mapped_column(Integer, nullable=True)
    next_refill_at      : Mapped[datetime | None]= mapped_column(DateTime(timezone=True), nullable=True)
    created_at  : Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at  : Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at  : Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)

    user        : Mapped["User"]                = relationship(back_populates="orders")
    search      : Mapped["Search"]              = relationship(back_populates="orders")
    prescription: Mapped["Prescription | None"] = relationship(back_populates="orders")

    __table_args__ = (
        Index("ix_orders_user_created", "user_id", "created_at"),
        Index("ix_orders_status",       "status"),
    )

    def __repr__(self) -> str:
        return f"<Order {self.order_number} status={self.status}>"


# ─────────────────────────────────────────────────────────
# vendor_price_history
# ─────────────────────────────────────────────────────────

class VendorPriceHistory(Base):
    __tablename__ = "vendor_price_history"

    id            : Mapped[str]        = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    medicine_name : Mapped[str]        = mapped_column(String(200), nullable=False)
    generic_name  : Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    vendor        : Mapped[str]        = mapped_column(String(30),  nullable=False)
    brand_name    : Mapped[str]        = mapped_column(String(200), nullable=False)
    manufacturer  : Mapped[str | None] = mapped_column(String(200), nullable=True)
    price_per_unit: Mapped[float]      = mapped_column(Float,       nullable=False)
    mrp           : Mapped[float]      = mapped_column(Float,       nullable=False)
    discount_pct  : Mapped[float]      = mapped_column(Float,       nullable=False, default=0.0)
    in_stock      : Mapped[bool]       = mapped_column(Boolean,     nullable=False, default=True)
    schedule      : Mapped[str]        = mapped_column(String(20),  nullable=False, default="unknown")
    pincode       : Mapped[str]        = mapped_column(String(6),   nullable=False, index=True)
    recorded_at   : Mapped[datetime]   = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    __table_args__ = (
        Index("ix_vph_medicine_vendor_pin_time", "medicine_name", "vendor", "pincode", "recorded_at"),
        Index("ix_vph_generic_vendor_time",      "generic_name",  "vendor", "recorded_at"),
    )

    def __repr__(self) -> str:
        return f"<VendorPriceHistory {self.vendor}/{self.medicine_name} ₹{self.price_per_unit}>"


# ─────────────────────────────────────────────────────────
# vendors
# ─────────────────────────────────────────────────────────

class Vendor(Base):
    """Delivery rules loaded from here at runtime (not hardcoded)."""
    __tablename__ = "vendors"

    id               : Mapped[str]           = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name             : Mapped[str]           = mapped_column(String(30),  unique=True, nullable=False)
    display_name     : Mapped[str]           = mapped_column(String(100), nullable=False)
    website          : Mapped[str | None]    = mapped_column(String(200), nullable=True)
    logo_url         : Mapped[str | None]    = mapped_column(String(500), nullable=True)
    flat_fee         : Mapped[float]         = mapped_column(Float, nullable=False, default=0.0)
    free_above       : Mapped[float]         = mapped_column(Float, nullable=False, default=0.0)
    min_order        : Mapped[float]         = mapped_column(Float, nullable=False, default=0.0)
    is_active        : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=True)
    last_verified_at : Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at       : Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at       : Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Vendor {self.name} fee=₹{self.flat_fee}>"
