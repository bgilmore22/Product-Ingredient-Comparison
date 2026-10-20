"""
Scrapers/go-to_skincare.py - Go-To Skincare product page scraper (Source 2,
the brand's own store).

Shopify store. Brand and images come from the schema.org Product JSON-LD block
(stable), text fields come from the tabbed product-content sections.

The hyphen in the filename means run_pipeline.py loads this with importlib
rather than a normal import. Nothing here writes files.
"""
from __future__ import annotations

import json
from typing import Optional

from bs4 import BeautifulSoup

from Scrapers import common

WEBSITE_NAME = "Go-To Skincare"
WEBSITE_SLUG = "gotoskincare"


def _get_product_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        # some themes wrap products in an @graph array
        for item in list(items):
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                items.extend(item["@graph"])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return None


def extract_ingredients(html: str) -> Optional[str]:
    # "What's in it?" tab holds the full INCI list. The "Hero ingredients"
    # accordion and the glossary drawer are partial / descriptive - not wanted.
    # Joined over every <p> in the tab so a list split across paragraphs isn't
    # cut to the first one.
    try:
        soup = BeautifulSoup(html, "html.parser")
        paragraphs = soup.select("div.whats-in-it--text div.text p")
        if paragraphs:
            text = " ".join(p.get_text(" ", strip=True) for p in paragraphs).strip()
        else:
            block = soup.select_one("div.whats-in-it--text")
            text = block.get_text(" ", strip=True).strip() if block else ""
        return text or None
    except Exception:
        return None


def extract_packaging_text(html: str) -> Optional[str]:
    # "How it's made" panel under "Doing our bit" lists pack materials, one <p> each.
    # The panel is scoped to packaging only, so no keyword filter is applied.
    # ("How to recycle" is disposal instructions, so it is left out.)
    try:
        paragraphs = BeautifulSoup(html, "html.parser").select("div.how-its-made p")
        text = " ".join(p.get_text(" ", strip=True) for p in paragraphs).strip()
        return text or None
    except Exception:
        return None


def extract_product_name(html: str) -> Optional[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1", class_="product-meta__title")
        if h1 is None:
            item = _get_product_jsonld(soup)
            name = item.get("name") if item else None
            return name.strip() if isinstance(name, str) and name.strip() else None
        return h1.get_text(" ", strip=True) or None
    except Exception:
        return None


def extract_brand(html: str) -> Optional[str]:
    try:
        item = _get_product_jsonld(BeautifulSoup(html, "html.parser"))
        brand = item.get("brand") if item else None
        if isinstance(brand, dict):
            brand = brand.get("name")
        return brand or None
    except Exception:
        return None


def extract_image_urls(html: str) -> list[str]:
    try:
        item = _get_product_jsonld(BeautifulSoup(html, "html.parser"))
        images = item.get("image") if item else None
        if isinstance(images, str):
            images = [images]
        if isinstance(images, dict):          # ImageObject
            images = [images.get("url") or images.get("contentUrl")]
        urls: list[str] = []
        for u in images or []:
            if isinstance(u, dict):
                u = u.get("url") or u.get("contentUrl")
            if not isinstance(u, str):
                continue
            if u.startswith("//"):
                u = "https:" + u
            if u.startswith("http") and u not in urls:
                urls.append(u)
        return urls
    except Exception:
        return []
