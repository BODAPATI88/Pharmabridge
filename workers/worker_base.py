"""
pharmabridge/workers/worker_base.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Abstract base class for all PharmaBridge scraper workers.

Each concrete worker (worker_1mg.py, worker_pharmeasy.py, etc.) inherits
this class and only needs to implement:
  • `scrape(medicines, pincode, prefer_generic)  → list[VendorMedicineResult]`

Everything else – Redis listening, heartbeat publishing, structured error
handling, result serialisation, exponential-backoff retry – is handled here.

Worker lifecycle
────────────────
1. Worker pod starts, calls `run()`.
2. Enters a BLPOP loop on `pb:queue:{vendor}` with a 30 s timeout.
3. On each job: deserialise, call `scrape()`, serialise result back to
   `pb:task:{task_id}:result:{vendor}` with RESULT_TTL_S expiry.
4. Every HEARTBEAT_INTERVAL_S seconds, publishes a WorkerHeartbeat JSON
   to `pb:heartbeat:{vendor}` with HEARTBEAT_TTL_S expiry.
5. On SIGTERM, finishes current job then exits cleanly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

# ── Shared model definitions are duplicated here as dicts to avoid
#    a circular-import between the gateway package and worker package.
#    In production you'd extract models.py into a shared `pb_common` pip
#    package; for this monorepo layout we re-import from gateway via PYTHONPATH.
import sys
sys.path.insert(0, "/app/gateway")   # set by Dockerfile WORKDIR / K8s volume mount

from models import (
    ScheduleClass,
    TaskStatus,
    VendorBatchResult,
    VendorMedicineResult,
    VendorName,
    WorkerHeartbeat,
)

logger = logging.getLogger("pharmabridge.worker")

# ─────────────────────────────────────────────────────────
# Config from environment
# ─────────────────────────────────────────────────────────

REDIS_URL            = os.getenv("REDIS_URL",              "redis://redis-svc:6379/0")
RESULT_TTL_S         = int(os.getenv("RESULT_TTL_S",        "3600"))
HEARTBEAT_TTL_S      = int(os.getenv("HEARTBEAT_TTL_S",     "90"))
HEARTBEAT_INTERVAL_S = int(os.getenv("HEARTBEAT_INTERVAL_S","30"))
MAX_RETRIES          = int(os.getenv("WORKER_MAX_RETRIES",  "3"))
BACKOFF_BASE_S       = float(os.getenv("WORKER_BACKOFF_BASE","2.0"))
POD_NAME             = os.getenv("POD_NAME",               "unknown-pod")


# ─────────────────────────────────────────────────────────
# Base worker
# ─────────────────────────────────────────────────────────

class BaseWorker(ABC):
    """
    Inherit this class and implement `scrape()` to create a vendor worker.

    Attributes
    ──────────
    vendor          The VendorName enum value this worker handles.
    queue_key       Redis list key this worker BLPOPs from.
    """

    vendor: VendorName  # Must be set in concrete subclass as class attribute

    def __init__(self) -> None:
        self._redis   : Optional[aioredis.Redis] = None
        self._running : bool = True
        self._tasks_done: int = 0
        self._start_time: float = time.monotonic()

        # Register graceful shutdown handler
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_shutdown)

    @property
    def queue_key(self) -> str:
        return f"pb:queue:{self.vendor.value}"

    # ── Abstract interface ──────────────────────────────────

    @abstractmethod
    async def scrape(
        self,
        medicines    : list[dict],   # List of MedicineItem dicts
        pincode      : str,
        prefer_generic: bool,
    ) -> list[VendorMedicineResult]:
        """
        Perform the actual scraping / API call for this vendor.

        Parameters
        ──────────
        medicines
            List of dicts matching MedicineItem schema (name, quantity,
            strength, dosage_form).
        pincode
            6-digit Indian PIN code for stock/price locality.
        prefer_generic
            If True, the scraper should prefer generic drug listings
            over branded ones where available.

        Returns
        ───────
        List of VendorMedicineResult – one entry per medicine found.
        Missing medicines simply have no entry in the list.

        Raises
        ──────
        Any exception is caught by the base class retry loop and converted
        into a FAILED VendorBatchResult.
        """
        ...

    # ── Main run loop ───────────────────────────────────────

    async def run(self) -> None:
        """Entry point.  Call `await worker.run()` from your script's main."""
        logger.info("[%s] Worker starting on pod %s", self.vendor.value, POD_NAME)
        self._redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True, max_connections=10
        )

        # Start heartbeat publisher as a background task
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            await self._consume_loop()
        finally:
            heartbeat_task.cancel()
            await self._redis.aclose()
            logger.info("[%s] Worker shutdown complete. Tasks done: %d",
                        self.vendor.value, self._tasks_done)

    async def _consume_loop(self) -> None:
        """BLPOP loop – blocks for up to 30 s waiting for a job."""
        logger.info("[%s] Listening on queue: %s", self.vendor.value, self.queue_key)

        while self._running:
            try:
                # BLPOP returns (key, value) tuple or None on timeout
                result = await self._redis.blpop(self.queue_key, timeout=30)
                if result is None:
                    continue  # timeout – loop back and check self._running

                _, raw_job = result
                job = json.loads(raw_job)
                await self._handle_job(job)

            except aioredis.ConnectionError as exc:
                logger.error("[%s] Redis connection error: %s – retrying in 5 s", self.vendor.value, exc)
                await asyncio.sleep(5)
            except Exception as exc:
                logger.error("[%s] Unexpected error in consume loop: %s", self.vendor.value, exc, exc_info=True)
                await asyncio.sleep(1)

    async def _handle_job(self, job: dict) -> None:
        """Process a single job dict with retry logic."""
        task_id    = job["task_id"]
        medicines  = job["medicines"]        # list[dict]
        pincode    = job["pincode"]
        prefer_gen = job.get("prefer_generic", False)

        logger.info("[%s] Processing task %s (%d medicines, PIN %s)",
                    self.vendor.value, task_id, len(medicines), pincode)

        start_ms = time.monotonic() * 1000
        last_exc : Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                results = await self.scrape(medicines, pincode, prefer_gen)
                elapsed = time.monotonic() * 1000 - start_ms

                batch = VendorBatchResult(
                    task_id     = task_id,
                    vendor      = self.vendor,
                    status      = TaskStatus.DONE,
                    results     = results,
                    duration_ms = round(elapsed, 1),
                )
                await self._publish_result(task_id, batch)
                self._tasks_done += 1
                logger.info("[%s] Task %s done in %.0f ms, %d results.",
                            self.vendor.value, task_id, elapsed, len(results))
                return  # success – exit retry loop

            except Exception as exc:
                last_exc = exc
                backoff = BACKOFF_BASE_S ** attempt
                logger.warning(
                    "[%s] Attempt %d/%d failed for task %s: %s – backing off %.1f s",
                    self.vendor.value, attempt, MAX_RETRIES, task_id, exc, backoff
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(backoff)

        # All retries exhausted
        logger.error("[%s] Task %s FAILED after %d attempts: %s",
                     self.vendor.value, task_id, MAX_RETRIES, last_exc)
        failed_batch = VendorBatchResult(
            task_id   = task_id,
            vendor    = self.vendor,
            status    = TaskStatus.FAILED,
            error_msg = str(last_exc),
        )
        await self._publish_result(task_id, failed_batch)

    async def _publish_result(self, task_id: str, batch: VendorBatchResult) -> None:
        """Write the batch result to Redis so the gateway can collect it."""
        key = f"pb:task:{task_id}:result:{self.vendor.value}"
        await self._redis.set(key, batch.model_dump_json(), ex=RESULT_TTL_S)
        logger.debug("[%s] Published result to %s", self.vendor.value, key)

    async def _heartbeat_loop(self) -> None:
        """Publish a heartbeat every HEARTBEAT_INTERVAL_S seconds."""
        while self._running:
            try:
                hb = WorkerHeartbeat(
                    vendor     = self.vendor,
                    pod_name   = POD_NAME,
                    status     = "idle",   # workers don't track busy state for now
                    tasks_done = self._tasks_done,
                    uptime_s   = round(time.monotonic() - self._start_time, 1),
                    timestamp  = datetime.now(timezone.utc),
                )
                key = f"pb:heartbeat:{self.vendor.value}"
                await self._redis.set(key, hb.model_dump_json(), ex=HEARTBEAT_TTL_S)
            except Exception as exc:
                logger.warning("[%s] Heartbeat publish failed: %s", self.vendor.value, exc)

            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    def _handle_shutdown(self, signum: int, frame) -> None:
        logger.info("[%s] Received signal %d – stopping after current job …",
                    self.vendor.value, signum)
        self._running = False
