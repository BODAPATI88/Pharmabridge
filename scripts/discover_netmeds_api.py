#!/usr/bin/env python3

import asyncio
from playwright.async_api import async_playwright


async def main():
    captured = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )

        page = await browser.new_page()

        async def log_request(request):
            url = request.url.lower()

            interesting = [
                "api",
                "search",
                "graphql",
                "query",
                "autocomplete",
                "product",
                "medicine",
                "catalog",
                "algolia",
                "elastic",
                "typesense"
            ]

            if any(x in url for x in interesting):
                captured.append({
                    "method": request.method,
                    "url": request.url
                })

        page.on("request", log_request)

        print("\n=== Opening Netmeds ===\n")

        await page.goto(
            "https://www.netmeds.com",
            wait_until="networkidle",
            timeout=60000
        )

        print("Page title:", await page.title())

        search_selectors = [
            'input[type="search"]',
            'input[placeholder*="search" i]',
            'input[name*="search" i]',
            'input'
        ]

        search_box = None

        for selector in search_selectors:
            try:
                search_box = await page.query_selector(selector)
                if search_box:
                    print(f"Found search box using: {selector}")
                    break
            except Exception:
                pass

        if not search_box:
            print("Could not locate search box")
            await browser.close()
            return

        await search_box.click()
        await search_box.fill("paracetamol")

        await page.keyboard.press("Enter")

        await page.wait_for_timeout(5000)

        print("\n=== AFTER SEARCH ===")
        print("URL:", page.url)
        print("TITLE:", await page.title())


        print("\nWaiting for requests...\n")

        await page.wait_for_timeout(8000)

        print("\n=== Captured Requests ===\n")

        seen = set()

        for item in captured:
            key = f"{item['method']} {item['url']}"

            if key not in seen:
                seen.add(key)
                print(key)

        print("\n=== Current URL ===")
        print(page.url)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
