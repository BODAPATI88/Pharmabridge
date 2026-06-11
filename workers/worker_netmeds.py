"""
pharmabridge/workers/worker_netmeds.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PharmaBridge  ·  Netmeds Scraper Worker

Netmeds strategy
────────────────
Netmeds serves a static HTML product listing that BeautifulSoup can parse
without Playwright, making this the lightest worker.

Search URL:
  https://www.netmeds.com/catalogsearch/result?q={medicine_name}

Key CSS selectors (as of 2025):
  Product cards : div.cat-item
  Brand name    : h3.clsgetname
  Price         : span.final-price
  MRP           : span.price-del
  Manufacturer  : span.mfr-name   (on the product detail page only)
  Schedule      : span.rx-label
  Pack size     : span.pack-size
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from worker_base import BaseWorker
from models import ScheduleClass, VendorMedicineResult, VendorName

logger = logging.getLogger("pharmabridge.worker.netmeds")

USE_MOCK          = os.getenv("USE_MOCK", "true").lower() == "true"
REQUEST_TIMEOUT_S = int(os.getenv("REQUEST_TIMEOUT_MS", "12000")) / 1000

MOCK_CATALOGUE: dict[str, dict] = {
    "metformin": {
        "brand_name"  : "Metformin 500mg Tablet",
        "generic_name": "Metformin Hydrochloride",
        "manufacturer": "Cipla Ltd",
        "mrp"         : 42.50,
        "price"       : 36.90,
        "strip_size"  : 15,
        "schedule"    : ScheduleClass.H,
        "requires_rx" : True,
    },
    "atorvastatin": {
        "brand_name"  : "Atorvastatin 10mg Tablet",
        "generic_name": "Atorvastatin",
        "manufacturer": "Sun Pharma",
        "mrp"         : 89.00,
        "price"       : 68.53,   # Best price across all three vendors
        "strip_size"  : 10,
        "schedule"    : ScheduleClass.H,
        "requires_rx" : True,
    },
    "amlodipine": {
        "brand_name"  : "Amlodipine 5mg Tablet",
        "generic_name": "Amlodipine",
        "manufacturer": "Cipla Ltd",
        "mrp"         : 55.00,
        "price"       : 46.75,
        "strip_size"  : 15,
        "schedule"    : ScheduleClass.H,
        "requires_rx" : True,
    },
    "pantoprazole": {
        "brand_name"  : "Pantoprazole 40mg Tablet",
        "generic_name": "Pantoprazole",
        "manufacturer": "Cipla Ltd",
        "mrp"         : 65.00,
        "price"       : 54.00,
        "strip_size"  : 15,
        "schedule"    : ScheduleClass.H,
        "requires_rx" : True,
    },
    "cetirizine": {
        "brand_name"  : "Cetirizine 10mg Tablet",
        "generic_name": "Cetirizine",
        "manufacturer": "Cipla Ltd",
        "mrp"         : 28.00,
        "price"       : 21.00,   # Best price
        "strip_size"  : 10,
        "schedule"    : ScheduleClass.OTC,
        "requires_rx" : False,
    },
    "paracetamol": {
        "brand_name"  : "Paracetamol 500mg Tablet",
        "generic_name": "Paracetamol",
        "manufacturer": "Cipla Ltd",
        "mrp"         : 22.00,
        "price"       : 18.70,
        "strip_size"  : 15,
        "schedule"    : ScheduleClass.OTC,
        "requires_rx" : False,
    },
}

# Mimic a real browser to avoid 403s
HEADERS = {
    "User-Agent"     : "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept"         : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


class WorkerNetmeds(BaseWorker):
    vendor = VendorName.NETMEDS

    async def scrape(
        self,
        medicines    : list[dict],
        pincode      : str,
        prefer_generic: bool,
    ) -> list[VendorMedicineResult]:
        if USE_MOCK:
            logger.info("[netmeds] Running in MOCK mode")
            return await self._mock_scrape(medicines)
        else:
            return await self._bs4_scrape(medicines)

    async def _mock_scrape(self, medicines: list[dict]) -> list[VendorMedicineResult]:
        results = []
        for med in medicines:
            norm = med["name"].strip().lower()
            key  = next((k for k in MOCK_CATALOGUE if k in norm or norm in k), None)
            if not key:
                continue

            cat    = MOCK_CATALOGUE[key]
            jitter = random.uniform(0.96, 1.04)
            price  = round(cat["price"] * jitter, 2)
            mrp    = cat["mrp"]
            strip  = cat["strip_size"]

            await asyncio.sleep(random.uniform(0.04, 0.12))

            results.append(VendorMedicineResult(
                medicine_name   = med["name"],
                vendor          = VendorName.NETMEDS,
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
                product_url     = f"https://www.netmeds.com/prescriptions/{key}",
                scraped_at      = datetime.now(timezone.utc),
            ))
        return results

    async def _bs4_scrape(self, medicines: list[dict]) -> list[VendorMedicineResult]:
        """HTTP + BeautifulSoup scraper – no browser required."""
        results = []

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, headers=HEADERS,
                                      follow_redirects=True) as client:
            for med in medicines:
                try:
                    url  = "https://www.netmeds.com/catalogsearch/result"
                    resp = await client.get(url, params={"q": med["name"]})
                    resp.raise_for_status()

                    result = self._parse_html(med["name"], resp.text)
                    if result:
                        results.append(result)

                    await asyncio.sleep(random.uniform(1.5, 3.5))

                except Exception as exc:
                    logger.warning("[netmeds] Scrape failed for '%s': %s", med["name"], exc)

        return results

    def _parse_html(
        self,
        medicine_name: str,
        html         : str,
    ) -> Optional[VendorMedicineResult]:
        """Parse Netmeds search result HTML with BeautifulSoup."""
        soup = BeautifulSoup(html, "html.parser")

        # First product card
        card = soup.select_one("div.cat-item")
        if not card:
            return None

        try:
            brand = card.select_one("h3.clsgetname")
            brand_name = brand.get_text(strip=True) if brand else medicine_name
        except Exception:
            brand_name = medicine_name

        try:
            price_tag = card.select_one("span.final-price")
            price_str = re.sub(r"[^\d.]", "", price_tag.get_text()) if price_tag else ""
            price     = float(price_str) if price_str else 0.0
        except Exception:
            return None

        if price <= 0:
            return None

        try:
            mrp_tag = card.select_one("span.price-del")
            mrp_str = re.sub(r"[^\d.]", "", mrp_tag.get_text()) if mrp_tag else ""
            mrp     = float(mrp_str) if mrp_str else price
        except Exception:
            mrp = price

        try:
            pack_tag   = card.select_one("span.pack-size")
            pack_text  = pack_tag.get_text(strip=True) if pack_tag else "10"
            strip_size = int(re.search(r"\d+", pack_text).group()) if re.search(r"\d+", pack_text) else 10
        except Exception:
            strip_size = 10

        try:
            rx_tag    = card.select_one("span.rx-label")
            rx_text   = rx_tag.get_text(strip=True).upper() if rx_tag else ""
            schedule  = ScheduleClass.H if rx_text else ScheduleClass.OTC
            requires_rx = bool(rx_text)
        except Exception:
            schedule    = ScheduleClass.UNKNOWN
            requires_rx = False

        try:
            link = card.select_one("a[href]")
            url  = link["href"] if link else None
            if url and not url.startswith("http"):
                url = "https://www.netmeds.com" + url
        except Exception:
            url = None

        return VendorMedicineResult(
            medicine_name   = medicine_name,
            vendor          = VendorName.NETMEDS,
            brand_name      = brand_name,
            generic_name    = None,
            manufacturer    = None,
            price_per_unit  = round(price / strip_size, 4),
            price_per_strip = price,
            strip_size      = strip_size,
            mrp             = mrp,
            discount_pct    = round((1 - price / mrp) * 100, 1) if mrp > 0 else 0.0,
            in_stock        = True,
            schedule        = schedule,
            requires_rx     = requires_rx,
            product_url     = url,
            scraped_at      = datetime.now(timezone.utc),
        )


if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(level=_log.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
    worker = WorkerNetmeds()
    asyncio.run(worker.run())
