"""
Scrapers/mecca.py - MECCA product page scraper (Source 1, the retailer).

MECCA's class names are emotion hashes (css-1i82ydo) that change on redeploy,
so every selector here hangs off data-testid or a visible heading instead.

Each extract_* function takes the page HTML and returns a value or None;
run_pipeline.py assembles the record. Nothing here writes files.
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from Scrapers import common

WEBSITE_NAME = "MECCA"
WEBSITE_SLUG = "mecca"

# MECCA appends this disclaimer to every ingredients block. It contains commas,
# so if it leaks through it gets split into fake ingredients downstream.
_INGREDIENTS_DISCLAIMER = re.compile(
    r"[,\s]*Occasionally, our brands.*$", re.IGNORECASE | re.DOTALL
)

# Free-from claims after the list ("No silicones, parabens or sulfates.",
# "Free from fragrance.", "Formulated without ...") would false-flag the very
# things they deny. An INCI list has no sentence breaks, so a full stop
# followed by one of these openers is reliably the start of marketing text.
_FREE_FROM_DISCLAIMER = re.compile(
    r"(?<=[a-z0-9)\]])[.;,]\s*(?:no|free\s+(?:from|of)|without|formulated\s+without|"
    r"does\s+not\s+contain|contains\s+no)\s+[a-z].*$"
    r"|[.;,]?\s*for\s+a\s+full\s+explanation\s+of\s+each\s+ingredient.*$",
    re.IGNORECASE | re.DOTALL,
)


def _get_accordion(soup: BeautifulSoup, title: str):
    """Find the accordion <details> whose <summary> heading equals `title`."""
    for details in soup.find_all("details", attrs={"data-testid": "product-details-disclosure"}):
        summary = details.find("summary")
        if summary and summary.get_text(" ", strip=True).lower() == title.lower():
            return details
    return None


def _accordion_body_text(details) -> str:
    """All text in the accordion except its <summary> heading. Prefers the
    <p> blocks (joined, so a list split over several paragraphs isn't cut to
    the first one); falls back to the whole body when there are none."""
    paragraphs = details.find_all("p")
    if paragraphs:
        return " ".join(p.get_text(" ", strip=True) for p in paragraphs)
    summary = details.find("summary")
    if summary:
        summary.extract()
    return details.get_text(" ", strip=True)


def extract_ingredients(html: str) -> Optional[str]:
    try:
        details = _get_accordion(BeautifulSoup(html, "html.parser"), "Ingredients")
        if details is None:
            return None
        text = _accordion_body_text(details)
        text = _INGREDIENTS_DISCLAIMER.sub("", text)
        text = _FREE_FROM_DISCLAIMER.sub("", text).strip()
        return text or None
    except Exception:
        return None


def extract_packaging_text(html: str) -> Optional[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for title in ("Product details", "Details", "About"):
            details = _get_accordion(soup, title)
            if details is not None:
                found = common.find_packaging_mentions(details.get_text(" ", strip=True))
                if found:
                    return found
        return None
    except Exception:
        return None


def extract_product_name(html: str) -> Optional[str]:
    # The h1 reads "Go-To Much Plumper Skin" (brand + name); this is the name span only.
    try:
        soup = BeautifulSoup(html, "html.parser")
        el = soup.find(attrs={"data-testid": "hero-product-name"})
        if el is None:
            el = soup.find("h1")
        return el.get_text(" ", strip=True) if el else None
    except Exception:
        return None


def extract_brand(html: str) -> Optional[str]:
    try:
        el = BeautifulSoup(html, "html.parser").find(attrs={"data-testid": "hero-brand"})
        return el.get_text(strip=True) if el else None
    except Exception:
        return None


def extract_image_urls(html: str) -> list[str]:
    """
    Main gallery photos only. The swiper loops, so slides repeat - dedupe in order.
    URLs carry ?t=blur (a low-res placeholder); strip the query for full size.

    The page also has a "pair it with" carousel reusing the same slide-N test ids
    for OTHER products, so scope to the section around the first slide (the
    product gallery sits above it in the DOM).
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        first = soup.select_one('[data-testid^="slide-"]')
        if first is None:
            return []
        gallery = first.find_parent("section") or soup
        urls: list[str] = []
        for img in gallery.select('[data-testid^="slide-"] img'):
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            src = src.split("?")[0]
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http") and src not in urls:
                urls.append(src)
        return urls
    except Exception:
        return []