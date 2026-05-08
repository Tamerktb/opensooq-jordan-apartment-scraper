#!/usr/bin/env python3
"""
OpenSooq Jordan apartment-for-rent scraper.

Scrapes apartment listings from a given OpenSooq Jordan search page
and exports them to a clean CSV.
"""

import asyncio
import csv
import logging
import re
from typing import Any
from playwright.async_api import async_playwright, Page
from bs4 import BeautifulSoup

# --- Config ---
BASE_URL = "https://jo.opensooq.com/en/amman/property/apartments-for-rent"
NUM_PAGES = 5
OUTPUT_FILE = "listings.csv"
SITE_ROOT = "https://jo.opensooq.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("opensooq")

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def parse_price(text: str) -> dict[str, Any]:
    """'20 JOD' / '1,200 JOD' / 'JOD 250' -> {price: int, currency: 'JOD'}."""
    if not text:
        return {"price": None, "currency": None}
    t = text.strip()
    # number first
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(JOD|JD|دينار)", t, re.IGNORECASE)
    if m:
        num, currency = m.group(1), m.group(2)
    else:
        # currency first
        m = re.search(r"(JOD|JD|دينار)\s*([\d,]+(?:\.\d+)?)", t, re.IGNORECASE)
        if m:
            num, currency = m.group(2), m.group(1)
        else:
            return {"price": None, "currency": None}
    try:
        price = float(num.replace(",", ""))
        price = int(price) if price.is_integer() else price
    except ValueError:
        price = None
    currency = currency.upper().replace("JD", "JOD")
    return {"price": price, "currency": currency}


def parse_count(text: str, keyword: str) -> int | None:
    t = text.strip().lower()
    if "studio" in t and keyword == "bedroom":
        return 0
    m = re.search(rf"(\d+)\s*{keyword}", t)
    if m:
        return int(m.group(1))
    for word, n in WORD_NUMBERS.items():
        if re.search(rf"\b{word}\s+{keyword}", t):
            return n
    return None


def parse_area(text: str) -> int | None:
    m = re.search(r"surface area:\s*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*m2", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def extract_listing(card) -> dict[str, Any] | None:
    href = card.get("href", "")
    full_url = href if href.startswith("http") else SITE_ROOT + href
    m = re.search(r"/(\d{6,})(?:/|$|\?)", href)
    listing_id = m.group(1) if m else None

    price_el = card.find("div", class_="redColor")
    price = parse_price(price_el.get_text(strip=True) if price_el else "")

    h2 = card.find("h2")
    title = h2.get_text(strip=True) if h2 else None

    details_text = [
        s.get_text(strip=True)
        for s in card.find_all("span", class_="darkGrayColor")
    ]

    bedrooms = bathrooms = area = furnished = floor = None
    for d in details_text:
        dl = d.lower()
        if bedrooms is None and ("bedroom" in dl or "studio" in dl):
            bedrooms = parse_count(d, "bedroom")
        if bathrooms is None and "bathroom" in dl:
            bathrooms = parse_count(d, "bathroom")
        if area is None and ("surface area" in dl or re.search(r"\d+\s*m2", d, re.I)):
            area = parse_area(d)
        if furnished is None and any(
            f in dl for f in ("furnished", "unfurnished", "semi furnished")
        ):
            furnished = d
        if floor is None and "floor" in dl:
            floor = d

    # Location: span inside a div with font-13/bold classes that also has an SVG
    location = None
    for div in card.find_all("div"):
        classes = div.get("class") or []
        if "font-13" in classes and "bold" in classes and div.find("svg"):
            sp = div.find("span")
            if sp:
                location = sp.get_text(strip=True)
                break

    return {
        "listing_id": listing_id,
        "title": title,
        "price": price["price"],
        "currency": price["currency"],
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "area_m2": area,
        "furnished": furnished,
        "floor": floor,
        "location": location,
        "details_raw": " | ".join(details_text),
        "url": full_url,
    }


async def fetch_page_html(page: Page, url: str) -> str:
    log.info(f"  navigating: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    for _ in range(4):
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(500)
    await page.wait_for_timeout(1000)
    return await page.content()


def parse_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all("a", class_="postListItemData")
    log.info(f"  found {len(cards)} cards")
    rows = []
    for c in cards:
        try:
            row = extract_listing(c)
            if row:
                rows.append(row)
        except Exception as e:
            log.warning(f"  card parse failed: {e}")
    return rows


async def main():
    all_rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        for n in range(1, NUM_PAGES + 1):
            url = BASE_URL if n == 1 else f"{BASE_URL}?page={n}"
            log.info(f"Page {n}/{NUM_PAGES}")
            try:
                html = await fetch_page_html(page, url)
            except Exception as e:
                log.error(f"  page {n} failed: {e}")
                continue
            new = 0
            for row in parse_html(html):
                if row["listing_id"] in seen:
                    continue
                if row["listing_id"]:
                    seen.add(row["listing_id"])
                all_rows.append(row)
                new += 1
            log.info(f"  +{new} new (total {len(all_rows)})")

        await browser.close()

    if not all_rows:
        log.warning("No listings collected.")
        return

    fields = list(all_rows[0].keys())
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)
    log.info(f"Saved {len(all_rows)} listings -> {OUTPUT_FILE}")

    try:
        import pandas as pd
        df = pd.DataFrame(all_rows)
        print("\nTop 10 preview:")
        print(
            df[["title", "price", "currency", "bedrooms", "area_m2", "location"]]
            .head(10)
            .to_string(index=False)
        )
    except ImportError:
        pass


if __name__ == "__main__":
    asyncio.run(main())