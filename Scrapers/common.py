"""
Scrapers/common.py - shared helpers used by every scraper and by the pipeline.

  - fetch(): one HTTP GET for the whole project, with retries on network
    errors and on 429/5xx so a DNS blip or a rate-limit doesn't fail a pair.
  - split_ingredients(): label text -> list of ingredient strings. Aware of
    brackets and of commas inside names ("1,2-Hexanediol"), strips
    "Ingredients:" and "May contain (+/-)" prefixes.
  - find_packaging_mentions(): packaging keyword + material word in one chunk.
  - get_packaging_from_images(): Gemini vision fallback, in-memory only.
  - build_product_record(): the dict every scraper hands to the pipeline.

Nothing here writes files. The old save_product_json / data/ helpers were
left over from the standalone build and have been removed.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()  # reads GEMINI_API_KEY from a .env file in the project root

GEMINI_MODEL = "gemini-3.6-flash"

try:
    from google import genai
    from google.genai import types

    _api_key = os.getenv("GEMINI_API_KEY")
    GEMINI_CLIENT = genai.Client(api_key=_api_key) if _api_key else None
except ImportError:  # package not installed - every Gemini step degrades to "not found"
    genai = None
    types = None
    GEMINI_CLIENT = None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
}
RETRY_STATUS = {429, 500, 502, 503, 504}


def fetch(url: str, *, timeout: float = 20.0, retries: int = 3, backoff: float = 2.0,
          headers: Optional[dict] = None) -> httpx.Response:
    """GET `url`, retrying on transport errors (DNS, reset, timeout) and on
    transient status codes (429, 5xx). A 4xx other than 429 is raised at once -
    a 404 will never succeed on retry. Returns the Response; callers use
    .text or .content."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.get(url, headers=headers or HEADERS, follow_redirects=True, timeout=timeout)
            if resp.status_code in RETRY_STATUS and attempt < retries:
                wait = backoff * attempt
                retry_after = resp.headers.get("retry-after", "")
                if retry_after.isdigit():
                    wait = max(wait, int(retry_after))
                print(f"HTTP {resp.status_code} for {url} - retry {attempt}/{retries - 1} in {wait:.0f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt < retries:
                wait = backoff * attempt
                print(f"{type(exc).__name__} for {url} - retry {attempt}/{retries - 1} in {wait:.0f}s")
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# INGREDIENTS
# ---------------------------------------------------------------------------
# "Ingredients:", "Full ingredients list -", "INCI:" etc. at the very start
_LIST_PREFIX = re.compile(r"^\s*(?:full\s+)?(?:ingredients?|inci)(?:\s+list)?\s*[:\-–]\s*", re.IGNORECASE)
# "May contain", "+/-", "(+/-)" and any run of ": ( ) + / -" after them, at the
# start of an item. Repeated so "May contain (+/-): CI 77891" -> "CI 77891".
_MAY_CONTAIN = re.compile(r"^\s*(?:(?:may\s+contain|\+/-|\(\+/-\))[\s:()+/\-]*)+", re.IGNORECASE)
_SEPARATORS = ",;•·|"


def split_ingredients(ingredients_text: Optional[str]) -> list[str]:
    """
    "Aqua, 1,2-Hexanediol, Parfum (Fragrance), May contain (+/-): CI 77891."
      -> ["Aqua", "1,2-Hexanediol", "Parfum (Fragrance)", "CI 77891"]

    Splits on , ; • · | at bracket depth 0 only, and never on a comma that
    sits between two digits (chemical locants like 1,2- and 1,3-).
    """
    if not ingredients_text:
        return []
    text = _LIST_PREFIX.sub("", ingredients_text.strip())
    parts, buf, depth = [], [], 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if depth == 0 and ch in _SEPARATORS:
            locant = ch == "," and buf and buf[-1].isdigit() and text[i + 1:i + 2].isdigit()
            if not locant:
                parts.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    parts.append("".join(buf))
    out = []
    for p in parts:
        p = _MAY_CONTAIN.sub("", p.strip()).strip(" .")
        if p:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# PACKAGING (page text)
# ---------------------------------------------------------------------------
# Word-boundary matched so nav junk like "Boxer Briefs" or "Toiletry Bags"
# can't false-positive on "box"/"bag". Keep this list scoped to real
# packaging vocabulary, not generic container words.
PACKAGING_KEYWORDS = [
    "jar", "pouch", "tub", "tin", "bottle", "resealable",
    "sachet", "carton", "box", "pump", "tube", "canister",
    "packaging", "container", "lid", "cap",
]
_PACKAGING_PATTERN = re.compile(
    r"\b(" + "|".join(PACKAGING_KEYWORDS) + r")\b", re.IGNORECASE
)

# A packaging keyword alone isn't enough - "pump" shows up constantly in "Usage:
# dispense one pump" instructions, which is NOT packaging material info. Require
# a material/eco word to co-occur in the same chunk.
_MATERIAL_WORDS = re.compile(
    r"\b(glass|plastic|polypropylene|polyethylene|hdpe|ldpe|pet|aluminium|aluminum|"
    r"cardboard|paper|paperboard|metal|steel|recycled|recyclable|virgin|materials?|fsc|"
    r"biodegradable|compostable|stainless|acrylonitrile|abs|pp|evoh|wood|bamboo|"
    r"ceramic|silicone)\b",
    re.IGNORECASE,
)


def find_packaging_mentions(text: Optional[str]) -> Optional[str]:
    """
    Search `text` for a packaging keyword AND a material/eco word co-occurring
    in the same chunk. Returns every such chunk joined with "; " (a page can
    describe the bottle and the carton in separate sentences), or None.

    Splits on sentence punctuation, newlines, AND " - " bullet separators, so
    unrelated sections/bullets can never bleed into each other even when the
    source HTML has no clean punctuation between them.
    """
    if not text:
        return None
    hits = []
    for chunk in re.split(r"(?<=[.!?])\s+|\n+|\s+-\s+", text):
        chunk = chunk.strip(" .")
        if chunk and _PACKAGING_PATTERN.search(chunk) and _MATERIAL_WORDS.search(chunk) and chunk not in hits:
            hits.append(chunk)
    return "; ".join(hits) if hits else None


# ---------------------------------------------------------------------------
# PRODUCT RECORD
# ---------------------------------------------------------------------------
def build_product_record(
    *,
    website: str,
    url: str,
    product_name: str,
    brand: Optional[str],
    ingredients_text: Optional[str],
    packaging_text: Optional[str],
    packaging_source: str,
) -> dict:
    """packaging_source is one of: "text", "image", "not_found"."""
    return {
        "website": website,
        "url": url,
        "product_name": product_name,
        "brand": brand,
        "ingredients": split_ingredients(ingredients_text),
        "ingredients_raw": ingredients_text,
        "packaging": [packaging_text] if packaging_text else [],
        "packaging_source": packaging_source,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def failed_product_record(website: str, url: str, error: str) -> dict:
    """What the pipeline records when a page couldn't be fetched or parsed, so
    the pair still produces a row (the other side's colour is used)."""
    return {
        "website": website,
        "url": url,
        "product_name": None,
        "brand": None,
        "ingredients": [],
        "ingredients_raw": None,
        "packaging": [],
        "packaging_source": "not_found",
        "status": "scrape_failed",
        "error": error,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# PACKAGING (image fallback)
# ---------------------------------------------------------------------------
_VISION_PROMPT = (
"""You are extracting PACKAGING information from a product image scraped from a retail website.

"Packaging information" means printed text or symbols stating what the product's
packaging/container is made of, or its environmental/recycling/disposal
properties (material, recycled content, recyclability, compostability,
sustainability claims about the pack). It does NOT include: ingredients,
nutrition, certifications about the contents (e.g. halal, kosher, vegan),
usage or storage instructions, safety warnings, tamper-seal notices,
marketing slogans, or your own description of the container's look.

Steps:
1. Transcribe ALL legible text in the image, verbatim, in reading order,
   including small print. Skip any text that is cut off, blurred or unclear.
   Do not guess at missing words.
2. From your transcription ONLY, select lines that are packaging information
   as defined above.
3. Each claim must be copied exactly from your transcription. If a claim is
   only partially legible, leave it out.

If the transcription contains no packaging information, packaging_claims must
be an empty list. That is the correct and expected answer for many images.
Never describe the container's shape, colour or apparent material.

Respond with JSON only, with the keys in this order:
{"transcription": "<all legible text>", "packaging_claims": ["<exact line>", ...]}"""
)

_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}


def get_packaging_from_images(image_urls: list[str], max_concurrent: int = 15,
                              max_images: int = 12) -> Optional[str]:
    """
    Checks product photos in reverse order (packaging/back-of-pack info is
    usually near the end of the gallery), up to `max_concurrent` in flight at
    once and at most `max_images` in total (bounds the Gemini spend per
    product). Returns the first one with packaging claims.
    Returns None if the API key isn't set, the package isn't installed, all
    requests fail, or no image contains packaging info. Never raises.
    """
    if genai is None:
        print("google-genai not installed - skipping image fallback")
        return None
    if GEMINI_CLIENT is None:
        print("GEMINI_API_KEY not set - skipping image fallback")
        return None

    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0)

    def _norm(s: str) -> str:
        return " ".join(s.lower().split())

    def check_one(url: str) -> list[str]:
        try:
            resp = fetch(url, timeout=15, retries=2)
            mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
            if mime not in _IMAGE_TYPES:
                ext = url.lower().rsplit(".", 1)[-1].split("?")[0]
                mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                        "webp": "image/webp"}.get(ext, "image/jpeg")
            image_part = types.Part.from_bytes(data=resp.content, mime_type=mime)
            result = GEMINI_CLIENT.models.generate_content(
                model=GEMINI_MODEL, contents=[image_part, _VISION_PROMPT], config=config)
            raw = (result.text or "").strip()
            if not raw:
                return []
            data, _ = json.JSONDecoder().raw_decode(raw)
            transcript = _norm(str(data.get("transcription", "")))
            claims = data.get("packaging_claims", [])
            if not isinstance(claims, list):
                return []
            # only keep claims the model actually transcribed - stops it inventing
            return [c for c in claims if isinstance(c, str) and c.strip() and _norm(c) in transcript]
        except Exception as exc:
            print(f"Vision fallback failed on {url}: {type(exc).__name__}: {exc}")
            return []

    candidates = list(reversed(image_urls))[:max_images]
    if not candidates:
        return None

    found: list[str] = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent)
    try:
        futures = {executor.submit(check_one, url): url for url in candidates}
        for future in concurrent.futures.as_completed(futures):
            claims = future.result()
            if claims:
                found = [c for c in claims if c not in found] + found
                break  # first image that answers wins - stop waiting on the rest
    finally:
        executor.shutdown(wait=False, cancel_futures=True)  # cancels anything still queued/unstarted

    return "; ".join(found) if found else None
