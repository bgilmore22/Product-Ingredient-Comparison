"""
analyse.py (project root)

Pure comparison logic: ingredients, packaging, naughty-list scoring, and
analyse_pair() to run all three on two already-scraped records. Called by
run_pipeline.py and test_analyse.py; writes no files. The packaging-materials
cache is in memory for the run only.

Needs: rapidfuzz, and (only when packaging text has to be turned into material
categories) the google-genai package plus GEMINI_API_KEY. Without the key the
packaging step degrades to "lists no materials" instead of failing the pair.
"""
import hashlib
import json
import re
import unicodedata

from dotenv import load_dotenv
from rapidfuzz import fuzz

from naughty_list import NAUGHTY_LIST
try:
    from naughty_list import CATEGORY_PRIORITY
except ImportError:                       # older naughty_list.py without the list: file order
    CATEGORY_PRIORITY = list(NAUGHTY_LIST)
from Scrapers import common

load_dotenv()
MATERIALS_MODEL = common.GEMINI_MODEL

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
# Which store is Source 1. ONE-LINE CHANGE:
#   "retailer" -> Source 1 is the retailer (Mecca), Source 2 the brand's own store
#   "brand"    -> Source 1 is the brand's own store, Source 2 the retailer
SOURCE1 = "retailer"

# ---------------------------------------------------------------------------
# 1. INGREDIENT COMPARISON (code only)
# ---------------------------------------------------------------------------
# True synonyms only. A string whose words are ALL in one group becomes that
# group's canonical name ("aqua water" -> "water"). Related-but-different
# ingredients (hyaluronic acid / sodium hyaluronate) must NOT go here.
SYNONYM_GROUPS = {
    "water": {"aqua", "water", "eau", "purified"},
    "fragrance": {"parfum", "fragrance", "perfume"},
    "glycerin": {"glycerin", "glycerine", "glycerol"},
}

WHOLE_STRING_CLOSE = 85   # fuzz.ratio on the whole normalised name
TOKEN_CLOSE = 75          # fuzz.ratio between two differing words (>= 6 letters)
MIN_TOKEN_LEN = 6


def normalise(item):
    """Comparison form only; the raw text is always kept alongside."""
    s = re.sub(r"[*†‡§¹²³⁴⁵⁶⁷⁸⁹⁰]", " ", item)          # footnote marks (before NFKC turns ¹ into 1)
    s = unicodedata.normalize("NFKC", s).lower()
    prev = None
    while prev != s:                                       # bracket content, incl. nested
        prev = s
        s = re.sub(r"[(\[{][^()\[\]{}]*[)\]}]", " ", s)
    s = re.sub(r"\d+(?:[.,]\d+)?\s*%", " ", s)             # percentages
    s = re.sub(r"\borganic\b", " ", s)
    s = re.sub(r"[\W_]+", " ", s)                          # punctuation -> SPACE ("aqua/water" -> "aqua water")
    s = re.sub(r"\s+", " ", s).strip()
    words = set(s.split())
    for canonical, group in SYNONYM_GROUPS.items():
        if words and words <= group:
            return canonical
    return s


def _index(items):
    """normalised name -> list of raw items (label order kept)."""
    out = {}
    for raw in items:
        n = normalise(raw)
        if n:
            out.setdefault(n, []).append(raw)
    return out


def _close_score(a, b):
    """Score if a and b look like related-but-different ingredients, else None."""
    ta, tb = set(a.split()), set(b.split())
    best = None
    whole = fuzz.ratio(a, b)
    if whole >= WHOLE_STRING_CLOSE:
        best = whole
    if ta < tb or tb < ta:                                 # one name is the other plus extra words
        best = 100.0
    for x in ta - tb:
        for y in tb - ta:
            if len(x) >= MIN_TOKEN_LEN and len(y) >= MIN_TOKEN_LEN:
                r = fuzz.ratio(x, y)
                if r >= TOKEN_CLOSE and (best is None or r > best):
                    best = r
    return None if best is None else round(best, 1)


def _squash(s):
    return re.sub(r"\s+", " ", s.lower()).strip()


