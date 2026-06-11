"""
pharmabridge/workers/reaper.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Upload Reaper — cleans up abandoned UPLOADED prescriptions.

Problem
───────
The two-step upload flow (get-presigned-url → PUT to MinIO → confirm)
can be abandoned between step 1 and step 2:
  - User closes the browser tab during upload
  - Network failure after MinIO receives the file
  - Client bug that never calls /confirm

Result without a reaper:
  - DB rows stuck in UPLOADED state indefinitely
  - MinIO objects that are never confirmed into the workflow
  - At 200 prescriptions/day with 5% abandonment: ~3,600 orphan rows/year

What the reaper does
────────────────────
Every run (triggered by K8s CronJob, default daily at 03:00 IST):

1. Query prescriptions WHERE:
     verification_status = 'UPLOADED'
     AND created_at < now() - ABANDON_AGE_HOURS

2. For each orphaned row:
   a. Attempt to delete the MinIO object (best-effort; object may not exist)
   b. Write a DELETED audit event (actor_role='system')
   c. Hard-delete the prescription row
      (no soft-delete — UPLOADED rows have no patient data worth keeping)

3. Log a structured summary: rows_scanned, objects_deleted, rows_deleted, errors

Why hard-delete instead of soft-delete?
────────────────────────────────────────
Soft-delete is for records with business value (orders, approved prescriptions).
A row that was never confirmed has zero clinical or financial value. Keeping
it pollutes analytics ("how many prescriptions were uploaded this month?" would
include noise) and wastes storage. Hard-delete is correct here.

Why 24 hours default, not 1 hour?
───────────────────────────────────
A user on a slow mobile connection may take up to several hours to complete
a large PDF upload. 24 hours is conservative enough to not punish slow
connections while still clearing debris before it accumulates.

Running manually
────────────────
  python reaper.py                    # uses defaults
  ABANDON_AGE_HOURS=1 python reaper.py  # aggressive (testing)
  DRY_RUN=true python reaper.py       # logs what would be deleted, touches nothing
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure gateway modules are importable
sys.path.insert(0, str(Path(__file__).parent.parent / "gateway"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.orm_models import Prescription, PrescriptionAudit
from db.session import db_session

# Lazy-import storage to avoid aiobotocore overhead if DRY_RUN=true
if os.getenv("DRY_RUN", "false").lower() != "true":
    from storage import delete_object, object_exists
else:
    async def delete_object(key: str) -> None:  # type: ignore[misc]
        pass
    async def object_exists(key: str) -> bool:  # type: ignore[misc]
        return True

# ─────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────

ABANDON_AGE_HOURS = int(os.getenv("ABANDON_AGE_HOURS", "24"))
DRY_RUN           = os.getenv("DRY_RUN", "false").lower() == "true"
BATCH_SIZE        = int(os.getenv("REAPER_BATCH_SIZE", "100"))   # rows per DB query

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt= "%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("pharmabridge.reaper")


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────

async def run() -> dict:
    """
    Execute one reaper run.

    Returns a summary dict suitable for structured logging and
    for the K8s Job exit-code decision (raises on unexpected errors,
    returns normally on partial failures).
    """
    cutoff       = datetime.now(timezone.utc) - timedelta(hours=ABANDON_AGE_HOURS)
    mode         = "DRY_RUN" if DRY_RUN else "LIVE"

    logger.info(
        "reaper_start",
        extra={
            "cutoff"       : cutoff.isoformat(),
            "abandon_hours": ABANDON_AGE_HOURS,
            "batch_size"   : BATCH_SIZE,
            "mode"         : mode,
        }
    )

    rows_scanned     = 0
    objects_deleted  = 0
    rows_deleted     = 0
    errors           = 0
    skipped_no_object= 0

    # Process in batches to avoid loading thousands of rows into memory
    offset = 0
    while True:
        async with db_session() as db:
            result = await db.execute(
                select(Prescription)
                .where(
                    Prescription.verification_status == "UPLOADED",
                    Prescription.created_at < cutoff,
                )
                .order_by(Prescription.created_at.asc())
                .limit(BATCH_SIZE)
                .offset(offset)
            )
            batch: list[Prescription] = list(result.scalars().all())

        if not batch:
            break

        rows_scanned += len(batch)
        logger.info("reaper_batch", offset=offset, batch_size=len(batch))

        for presc in batch:
            try:
                deleted = await _reap_one(presc, dry_run=DRY_RUN)
                if deleted == "object_deleted":
                    objects_deleted += 1
                    rows_deleted    += 1
                elif deleted == "object_missing":
                    skipped_no_object += 1
                    rows_deleted      += 1
                elif deleted == "dry_run":
                    rows_deleted += 1
            except Exception as exc:
                errors += 1
                logger.error(
                    "reaper_row_error",
                    prescription_id = presc.id[:8] if presc.id else "?",
                    error           = str(exc),
                    exc_info        = True,
                )

        # If this batch was smaller than BATCH_SIZE, we've processed everything
        if len(batch) < BATCH_SIZE:
            break

        offset += BATCH_SIZE

    summary = {
        "mode"             : mode,
        "abandon_hours"    : ABANDON_AGE_HOURS,
        "cutoff"           : cutoff.isoformat(),
        "rows_scanned"     : rows_scanned,
        "objects_deleted"  : objects_deleted,
        "rows_deleted"     : rows_deleted,
        "skipped_no_object": skipped_no_object,
        "errors"           : errors,
    }
    logger.info("reaper_complete", extra=summary)
    return summary


async def _reap_one(presc: Prescription, dry_run: bool) -> str:
    """
    Reap a single abandoned prescription.

    Returns one of: "object_deleted" | "object_missing" | "dry_run"
    Raises on unexpected errors (caller logs and counts them).
    """
    pid      = presc.id
    key      = presc.storage_key
    user_id  = presc.user_id

    if dry_run:
        logger.info(
            "reaper_would_delete",
            prescription_id = pid[:8],
            storage_key     = key,
            created_at      = presc.created_at.isoformat() if presc.created_at else None,
        )
        return "dry_run"

    # 1. Attempt MinIO deletion (best-effort)
    obj_outcome = "object_missing"
    try:
        exists = await object_exists(key)
        if exists:
            await delete_object(key)
            obj_outcome = "object_deleted"
            logger.debug("reaper_minio_deleted", key=key)
        else:
            logger.debug("reaper_minio_not_found", key=key)
    except Exception as exc:
        # MinIO deletion failure is non-fatal — the DB row should still be removed
        # to prevent the reaper from looping on the same record forever.
        # The orphan object in MinIO will be caught by the MinIO lifecycle rule.
        logger.warning("reaper_minio_error", key=key, error=str(exc))
        obj_outcome = "object_missing"   # treat as if not found

    # 2. Write audit record + delete DB row in one transaction
    async with db_session() as db:
        # Audit event
        audit_row = PrescriptionAudit(
            id               = str(uuid.uuid4()),
            prescription_id  = pid,
            actor_id         = None,        # system action — no human actor
            actor_role       = "system",
            event_type       = "DELETED",
            from_status      = "UPLOADED",
            to_status        = "DELETED",
            notes            = (
                f"Abandoned upload reaped after {ABANDON_AGE_HOURS}h. "
                f"MinIO object: {obj_outcome}. Key: {key}"
            ),
        )
        db.add(audit_row)

        # Hard-delete the prescription row
        await db.execute(
            delete(Prescription).where(Prescription.id == pid)
        )
        # db_session context manager commits on clean exit

    logger.info(
        "reaper_reaped",
        prescription_id = pid[:8],
        storage_key     = key,
        minio_outcome   = obj_outcome,
    )
    return obj_outcome


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main():
        summary = await run()
        # Non-zero exit if there were unexpected errors (K8s Job will retry)
        if summary["errors"] > 0:
            logger.error("reaper_finished_with_errors", errors=summary["errors"])
            sys.exit(1)
        sys.exit(0)

    asyncio.run(_main())
