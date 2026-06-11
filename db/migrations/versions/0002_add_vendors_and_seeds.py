"""Add vendors table and seed default vendor delivery rules

Revision ID: 0002_add_vendors_and_seeds
Revises: 0001_initial_schema
Create Date: 2026-06-07 00:00:00
"""

from __future__ import annotations
import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision      = "0002_add_vendors_and_seeds"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on    = None

_VENDORS = [
    # name        display_name              website                        flat_fee  free_above  min_order
    ("1mg",       "1mg (Tata 1mg)",         "https://www.1mg.com",         49.0,     299.0,      0.0),
    ("pharmeasy", "PharmEasy",              "https://pharmeasy.in",        49.0,     299.0,      0.0),
    ("netmeds",   "Netmeds (Reliance)",     "https://www.netmeds.com",     39.0,     250.0,      0.0),
    ("apollo",    "Apollo Pharmacy",        "https://www.apollopharmacy.in", 0.0,    0.0,        0.0),
]


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("id",               postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name",             sa.String(30),  nullable=False),
        sa.Column("display_name",     sa.String(100), nullable=False),
        sa.Column("website",          sa.String(200), nullable=True),
        sa.Column("logo_url",         sa.String(500), nullable=True),
        sa.Column("flat_fee",         sa.Float(),     nullable=False, server_default="0"),
        sa.Column("free_above",       sa.Float(),     nullable=False, server_default="0"),
        sa.Column("min_order",        sa.Float(),     nullable=False, server_default="0"),
        sa.Column("is_active",        sa.Boolean(),   nullable=False, server_default="true"),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",       sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at",       sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("name", name="uq_vendors_name"),
    )
    op.create_index("ix_vendors_name",      "vendors", ["name"], unique=True)
    op.create_index("ix_vendors_is_active", "vendors", ["is_active"])

    # Seed default vendor data
    vendors_table = sa.table(
        "vendors",
        sa.column("id"),
        sa.column("name"),
        sa.column("display_name"),
        sa.column("website"),
        sa.column("flat_fee"),
        sa.column("free_above"),
        sa.column("min_order"),
    )
    op.bulk_insert(
        vendors_table,
        [
            {
                "id"          : str(uuid.uuid4()),
                "name"        : name,
                "display_name": display,
                "website"     : website,
                "flat_fee"    : flat_fee,
                "free_above"  : free_above,
                "min_order"   : min_order,
            }
            for name, display, website, flat_fee, free_above, min_order in _VENDORS
        ],
    )


def downgrade() -> None:
    op.drop_table("vendors")
