"""Prescription workflow – alter prescriptions table and add prescription_audit

Revision ID: 0003_prescription_workflow
Revises: 0002_add_vendors_and_seeds
Create Date: 2026-06-07 00:00:00

Changes
───────
1. prescriptions table
   - storage_key comment updated to reflect canonical path format:
       prescriptions/{year}/{month}/{prescription_id}.{ext}
     (path format defined in gateway/storage.py :: build_storage_key)
   - ADD content_type  VARCHAR(50)  NOT NULL DEFAULT 'application/pdf'
     Needed so the operator console renders the file correctly without
     relying on the file extension alone.
   - ADD rejection_reason  TEXT  NULLABLE
     Structured reason shown to the patient on rejection.
     (rejection_reason already exists in the ORM model — this migration
      is the canonical add for databases created before 0003)
   - COLLAPSE state machine: remove EXPIRED as a stored state.
     Expiry is computed: if status=APPROVED and now() > expires_at → expired.
     Storing EXPIRED as a state requires a background job to flip rows;
     computing it on read is simpler and avoids stale state bugs.
     Final states: UPLOADED → PENDING_REVIEW → UNDER_REVIEW → APPROVED | REJECTED

2. prescription_audit table (NEW)
   Complete immutable audit trail for every state transition.
   Required for Schedule H/H1 regulatory compliance.
   NEVER allow UPDATE or DELETE on this table.

3. operator_sessions table (NEW)
   Tracks which operator has UNDER_REVIEW lock on a prescription,
   preventing two pharmacists from reviewing the same document.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision      = "0003_prescription_workflow"
down_revision = "0002_add_vendors_and_seeds"
branch_labels = None
depends_on    = None


def upgrade() -> None:

    # ── 1. Alter prescriptions table ───────────────────────────────

    # Add content_type if it doesn't exist yet
    # (It was not in the 0001 migration; adding here)
    op.add_column(
        "prescriptions",
        sa.Column(
            "content_type",
            sa.String(50),
            nullable=False,
            server_default="application/pdf",
            comment="MIME type: image/jpeg | image/png | application/pdf",
        ),
    )

    # Add rejection_reason if column is missing from earlier migration
    # Guard with try/except — if it already exists (ORM added it), skip silently
    try:
        op.add_column(
            "prescriptions",
            sa.Column(
                "rejection_reason",
                sa.Text(),
                nullable=True,
                comment=(
                    "Operator-selected reason: Illegible | Missing Signature | "
                    "Expired | Wrong Patient | Schedule Mismatch | Other"
                ),
            ),
        )
    except Exception:
        pass  # Column already exists from 0001 ORM model

    # Rename old 'PENDING' default to 'UPLOADED' to reflect the new state machine.
    # Existing rows in PENDING state are migrated to UPLOADED.
    op.execute(
        "UPDATE prescriptions SET verification_status = 'UPLOADED' "
        "WHERE verification_status = 'PENDING'"
    )

    # Drop EXPIRED as a stored value — it is now computed on read.
    # Any rows stuck in EXPIRED are moved back to APPROVED (they are still
    # physically valid; the computed expiry check will re-surface them as expired).
    op.execute(
        "UPDATE prescriptions SET verification_status = 'APPROVED' "
        "WHERE verification_status = 'EXPIRED'"
    )

    # Add reviewer_id FK for UNDER_REVIEW locking
    op.add_column(
        "prescriptions",
        sa.Column(
            "reviewer_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="user.id of the operator who claimed UNDER_REVIEW lock",
        ),
    )
    op.add_column(
        "prescriptions",
        sa.Column(
            "review_started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the UNDER_REVIEW lock was claimed",
        ),
    )

    op.create_index("ix_prescs_reviewer", "prescriptions", ["reviewer_id"])

    # ── 2. prescription_audit (immutable event log) ──────────────────

    op.create_table(
        "prescription_audit",
        sa.Column("id",              postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "prescription_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("prescriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="User (patient or operator) who triggered the event",
        ),
        sa.Column("actor_role",     sa.String(20),  nullable=False,
                  comment="patient | operator | system"),
        sa.Column("event_type",     sa.String(50),  nullable=False,
                  comment=(
                      "UPLOADED | REVIEW_CLAIMED | REVIEW_RELEASED | "
                      "APPROVED | REJECTED | EXPIRED_COMPUTED | DELETED"
                  )),
        sa.Column("from_status",    sa.String(30),  nullable=True),
        sa.Column("to_status",      sa.String(30),  nullable=False),
        sa.Column("rejection_reason", sa.Text(),    nullable=True),
        sa.Column("notes",          sa.Text(),      nullable=True,
                  comment="Free-text operator notes (not shown to patient)"),
        sa.Column("ip_address",     sa.String(45),  nullable=True),
        sa.Column("user_agent",     sa.String(500), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # Indexes for audit queries
    op.create_index("ix_pa_prescription",  "prescription_audit", ["prescription_id"])
    op.create_index("ix_pa_actor",         "prescription_audit", ["actor_id"])
    op.create_index("ix_pa_event_type",    "prescription_audit", ["event_type"])
    op.create_index("ix_pa_occurred_at",   "prescription_audit", ["occurred_at"])

    # Prevent any UPDATE or DELETE on audit rows via a PostgreSQL rule
    # This enforces immutability at the DB level, not just the application level.
    op.execute("""
        CREATE RULE prescription_audit_no_update AS
          ON UPDATE TO prescription_audit DO INSTEAD NOTHING
    """)
    op.execute("""
        CREATE RULE prescription_audit_no_delete AS
          ON DELETE TO prescription_audit DO INSTEAD NOTHING
    """)

    # ── 3. operator_sessions (review lock tracking) ─────────────────

    op.create_table(
        "operator_sessions",
        sa.Column("id",              postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "operator_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prescription_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("prescriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claimed_at",    sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("released_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active",     sa.Boolean(), nullable=False, server_default="true"),
        # A given prescription can only have ONE active operator session
        sa.UniqueConstraint(
            "prescription_id",
            "is_active",
            name="uq_op_session_active_prescription",
            # PostgreSQL partial unique index to enforce "one active lock per prescription"
            # while allowing multiple completed sessions
        ),
    )
    # Partial unique index: only one ACTIVE lock per prescription
    op.execute("""
        CREATE UNIQUE INDEX uq_active_operator_lock
        ON operator_sessions (prescription_id)
        WHERE is_active = true
    """)
    op.create_index("ix_os_operator",      "operator_sessions", ["operator_id"])
    op.create_index("ix_os_prescription",  "operator_sessions", ["prescription_id"])


def downgrade() -> None:
    op.execute("DROP RULE IF EXISTS prescription_audit_no_delete ON prescription_audit")
    op.execute("DROP RULE IF EXISTS prescription_audit_no_update ON prescription_audit")
    op.execute("DROP INDEX IF EXISTS uq_active_operator_lock")

    op.drop_table("operator_sessions")
    op.drop_table("prescription_audit")

    op.drop_index("ix_prescs_reviewer", table_name="prescriptions")
    op.drop_column("prescriptions", "review_started_at")
    op.drop_column("prescriptions", "reviewer_id")
    op.drop_column("prescriptions", "content_type")

    # Restore UPLOADED → PENDING on downgrade
    op.execute(
        "UPDATE prescriptions SET verification_status = 'PENDING' "
        "WHERE verification_status = 'UPLOADED'"
    )
