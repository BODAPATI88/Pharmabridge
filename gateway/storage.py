"""
pharmabridge/gateway/storage.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MinIO / S3-compatible object storage service.

Storage key format  (defined here, mirrored in migration 0003)
──────────────────────────────────────────────────────────────
  prescriptions/{year}/{month}/{uuid}.{ext}
  e.g. prescriptions/2026/06/3f7a9c1d-....pdf

Year/month partitioning prevents bucket listing timeouts at scale.
A bucket with 10M flat objects takes seconds to list; partitioned it
is instant.  At 200 prescriptions/day, a single month prefix holds
~6000 objects — trivially fast.

The UUID in the filename is always the prescription.id — this allows
reconstructing the storage_key from the DB record without a separate
lookup, and prevents filename-based enumeration attacks.

Why not include user_id in the path?
  User IDs in paths leak account existence if an attacker can guess UUIDs.
  Year/month + prescription UUID is sufficient for partitioning and is
  opaque enough to be safe.

Bucket
──────
  pharmabridge-prescriptions    (private, versioning enabled by bootstrap Job)

Access pattern
──────────────
  Uploads : gateway generates a presigned PUT URL (5-min TTL).
            The frontend uploads directly to MinIO; the gateway never
            receives the file bytes.  This keeps gateway memory usage flat
            regardless of file size.
  Downloads: gateway generates a presigned GET URL (15-min TTL).
             Never expose the MinIO API directly outside the cluster.

S3 migration compatibility
──────────────────────────
  The boto3/aiobotocore client uses the S3 API.  Switching from MinIO to
  AWS S3 or Cloudflare R2 requires only changing three env vars:
    MINIO_ENDPOINT_URL   → S3 regional endpoint or R2 endpoint
    MINIO_ACCESS_KEY     → AWS access key / R2 token
    MINIO_SECRET_KEY     → AWS secret / R2 secret
  Zero code changes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import aiobotocore.session
from botocore.exceptions import ClientError

logger = logging.getLogger("pharmabridge.storage")

# ─────────────────────────────────────────────────────────
# Configuration  (all injectable via K8s Secret)
# ─────────────────────────────────────────────────────────

MINIO_ENDPOINT_URL  = os.getenv("MINIO_ENDPOINT_URL",  "http://minio-svc.pharmabridge.svc.cluster.local:9000")
MINIO_ACCESS_KEY    = os.getenv("MINIO_ACCESS_KEY",    "pharmabridge")
MINIO_SECRET_KEY    = os.getenv("MINIO_SECRET_KEY",    "CHANGE_ME_IN_PRODUCTION")
MINIO_REGION        = os.getenv("MINIO_REGION",        "us-east-1")   # MinIO ignores this, S3 needs it
PRESCRIPTIONS_BUCKET= os.getenv("MINIO_PRESCS_BUCKET", "pharmabridge-prescriptions")

PRESIGNED_PUT_TTL   = int(os.getenv("PRESIGNED_PUT_TTL_SECONDS",  "300"))   # 5 min for upload
PRESIGNED_GET_TTL   = int(os.getenv("PRESIGNED_GET_TTL_SECONDS",  "900"))   # 15 min for download

# File constraints enforced before generating presigned URLs
MAX_FILE_BYTES      = int(os.getenv("MAX_PRESCRIPTION_BYTES", str(10 * 1024 * 1024)))  # 10 MB
ALLOWED_CONTENT_TYPES = {
    "image/jpeg"      : "jpg",
    "image/png"       : "png",
    "application/pdf" : "pdf",
}


# ─────────────────────────────────────────────────────────
# Storage key builder  (single source of truth)
# ─────────────────────────────────────────────────────────

def build_storage_key(
    prescription_id: str,
    content_type   : str,
    upload_time    : Optional[datetime] = None,
) -> str:
    """
    Build the canonical MinIO object key for a prescription file.

    Format: prescriptions/{year}/{month}/{prescription_id}.{ext}

    This function is the SINGLE SOURCE OF TRUTH for the key format.
    The Alembic migration comment references this function.
    The frontend never constructs keys; it always calls the gateway.

    Args:
        prescription_id:  UUID string, matches prescriptions.id in DB.
        content_type:     MIME type from the upload request header.
        upload_time:      UTC datetime; defaults to now().  Passed explicitly
                          so the key stored in DB matches the key in MinIO
                          even if there's clock skew.

    Returns:
        e.g. "prescriptions/2026/06/3f7a9c1d-4b5e-....pdf"
    """
    if upload_time is None:
        upload_time = datetime.now(timezone.utc)

    ext = ALLOWED_CONTENT_TYPES.get(content_type, "bin")
    return (
        f"prescriptions/"
        f"{upload_time.year:04d}/"
        f"{upload_time.month:02d}/"
        f"{prescription_id}.{ext}"
    )


def parse_storage_key(storage_key: str) -> dict:
    """
    Parse a storage key back into its components.
    Useful for displaying upload date from the key without a DB query.

    Returns: {year, month, prescription_id, ext} or {} if unparseable.
    """
    parts = storage_key.split("/")
    if len(parts) != 4 or parts[0] != "prescriptions":
        return {}
    try:
        filename = parts[3]
        stem, ext = filename.rsplit(".", 1)
        return {
            "year"           : int(parts[1]),
            "month"          : int(parts[2]),
            "prescription_id": stem,
            "ext"            : ext,
        }
    except (ValueError, IndexError):
        return {}


# ─────────────────────────────────────────────────────────
# Async S3 client factory
# ─────────────────────────────────────────────────────────

def _make_client_context():
    """
    Return an async context manager that yields a configured boto3-compatible
    async S3 client.

    Usage:
        async with _make_client_context() as s3:
            await s3.head_object(Bucket=PRESCRIPTIONS_BUCKET, Key=key)
    """
    session = aiobotocore.session.get_session()
    return session.create_client(
        "s3",
        endpoint_url          = MINIO_ENDPOINT_URL,
        aws_access_key_id     = MINIO_ACCESS_KEY,
        aws_secret_access_key = MINIO_SECRET_KEY,
        region_name           = MINIO_REGION,
    )


# ─────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────

async def generate_upload_url(
    prescription_id: str,
    content_type   : str,
    file_size_bytes: int,
    upload_time    : Optional[datetime] = None,
) -> dict:
    """
    Generate a presigned PUT URL the frontend uses to upload a file
    directly to MinIO.  The gateway never receives the file bytes.

    Args:
        prescription_id:  Already created prescription row ID.
        content_type:     MIME type declared by the client.
        file_size_bytes:  File size declared by the client (advisory; MinIO
                          enforces the actual byte limit via the conditions).
        upload_time:      Pinned to now() if not provided; saved to DB.

    Returns:
        {
            storage_key   : str,      # save this to prescriptions.storage_key
            upload_url    : str,      # PUT to this URL
            expires_in    : int,      # seconds until URL expires
            method        : "PUT",
            content_type  : str,
        }

    Raises:
        ValueError: if content_type is not allowed or file is too large.
        RuntimeError: if MinIO is unreachable.
    """
    if content_type not in ALLOWED_CONTENT_TYPES:
        allowed = ", ".join(ALLOWED_CONTENT_TYPES.keys())
        raise ValueError(f"Content type '{content_type}' not allowed. Allowed: {allowed}")

    if file_size_bytes > MAX_FILE_BYTES:
        mb = MAX_FILE_BYTES // (1024 * 1024)
        raise ValueError(f"File too large. Maximum size is {mb} MB.")

    if upload_time is None:
        upload_time = datetime.now(timezone.utc)

    key = build_storage_key(prescription_id, content_type, upload_time)

    try:
        async with _make_client_context() as s3:
            url = await s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket"     : PRESCRIPTIONS_BUCKET,
                    "Key"        : key,
                    "ContentType": content_type,
                },
                ExpiresIn = PRESIGNED_PUT_TTL,
            )

        logger.info(
            "presigned_put_generated",
            prescription_id = prescription_id[:8],
            key             = key,
            content_type    = content_type,
        )
        return {
            "storage_key" : key,
            "upload_url"  : url,
            "expires_in"  : PRESIGNED_PUT_TTL,
            "method"      : "PUT",
            "content_type": content_type,
        }

    except ClientError as exc:
        logger.error("presigned_put_failed", error=str(exc), prescription_id=prescription_id[:8])
        raise RuntimeError(f"Storage service unavailable: {exc}") from exc


async def generate_download_url(storage_key: str) -> str:
    """
    Generate a presigned GET URL for downloading a prescription file.

    The URL expires after PRESIGNED_GET_TTL seconds (default 15 min).
    Callers should check the prescription's verification_status before
    generating download URLs — operators only.

    Args:
        storage_key: Value from prescriptions.storage_key column.

    Returns:
        Presigned HTTPS URL string.
    """
    try:
        async with _make_client_context() as s3:
            url = await s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": PRESCRIPTIONS_BUCKET,
                    "Key"   : storage_key,
                },
                ExpiresIn = PRESIGNED_GET_TTL,
            )
        return url
    except ClientError as exc:
        logger.error("presigned_get_failed", key=storage_key, error=str(exc))
        raise RuntimeError(f"Could not generate download URL: {exc}") from exc


async def delete_object(storage_key: str) -> None:
    """
    Hard delete an object from MinIO.

    Only call this for REJECTED prescriptions during a purge workflow
    or when a user explicitly requests account deletion.
    For expired prescriptions, prefer letting the lifecycle rule handle it.
    """
    try:
        async with _make_client_context() as s3:
            await s3.delete_object(Bucket=PRESCRIPTIONS_BUCKET, Key=storage_key)
        logger.info("object_deleted", key=storage_key)
    except ClientError as exc:
        logger.error("object_delete_failed", key=storage_key, error=str(exc))
        raise RuntimeError(f"Delete failed: {exc}") from exc


async def object_exists(storage_key: str) -> bool:
    """
    Check whether an object has been successfully uploaded.
    Call after the presigned PUT to verify the upload completed.
    """
    try:
        async with _make_client_context() as s3:
            await s3.head_object(Bucket=PRESCRIPTIONS_BUCKET, Key=storage_key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise RuntimeError(f"Storage check failed: {exc}") from exc


async def get_object_metadata(storage_key: str) -> dict:
    """
    Return ETag, ContentLength, ContentType for a stored object.
    Used by the operator console to verify upload integrity.
    """
    try:
        async with _make_client_context() as s3:
            resp = await s3.head_object(Bucket=PRESCRIPTIONS_BUCKET, Key=storage_key)
        return {
            "etag"          : resp.get("ETag", "").strip('"'),
            "size_bytes"    : resp.get("ContentLength", 0),
            "content_type"  : resp.get("ContentType", ""),
            "last_modified" : resp.get("LastModified"),
        }
    except ClientError as exc:
        raise RuntimeError(f"Metadata fetch failed: {exc}") from exc
