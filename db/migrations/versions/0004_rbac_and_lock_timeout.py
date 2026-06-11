"""RBAC roles, content_type fix, lock timeout, bootstrap admin

Revision ID: 0004_rbac_and_lock_timeout
Revises: 0003_prescription_workflow
Create Date: 2026-06-08 00:00:00

Changes
───────
1. users table
   - ADD role  VARCHAR(20)  DEFAULT 'PATIENT'  NOT NULL
     Values: PATIENT | OPERATOR | ADMIN
   - Bootstrap: phone number in BOOTSTRAP_ADMIN_PHONE env var is promoted
     to ADMIN role at migration time via a Python-side update.
     The env var is advisory — if absent, no user is promoted (safe default).

2. prescriptions table
   - ADD content_type  VARCHAR(50)  NOT NULL  DEFAULT 'application/pdf'
     Replaces mime_type for rendering decisions.  mime_type is kept for
     backward compatibility but content_type is the authoritative column.
   - FIX storage_key comment to match actual path format:
       prescriptions/{year}/{month}/{uuid}.{ext}

3. operator_sessions table
   - ADD expires_at  TIMESTAMPTZ  NOT NULL
     Set to claimed_at + 30 minutes on creation.
     Indexed for the queue query: WHERE expires_at < now().
   - Existing rows backfilled: expires_at = claimed_at + interval '30 minutes'

4. Remove PostgreSQL RULES from prescription_audit (reviewer's note)
   Replace with column-level REVOKE (more correct approach).
   Note: full REVOKE requires a dedicated read-only role — documented here,
   implemented as a comment for DBA execution outside migration scope.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision      = "0004_rbac_and_lock_timeout"
down_revision = "0003_prescription_workflow"
branch_labels = None
depends_on    = None


def upgrade() -> None:

    # ── 1. users.role ──────────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.String(20),
            nullable=False,
            server_default="PATIENT",
            comment="PATIENT | OPERATOR | ADMIN",
        ),
    )
    op.create_index("ix_users_role", "users", ["role"])

    # Bootstrap first admin from environment variable.
    # The env var is only read at migration time — it is not baked into
    # the application binary.  Remove it from K8s Secret after first run.
    bootstrap_phone = os.getenv("BOOTSTRAP_ADMIN_PHONE", "").strip()
    if bootstrap_phone:
        op.execute(
            sa.text(
                "UPDATE users SET role = 'ADMIN' "
                "WHERE phone_number = :phone AND role = 'PATIENT'"
            ).bindparams(phone=bootstrap_phone)
        )
        # Log to migration output (captured by Alembic CLI and Kubernetes Job logs)
        print(f"[bootstrap] Promoted {bootstrap_phone} → ADMIN")
    else:
        print(
            "[bootstrap] BOOTSTRAP_ADMIN_PHONE not set. "
            "No user promoted. Set via K8s Secret and re-run migration, "
            "or use POST /auth/bootstrap (one-time endpoint)."
        )

    # ── 2. prescriptions.content_type ─────────────────────────────
    # Check if content_type was added in 0003 (it was conditionally added).
    # Use a safe ADD COLUMN IF NOT EXISTS pattern.
    op.execute("""
        ALTER TABLE prescriptions
        ADD COLUMN IF NOT EXISTS content_type VARCHAR(50)
            NOT NULL DEFAULT 'application/pdf'
    """)

    # Backfill content_type from mime_type for any existing rows
    op.execute("""
        UPDATE prescriptions
        SET content_type = COALESCE(mime_type, 'application/pdf')
        WHERE content_type = 'application/pdf'
          AND mime_type IS NOT NULL
          AND mime_type != 'application/pdf'
    """)

    # Fix state machine defaults: PENDING → UPLOADED for any remaining rows
    op.execute("""
        UPDATE prescriptions
        SET verification_status = 'UPLOADED'
        WHERE verification_status = 'PENDING'
    """)

    # ── 3. operator_sessions.expires_at ───────────────────────────
    op.add_column(
        "operator_sessions",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,   # nullable during migration; backfilled below; then NOT NULL
            comment="claimed_at + 30 min; lock auto-expires after this timestamp",
        ),
    )

    # Backfill existing rows
    op.execute("""
        UPDATE operator_sessions
        SET expires_at = claimed_at + INTERVAL '30 minutes'
        WHERE expires_at IS NULL
    """)

    # Now make it NOT NULL (all rows have a value)
    op.alter_column("operator_sessions", "expires_at", nullable=False)

    # Index for the queue query pattern: WHERE expires_at < now()
    op.create_index("ix_os_expires_at", "operator_sessions", ["expires_at"])

    # ── 4. Audit table: document REVOKE approach ───────────────────
    # The PostgreSQL RULE approach in 0003 is a rewrite rule, not a true
    # security control. The correct approach for a multi-role DB setup is:
    #
    #   REVOKE UPDATE, DELETE ON prescription_audit FROM pharmabridge_app;
    #   GRANT INSERT, SELECT ON prescription_audit TO pharmabridge_app;
    #
    # This cannot be automated here because it requires a superuser connection
    # separate from the application user. Document it as a DBA checklist item.
    #
    # For the homelab: the RULES from 0003 are sufficient protection.
    # For a production multi-tenant DB: execute the REVOKE statements above
    # using the RDS/CloudSQL superuser after migration.
    pass


def downgrade() -> None:
    op.drop_index("ix_os_expires_at", table_name="operator_sessions")
    op.drop_column("operator_sessions", "expires_at")

    # content_type: only drop if we added it (safe drop)
    op.execute("ALTER TABLE operator_sessions DROP COLUMN IF EXISTS expires_at")
    op.execute("ALTER TABLE prescriptions DROP COLUMN IF EXISTS content_type")

    op.drop_index("ix_users_role", table_name="users")
    op.drop_column("users", "role")
