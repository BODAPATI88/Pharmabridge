"""
pharmabridge/gateway/cleanup_uploads.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Upload Reaper — cleans up abandoned UPLOADED-state prescriptions.

Problem
───────
The 2-step upload flow (get presigned URL → confirm) leaves orphan rows
if a user abandons after step 1:
  - PostgreSQL row in UPLOADED state
  - MinIO object that may or may not exist

At 200 prescriptions/day with a 5% abandonment rate, you accumulate
~3,650 orphan rows/year.  At 5MB average, that's ~18GB of MinIO storage
that never gets freed.

Strategy
────────
A row is considered abandoned if:
  - verification_status = 'UPLOADED'
  - created_at < now() - 24 hours
  (24 hours is generous: presigned URLs expire in 5 minutes, so
   any file not confirmed within 24 hours will never be confirmed.)

For each abandoned row:
  1. Attempt to delete the MinIO object (best-effort, non-fatal if absent)
  2. Delete the PostgreSQL row
  3. Write one prescription_audit row with event_type = 'DELETED'

Deployment
──────────
This script runs as a Kubernetes CronJob (see k8s/cleanup-cronjob.yaml).
Schedule: daily at 3:00 AM IST.

It can also be run manually:
  kubectl exec -it deploy/pharmabridge-gateway -- python cleanup_uploads.py

Idempotency
───────────
Multiple runs are safe:  DELETE WHERE status='UPLOADED' AND created_at < now()-24h
is idempotent.  If the MinIO delete fails, the row stays; next run retries it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.orm_models import Prescription, PrescriptionAudit
from db.session import db_session
from storage import delete_object, object_exists

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("pharmabridge.cleanup")

ABANDON_HOURS = int(os.getenv("UPLOAD_ABANDON_HOURS", "24"))
DRY_RUN       = os.getenv("DRY_RUN", "false").lower() == "true"


async def reap_abandoned_uploads() -> dict:
    """
    Find and delete all abandoned UPLOADED prescriptions.
    Returns a summary dict for logging / alerting.
    """
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=ABANDON_HOURS)
    deleted  = 0
    skipped  = 0
    errors   = 0
    freed_mb = 0.0

    async with db_session() as db:
        # Fetch candidates in batches of 100 to limit memory
        result = await db.execute(
            select(Prescription)
            .where(
                Prescription.verification_status == "UPLOADED",
                Prescription.created_at          < cutoff,
            )
            .limit(500)   # process max 500 per run to bound runtime
        )
        candidates = result.scalars().all()

        if not candidates:
            logger.info("reaper_nothing_to_do", cutoff=cutoff.isoformat())
            return {"deleted": 0, "skipped": 0, "errors": 0, "freed_mb": 0.0}

        logger.info("reaper_found", count=len(candidates),
                    cutoff=cutoff.isoformat(), dry_run=DRY_RUN)

        for presc in candidates:
            try:
                # Track freed storage
                freed_mb += (presc.file_size_bytes or 0) / (1024 * 1024)

                if not DRY_RUN:
                    # 1. Delete from MinIO (best-effort)
                    try:
                        if await object_exists(presc.storage_key):
                            await delete_object(presc.storage_key)
                            logger.info("reaper_object_deleted", key=presc.storage_key)
                        else:
                            logger.debug("reaper_object_missing", key=presc.storage_key)
                    except Exception as storage_exc:
                        logger.warning("reaper_storage_error", key=presc.storage_key,
                                       error=str(storage_exc))
                        # Don't abort: still delete the DB row

                    # 2. Write audit row before deleting the prescription
                    db.add(PrescriptionAudit(
                        prescription_id  = presc.id,
                        actor_id         = None,
                        actor_role       = "system",
                        event_type       = "DELETED",
                        from_status      = "UPLOADED",
                        to_status        = "DELETED",
                        notes            = f"Abandoned upload reaper: created_at={presc.created_at.isoformat()}",
                    ))
                    await db.flush()

                    # 3. Hard delete the prescription row
                    await db.delete(presc)
                    await db.flush()

                deleted += 1
                logger.info("reaper_deleted", prescription_id=presc.id[:8],
                            created_at=presc.created_at.isoformat(),
                            storage_key=presc.storage_key)

            except Exception as exc:
                errors += 1
                logger.error("reaper_error", prescription_id=presc.id[:8], error=str(exc))

    summary = {
        "deleted"    : deleted,
        "skipped"    : skipped,
        "errors"     : errors,
        "freed_mb"   : round(freed_mb, 2),
        "dry_run"    : DRY_RUN,
        "ran_at"     : datetime.now(timezone.utc).isoformat(),
    }
    logger.info("reaper_complete", **summary)
    return summary


if __name__ == "__main__":
    asyncio.run(reap_abandoned_uploads())
