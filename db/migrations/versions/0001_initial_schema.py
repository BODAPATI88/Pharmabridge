"""Initial schema – all 6 tables

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-06-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id",            postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("phone_number",  sa.String(20),  nullable=False),
        sa.Column("email",         sa.String(254), nullable=True),
        sa.Column("full_name",     sa.String(200), nullable=True),
        sa.Column("password_hash", sa.String(128), nullable=True),
        sa.Column("pincode",       sa.String(6),   nullable=True),
        sa.Column("is_active",     sa.Boolean(),   nullable=False, server_default="true"),
        sa.Column("is_verified",   sa.Boolean(),   nullable=False, server_default="false"),
        sa.Column("created_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("deleted_at",    sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_phone",   "users", ["phone_number"], unique=True)
    op.create_index("ix_users_email",   "users", ["email"],        unique=True, postgresql_where=sa.text("email IS NOT NULL"))

    # ── prescriptions ──────────────────────────────────────
    op.create_table(
        "prescriptions",
        sa.Column("id",                   postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id",              postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_key",          sa.String(512),  nullable=False),
        sa.Column("original_filename",    sa.String(255),  nullable=True),
        sa.Column("mime_type",            sa.String(50),   nullable=True),
        sa.Column("file_size_bytes",      sa.Integer(),    nullable=True),
        sa.Column("verification_status",  sa.String(20),   nullable=False, server_default="PENDING"),
        sa.Column("verified_by",          sa.String(200),  nullable=True),
        sa.Column("verified_at",          sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason",     sa.Text(),       nullable=True),
        sa.Column("expires_at",           sa.DateTime(timezone=True), nullable=True),
        sa.Column("extracted_medicines",  postgresql.JSONB(), nullable=True),
        sa.Column("doctor_name",          sa.String(200),  nullable=True),
        sa.Column("doctor_reg_number",    sa.String(50),   nullable=True),
        sa.Column("patient_name",         sa.String(200),  nullable=True),
        sa.Column("issued_date",          sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",           sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",           sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_prescs_user",   "prescriptions", ["user_id"])
    op.create_index("ix_prescs_status", "prescriptions", ["verification_status"])

    # ── searches ───────────────────────────────────────────
    op.create_table(
        "searches",
        sa.Column("id",                  postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id",             postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("prescription_id",     postgresql.UUID(as_uuid=False), sa.ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("pincode",             sa.String(6),   nullable=False),
        sa.Column("medicines_requested", postgresql.JSONB(), nullable=False),
        sa.Column("vendors_queried",     postgresql.JSONB(), nullable=False),
        sa.Column("prefer_generic",      sa.Boolean(),   nullable=False, server_default="false"),
        sa.Column("status",              sa.String(20),  nullable=False, server_default="pending"),
        sa.Column("cache_hit",           sa.Boolean(),   nullable=False, server_default="false"),
        sa.Column("duration_ms",         sa.Float(),     nullable=True),
        sa.Column("smart_split_result",  postgresql.JSONB(), nullable=True),
        sa.Column("grand_total",         sa.Float(),     nullable=True),
        sa.Column("total_saving",        sa.Float(),     nullable=True),
        sa.Column("created_at",          sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",          sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_searches_user",            "searches", ["user_id"])
    op.create_index("ix_searches_status",          "searches", ["status"])
    op.create_index("ix_searches_created",         "searches", ["created_at"])
    op.create_index("ix_searches_user_created",    "searches", ["user_id", "created_at"])
    op.create_index("ix_searches_pincode_created", "searches", ["pincode", "created_at"])

    # ── search_vendor_results ──────────────────────────────
    op.create_table(
        "search_vendor_results",
        sa.Column("id",          postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("search_id",   postgresql.UUID(as_uuid=False), sa.ForeignKey("searches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vendor",      sa.String(30),      nullable=False),
        sa.Column("status",      sa.String(20),      nullable=False),
        sa.Column("raw_results", postgresql.JSONB(), nullable=False),
        sa.Column("duration_ms", sa.Float(),         nullable=True),
        sa.Column("error_msg",   sa.Text(),          nullable=True),
        sa.Column("scraped_at",  sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("search_id", "vendor", name="uq_search_vendor"),
    )
    op.create_index("ix_svr_search",         "search_vendor_results", ["search_id"])
    op.create_index("ix_svr_vendor_scraped", "search_vendor_results", ["vendor", "scraped_at"])

    # ── orders ─────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id",                   postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("order_number",         sa.String(30),      nullable=False),
        sa.Column("user_id",              postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("search_id",            postgresql.UUID(as_uuid=False), sa.ForeignKey("searches.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("prescription_id",      postgresql.UUID(as_uuid=False), sa.ForeignKey("prescriptions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("medicine_total",       sa.Float(), nullable=False),
        sa.Column("delivery_total",       sa.Float(), nullable=False, server_default="0"),
        sa.Column("grand_total",          sa.Float(), nullable=False),
        sa.Column("total_saving",         sa.Float(), nullable=False, server_default="0"),
        sa.Column("status",               sa.String(30), nullable=False, server_default="PENDING_PAYMENT"),
        sa.Column("delivery_address",     postgresql.JSONB(), nullable=True),
        sa.Column("cart_json",            postgresql.JSONB(), nullable=False),
        sa.Column("vendor_order_ids",     postgresql.JSONB(), nullable=True),
        sa.Column("auto_refill_enabled",  sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("auto_refill_interval", sa.Integer(), nullable=True),
        sa.Column("next_refill_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",           sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",           sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("deleted_at",           sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_orders_order_number",  "orders", ["order_number"], unique=True)
    op.create_index("ix_orders_user",          "orders", ["user_id"])
    op.create_index("ix_orders_status",        "orders", ["status"])
    op.create_index("ix_orders_user_created",  "orders", ["user_id", "created_at"])

    # ── vendor_price_history ───────────────────────────────
    op.create_table(
        "vendor_price_history",
        sa.Column("id",             postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("medicine_name",  sa.String(200), nullable=False),
        sa.Column("generic_name",   sa.String(200), nullable=True),
        sa.Column("vendor",         sa.String(30),  nullable=False),
        sa.Column("brand_name",     sa.String(200), nullable=False),
        sa.Column("manufacturer",   sa.String(200), nullable=True),
        sa.Column("price_per_unit", sa.Float(),     nullable=False),
        sa.Column("mrp",            sa.Float(),     nullable=False),
        sa.Column("discount_pct",   sa.Float(),     nullable=False, server_default="0"),
        sa.Column("in_stock",       sa.Boolean(),   nullable=False, server_default="true"),
        sa.Column("schedule",       sa.String(20),  nullable=False, server_default="unknown"),
        sa.Column("pincode",        sa.String(6),   nullable=False),
        sa.Column("recorded_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_vph_medicine_vendor_pin_time", "vendor_price_history",
                    ["medicine_name", "vendor", "pincode", "recorded_at"])
    op.create_index("ix_vph_generic_vendor_time", "vendor_price_history",
                    ["generic_name", "vendor", "recorded_at"])


def downgrade() -> None:
    # Drop in reverse FK dependency order
    op.drop_table("vendor_price_history")
    op.drop_table("orders")
    op.drop_table("search_vendor_results")
    op.drop_table("searches")
    op.drop_table("prescriptions")
    op.drop_table("users")
