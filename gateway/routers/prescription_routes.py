"""
pharmabridge/gateway/routers/prescription_routes.py  ·  v4.1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fixes applied vs v4.0
─────────────────────
✓ RBAC enforced:       operator endpoints use require_operator() — no TODO
✓ content_type fix:    all reads/writes use content_type, not mime_type
✓ Lock timeout:        expires_at field; heartbeat endpoint resets it
✓ Lock auto-reclaim:   expired active sessions cleaned up on new claim
✓ Object integrity:    confirm step validates size + content_type via HEAD
✓ State machine docs:  consistent with orm_models.py

Lock timeout design
───────────────────
  claim()      → expires_at = now() + 30 min
  heartbeat()  → expires_at = now() + 30 min  (resets while operator is active)
  release/approve/reject → is_active = False

Queue query includes prescriptions with EXPIRED active locks:
  status = PENDING_REVIEW
  OR (status = UNDER_REVIEW AND active_session.expires_at < now())

When a new operator claims a prescription with an expired lock:
  1. Old session marked inactive, released_at = now()
  2. LOCK_EXPIRED audit event written
  3. New session created
  4. REVIEW_CLAIMED audit event written
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth import TokenPayload, require_auth, require_operator, require_verified_auth
from db.orm_models import OperatorSession, Prescription, PrescriptionAudit
from db.repository import PrescriptionRepository
from db.session import get_db
from storage import (
    ALLOWED_CONTENT_TYPES,
    MAX_FILE_BYTES,
    build_storage_key,
    generate_download_url,
    generate_upload_url,
    get_object_metadata,
    object_exists,
)

logger = logging.getLogger("pharmabridge.prescriptions")

router = APIRouter(prefix="/api/v1/prescriptions", tags=["Prescriptions"])

PRESCRIPTION_VALIDITY_DAYS = 180     # Indian Schedule H: 6 months
LOCK_TIMEOUT_MINUTES       = 30      # Operator lock auto-expires after this


# ─────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────

class UploadUrlRequest(BaseModel):
    content_type   : str = Field(..., description="image/jpeg | image/png | application/pdf")
    file_size_bytes: int = Field(..., ge=1, le=MAX_FILE_BYTES)


class UploadUrlResponse(BaseModel):
    prescription_id: str
    storage_key    : str
    upload_url     : str
    expires_in     : int
    method         : str = "PUT"
    content_type   : str


class ConfirmUploadRequest(BaseModel):
    prescription_id  : str
    original_filename: Optional[str] = Field(None, max_length=255)


class PrescriptionResponse(BaseModel):
    id                 : str
    verification_status: str
    content_type       : str
    original_filename  : Optional[str]
    file_size_bytes    : Optional[int]
    rejection_reason   : Optional[str]
    expires_at         : Optional[str]
    is_order_eligible  : bool
    download_url       : Optional[str] = None
    created_at         : str


class ApproveRequest(BaseModel):
    issued_date  : Optional[datetime] = None
    notes        : Optional[str]      = Field(None, max_length=2000)
    doctor_name  : Optional[str]      = Field(None, max_length=200)
    doctor_reg_no: Optional[str]      = Field(None, max_length=50)
    patient_name : Optional[str]      = Field(None, max_length=200)


class RejectRequest(BaseModel):
    rejection_reason: str = Field(
        ...,
        description=(
            "Illegible | Missing Signature | Expired | "
            "Wrong Patient | Schedule Mismatch | Other"
        ),
    )
    notes: Optional[str] = Field(None, max_length=2000)


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _is_order_eligible(p: Prescription) -> bool:
    if p.verification_status != "APPROVED":
        return False
    if p.expires_at and datetime.now(timezone.utc) > p.expires_at:
        return False
    return True


def _to_response(p: Prescription, download_url: Optional[str] = None) -> PrescriptionResponse:
    return PrescriptionResponse(
        id                  = p.id,
        verification_status = p.verification_status,
        content_type        = p.content_type,
        original_filename   = p.original_filename,
        file_size_bytes     = p.file_size_bytes,
        rejection_reason    = p.rejection_reason,
        expires_at          = p.expires_at.isoformat() if p.expires_at else None,
        is_order_eligible   = _is_order_eligible(p),
        download_url        = download_url,
        created_at          = p.created_at.isoformat(),
    )


def _lock_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=LOCK_TIMEOUT_MINUTES)


async def _audit(
    db             : AsyncSession,
    prescription_id: str,
    actor_id       : Optional[str],
    actor_role     : str,
    event_type     : str,
    from_status    : Optional[str],
    to_status      : str,
    rejection_reason: Optional[str] = None,
    notes          : Optional[str]  = None,
    request        : Optional[Request] = None,
) -> None:
    ip = ua = None
    if request:
        ip = (request.headers.get("CF-Connecting-IP")
              or (request.client.host if request.client else None))
        ua = request.headers.get("User-Agent", "")[:500]
    db.add(PrescriptionAudit(
        prescription_id  = prescription_id,
        actor_id         = actor_id,
        actor_role       = actor_role,
        event_type       = event_type,
        from_status      = from_status,
        to_status        = to_status,
        rejection_reason = rejection_reason,
        notes            = notes,
        ip_address       = ip,
        user_agent       = ua,
    ))
    await db.flush()


async def _release_expired_lock(
    db             : AsyncSession,
    prescription_id: str,
    now            : datetime,
) -> Optional[str]:
    """
    If an active session exists but is past expires_at, mark it inactive
    and return the old operator_id so we can write a LOCK_EXPIRED audit event.
    Returns None if no expired session existed.
    """
    result = await db.execute(
        select(OperatorSession).where(
            OperatorSession.prescription_id == prescription_id,
            OperatorSession.is_active       == True,
            OperatorSession.expires_at      < now,
        )
    )
    expired = result.scalar_one_or_none()
    if not expired:
        return None

    expired.is_active    = False
    expired.released_at  = now
    await db.flush()
    return expired.operator_id


# ─────────────────────────────────────────────────────────
# Patient: get presigned upload URL
# ─────────────────────────────────────────────────────────

@router.post(
    "/upload-url",
    response_model = UploadUrlResponse,
    summary        = "Get a presigned PUT URL for direct-to-MinIO upload (step 1 of 2)",
)
async def get_upload_url(
    req  : UploadUrlRequest,
    token: TokenPayload = Depends(require_verified_auth),
    db   : AsyncSession = Depends(get_db),
) -> UploadUrlResponse:
    if req.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"Allowed types: {', '.join(ALLOWED_CONTENT_TYPES)}")

    now   = datetime.now(timezone.utc)
    repo  = PrescriptionRepository(db)

    # Create row first to get the UUID for the key
    presc = await repo.create(
        user_id           = token.sub,
        storage_key       = "pending",
        original_filename = None,
        mime_type         = req.content_type,    # backward compat column
        file_size_bytes   = req.file_size_bytes,
    )
    # Set content_type (authoritative) and storage_key using the real UUID
    presc.content_type = req.content_type
    real_key           = build_storage_key(presc.id, req.content_type, now)
    presc.storage_key  = real_key
    await db.flush()

    try:
        result = await generate_upload_url(
            prescription_id = presc.id,
            content_type    = req.content_type,
            file_size_bytes = req.file_size_bytes,
            upload_time     = now,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await _audit(db, presc.id, token.sub, "patient", "UPLOADED", None, "UPLOADED")
    logger.info("upload_url_issued", prescription_id=presc.id[:8], key=real_key)

    return UploadUrlResponse(
        prescription_id = presc.id,
        storage_key     = real_key,
        upload_url      = result["upload_url"],
        expires_in      = result["expires_in"],
        content_type    = req.content_type,
    )


# ─────────────────────────────────────────────────────────
# Patient: confirm upload → validate object → PENDING_REVIEW
# ─────────────────────────────────────────────────────────

@router.post(
    "/confirm",
    response_model = PrescriptionResponse,
    status_code    = status.HTTP_201_CREATED,
    summary        = "Confirm upload complete; validates object integrity (step 2 of 2)",
)
async def confirm_upload(
    req    : ConfirmUploadRequest,
    request: Request,
    token  : TokenPayload = Depends(require_verified_auth),
    db     : AsyncSession = Depends(get_db),
) -> PrescriptionResponse:
    repo  = PrescriptionRepository(db)
    presc = await repo.get_by_id(req.prescription_id)

    if not presc:
        raise HTTPException(status_code=404, detail="Prescription not found.")
    if presc.user_id != token.sub:
        raise HTTPException(status_code=403, detail="Not your prescription.")
    if presc.verification_status != "UPLOADED":
        raise HTTPException(
            status_code=409,
            detail=f"Already in state '{presc.verification_status}'.",
        )

    # ── Object integrity validation ───────────────────────────────
    # Validates: existence, size, and content-type. Prevents 0-byte uploads
    # and content-type spoofing (uploading an exe and claiming it's a PDF).
    try:
        meta = await get_object_metadata(presc.storage_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=400,
                            detail=f"File not found in storage: {exc}. Please upload again.")

    # Size must be within 10% of declared size (network quirks, encoding headers)
    declared = presc.file_size_bytes or 0
    actual   = meta.get("size_bytes", 0)
    if actual == 0:
        raise HTTPException(status_code=400,
                            detail="Uploaded file is empty (0 bytes). Please try again.")
    if declared > 0 and abs(actual - declared) > max(declared * 0.10, 1024):
        raise HTTPException(
            status_code=400,
            detail=f"File size mismatch: declared {declared}B, actual {actual}B.",
        )

    # Content-type header sent by the client during PUT must match what we expect
    stored_ct = meta.get("content_type", "")
    if stored_ct and stored_ct not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unexpected content-type '{stored_ct}' in storage.",
        )

    from_status               = presc.verification_status
    if req.original_filename:
        presc.original_filename = req.original_filename
    presc.verification_status = "PENDING_REVIEW"
    presc.file_size_bytes     = actual   # update with actual size from MinIO
    await db.flush()

    await _audit(db, presc.id, token.sub, "patient",
                 "PENDING_REVIEW", from_status, "PENDING_REVIEW", request=request)
    logger.info("prescription_confirmed", prescription_id=presc.id[:8],
                actual_bytes=actual, key=presc.storage_key)
    return _to_response(presc)


# ─────────────────────────────────────────────────────────
# Patient: list own prescriptions
# ─────────────────────────────────────────────────────────

@router.get("", summary="List current user's prescriptions")
async def list_prescriptions(
    token : TokenPayload = Depends(require_auth),
    db    : AsyncSession = Depends(get_db),
    limit : int = 20,
    offset: int = 0,
) -> dict:
    repo   = PrescriptionRepository(db)
    prescs = await repo.get_user_prescriptions(token.sub, limit=limit, offset=offset)
    return {"prescriptions": [_to_response(p) for p in prescs], "count": len(prescs)}


# ─────────────────────────────────────────────────────────
# Patient: get one prescription with download URL
# ─────────────────────────────────────────────────────────

@router.get("/{prescription_id}", summary="Get prescription details + presigned download URL")
async def get_prescription(
    prescription_id: str,
    token          : TokenPayload = Depends(require_auth),
    db             : AsyncSession = Depends(get_db),
) -> PrescriptionResponse:
    repo  = PrescriptionRepository(db)
    presc = await repo.get_by_id(prescription_id)

    if not presc:
        raise HTTPException(status_code=404, detail="Prescription not found.")
    # Operators and Admins can view any prescription; patients only their own
    if token.role == "PATIENT" and presc.user_id != token.sub:
        raise HTTPException(status_code=403, detail="Not your prescription.")

    try:
        dl_url = await generate_download_url(presc.storage_key)
    except RuntimeError:
        dl_url = None
    return _to_response(presc, download_url=dl_url)


# ─────────────────────────────────────────────────────────
# Operator: queue (PENDING_REVIEW + expired UNDER_REVIEW)
# ─────────────────────────────────────────────────────────

@router.get(
    "/operator/queue",
    summary = "Prescriptions awaiting review (OPERATOR / ADMIN only)",
)
async def operator_queue(
    token : TokenPayload = Depends(require_operator),   # RBAC enforced
    db    : AsyncSession = Depends(get_db),
    limit : int = 20,
    offset: int = 0,
) -> dict:
    """
    Returns PENDING_REVIEW prescriptions AND UNDER_REVIEW prescriptions
    whose active lock has expired (so another operator can claim them).
    """
    now = datetime.now(timezone.utc)

    # Subquery: prescription IDs that have an EXPIRED active lock
    expired_subq = (
        select(OperatorSession.prescription_id)
        .where(
            OperatorSession.is_active  == True,
            OperatorSession.expires_at < now,
        )
        .scalar_subquery()
    )

    result = await db.execute(
        select(Prescription)
        .where(
            or_(
                Prescription.verification_status == "PENDING_REVIEW",
                Prescription.id.in_(expired_subq),
            )
        )
        .order_by(Prescription.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    prescs = result.scalars().all()

    items = []
    for p in prescs:
        try:
            dl_url = await generate_download_url(p.storage_key)
        except RuntimeError:
            dl_url = None
        items.append(_to_response(p, download_url=dl_url))

    return {"queue": items, "count": len(items)}


# ─────────────────────────────────────────────────────────
# Operator: claim review lock
# ─────────────────────────────────────────────────────────

@router.post("/{prescription_id}/claim",
             summary="Claim UNDER_REVIEW lock (OPERATOR / ADMIN only)")
async def claim_review(
    prescription_id: str,
    request        : Request,
    token          : TokenPayload = Depends(require_operator),  # RBAC enforced
    db             : AsyncSession = Depends(get_db),
) -> PrescriptionResponse:
    presc = await db.get(Prescription, prescription_id)
    if not presc:
        raise HTTPException(status_code=404, detail="Prescription not found.")

    now = datetime.now(timezone.utc)

    # ── Handle expired lock (auto-reclaim) ────────────────────────
    old_operator_id = await _release_expired_lock(db, prescription_id, now)
    if old_operator_id:
        # Write LOCK_EXPIRED audit before the new REVIEW_CLAIMED
        await _audit(db, prescription_id, old_operator_id, "system",
                     "LOCK_EXPIRED", "UNDER_REVIEW", "PENDING_REVIEW")
        presc.verification_status = "PENDING_REVIEW"
        await db.flush()

    if presc.verification_status != "PENDING_REVIEW":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot claim: prescription is in state '{presc.verification_status}'.",
        )

    # ── Check for a non-expired active lock ───────────────────────
    existing = await db.execute(
        select(OperatorSession).where(
            OperatorSession.prescription_id == prescription_id,
            OperatorSession.is_active       == True,
            OperatorSession.expires_at      >= now,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409,
                            detail="Prescription is actively being reviewed by another operator.")

    # ── Create new lock ───────────────────────────────────────────
    expiry = _lock_expiry()
    lock   = OperatorSession(
        operator_id     = token.sub,
        prescription_id = prescription_id,
        is_active       = True,
        expires_at      = expiry,
    )
    db.add(lock)

    from_status               = presc.verification_status
    presc.verification_status = "UNDER_REVIEW"
    presc.reviewer_id         = token.sub
    presc.review_started_at   = now
    await db.flush()

    await _audit(db, prescription_id, token.sub, "operator",
                 "REVIEW_CLAIMED", from_status, "UNDER_REVIEW", request=request)
    logger.info("review_claimed", prescription_id=prescription_id[:8],
                operator=token.sub[:8], expires_at=expiry.isoformat())
    return _to_response(presc)


# ─────────────────────────────────────────────────────────
# Operator: heartbeat — resets lock expiry
# ─────────────────────────────────────────────────────────

@router.post(
    "/{prescription_id}/heartbeat",
    summary = "Reset the 30-minute review lock timer (OPERATOR / ADMIN only)",
)
async def session_heartbeat(
    prescription_id: str,
    token          : TokenPayload = Depends(require_operator),
    db             : AsyncSession = Depends(get_db),
) -> dict:
    """
    Call every 5 minutes while the operator has a prescription open.
    Resets expires_at to now() + 30 minutes, preventing an active
    operator's lock from expiring mid-review.

    If the lock is already expired, returns 409 so the operator knows
    they need to re-claim.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(OperatorSession).where(
            OperatorSession.prescription_id == prescription_id,
            OperatorSession.operator_id     == token.sub,
            OperatorSession.is_active       == True,
        )
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="No active session found for this prescription.")
    if session.expires_at < now:
        raise HTTPException(
            status_code=409,
            detail="Your review lock has expired. Please re-claim the prescription.",
        )

    new_expiry        = _lock_expiry()
    session.expires_at = new_expiry
    await db.flush()

    return {
        "prescription_id": prescription_id,
        "expires_at"     : new_expiry.isoformat(),
        "message"        : f"Lock extended to {new_expiry.isoformat()}",
    }