# INCI vs common names. Go-To writes "prunus amygdalus dulcis (sweet almond) oil",
# Mecca writes "sweet almond oil". normalise() drops the bracket, so the two never
# meet. These are true synonyms, not related-but-different ingredients.
NAME_ALIASES = {
    "tocopherol": "vitamin e",
    "tocopheryl acetate": "vitamin e acetate",
    "ubiquinone": "coenzyme q10",
    "phyllanthus emblica fruit extract": "amla berry extract",
    "ascorbic acid": "vitamin c",
    "niacinamide": "vitamin b3",
    "panthenol": "provitamin b5",
    "ci 77891": "titanium dioxide",
    "ci 77491": "iron oxides",
    "ci 77492": "iron oxides",
    "ci 77499": "iron oxides",
    "ci 77007": "ultramarines",
    "ci 77019": "mica",
    "ci 77510": "ferric ferrocyanide",
    "ci 75470": "carmine",
    "ci 77266": "carbon black",
    "ci 77742": "manganese violet",
    "ci 77163": "bismuth oxychloride",
    "butyrospermum parkii butter": "shea butter",
    "simmondsia chinensis seed oil": "jojoba seed oil",
    "aloe barbadensis leaf juice": "aloe vera leaf juice",
    "sodium chloride": "salt",
}


def _alt_keys(raw):
    """Extra comparison forms for one raw ingredient name."""
    keys = set()
    m = re.search(r"\(([^()]+)\)", raw)
    if m:                                                  # "x y (sweet almond) oil" -> "sweet almond oil"
        common_name = m.group(1) + " " + raw[m.end():]
        keys.add(normalise(common_name))
        keys.add(normalise(re.sub(r"\bseed\b", " ", common_name, flags=re.I)))
    n = normalise(raw)
    keys.add(n)
    keys.add(NAME_ALIASES.get(n, n))
    keys.discard("")
    return keys


def _match_alt_names(n1, n2, only1, only2):
    """Pairs up leftovers that are the same ingredient written two ways.
    Returns (still_only1, still_only2, number_paired)."""
    keys2 = {b: _alt_keys(n2[b][0]) for b in only2}
    left2, left1, paired = set(only2), [], 0
    for a in only1:
        ka = _alt_keys(n1[a][0])
        hit = next((b for b in only2 if b in left2 and ka & keys2[b]), None)
        if hit:
            left2.discard(hit)
            paired += 1
        else:
            left1.append(a)
    return left1, [b for b in only2 if b in left2], paired


def compare_ingredients(items1, items2):
    items1, items2 = items1 or [], items2 or []
    base = {"only_in_source1": [], "only_in_source2": [], "close_matches": [], "raw_text_differences": []}
    if not items1 or not items2:
        missing = [n for n, i in (("source 1", items1), ("source 2", items2)) if not i]
        reason = (missing[0] + " has no ingredient list") if len(missing) == 1 else "neither source has an ingredient list"
        return {"comparison": "cannot_compare", "reason": reason, **base}

    n1, n2 = _index(items1), _index(items2)
    only1 = [k for k in n1 if k not in n2]
    only2 = [k for k in n2 if k not in n1]
    only1, only2, renamed = _match_alt_names(n1, n2, only1, only2)

    close = []
    for a in only1:                                        # never merged: reported next to only_in_*
        scored = [(s, b) for b in only2 if (s := _close_score(a, b)) is not None]
        if scored:
            s, b = max(scored)
            close.append({"source1": n1[a][0], "source2": n2[b][0], "score": s})

    raw_diffs = [{"normalised": k, "source1": n1[k][0], "source2": n2[k][0]}
                 for k in n1 if k in n2 and _squash(n1[k][0]) != _squash(n2[k][0])]

    out = {
        "only_in_source1": [r for k in only1 for r in n1[k]],
        "only_in_source2": [r for k in only2 for r in n2[k]],
        "close_matches": close,
        "raw_text_differences": raw_diffs,
    }
    if not only1 and not only2:
        reason = f"all {len(n1)} ingredients match after normalising"
        if renamed:
            reason += f" ({renamed} matched via INCI/common names)"
        if raw_diffs:
            reason += f" ({len(raw_diffs)} with different raw text, see raw_text_differences)"
        return {"comparison": "same", "reason": reason, **out}
    reason = f"{len(only1)} only in source 1, {len(only2)} only in source 2"
    if renamed:
        reason += f"; {renamed} matched via INCI/common names"
    if close:
        reason += f"; {len(close)} flagged as close match, not merged"
    return {"comparison": "different", "reason": reason, **out}


# ---------------------------------------------------------------------------
# 1b. KIT DETECTION (title words here; AI check in run_pipeline.py; slug words in match_products.py)
# ---------------------------------------------------------------------------
# A retailer page for a kit sometimes pools every product's INCI list into one
# block (Mecca's Skintroduction does this). Scoring that as one product is
# wrong. Two signals that a single formula never shows: water listed more than
# once, or several "Product name: ingredients" headers.
_HEADER_ITEM = re.compile(r"^\s*[^:]{3,80}:\s*\S")


