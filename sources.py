"""
sources.py (project root) - the two sites a run compares, described as data.

run_pipeline.py no longer knows the names "Mecca" or "Go-To": it takes a
`brand` Source (the store whose product list drives the run) and a `retailer`
Source (the pool it is matched against), and everything site-specific lives
in the Source object. The default pair is Go-To Skincare vs MECCA.

To add a retailer:
  1. Write Scrapers/<site>.py with the five extract_* functions mecca.py has
     (extract_ingredients, extract_packaging_text, extract_product_name,
     extract_brand, extract_image_urls - each takes page HTML, returns a
     value or None/[]), plus WEBSITE_NAME and WEBSITE_SLUG.
  2. Add a crawl function in crawl_sitemaps.py that returns its product URLs.
  3. Add a Source(...) below and register it in SOURCES.
  4. Run:  python3 run_pipeline.py --brand goto --retailer <key>

A Source for a Shopify store can reuse crawl_shopify_products and the
product-JSON helpers in Scrapers/go-to_skincare.py (product_json,
kit_signals, structured_facts) - see SOURCE_GOTO.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from Scrapers import mecca as _mecca
from crawl_sitemaps import crawl_mecca_products, crawl_shopify_products

ROOT = Path(__file__).resolve().parent


def load_scraper(filename: str):
    """Import Scrapers/<filename> by path. Needed because go-to_skincare.py has a
    hyphen, which a normal import statement can't use. The folder is
    "Scrapers" with a capital S: macOS ignores case, Linux doesn't."""
    path = ROOT / "Scrapers" / filename
    name = "Scrapers." + re.sub(r"[^0-9A-Za-z_]", "_", path.stem)
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@dataclass
class Source:
    key: str                                   # command-line name: "mecca", "goto"
    role: str                                  # "retailer" or "brand" - which side is Source 1 (analyse.SOURCE1)
    scraper: object                            # module with the extract_* functions and WEBSITE_NAME
    crawl: Callable[[], list[str]]             # -> every product URL on the site
    slug: Callable[[str], str]                 # product URL -> comparable slug ("face-hero")
    describe: str = ""                         # one line for the matching prompt ("the brand's own store")
    prefer_url: Optional[Callable[[str, str], str]] = None   # tie-break when two URLs share a slug
    product_json: Optional[Callable[[str], Optional[dict]]] = None   # structured product data, if the site has it
    kit_signals: Optional[Callable[[Optional[dict]], Optional[str]]] = None
    structured_facts: Optional[Callable[[Optional[dict]], dict]] = None
    delay_seconds: float = 1.0                 # politeness pause after each live page fetch

    @property
    def name(self) -> str:
        return self.scraper.WEBSITE_NAME

    def facts(self, url: str) -> tuple[Optional[dict], dict, Optional[str]]:
        """(raw structured product, facts for the record, kit reason) - all
        empty when the site has no structured data."""
        if not self.product_json:
            return None, {}, None
        raw = self.product_json(url)
        facts = self.structured_facts(raw) if self.structured_facts else {}
        kit = self.kit_signals(raw) if self.kit_signals else None
        return raw, facts, kit


# ---------------------------------------------------------------------------
# Go-To Skincare (Shopify) - the brand's own store
# ---------------------------------------------------------------------------
_goto = load_scraper("go-to_skincare.py")
GOTO_SITEMAP = "https://gotoskincare.com/sitemap.xml"


def goto_slug(url: str) -> str:
    return url.rstrip("/").split("/products/")[-1]


SOURCE_GOTO = Source(
    key="goto",
    role="brand",
    scraper=_goto,
    crawl=lambda: crawl_shopify_products(GOTO_SITEMAP),
    slug=goto_slug,
    describe="the brand's own store",
    product_json=_goto.fetch_product_json,
    kit_signals=_goto.kit_signals,
    structured_facts=_goto.structured_facts,
)

# ---------------------------------------------------------------------------
# MECCA - a multi-brand retailer; only its Go-To listings are wanted
# ---------------------------------------------------------------------------
MECCA_BRAND_PATH = "/go-to/"
_MECCA_ID_RE = re.compile(r"-[A-Z]-\d+$")


def mecca_slug(url: str) -> str:
    return _MECCA_ID_RE.sub("", url.rstrip("/").split("/")[-1])


def _prefer_single_item(existing: str, new: str) -> str:
    """Mecca lists the same slug as -I-<id> (single item) and -V-<id> (variant
    group). Keep the -I- page: one SKU, one formula."""
    return new if ("-I-" in new and "-V-" in existing) else existing


SOURCE_MECCA = Source(
    key="mecca",
    role="retailer",
    scraper=_mecca,
    crawl=lambda: [u for u in crawl_mecca_products() if MECCA_BRAND_PATH in u],
    slug=mecca_slug,
    describe="a multi-brand retailer",
    prefer_url=_prefer_single_item,
)

SOURCES = {s.key: s for s in (SOURCE_GOTO, SOURCE_MECCA)}
DEFAULT_BRAND = "goto"
DEFAULT_RETAILER = "mecca"