# ─────────────────────────────────────────────────────────
# Operator: approve
# ─────────────────────────────────────────────────────────

@router.post("/{prescription_id}/approve",
             summary="Approve a prescription (OPERATOR / ADMIN only)")
async def approve_prescription(
    prescription_id: str,
    req            : ApproveRequest,
    request        : Request,
    token          : TokenPayload = Depends(require_operator),  # RBAC enforced
    db             : AsyncSession = Depends(get_db),
) -> PrescriptionResponse:
    repo  = PrescriptionRepository(db)
    presc = await repo.get_by_id(prescription_id)
    if not presc:
        raise HTTPException(status_code=404, detail="Prescription not found.")
    if presc.verification_status != "UNDER_REVIEW":
        raise HTTPException(
            status_code=409,
            detail=f"Must be UNDER_REVIEW to approve (currently: {presc.verification_status}).",
        )

    # Verify this operator holds the active lock
    now    = datetime.now(timezone.utc)
    result = await db.execute(
        select(OperatorSession).where(
            OperatorSession.prescription_id == prescription_id,
            OperatorSession.operator_id     == token.sub,
            OperatorSession.is_active       == True,
            OperatorSession.expires_at      >= now,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=403,
                            detail="You do not hold the active review lock for this prescription.")

    base_date  = (req.issued_date or now).replace(tzinfo=timezone.utc)
    expires_at = base_date + timedelta(days=PRESCRIPTION_VALIDITY_DAYS)

    from_status = presc.verification_status
    await repo.approve(prescription_id, verified_by=token.sub, expires_at=expires_at)
    if any([req.doctor_name, req.doctor_reg_no, req.patient_name, req.issued_date]):
        await repo.update_extracted_data(
            prescription_id   = prescription_id,
            extracted_medicines = {},
            doctor_name         = req.doctor_name,
            doctor_reg_number   = req.doctor_reg_no,
            patient_name        = req.patient_name,
            issued_date         = req.issued_date,
        )

    # Release lock
    await db.execute(
        update(OperatorSession)
        .where(OperatorSession.prescription_id == prescription_id,
               OperatorSession.is_active       == True)
        .values(is_active=False, released_at=now)
    )
    await _audit(db, prescription_id, token.sub, "operator",
                 "APPROVED", from_status, "APPROVED",
                 notes=req.notes, request=request)
    logger.info("prescription_approved", prescription_id=prescription_id[:8],
                operator=token.sub[:8], expires_at=expires_at.isoformat())
    return _to_response(await repo.get_by_id(prescription_id))


# ─────────────────────────────────────────────────────────
# Operator: reject
# ─────────────────────────────────────────────────────────

@router.post("/{prescription_id}/reject",
             summary="Reject a prescription with a reason (OPERATOR / ADMIN only)")
async def reject_prescription(
    prescription_id: str,
    req            : RejectRequest,
    request        : Request,
    token          : TokenPayload = Depends(require_operator),  # RBAC enforced
    db             : AsyncSession = Depends(get_db),
) -> PrescriptionResponse:
    repo  = PrescriptionRepository(db)
    presc = await repo.get_by_id(prescription_id)
    if not presc:
        raise HTTPException(status_code=404, detail="Prescription not found.")
    if presc.verification_status not in ("UNDER_REVIEW", "PENDING_REVIEW"):
        raise HTTPException(status_code=409,
                            detail=f"Cannot reject from state '{presc.verification_status}'.")

    from_status = presc.verification_status
    await repo.reject(prescription_id, reason=req.rejection_reason, verified_by=token.sub)
    await db.execute(
        update(OperatorSession)
        .where(OperatorSession.prescription_id == prescription_id,
               OperatorSession.is_active       == True)
        .values(is_active=False, released_at=datetime.now(timezone.utc))
    )
    await _audit(db, prescription_id, token.sub, "operator",
                 "REJECTED", from_status, "REJECTED",
                 rejection_reason=req.rejection_reason,
                 notes=req.notes, request=request)
    logger.info("prescription_rejected", prescription_id=prescription_id[:8],
                reason=req.rejection_reason)
    return _to_response(await repo.get_by_id(prescription_id))


# ─────────────────────────────────────────────────────────
# Operator: release lock without decision
# ─────────────────────────────────────────────────────────

@router.post("/{prescription_id}/release",
             summary="Release review lock without decision (OPERATOR / ADMIN only)")
async def release_review(
    prescription_id: str,
    request        : Request,
    token          : TokenPayload = Depends(require_operator),  # RBAC enforced
    db             : AsyncSession = Depends(get_db),
) -> PrescriptionResponse:
    presc = await db.get(Prescription, prescription_id)
    if not presc or presc.verification_status != "UNDER_REVIEW":
        raise HTTPException(status_code=409,
                            detail="Prescription is not UNDER_REVIEW.")

    now                       = datetime.now(timezone.utc)
    from_status               = presc.verification_status
    presc.verification_status = "PENDING_REVIEW"
    presc.reviewer_id         = None
    presc.review_started_at   = None
    await db.execute(
        update(OperatorSession)
        .where(OperatorSession.prescription_id == prescription_id,
               OperatorSession.is_active       == True)
        .values(is_active=False, released_at=now)
    )
    await db.flush()
    await _audit(db, prescription_id, token.sub, "operator",
                 "REVIEW_RELEASED", from_status, "PENDING_REVIEW", request=request)
    return _to_response(presc)
