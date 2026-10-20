"""
match_products.py (project root) - Stage 4: pairs gotoskincare.com products
against Mecca's Go-To listings, in memory. Called by run_pipeline.py; writes
no files itself.

Exact slug match is done first by run_pipeline.py; the functions here also
support the fuzzy fallback - scoring leftover candidates and settling each
one with an LLM verdict on the two page titles ("same" / "different" /
"uncertain"), never a human click.
"""
import html
import json
import re

from Scrapers import common

# tokens that don't help identify the product but do flag a multi-product
# listing that can't be scored as one formula. "double" catches Go-To's Double
# Cleanse bundle naming (soft-and-creamy-double-cleanse).
REVIEW_FLAG_WORDS = {"set", "kit", "bundle", "duo", "trio", "gift", "discovery",
                     "double", "collection", "starter", "minis"}
# true filler only — "the" is a stylistic prefix Go-To uses inconsistently,
# "copy" is a Shopify duplicate-slug artifact. Everything else (refill, mini,
# gift, size, colour words) is a real SKU differentiator and must be kept,
# or products that are genuinely different get scored as identical.
NOISE_WORDS = {"the", "copy"}
SIZE_RE = re.compile(r"^\d+(?:ml|g|kg|l|oz)$")
MECCA_ID_RE = re.compile(r"-[A-Z]-\d+$")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# og:title with the attributes in either order
_OG_TITLE_RES = [
    re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:title["\']', re.IGNORECASE),
]
# " | MECCA", " – Go-To Skincare" etc. on the end of <title>
_TITLE_SUFFIX_RE = re.compile(r"\s*[|–—-]\s*(mecca|go-to skincare|go-to)\s*$", re.IGNORECASE)

_VERIFY_PROMPT = """Two product listings, possibly the same product on two different retailers.

Go-To Skincare (the brand's own store) title: {t1}
Mecca (a multi-brand retailer) title: {t2}

Are these the same physical product (same formula)?
- A bundle, kit, set, duo or trio that CONTAINS the product is NOT the same as
  the product sold alone - answer "different", even if the product name is a
  substring of the bundle's name.
- Different shades, colours, scents or flavours are different products.
- If one title is too vague to be sure, answer "uncertain".

Respond with JSON only: {{"verdict": "same" | "different" | "uncertain"}}"""

_title_cache = {}


def goto_slug(url):
    return url.rstrip("/").split("/products/")[-1]


def mecca_slug(url):
    raw = url.rstrip("/").split("/")[-1]
    return MECCA_ID_RE.sub("", raw)


def normalise(slug):
    """Token set used only for fuzzy scoring, not for the exact pass.
    Sorted (not left in URL order) so word-order swaps like
    "pinky-lip-glaze" vs "lip-glaze-pinky" still score as identical."""
    tokens = sorted(t for t in slug.split("-") if t and t not in NOISE_WORDS and not SIZE_RE.match(t))
    return "-".join(tokens)


def has_review_flag(slug):
    return bool(REVIEW_FLAG_WORDS & set(slug.split("-")))


def fetch_title(url):
    """og:title first (cleaner product name), <title> as fallback. None on any
    failure. Cached per run so a URL that is a candidate for several products
    is fetched once."""
    if url in _title_cache:
        return _title_cache[url]
    title = None
    try:
        text = common.fetch(url, timeout=15).text
        m = None
        for rx in _OG_TITLE_RES:
            m = rx.search(text)
            if m:
                break
        if not m:
            m = _TITLE_RE.search(text)
        if m:
            title = html.unescape(m.group(1)).strip()
            title = _TITLE_SUFFIX_RE.sub("", title).strip() or None
    except Exception as exc:
        print(f"Title fetch failed for {url}: {type(exc).__name__}: {exc}")
    _title_cache[url] = title
    return title


def llm_verify(t1, t2):
    """same / different / uncertain. Falls back to "uncertain" (never auto-accepted)
    if the client isn't configured or the call fails - a missing API key must
    never silently turn into a false "same"."""
    if common.GEMINI_CLIENT is None or common.types is None:
        return "uncertain"
    config = common.types.GenerateContentConfig(response_mime_type="application/json", temperature=0)
    try:
        result = common.GEMINI_CLIENT.models.generate_content(
            model=common.GEMINI_MODEL,
            contents=[_VERIFY_PROMPT.format(t1=t1, t2=t2)],
            config=config,
        )
        data, _ = json.JSONDecoder().raw_decode((result.text or "").strip())
        verdict = str(data.get("verdict", "uncertain")).strip().lower() if isinstance(data, dict) else "uncertain"
        return verdict if verdict in ("same", "different", "uncertain") else "uncertain"
    except Exception as exc:
        print(f"LLM verify failed: {type(exc).__name__}: {exc}")
        return "uncertain"
