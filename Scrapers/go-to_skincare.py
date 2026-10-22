"""
Scrapers/go-to_skincare.py - Go-To Skincare product page scraper (Source 2,
the brand's own store).

Shopify store. Brand and images come from the schema.org Product JSON-LD block
(stable), text fields come from the tabbed product-content sections.

Shopify also serves every product as JSON at /products/<handle>.json
(product_type, tags, variants, images - but NOT the ingredients tab, which is
a theme metafield). fetch_product_json() reads that; kit_signals() and
variant_titles() turn it into the structured facts the pipeline wants
instead of guessing from page text. All of it fails open (None/[]).

The hyphen in the filename means run_pipeline.py loads this with importlib
rather than a normal import. Nothing here writes files.
"""
from __future__ import annotations

import json
import re
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


# ---------------------------------------------------------------------------
# Shopify product JSON (structured data, preferred over HTML where it exists)
# ---------------------------------------------------------------------------
_KIT_TYPE = re.compile(r"\b(kit|set|bundle|duo|trio|gift|collection|calendar|advent|discovery|starter|pack)s?\b",
                       re.IGNORECASE)


def product_json_url(product_url: str) -> str:
    """https://gotoskincare.com/products/face-hero?variant=1 -> .../products/face-hero.json"""
    base = product_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return base if base.endswith(".json") else base + ".json"


def fetch_product_json(product_url: str) -> Optional[dict]:
    """The Shopify product object for a product page, or None on any failure
    (non-Shopify URL, 404, blocked, malformed). Never raises."""
    try:
        resp = common.fetch(product_json_url(product_url), timeout=15, retries=2)
        data = json.loads(resp.text)
        product = data.get("product") if isinstance(data, dict) else None
        return product if isinstance(product, dict) else None
    except Exception as exc:
        print(f"    product json unavailable for {product_url}: {type(exc).__name__}: {exc}")
        return None


def _tags(product: dict) -> list[str]:
    tags = product.get("tags")
    if isinstance(tags, str):                       # "a, b, c" on some stores
        tags = [t.strip() for t in tags.split(",")]
    return [t for t in (tags or []) if isinstance(t, str) and t.strip()]


def kit_signals(product: Optional[dict]) -> Optional[str]:
    """A reason string if Shopify's own product_type says this listing is a
    kit/set/bundle ('Gifts & Sets'), else None. The store's classification
    beats any guess from the page text.

    Tags are deliberately NOT used: on the live store single products carry
    merchandising tags like 'secret skincare set' and 'special-bundle'
    (meaning "appears in a set"), and treating those as kit signals skipped
    34 of 50 products on 22 Oct 2026. Tags are still kept on the record."""
    if not product:
        return None
    ptype = str(product.get("product_type") or "").strip()
    m = _KIT_TYPE.search(ptype)
    if m:
        return f"Shopify product_type '{ptype}'"
    return None


def variant_titles(product: Optional[dict]) -> list[str]:
    """Shade/size names of the product's variants (Shopify uses 'Default Title'
    for a product with only one). ["Rose", "Peach"] for a two-shade blush."""
    if not product:
        return []
    out = []
    for v in product.get("variants") or []:
        t = str(v.get("title") or "").strip() if isinstance(v, dict) else ""
        if t and t.lower() != "default title":
            out.append(t)
    return out


def structured_facts(product: Optional[dict]) -> dict:
    """The small, stable subset kept on the product record for the dashboard."""
    if not product:
        return {}
    return {
        "product_type": product.get("product_type") or None,
        "tags": _tags(product),
        "variants": variant_titles(product),
        "vendor": product.get("vendor") or None,
    }
