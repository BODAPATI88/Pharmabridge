"""
pharmabridge/workers/worker_netmeds.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PharmaBridge  ·  Netmeds Scraper Worker

Netmeds strategy
────────────────
Netmeds' storefront migrated to a Vue/Nuxt SSR shell; the legacy
`/catalogsearch/result` HTML route now 404s. Product data is instead
served by Netmeds' Fynd-commerce-based search API, which returns JSON
directly — no browser/Playwright required.

Search endpoint:
  GET https://www.netmeds.com/ext/search/application/api/v1.0/products
  params: {"page_id": "*", "page_size": 12, "q": medicine_name}

Response shape (abridged):
  {
    "items": [
      {
        "name": "Paracetamol 500mg Tablet 10'S",
        "slug": "paracetamol-500mg-tablet-10s-m1v2mv-8520913",
        "sellable": true,
        "price": {"effective": {"min": 6.76}, "marked": {"min": 9.65}},
        "sizes": ["10"],
        "medias": [{"url": "https://..."}],
        "attributes": {
            "genericname": "Paracetamol",
            "manufacturername": "Cipla Ltd",
            "mstar-rxrequired": "Rx not requried"
        }
      }
    ]
  }
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from worker_base import BaseWorker
from models import ScheduleClass, VendorMedicineResult, VendorName

logger = logging.getLogger("pharmabridge.worker.netmeds")

USE_MOCK          = os.getenv("USE_MOCK", "true").lower() == "true"
REQUEST_TIMEOUT_S = int(os.getenv("REQUEST_TIMEOUT_MS", "12000")) / 1000

SEARCH_URL = "https://www.netmeds.com/ext/search/application/api/v1.0/products"
DEFAULT_STRIP_SIZE = 10
MAX_RETRIES = 2

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
        "price"       : 68.53,
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
        "price"       : 21.00,
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
    "Accept"         : "application/json",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer"        : "https://www.netmeds.com/",
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
            return await self._api_scrape(medicines)

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

    async def _api_scrape(self, medicines: list[dict]) -> list[VendorMedicineResult]:
        """JSON API scraper – no browser required."""
        results = []

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, headers=HEADERS,
                                      follow_redirects=True) as client:
            for med in medicines:
                name = med["name"]
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        resp = await client.get(
                            SEARCH_URL,
                            params={"page_id": "*", "page_size": 12, "q": name},
                        )
                        resp.raise_for_status()

                        data = resp.json()

                        if not isinstance(data, dict):
                            logger.warning(
                                "[netmeds] Unexpected response type: %s",
                                type(data).__name__,
                            )
                            break

                        if attempt == 1 and not getattr(self, "_schema_logged", False):
                            logger.info(
                                "[netmeds] Response keys: %s",
                                list(data.keys())[:20],
                            )
                            self._schema_logged = True

                        items = data.get("items") or []

                        best = self._select_best_match(name, items)
                        if best is not None:
                            result = self._map_item(name, best)
                            if result is not None:
                                results.append(result)
                        else:
                            logger.info("[netmeds] No match for '%s'", name)

                        break

                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        logger.warning(
                            "[netmeds] Attempt %d/%d failed for '%s': %s",
                            attempt, MAX_RETRIES, name, exc,
                        )
                        if attempt == MAX_RETRIES:
                            logger.warning("[netmeds] Giving up on '%s'", name)
                        else:
                            await asyncio.sleep(random.uniform(0.5, 1.5))

                    except Exception as exc:
                        logger.warning("[netmeds] Scrape failed for '%s': %s", name, exc)
                        break

                await asyncio.sleep(random.uniform(0.3, 0.8))

        return results

    @staticmethod
    def _select_best_match(query: str, items: list[dict]) -> Optional[dict]:
        """
        Pick the best-matching item from the search results.

        Scoring:
          100 = exact product name match
           90 = exact generic name match
           80 = query contained in product name
           70 = product name contained in query
           50 = partial token overlap
            0 = no match
        """
        if not items:
            return None

        norm_query = query.strip().lower()
        query_tokens = set(norm_query.split())

        best_item: Optional[dict] = None
        best_score = -1

        for item in items:
            if not isinstance(item, dict):
                continue

            name = (item.get("name") or "").strip().lower()
            generic = (
                (item.get("attributes") or {}).get("genericname") or ""
            ).strip().lower()

            score = 0
            if name and name == norm_query:
                score = 100
            elif generic and generic == norm_query:
                score = 90
            elif name and norm_query in name:
                score = 80
            elif name and name in norm_query:
                score = 70
            else:
                name_tokens = set(name.split())
                if query_tokens and name_tokens and (query_tokens & name_tokens):
                    score = 50

            if score > best_score:
                best_score = score
                best_item = item

        if best_score <= 0:
            return None

        return best_item

    @staticmethod
    def _map_item(medicine_name: str, item: dict[str, Any]) -> Optional[VendorMedicineResult]:
        """Map a Netmeds search-API item into a VendorMedicineResult."""
        try:
            attributes = item.get("attributes") or {}

            price_block = item.get("price") or {}
            effective = (price_block.get("effective") or {}).get("min")
            marked    = (price_block.get("marked") or {}).get("min")

            if effective is None:
                logger.warning(
                    "[netmeds] Missing effective price for '%s' (slug=%s)",
                    medicine_name, item.get("slug"),
                )
                return None

            price_per_strip = float(effective)
            mrp = float(marked) if marked is not None else price_per_strip

            sizes = item.get("sizes") or []
            try:
                strip_size = int(sizes[0])
                if strip_size <= 0:
                    strip_size = DEFAULT_STRIP_SIZE
            except (IndexError, ValueError, TypeError):
                strip_size = DEFAULT_STRIP_SIZE

            price_per_unit = round(price_per_strip / strip_size, 4)
            discount_pct = (
                round((mrp - price_per_strip) / mrp * 100, 1) if mrp > 0 else 0.0
            )

            rx_attr = (attributes.get("mstar-rxrequired") or "").strip().lower()
            requires_rx = bool(rx_attr) and "not req" not in rx_attr
            schedule = ScheduleClass.H if requires_rx else ScheduleClass.OTC

            medias = item.get("medias") or []
            image_url = None
            if medias and isinstance(medias[0], dict):
                image_url = medias[0].get("url")

            slug = item.get("slug")
            product_url = (
                f"https://www.netmeds.com/prescriptions/{slug}" if slug else None
            )

            brand_name = item.get("name") or medicine_name

            return VendorMedicineResult(
                medicine_name   = medicine_name,
                vendor          = VendorName.NETMEDS,
                brand_name      = brand_name,
                generic_name    = attributes.get("genericname"),
                manufacturer    = attributes.get("manufacturername"),
                price_per_unit  = price_per_unit,
                price_per_strip = price_per_strip,
                strip_size      = strip_size,
                mrp             = mrp,
                discount_pct    = discount_pct,
                in_stock        = bool(item.get("sellable", False)),
                schedule        = schedule,
                requires_rx     = requires_rx,
                product_url     = product_url,
                image_url       = image_url,
                scraped_at      = datetime.now(timezone.utc),
            )

        except Exception as exc:
            logger.warning(
                "[netmeds] Failed to map item for '%s' (slug=%s): %s",
                medicine_name, item.get("slug"), exc,
            )
            return None


if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(level=_log.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s")
    worker = WorkerNetmeds()
    asyncio.run(worker.run())