# Words a retailer/brand puts in a title ON PURPOSE when the listing is several
# products sold together. ("mini" alone is NOT here: "Properly Clean Mini" is a
# single product.) Slug words are checked separately in match_products.py.
_KIT_TITLE = re.compile(
    r"\b(kit|set|bundle|duo|trio|collection|calendar|advent|discovery|starter|minis|gifts?|"
    r"(?:twelve|12)\s+days(?:\s+of)?)\b", re.IGNORECASE)


def kit_title_reason(titles):
    """titles: [(site, title), ...]. A reason string if a title names a kit, else None."""
    for site, title in titles:
        m = _KIT_TITLE.search(title or "")
        if m:
            return f"{site} title '{title}' contains '{m.group(0)}'"
    return None


def pooled_list_warning(items):
    """NOT a skip reason: ingredient-text structure (repeated 'Go-To X:' headers) cannot tell
    a kit from a shade list, so it only produces a note on the row."""
    branded = [i for i in (items or []) if _BRAND_HEADER.match(i)]
    if len(branded) >= 2:
        return f"ingredient list has {len(branded)} product headers (e.g. '{branded[0][:40]}') - may be several products"
    return None


_BRAND_HEADER = re.compile(r"^\s*go-to\b[^:]{0,70}:\s*\S", re.IGNORECASE)   # "Go-To Face Hero: Aqua"


def flatten_variants(items):
    """For a page listing a few shades/variants in one block: strip the 'Shade:'
    prefixes and de-duplicate, so every ingredient from any variant is scored once.
    Returns (items, note); note is '' for an ordinary single list."""
    if not items:
        return items, ""
    headers = [i for i in items if _HEADER_ITEM.match(i)]
    water = sum(1 for i in items if normalise(i) == "water")
    if not headers and water < 2:
        return items, ""
    out, seen = [], set()
    for i in items:
        if _HEADER_ITEM.match(i):
            i = i.split(":", 1)[1].strip()
        key = normalise(i)
        if i and key not in seen:
            seen.add(key)
            out.append(i)
    n = max(len(headers), water)
    return out, f"lists {n} variants/shades in one block - scored on all of them combined"


# ---------------------------------------------------------------------------
# 2. PACKAGING COMPARISON
# ---------------------------------------------------------------------------
MATERIALS_PROMPT = (
    "You read text about a product's packaging and list the packaging materials it names. "
    "Reply with ONLY a JSON array of short lowercase material names, e.g. "
    '["glass", "polypropylene", "cardboard"]. Use the most specific material the text states '
    "(polypropylene rather than plastic when it says polypropylene). One entry per material, no "
    "duplicates. Ignore certifications, recyclability claims and marketing. Do not guess: if the "
    "text names no material, reply []."
)

# Same material, different spellings/abbreviations. Applied to scraper-provided
# AND LLM-returned lists, so "pp" on one site and "polypropylene" on the other
# compare as the same.
MATERIAL_SYNONYMS = {
    "pp": "polypropylene",
    "pet": "polyethylene terephthalate",
    "pete": "polyethylene terephthalate",
    "rpet": "polyethylene terephthalate",
    "recycled pet": "polyethylene terephthalate",
    "hdpe": "high-density polyethylene",
    "high density polyethylene": "high-density polyethylene",
    "ldpe": "low-density polyethylene",
    "low density polyethylene": "low-density polyethylene",
    "pe": "polyethylene",
    "abs": "acrylonitrile butadiene styrene",
    "aluminum": "aluminium",
    "paperboard": "cardboard",
    "card": "cardboard",
    "carton board": "cardboard",
    "fsc paper": "paper",
    "fsc-certified paper": "paper",
    "recycled paper": "paper",
    "recycled glass": "glass",
    "recycled plastic": "plastic",
}


def canonical_materials(names):
    out = set()
    for m in names:
        m = re.sub(r"\s+", " ", str(m).strip().lower())
        if not m:
            continue
        m = MATERIAL_SYNONYMS.get(m, m)
        m = re.sub(r"^(?:post[- ]consumer\s+)?recycled\s+", "", m)   # "recycled aluminium" -> "aluminium"
        m = MATERIAL_SYNONYMS.get(m, m)
        if m:
            out.add(m)
    return sorted(out)


