"""
pharmabridge/workers/worker_pharmeasy.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PharmaBridge  ·  PharmEasy Scraper Worker

PharmEasy strategy
──────────────────
PharmEasy exposes a semi-public search API endpoint:
  GET https://pharmeasy.in/api/search/medicine?name={query}

The response is JSON (no Playwright needed), making this worker lighter
than the 1mg worker.  In production you must rotate the `x-api-key` header
(or spoof the browser headers) to avoid rate limits.

Selector fallback (if API is blocked):
  Playwright → https://pharmeasy.in/search/all?name={query}
  Product card: div[class*="MedicineCard__"]
  Price:        span[class*="MedicineCard__price"]
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Optional

import httpx

from worker_base import BaseWorker
from models import ScheduleClass, VendorMedicineResult, VendorName

logger = logging.getLogger("pharmabridge.worker.pharmeasy")

USE_MOCK           = os.getenv("USE_MOCK", "true").lower() == "true"
REQUEST_TIMEOUT_S  = int(os.getenv("REQUEST_TIMEOUT_MS", "12000")) / 1000

# PharmEasy prices tend to be slightly different from 1mg – ±8% mock variance
MOCK_CATALOGUE: dict[str, dict] = {
    "metformin": {
        "brand_name"   : "Glucophage 500mg",
        "generic_name" : "Metformin Hydrochloride",
        "manufacturer" : "Abbott India",
        "mrp"          : 42.50,
        "price"        : 33.15,   # Better deal than 1mg on this one
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
    },
    "atorvastatin": {
        "brand_name"   : "Lipitor 10mg",
        "generic_name" : "Atorvastatin",
        "manufacturer" : "Pfizer",
        "mrp"          : 89.00,
        "price"        : 75.65,   # Worse deal than 1mg – Smart-Split picks 1mg
        "strip_size"   : 10,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
    },
    "amlodipine": {
        "brand_name"   : "Amlogard 5mg",
        "generic_name" : "Amlodipine Besylate",
        "manufacturer" : "Pfizer",
        "mrp"          : 55.00,
        "price"        : 41.25,   # Better than 1mg
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
    },
    "pantoprazole": {
        "brand_name"   : "Pantocid 40mg",
        "generic_name" : "Pantoprazole",
        "manufacturer" : "Sun Pharma",
        "mrp"          : 65.00,
        "price"        : 48.75,   # Better than 1mg
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
    },
    "cetirizine": {
        "brand_name"   : "Alerid 10mg",
        "generic_name" : "Cetirizine",
        "manufacturer" : "Cipla",
        "mrp"          : 28.00,
        "price"        : 23.80,   # Slightly worse than 1mg
        "strip_size"   : 10,
        "schedule"     : ScheduleClass.OTC,
        "requires_rx"  : False,
    },
    "paracetamol": {
        "brand_name"   : "Dolo 500",
        "generic_name" : "Paracetamol",
        "manufacturer" : "Micro Labs",
        "mrp"          : 22.00,
        "price"        : 16.50,   # Better than 1mg Crocin
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.OTC,
        "requires_rx"  : False,
    },
}


class WorkerPharmEasy(BaseWorker):
    vendor = VendorName.PHARMEASY

    async def scrape(
        self,
        medicines    : list[dict],
        pincode      : str,
        prefer_generic: bool,
    ) -> list[VendorMedicineResult]:
        if USE_MOCK:
            logger.info("[pharmeasy] Running in MOCK mode")
            return await self._mock_scrape(medicines)
        else:
            return await self._api_scrape(medicines, pincode)

    async def _mock_scrape(self, medicines: list[dict]) -> list[VendorMedicineResult]:
        results = []
        for med in medicines:
            norm = med["name"].strip().lower()
            key  = next((k for k in MOCK_CATALOGUE if k in norm or norm in k), None)
            if not key:
                continue

            cat    = MOCK_CATALOGUE[key]
            jitter = random.uniform(0.93, 1.07)
            price  = round(cat["price"] * jitter, 2)
            mrp    = cat["mrp"]
            strip  = cat["strip_size"]

            await asyncio.sleep(random.uniform(0.05, 0.2))

            results.append(VendorMedicineResult(
                medicine_name   = med["name"],
                vendor          = VendorName.PHARMEASY,
                brand_name      = cat["brand_name"],
                generic_name    = cat.get("generic_name"),
                manufacturer    = cat.get("manufacturer"),
                price_per_unit  = round(price / strip, 4),
                price_per_strip = price,
                strip_size      = strip,
                mrp             = mrp,
                discount_pct    = round((1 - price / mrp) * 100, 1),
                in_stock        = True,
                schedule        = cat["schedule"],
                requires_rx     = cat["requires_rx"],
                product_url     = f"https://pharmeasy.in/online-medicine-order/{key}-123456",
                scraped_at      = datetime.now(timezone.utc),
            ))
        return results

    async def _api_scrape(
        self,
        medicines: list[dict],
        pincode  : str,
    ) -> list[VendorMedicineResult]:
        """
        Uses PharmEasy's internal search JSON API.

        Headers mimic a Chrome browser session.  The `pincode` cookie
        controls local stock availability.
        """
        results = []
        headers = {
            "User-Agent"     : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
            "Accept"         : "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer"        : "https://pharmeasy.in/",
            "Cookie"         : f"userPinCode={pincode}; countryCode=IN",
        }

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, headers=headers) as client:
            for med in medicines:
                try:
                    url  = f"https://pharmeasy.in/api/search/medicine"
                    resp = await client.get(url, params={"name": med["name"]})
                    resp.raise_for_status()
                    data = resp.json()

                    result = self._parse_api_response(med["name"], data)
                    if result:
                        results.append(result)

                    await asyncio.sleep(random.uniform(1.0, 3.0))

                except Exception as exc:
                    logger.warning("[pharmeasy] API call failed for '%s': %s", med["name"], exc)

        return results

    def _parse_api_response(
        self,
        medicine_name: str,
        data         : dict,
    ) -> Optional[VendorMedicineResult]:
        """
        Parse the PharmEasy search API JSON response.

        Response structure (representative):
        {
          "statusCode": 200,
          "data": {
            "medicines": [{
              "name": "...",
              "saltComposition": "...",
              "manufacturer": "...",
              "sellingPrice": 35.70,
              "mrp": 42.50,
              "packForm": "Strip of 15 Tablets",
              "sku": "12345",
              "prescriptionRequired": true,
              "scheduleType": "H"
            }]
          }
        }
        """
        medicines = data.get("data", {}).get("medicines", [])
        if not medicines:
            return None

        item = medicines[0]  # Take the top result

        price = float(item.get("sellingPrice", 0))
        mrp   = float(item.get("mrp", price))
        if price <= 0:
            return None

        # Extract strip size from pack form string  e.g. "Strip of 15 Tablets"
        pack_form  = item.get("packForm", "")
        strip_size = 10
        for token in pack_form.split():
            if token.isdigit():
                strip_size = int(token)
                break

        schedule_raw = item.get("scheduleType", "OTC").upper()
        schedule_map = {
            "H1": ScheduleClass.H1,
            "H" : ScheduleClass.H,
            "X" : ScheduleClass.X,
            "OTC": ScheduleClass.OTC,
        }
        schedule = schedule_map.get(schedule_raw, ScheduleClass.UNKNOWN)
        sku      = item.get("sku", "")

        return VendorMedicineResult(
            medicine_name   = medicine_name,
            vendor          = VendorName.PHARMEASY,
            brand_name      = item.get("name", medicine_name),
            generic_name    = item.get("saltComposition"),
            manufacturer    = item.get("manufacturer"),
            price_per_unit  = round(price / strip_size, 4),
            price_per_strip = price,
            strip_size      = strip_size,
            mrp             = mrp,
            discount_pct    = round((1 - price / mrp) * 100, 1) if mrp > 0 else 0.0,
            in_stock        = True,
            schedule        = schedule,
            requires_rx     = item.get("prescriptionRequired", False),
            product_url     = f"https://pharmeasy.in/online-medicine-order/-{sku}" if sku else None,
            scraped_at      = datetime.now(timezone.utc),
        )


if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(level=_log.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
    worker = WorkerPharmEasy()
    asyncio.run(worker.run())
