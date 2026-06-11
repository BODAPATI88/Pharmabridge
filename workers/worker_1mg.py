"""
pharmabridge/workers/worker_1mg.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PharmaBridge  ·  1mg Scraper Worker
────────────────────────────────────
This worker listens on `pb:queue:1mg`, fetches medicine prices from 1mg.com,
and publishes structured results back to Redis.

Scraping strategy
─────────────────
1.  Use Playwright (async, Chromium headless) to load the 1mg search page.
    1mg is a React SPA – BeautifulSoup alone cannot parse its rendered DOM.
2.  Search URL pattern:
      https://www.1mg.com/search/all?name={medicine_name}
3.  Wait for the product-list selector to appear (up to 10 s).
4.  Extract: brand_name, price, MRP, discount, manufacturer, schedule tag,
    pack size, product URL.
5.  Parse the schedule from the "Rx required" / "Schedule H" badge if present.

NOTE ON PRODUCTION READINESS
─────────────────────────────
The `scrape()` method below contains:
  a. Full Playwright boilerplate (ready to enable with `USE_MOCK=false`)
  b. A deterministic mock that returns realistic data so the full pipeline
     can be integration-tested without hitting live websites during CI or
     during initial K8s cluster bring-up.

Set environment variable USE_MOCK=false to engage real scraping.
Real scraping requires the `playwright install chromium` step in the Dockerfile.

Anti-bot considerations
───────────────────────
  • Rotate user-agent strings from `USER_AGENTS` pool.
  • Random sleep 1.5–4 s between page loads.
  • Cache results in Redis (gateway layer) to avoid repeat hits.
  • For high-volume production: integrate a residential proxy pool via
    PLAYWRIGHT_PROXY env var  (e.g. Oxylabs / Bright Data).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Optional

from worker_base import BaseWorker
from models import ScheduleClass, VendorMedicineResult, VendorName

logger = logging.getLogger("pharmabridge.worker.1mg")

# ── Config ────────────────────────────────────────────────
USE_MOCK           = os.getenv("USE_MOCK", "true").lower() == "true"
PLAYWRIGHT_PROXY   = os.getenv("PLAYWRIGHT_PROXY", "")      # "http://user:pass@host:port"
REQUEST_TIMEOUT_MS = int(os.getenv("REQUEST_TIMEOUT_MS", "12000"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# ── Realistic mock data keyed by normalised medicine name ─
MOCK_CATALOGUE: dict[str, dict] = {
    "metformin": {
        "brand_name"   : "Glycomet 500mg",
        "generic_name" : "Metformin Hydrochloride",
        "manufacturer" : "USV Pvt Ltd",
        "mrp"          : 42.50,
        "price"        : 35.70,
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
        "product_url"  : "https://www.1mg.com/drugs/glycomet-500-tablet-7087",
    },
    "atorvastatin": {
        "brand_name"   : "Atorva 10mg",
        "generic_name" : "Atorvastatin",
        "manufacturer" : "Zydus Cadila",
        "mrp"          : 89.00,
        "price"        : 71.20,
        "strip_size"   : 10,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
        "product_url"  : "https://www.1mg.com/drugs/atorva-10-tablet-30894",
    },
    "amlodipine": {
        "brand_name"   : "Amlokind 5mg",
        "generic_name" : "Amlodipine Besylate",
        "manufacturer" : "Mankind Pharma",
        "mrp"          : 55.00,
        "price"        : 44.00,
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
        "product_url"  : "https://www.1mg.com/drugs/amlokind-5-tablet-1234",
    },
    "pantoprazole": {
        "brand_name"   : "Pan 40",
        "generic_name" : "Pantoprazole",
        "manufacturer" : "Alkem Laboratories",
        "mrp"          : 65.00,
        "price"        : 52.00,
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.H,
        "requires_rx"  : True,
        "product_url"  : "https://www.1mg.com/drugs/pan-40-tablet-5678",
    },
    "cetirizine": {
        "brand_name"   : "Cetzine 10mg",
        "generic_name" : "Cetirizine Hydrochloride",
        "manufacturer" : "GSK Pharmaceuticals",
        "mrp"          : 28.00,
        "price"        : 22.40,
        "strip_size"   : 10,
        "schedule"     : ScheduleClass.OTC,
        "requires_rx"  : False,
        "product_url"  : "https://www.1mg.com/drugs/cetzine-10mg-tablet-9012",
    },
    "paracetamol": {
        "brand_name"   : "Crocin 500mg",
        "generic_name" : "Paracetamol",
        "manufacturer" : "GSK Consumer Healthcare",
        "mrp"          : 22.00,
        "price"        : 17.60,
        "strip_size"   : 15,
        "schedule"     : ScheduleClass.OTC,
        "requires_rx"  : False,
        "product_url"  : "https://www.1mg.com/drugs/crocin-500-tablet-3456",
    },
}


class Worker1mg(BaseWorker):
    vendor = VendorName.ONEmg

    async def scrape(
        self,
        medicines    : list[dict],
        pincode      : str,
        prefer_generic: bool,
    ) -> list[VendorMedicineResult]:
        """
        Scrape 1mg for each medicine in the list.
        Returns one VendorMedicineResult per found medicine.
        """
        if USE_MOCK:
            logger.info("[1mg] Running in MOCK mode (set USE_MOCK=false for live scraping)")
            return await self._mock_scrape(medicines, pincode)
        else:
            return await self._playwright_scrape(medicines, pincode, prefer_generic)

    # ── Mock implementation ────────────────────────────────

    async def _mock_scrape(
        self,
        medicines: list[dict],
        pincode  : str,
    ) -> list[VendorMedicineResult]:
        """
        Returns deterministic realistic results from the mock catalogue.
        Adds ±5% price jitter to simulate real-world price variance, so
        the Smart-Split algorithm has something meaningful to optimise.
        """
        results: list[VendorMedicineResult] = []

        for med in medicines:
            norm_name = med["name"].strip().lower()
            # Fuzzy match: check if any catalogue key is a substring
            matched_key = next(
                (k for k in MOCK_CATALOGUE if k in norm_name or norm_name in k),
                None
            )
            if matched_key is None:
                logger.debug("[1mg][mock] No mock entry for '%s'", med["name"])
                continue

            cat     = MOCK_CATALOGUE[matched_key]
            jitter  = random.uniform(0.95, 1.05)   # ±5% price variance
            mrp     = round(cat["mrp"], 2)
            price   = round(cat["price"] * jitter, 2)
            strip   = cat["strip_size"]
            unit_p  = round(price / strip, 4)
            disc_pct= round((1 - price / mrp) * 100, 1)

            # Simulate a brief network delay
            await asyncio.sleep(random.uniform(0.05, 0.15))

            results.append(VendorMedicineResult(
                medicine_name   = med["name"],
                vendor          = VendorName.ONEmg,
                brand_name      = cat["brand_name"],
                generic_name    = cat.get("generic_name"),
                manufacturer    = cat.get("manufacturer"),
                price_per_unit  = unit_p,
                price_per_strip = price,
                strip_size      = strip,
                mrp             = mrp,
                discount_pct    = disc_pct,
                in_stock        = True,
                schedule        = cat["schedule"],
                requires_rx     = cat["requires_rx"],
                product_url     = cat.get("product_url"),
                scraped_at      = datetime.now(timezone.utc),
            ))

        return results

    # ── Real Playwright implementation ─────────────────────

    async def _playwright_scrape(
        self,
        medicines    : list[dict],
        pincode      : str,
        prefer_generic: bool,
    ) -> list[VendorMedicineResult]:
        """
        Live scraper using Playwright headless Chromium.

        CSS selectors are correct as of 1mg.com DOM structure circa 2025.
        They WILL break when 1mg updates their frontend – monitor in prod
        with weekly selector-health checks.

        Selectors:
          Product cards   : div[class*="style__product-card"]
          Brand name      : span[class*="style__pro-title"]
          Price           : span[class*="style__price-tag"]
          MRP             : span[class*="style__mrp"]
          Manufacturer    : span[class*="style__mfr-name"]
          Rx badge        : span[class*="style__rx-label"]
          Pack size       : span[class*="style__pack-size"]
        """
        try:
            from playwright.async_api import async_playwright, TimeoutError as PWTimeout
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed.  Run: pip install playwright && "
                "playwright install chromium"
            )

        results: list[VendorMedicineResult] = []

        async with async_playwright() as pw:
            launch_opts: dict = {
                "headless": True,
                "args"    : [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",   # Required in Docker
                    "--disable-gpu",
                ],
            }
            if PLAYWRIGHT_PROXY:
                launch_opts["proxy"] = {"server": PLAYWRIGHT_PROXY}

            browser = await pw.chromium.launch(**launch_opts)
            context = await browser.new_context(
                user_agent   = random.choice(USER_AGENTS),
                locale       = "en-IN",
                timezone_id  = "Asia/Kolkata",
                viewport     = {"width": 1280, "height": 800},
                extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
            )

            for med in medicines:
                page = await context.new_page()
                try:
                    result = await self._scrape_one_medicine(page, med, pincode)
                    if result:
                        results.append(result)
                except PWTimeout:
                    logger.warning("[1mg] Timeout scraping '%s'", med["name"])
                except Exception as exc:
                    logger.error("[1mg] Error scraping '%s': %s", med["name"], exc)
                finally:
                    await page.close()
                    # Polite delay between requests
                    await asyncio.sleep(random.uniform(1.5, 4.0))

            await context.close()
            await browser.close()

        return results

    async def _scrape_one_medicine(
        self,
        page    : "Page",   # type: ignore[name-defined]
        med     : dict,
        pincode : str,
    ) -> Optional[VendorMedicineResult]:
        """Scrape a single medicine from 1mg search results page."""
        from playwright.async_api import TimeoutError as PWTimeout

        search_url = f"https://www.1mg.com/search/all?name={med['name'].replace(' ', '+')}"
        logger.debug("[1mg] Fetching %s", search_url)

        await page.goto(search_url, wait_until="networkidle", timeout=REQUEST_TIMEOUT_MS)

        # Wait for product cards to appear
        try:
            await page.wait_for_selector("div[class*='style__product-card']", timeout=8000)
        except PWTimeout:
            logger.warning("[1mg] No product cards found for '%s'", med["name"])
            return None

        # Take the first result (most relevant)
        card = page.locator("div[class*='style__product-card']").first

        try:
            brand_raw = await card.locator("span[class*='style__pro-title']").inner_text(timeout=3000)
        except Exception:
            brand_raw = med["name"]

        try:
            price_raw = await card.locator("span[class*='style__price-tag']").inner_text(timeout=3000)
            price     = float(price_raw.replace("₹", "").replace(",", "").strip())
        except Exception:
            return None  # Can't trust a result without a price

        try:
            mrp_raw = await card.locator("span[class*='style__mrp']").inner_text(timeout=2000)
            mrp     = float(mrp_raw.replace("MRP:", "").replace("₹", "").replace(",", "").strip())
        except Exception:
            mrp = price  # Fallback: no discount

        try:
            pack_raw  = await card.locator("span[class*='style__pack-size']").inner_text(timeout=2000)
            # Pack size is usually "15 tablets" or "10 strips of 10 tablets"
            strip_size = int("".join(filter(str.isdigit, pack_raw.split()[0])))
        except Exception:
            strip_size = 10  # industry default

        try:
            mfr = await card.locator("span[class*='style__mfr-name']").inner_text(timeout=2000)
        except Exception:
            mfr = None

        # Check for Rx / schedule badge
        requires_rx = False
        schedule    = ScheduleClass.OTC
        try:
            rx_badge = await card.locator("span[class*='style__rx-label']").inner_text(timeout=1000)
            if rx_badge:
                requires_rx = True
                if "H1" in rx_badge or "h1" in rx_badge:
                    schedule = ScheduleClass.H1
                else:
                    schedule = ScheduleClass.H
        except Exception:
            pass

        try:
            product_url = await card.locator("a").first.get_attribute("href", timeout=1000)
            if product_url and not product_url.startswith("http"):
                product_url = "https://www.1mg.com" + product_url
        except Exception:
            product_url = None

        disc_pct   = round((1 - price / mrp) * 100, 1) if mrp > 0 else 0.0
        unit_price = round(price / strip_size, 4)

        return VendorMedicineResult(
            medicine_name   = med["name"],
            vendor          = VendorName.ONEmg,
            brand_name      = brand_raw.strip(),
            generic_name    = None,  # 1mg doesn't surface generic name in search list
            manufacturer    = mfr,
            price_per_unit  = unit_price,
            price_per_strip = price,
            strip_size      = strip_size,
            mrp             = mrp,
            discount_pct    = disc_pct,
            in_stock        = True,  # only in-stock items appear in search
            schedule        = schedule,
            requires_rx     = requires_rx,
            product_url     = product_url,
            scraped_at      = datetime.now(timezone.utc),
        )


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    worker = Worker1mg()
    asyncio.run(worker.run())
