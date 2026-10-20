"""
crawl_sitemaps.py (project root) - Stage 3: URL discovery.

Crawls each site's sitemap and returns product URLs in memory. Called by
run_pipeline.py; writes no files itself.

Sitemap shapes (checked by hand before writing this):
  - Go-To, Inca: standard Shopify sitemap index at /sitemap.xml -> numbered
    sub-sitemaps -> URLs containing /products/
  - Mecca: 3 catalog sitemaps (sitemaps_catalog_1/2/3.xml) - deliberately
    NOT sitemaps_content.xml (blog/editorial) or sitemaps_services.xml
    (in-store bookings) - filtered to URLs ending in -I-<digits>/ or
    -V-<digits>/ (single item vs variant group)
  - Iconic: NOT included. Confirmed by hand: product AND category pages are
    both JS-rendered with no product links in the plain HTML, no product-data
    API call visible in DevTools network traffic, and no embedded JSON blob
    in the page source either. Discovery stays manual for this site.

Fetches go through common.fetch (retries on network errors and 429/5xx).
Sub-sitemaps served as .gz are decompressed.
"""
import gzip
import re
import xml.etree.ElementTree as ET

from Scrapers import common

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def fetch_xml(url):
    """Sitemap bytes, decompressed if the file is gzipped. Bytes (not .text)
    so ElementTree honours the document's own encoding declaration."""
    resp = common.fetch(url, timeout=30)
    data = resp.content
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def parse_sitemap_index(xml_bytes):
    """<sitemapindex><sitemap><loc>...</loc></sitemap>...</sitemapindex> -> list of sub-sitemap URLs.
    Namespace-aware findall ignores any unrelated tags (some sitemap hosts inject
    stray <script> elements) since we only match sm:sitemap/sm:loc specifically."""
    root = ET.fromstring(xml_bytes)
    return [el.text.strip() for el in root.findall(".//sm:sitemap/sm:loc", NS) if el.text]


def parse_urlset(xml_bytes):
    """<urlset><url><loc>...</loc></url>...</urlset> -> list of page URLs."""
    root = ET.fromstring(xml_bytes)
    return [el.text.strip() for el in root.findall(".//sm:url/sm:loc", NS) if el.text]


def collect_all_urls_from_index(index_url):
    """Fetch a sitemap index, then every sub-sitemap it lists, and return the
    union of every <loc> URL found across all of them. A sub-sitemap that
    can't be fetched is logged and skipped rather than aborting the crawl."""
    sub_sitemaps = parse_sitemap_index(fetch_xml(index_url))
    all_urls = []
    for sm_url in sub_sitemaps:
        try:
            all_urls.extend(parse_urlset(fetch_xml(sm_url)))
        except Exception as exc:
            print(f"WARNING sub-sitemap skipped {sm_url}: {type(exc).__name__}: {exc}")
    return all_urls


# ---------------------------------------------------------------------------
# GO-TO / INCA (Shopify): fetch /sitemap.xml, every sub-sitemap, keep URLs
# containing /products/ - robust to whatever the sub-sitemap files are named.
# ---------------------------------------------------------------------------
def crawl_shopify_products(sitemap_index_url):
    all_urls = collect_all_urls_from_index(sitemap_index_url)
    products = {u.split("?")[0].split("#")[0] for u in all_urls if "/products/" in u}
    return sorted(products)


# ---------------------------------------------------------------------------
# MECCA: 3 named catalog sitemaps only (content/services deliberately excluded),
# keep URLs ending in -I-<digits>/ or -V-<digits>/
# ---------------------------------------------------------------------------
MECCA_CATALOG_SITEMAPS = [
    "https://www.mecca.com/en-au/sitemaps_catalog_1.xml",
    "https://www.mecca.com/en-au/sitemaps_catalog_2.xml",
    "https://www.mecca.com/en-au/sitemaps_catalog_3.xml",
]
_MECCA_PRODUCT_PATTERN = re.compile(r"-[IV]-\d+/?$")


def crawl_mecca_products():
    all_urls = []
    for sm_url in MECCA_CATALOG_SITEMAPS:
        try:
            all_urls.extend(parse_urlset(fetch_xml(sm_url)))
        except Exception as exc:
            print(f"WARNING Mecca sitemap skipped {sm_url}: {type(exc).__name__}: {exc}")
    products = {u for u in all_urls if _MECCA_PRODUCT_PATTERN.search(u)}
    return sorted(products)
