"""
src1_scraper.py - The Iconic product page scraper (Product 1: collagen powder).

Fixture: test_cases/src1_cases/iconic_full_details.html
Must be captured via DevTools "Copy outerHTML" AFTER full JS render -
The Iconic is a JS SPA, raw httpx GET returns nav-only HTML in production.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
import re
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from Scrapers import common

WEBSITE_NAME = "The Iconic"
WEBSITE_SLUG = "theiconic"


def _get_product_description_div(soup: BeautifulSoup):
    # The page duplicates this div once for desktop, once inside a mobile
    # accordion tab. Both copies are identical - first match is fine.
    return soup.find("div", class_="product-description")


_INGREDIENTS_LABEL = re.compile(r'^\s*(full\s+)?ingredients(\s+list)?\s*:?\s*', re.IGNORECASE)


def extract_ingredients(html: str) -> Optional[str]:
    try:
        desc = _get_product_description_div(BeautifulSoup(html, "html.parser"))
        if desc is None:
            return None

        for p in desc.find_all("p"):
            text = p.get_text(" ", strip=True)
            m = _INGREDIENTS_LABEL.match(text)
            if m:
                remainder = text[m.end():].strip()
                return remainder or None
        return None
    except Exception:
        return None


def extract_packaging_text(html: str) -> Optional[str]:
    try:
        desc = _get_product_description_div(BeautifulSoup(html, "html.parser"))
        if desc is None:
            return None
        return common.find_packaging_mentions(desc.get_text(" ", strip=True))
    except Exception:
        return None


def extract_product_name(html: str) -> Optional[str]:
    try:
        h1 = BeautifulSoup(html, "html.parser").find("h1", class_="product-title")
        return h1.get_text(" ", strip=True) if h1 else None
    except Exception:
        return None


def extract_brand(html: str) -> Optional[str]:
    try:
        brand = BeautifulSoup(html, "html.parser").find("a", class_="product-brand")
        return brand.get_text(strip=True) if brand else None
    except Exception:
        return None


def extract_image_urls(html: str) -> list[str]:
    """Full-resolution product photos, not the low-res lazyload thumbnails."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.find_all("a", class_="product-image-frame")
        urls = []
        for a in anchors:
            href = a.get("data-ti-fullsizable") or a.get("href")
            if href:
                urls.append(href if href.startswith("http") else f"https:{href}")
        return urls
    except Exception:
        return []


def scrape_and_save(html: str, url: str) -> Path:
    ingredients = extract_ingredients(html)
    packaging = extract_packaging_text(html)
    packaging_source = "text" if packaging else "not_found"

    if not packaging:
        packaging = common.get_packaging_from_images(extract_image_urls(html))
        if packaging:
            packaging_source = "image"

    record = common.build_product_record(
        website=WEBSITE_NAME,
        url=url,
        product_name=extract_product_name(html) or "unknown-product",
        brand=extract_brand(html),
        ingredients_text=ingredients,
        packaging_text=packaging,
        packaging_source=packaging_source,
    )
    return common.save_product_json(record, WEBSITE_SLUG)