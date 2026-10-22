"""
Scrapers/common.py - shared helpers used by every scraper and by the pipeline.

  - fetch(): one HTTP GET for the whole project, with retries on network
    errors and on 429/5xx so a DNS blip or a rate-limit doesn't fail a pair.
    Every response is cached on disk (cache/http/) so a re-run within
    CACHE_MAX_AGE_HOURS re-reads the saved page instead of hitting the site.
  - llm_cached(): disk cache (cache/llm/) around every Gemini text call, so a
    re-run never pays for the same prompt twice. Vision calls use it too.
  - split_ingredients(): label text -> list of ingredient strings. Aware of
    brackets and of commas inside names ("1,2-Hexanediol"), strips
    "Ingredients:" and "May contain (+/-)" prefixes.
  - find_packaging_mentions(): packaging keyword + material word in one chunk.
  - get_packaging_from_images(): Gemini vision fallback, in-memory only.
  - build_product_record(): the dict every scraper hands to the pipeline.

The only files written here are the cache/ entries described above. Delete
the cache/ folder (or run with --refresh) to force everything to be fetched
again; run with --offline to refuse all network and use the cache only.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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


# ---------------------------------------------------------------------------
# DISK CACHE
# ---------------------------------------------------------------------------
# cache/http/<host>/<sha1(url)>.bin + .json   - every fetched page/sitemap/image
# cache/llm/<sha1(model+prompt)>.json         - every Gemini answer
#
# Modes (set once by run_pipeline.py from its command line):
#   "use"     default - read a cached copy if it is younger than CACHE_MAX_AGE_HOURS,
#             otherwise fetch and store
#   "refresh" always fetch, always store (ignore what is cached)
#   "offline" never touch the network; a URL that is not cached raises CacheMiss
CACHE_DIR = Path(os.getenv("PIPELINE_CACHE_DIR", "cache"))
CACHE_MAX_AGE_HOURS = float(os.getenv("PIPELINE_CACHE_MAX_AGE_HOURS", "24"))
CACHE_MODE = "use"
CACHE_STATS = {"http_hit": 0, "http_miss": 0, "llm_hit": 0, "llm_miss": 0}


class CacheMiss(Exception):
    """Raised in offline mode for a URL/prompt that has never been fetched."""


def set_cache_mode(mode: str) -> None:
    if mode not in ("use", "refresh", "offline"):
        raise ValueError(f"unknown cache mode {mode!r}")
    global CACHE_MODE
    CACHE_MODE = mode


def _cache_key(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _is_fresh(meta_path: Path) -> bool:
    if CACHE_MODE == "offline":
        return meta_path.exists()
    if CACHE_MODE == "refresh" or not meta_path.exists():
        return False
    age_hours = (time.time() - meta_path.stat().st_mtime) / 3600
    return age_hours <= CACHE_MAX_AGE_HOURS


class CachedResponse:
    """The subset of httpx.Response the project uses (.text, .content,
    .status_code, .headers, .url), rebuilt from the cache files."""

    def __init__(self, url: str, content: bytes, status_code: int, headers: dict, encoding: Optional[str]):
        self.url = url
        self.content = content
        self.status_code = status_code
        self.headers = headers
        self.encoding = encoding or "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")


def _http_paths(url: str) -> tuple[Path, Path]:
    host = re.sub(r"[^a-z0-9.-]", "_", (httpx.URL(url).host or "unknown").lower())
    base = CACHE_DIR / "http" / host / _cache_key(url)
    return base.with_suffix(".bin"), base.with_suffix(".json")


def fetch(url: str, *, timeout: float = 20.0, retries: int = 3, backoff: float = 2.0,
          headers: Optional[dict] = None):
    """GET `url` through the disk cache. Returns an httpx.Response on a live
    fetch or a CachedResponse from the cache; both have .text/.content/
    .status_code/.headers. See the DISK CACHE notes above for the modes."""
    body_path, meta_path = _http_paths(url)
    if _is_fresh(meta_path) and body_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            CACHE_STATS["http_hit"] += 1
            return CachedResponse(url, body_path.read_bytes(), meta.get("status_code", 200),
                                  meta.get("headers", {}), meta.get("encoding"))
        except (OSError, ValueError):
            pass  # unreadable entry: fall through to a real fetch
    if CACHE_MODE == "offline":
        raise CacheMiss(f"offline mode and no cached copy of {url}")
    CACHE_STATS["http_miss"] += 1
    resp = _fetch_network(url, timeout=timeout, retries=retries, backoff=backoff, headers=headers)
    try:
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(resp.content)
        meta_path.write_text(json.dumps({
            "url": url, "final_url": str(resp.url), "status_code": resp.status_code,
            "encoding": resp.encoding, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "headers": {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "last-modified", "etag")},
        }, indent=1), "utf-8")
    except OSError as exc:
        print(f"cache write failed for {url}: {exc}")
    return resp


def llm_cached(model: str, prompt_key: str, call: Callable[[], Optional[str]]) -> Optional[str]:
    """Run `call` (which returns the model's raw text, or None on failure) unless
    the same model+prompt was answered before, in which case return the saved
    answer. Failures are never cached. In offline mode an unanswered prompt
    returns None (the caller's normal failure path) rather than raising."""
    path = CACHE_DIR / "llm" / f"{_cache_key(model, prompt_key)}.json"
    if CACHE_MODE != "refresh" and path.exists():
        try:
            CACHE_STATS["llm_hit"] += 1
            return json.loads(path.read_text("utf-8"))["text"]
        except (OSError, ValueError, KeyError):
            pass
    if CACHE_MODE == "offline":
        return None
    CACHE_STATS["llm_miss"] += 1
    text = call()
    if text is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"model": model, "key": prompt_key[:200], "text": text,
                                        "answered_at": datetime.now(timezone.utc).isoformat()}, indent=1), "utf-8")
        except OSError as exc:
            print(f"llm cache write failed: {exc}")
    return text


def cache_summary() -> str:
    s = CACHE_STATS
    return (f"cache: pages {s['http_hit']} from cache, {s['http_miss']} fetched; "
            f"Gemini {s['llm_hit']} from cache, {s['llm_miss']} called (mode {CACHE_MODE})")


def _fetch_network(url: str, *, timeout: float = 20.0, retries: int = 3, backoff: float = 2.0,
          headers: Optional[dict] = None) -> httpx.Response:
    """The real GET. Retries on transport errors (DNS, reset, timeout) and on
    transient status codes (429, 5xx). A 4xx other than 429 is raised at once -
    a 404 will never succeed on retry."""
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

            def ask():
                result = GEMINI_CLIENT.models.generate_content(
                    model=GEMINI_MODEL, contents=[image_part, _VISION_PROMPT], config=config)
                return (result.text or "").strip() or None

            raw = llm_cached(GEMINI_MODEL, "vision|" + url + "|" + _VISION_PROMPT, ask)
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