class LLMMaterialExtractor:
    """packaging text -> list of material categories. Cached by text hash so the
    same text is only sent once per run. Used only when a record has no
    `packaging_materials` list.

    Never raises: a Gemini failure (no key, 402 credits, network) returns []
    and is logged, so the pair still gets a row. After three failures in a
    row the extractor stops calling the API for the rest of the run."""

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, model=MATERIALS_MODEL):
        self.model = model
        self.cache = {}
        self.failures = 0
        self.disabled_reason = None
        if common.GEMINI_CLIENT is None:
            self.disabled_reason = ("google-genai not installed" if common.genai is None
                                    else "GEMINI_API_KEY not set")
            print(f"packaging materials: {self.disabled_reason} - packaging text will not be "
                  f"turned into material categories this run")

    def __call__(self, text):
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if key not in self.cache:
            result = self._ask(text)
            if result is None:          # failure: don't cache, so a later retry can succeed
                return []
            self.cache[key] = result
        return self.cache[key]

    def _ask(self, text):
        if self.disabled_reason:
            return None
        try:
            resp = common.GEMINI_CLIENT.models.generate_content(
                model=self.model, contents=text,
                config=common.types.GenerateContentConfig(
                    system_instruction=MATERIALS_PROMPT,
                    response_mime_type="application/json", temperature=0))
            raw = (resp.text or "").strip()
            data, _ = json.JSONDecoder().raw_decode(raw)
            if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
                raise ValueError(f"unexpected materials reply: {raw!r}")
        except Exception as exc:
            self.failures += 1
            print(f"packaging materials: Gemini call failed ({type(exc).__name__}: {exc})")
            if self.failures >= self.MAX_CONSECUTIVE_FAILURES:
                self.disabled_reason = f"{self.failures} consecutive Gemini failures"
                print(f"packaging materials: {self.disabled_reason} - giving up for the rest of this run")
            return None
        self.failures = 0
        return canonical_materials(data)


def get_materials(rec, extractor):
    if not rec:
        return []
    if isinstance(rec.get("packaging_materials"), list):   # scraper-provided wins
        return canonical_materials(rec["packaging_materials"])
    pk = rec.get("packaging")
    text = " ".join(pk) if isinstance(pk, list) else (pk or "")
    text = text.strip()
    return canonical_materials(extractor(text)) if text else []


_GENERIC_MATERIALS = {"plastic", "metal"}


def compare_packaging(m1, m2, src1=None, src2=None):
    s1, s2 = set(m1), set(m2)
    out = {"source1_materials": sorted(s1), "source2_materials": sorted(s2),
           "packaging_source": {"source1": src1, "source2": src2}}
    if not s1 and not s2:
        return {"comparison": "cannot_compare", "reason": "neither source lists materials", **out}
    if not s1 or not s2:
        side = "source 1" if not s1 else "source 2"
        tag = src1 if not s1 else src2
        return {"comparison": "cannot_compare",
                "reason": f"{side} lists no materials (packaging_source: {tag})", **out}
    if s1 == s2:
        return {"comparison": "same", "reason": f"both list {', '.join(sorted(s1))}", **out}
    # A bare generic word ("plastic") against specific materials is a difference
    # in detail, not in packaging, so don't report it as a mismatch.
    for a, b, side in ((s1, s2, "source 1"), (s2, s1, "source 2")):
        if a <= _GENERIC_MATERIALS and not (a & b):
            return {"comparison": "cannot_compare",
                    "reason": f"{side} only says {', '.join(sorted(a))} - too generic to compare with {', '.join(sorted(b))}", **out}
    return {"comparison": "different",
            "reason": f"only in source 1: {sorted(s1 - s2) or 'none'}; only in source 2: {sorted(s2 - s1) or 'none'}",
            **out}


# ---------------------------------------------------------------------------
# 3. NAUGHTY LIST (ingredients only, each source separately)
# ---------------------------------------------------------------------------
_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in NAUGHTY_LIST.items()}
# one ingredient is filed under the FIRST category in this order that it matches
_CATEGORY_ORDER = [c for c in CATEGORY_PRIORITY if c in _COMPILED] + [c for c in _COMPILED if c not in CATEGORY_PRIORITY]
COLOURS = {0: "GREEN", 1: "YELLOW"}   # 2 or more categories -> RED

