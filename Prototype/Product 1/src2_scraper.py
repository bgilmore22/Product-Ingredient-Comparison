"""
src2_scraper.py - Inca Organics product page scraper (Product 1: collagen powder).

Fixture: test_cases/src2_cases/inca_full_details.html
Shopify server-rendered page source - plain httpx GET should work in
production, no JS rendering needed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from Scrapers import common

WEBSITE_NAME = "Inca Organics"
WEBSITE_SLUG = "incaorganics"

_INGREDIENTS_LABEL = "minimal ingredients"


def _find_accordion_content(soup: BeautifulSoup, summary_text: str):
    """
    Find the <details> whose <summary> contains `summary_text` (case
    insensitive), then return its content div (class "collapsible__content").
    Returns None if either the summary or - critically - the content div's
    class can't be matched, even if the summary label itself is untouched.
    """
    for details in soup.find_all("details"):
        summary = details.find("summary")
        if summary is None:
            continue
        if summary_text.lower() not in summary.get_text(" ", strip=True).lower():
            continue
        return details.find("div", class_="collapsible__content")
    return None


def extract_ingredients(html: str) -> Optional[str]:
    try:
        content = _find_accordion_content(BeautifulSoup(html, "html.parser"), _INGREDIENTS_LABEL)
        if content is None:
            return None
        # The accordion holds 3 paragraphs: the actual ingredient list, then
        # "Halal and Kosher friendly.", then "No GMO, preservatives...".
        # Only the first is the ingredients themselves - grabbing the whole
        # div and splitting on commas mangles those trailing sentences into
        # fake "ingredients" like "preservatives" or "dairy or soy".
        first_p = content.find("p")
        if first_p is None:
            return None
        text = first_p.get_text(" ", strip=True)
        return text or None
    except Exception:
        return None


def extract_packaging_text(html: str) -> Optional[str]:
    try:
        content = _find_accordion_content(BeautifulSoup(html, "html.parser"), _INGREDIENTS_LABEL)
        if content is None:
            return None
        return common.find_packaging_mentions(content.get_text(" ", strip=True))
    except Exception:
        return None


def extract_product_name(html: str) -> Optional[str]:
    try:
        h1 = BeautifulSoup(html, "html.parser").find("h1", class_="product-title")
        return h1.get_text(" ", strip=True) if h1 else None
    except Exception:
        return None


def extract_brand(html: str) -> str:
    # Inca's own store: brand is always Inca Organics. Confirmed via the
    # og:site_name meta tag rather than hardcoding.
    try:
        meta = BeautifulSoup(html, "html.parser").find("meta", property="og:site_name")
        return meta["content"] if meta and meta.get("content") else WEBSITE_NAME
    except Exception:
        return WEBSITE_NAME


def extract_image_urls(html: str) -> list[str]:
    """Full-resolution product photos (product-single__media-zoom anchors)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        anchors = soup.find_all("a", class_="product-single__media-zoom")
        urls = []
        for a in anchors:
            href = a.get("href")
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