# Items that are claims, not ingredients: "Paraben-free", "No sulfates",
# "Free from silicones", "Without mineral oil". Scoring these would flag the
# very thing the label denies. Checked on the raw lowercased item.
_NEGATED_ITEM = re.compile(
    r"^\s*(?:no|free\s+(?:of|from)|without|does\s+not\s+contain|contains\s+no|formulated\s+without|"
    r"\d+\s*%\s*free)\b"
    r"|\b[a-z]+(?:\s*-\s*|\s+)free\b",
    re.IGNORECASE,
)


_CLAIM_TAIL_START = re.compile(
    r"^\s*(?:no|free\s+(?:of|from)|without|does\s+not\s+contain|contains\s+no|formulated\s+without)\s+[a-z]"
    r"|for\s+a\s+full\s+explanation|\bvisit\s+\S+\.(?:com|com\.au)\b",
    re.IGNORECASE,
)


def check_naughty(items):
    if not items:
        return {"status": "not_scored", "colour": None, "reason": "no ingredient list",
                "categories": {}, "unmatched_ingredients": [], "skipped_claims": []}
    categories, unmatched, skipped = {}, [], []
    in_claims = False
    for raw in items:
        low = raw.lower()                                  # raw text, lowercased; NOT the normalised form
        # An INCI list never continues after "No silicones; parabens; ..." or
        # "For a full explanation...", so everything after the first such opener
        # is the marketing tail ("parabens", "sulfates (SLS/SLES)"...), not ingredients.
        if _CLAIM_TAIL_START.search(low):
            in_claims = True
        if in_claims or _NEGATED_ITEM.search(low):
            skipped.append(raw)
            continue
        # one ingredient -> at most one category (the highest-priority match),
        # so a single ingredient can never take a product from YELLOW to RED
        cat = next((c for c in _CATEGORY_ORDER if any(p.search(low) for p in _COMPILED[c])), None)
        if cat:
            categories.setdefault(cat, []).append(raw)
        else:
            unmatched.append(raw)
    n = len(categories)
    colour = COLOURS.get(n, "RED")
    reason = "no categories hit" if n == 0 else f"{n} categor{'y' if n == 1 else 'ies'} hit: {', '.join(categories)}"
    if skipped:
        reason += f" ({len(skipped)} free-from claim{'s' if len(skipped) > 1 else ''} ignored)"
    return {"status": "scored", "colour": colour, "reason": reason,
            "categories": categories, "unmatched_ingredients": unmatched, "skipped_claims": skipped}


def compare_naughty(r1, r2):
    out = {"source1": r1, "source2": r2}
    if r1["status"] != "scored" or r2["status"] != "scored":
        side = [n for n, r in (("source 1", r1), ("source 2", r2)) if r["status"] != "scored"]
        return {**out, "colours_match": None, "categories_only_in_source1": None,
                "categories_only_in_source2": None, "reason": " and ".join(side) + " not scored"}
    c1, c2 = set(r1["categories"]), set(r2["categories"])
    return {**out, "colours_match": r1["colour"] == r2["colour"],
            "categories_only_in_source1": [c for c in r1["categories"] if c not in c2],
            "categories_only_in_source2": [c for c in r2["categories"] if c not in c1],
            "reason": f"source 1 {r1['colour']}, source 2 {r2['colour']}"}


# ---------------------------------------------------------------------------
# 4. PUT IT TOGETHER
# ---------------------------------------------------------------------------
def record_status(rec):
    if rec is None:
        return "missing_file"
    if rec.get("status"):
        return rec["status"]
    return "ok" if rec.get("ingredients") else "no_ingredients"


def _source_block(rec):
    if rec is None:
        return {"site": None, "url": None, "status": "missing_file"}
    return {"site": rec.get("site") or rec.get("website"), "url": rec.get("url"),
            "status": record_status(rec)}


def analyse_pair(product_name, rec1, rec2, extractor):
    """rec1/rec2 are already-loaded records (dicts) or None for a missing/failed
    source - never file paths. Called directly by run_pipeline.py with freshly
    scraped records."""
    ing1 = (rec1 or {}).get("ingredients") or []
    ing2 = (rec2 or {}).get("ingredients") or []
    return {
        "product": product_name,
        "sources": {"source1": _source_block(rec1), "source2": _source_block(rec2)},
        "ingredients": compare_ingredients(ing1, ing2),
        "packaging": compare_packaging(
            get_materials(rec1, extractor), get_materials(rec2, extractor),
            (rec1 or {}).get("packaging_source"), (rec2 or {}).get("packaging_source")),
        "naughty_list": compare_naughty(check_naughty(ing1), check_naughty(ing2)),
    }
