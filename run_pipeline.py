"""
run_pipeline.py (project root) - the orchestrator.

crawl both sitemaps -> match Go-To products to Mecca listings -> scrape both
pages of each pair live -> compare ingredients and packaging, score against
the naughty list -> write summary.csv and dashboard_data.js.

Everything between stages stays in memory. The only files written are the two
outputs, overwritten on every run.

Run:  python3 run_pipeline.py
Exit code 1 if any pair failed to scrape, else 0 (the outputs are written
either way).
"""
import csv
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from Scrapers import common
from analyse import SOURCE1, LLMMaterialExtractor, analyse_pair, flatten_variants, kit_title_reason, pooled_list_warning
from match_products import fetch_title, has_review_flag, llm_verify, normalise
from sources import DEFAULT_BRAND, DEFAULT_RETAILER, SOURCES, Source

ROOT = Path(__file__).resolve().parent
SUMMARY_FILE = ROOT / "summary.csv"
DASHBOARD_DATA_FILE = ROOT / "dashboard_data.js"
RUNS_DIR = ROOT / "runs"                 # one <timestamp>.json + .log per run; in_progress.json while running
PROGRESS_FILE = RUNS_DIR / "in_progress.json"
FUZZY_THRESHOLD = 0.4                    # SequenceMatcher ratio on normalised slugs
MAX_LLM_CANDIDATES = 3                   # per Go-To product, best-scored first

NOT_FOUND = "not found"
COLOUR_RANK = {"RED": 3, "YELLOW": 2, "GREEN": 1}


# The two sites of this run. Set by main() from --brand/--retailer; the
# defaults are Go-To Skincare vs MECCA. Everything site-specific (crawl, slug,
# scraper, structured data) is on the Source objects in sources.py.
BRAND: Source = SOURCES[DEFAULT_BRAND]
RETAILER: Source = SOURCES[DEFAULT_RETAILER]


def set_sources(brand_key, retailer_key):
    global BRAND, RETAILER
    BRAND, RETAILER = SOURCES[brand_key], SOURCES[retailer_key]
    if BRAND is RETAILER:
        raise SystemExit("--brand and --retailer must be two different sources")


# ---------------------------------------------------------------------------
# STAGE 3 + 4: crawl and match, in memory
# ---------------------------------------------------------------------------
def find_pairs():
    """(brand_url, retailer_url, method) for every brand product found on the
    retailer. The brand's product list drives; the retailer is the pool."""
    goto_urls = BRAND.crawl()
    mecca_urls = RETAILER.crawl()
    print(f"crawled {len(goto_urls)} {BRAND.name} URLs, {len(mecca_urls)} {RETAILER.name} URLs")
    goto_slug, mecca_slug = BRAND.slug, RETAILER.slug

    mecca_by_slug = {}
    for u in mecca_urls:
        s = mecca_slug(u)
        if s in mecca_by_slug:
            keep = RETAILER.prefer_url(mecca_by_slug[s], u) if RETAILER.prefer_url else mecca_by_slug[s]
            print(f"WARNING duplicate {RETAILER.name} slug '{s}': keeping {keep}")
            mecca_by_slug[s] = keep
        else:
            mecca_by_slug[s] = u

    pairs, matched_goto, matched_mecca_slugs = [], set(), set()

    # Pass 1: exact slug match
    for gu in goto_urls:
        gs = goto_slug(gu)
        if gs in mecca_by_slug:
            pairs.append((gu, mecca_by_slug[gs], "exact"))
            matched_goto.add(gu)
            matched_mecca_slugs.add(gs)

    remaining_goto = [u for u in goto_urls if u not in matched_goto]
    remaining_mecca = sorted({mecca_by_slug[s] for s in mecca_by_slug if s not in matched_mecca_slugs})

    # Pass 2: fuzzy match on leftovers. For each Go-To product, candidates are
    # ranked by score with non-flagged (no bundle/kit/etc words) candidates
    # first, and up to MAX_LLM_CANDIDATES get an LLM verdict on page titles.
    # Only "same" becomes a pair, and only a confirmed pair takes a Mecca URL
    # out of the pool - a "different" or "uncertain" verdict must not burn
    # the candidate for the next product, or the right pair is never tried.
    candidates = {}
    for gu in remaining_goto:
        gn = normalise(goto_slug(gu))
        flag_g = has_review_flag(goto_slug(gu))
        scored = []
        for mu in remaining_mecca:
            score = SequenceMatcher(None, gn, normalise(mecca_slug(mu))).ratio()
            if score >= FUZZY_THRESHOLD:
                flagged = flag_g or has_review_flag(mecca_slug(mu))
                scored.append((flagged, -score, mu))
        scored.sort()
        if scored:
            candidates[gu] = scored

    # best overall candidates first, so a strong pair claims its Mecca URL
    # before a weaker pair for another product can
    order = sorted(candidates, key=lambda g: candidates[g][0][:2])
    used_mecca, llm_confirmed, llm_checked = set(), 0, 0
    for gu in order:
        t1 = None
        tried = 0
        for flagged, neg_score, mu in candidates[gu]:
            if mu in used_mecca or tried >= MAX_LLM_CANDIDATES:
                continue
            tried += 1
            if t1 is None:
                t1 = fetch_title(gu)
                if not t1:
                    break
            t2 = fetch_title(mu)
            if not t2:
                continue
            llm_checked += 1
            verdict = llm_verify(t1, t2, BRAND.name, BRAND.describe, RETAILER.name, RETAILER.describe)
            print(f"  llm {verdict:<9} {-neg_score:.2f}  {goto_slug(gu)}  <->  {mecca_slug(mu)}")
            if verdict == "same":
                pairs.append((gu, mu, "llm"))
                used_mecca.add(mu)
                llm_confirmed += 1
                break

    exact_n = sum(1 for p in pairs if p[2] == "exact")
    print(f"matched {len(pairs)} pairs ({exact_n} exact, {llm_confirmed} LLM-confirmed of {llm_checked} checked); "
          f"{len(remaining_goto) - llm_confirmed} {BRAND.name} products unmatched")
    return pairs


# ---------------------------------------------------------------------------
# STAGE 5: scrape both pages live and analyse
# ---------------------------------------------------------------------------
def fetch(url, delay_seconds=1.0):
    resp = common.fetch(url, timeout=20)
    if not isinstance(resp, common.CachedResponse):
        time.sleep(delay_seconds)          # be polite to the live site; no need to wait for a cached page
    return resp.text


def scrape_record(source, url):
    """A product record for `url` on `source`, or a scrape_failed record if
    the page can't be fetched or parsed - never raises, so one bad page costs
    one side of one pair, not the whole pair."""
    scraper = source.scraper
    try:
        html_text = fetch(url, source.delay_seconds)
    except Exception as exc:
        print(f"    fetch failed {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return common.failed_product_record(scraper.WEBSITE_NAME, url, f"{type(exc).__name__}: {exc}")
    try:
        ingredients = scraper.extract_ingredients(html_text)
        packaging = scraper.extract_packaging_text(html_text)
        packaging_source = "text" if packaging else "not_found"
        if not packaging:
            packaging = common.get_packaging_from_images(scraper.extract_image_urls(html_text))
            if packaging:
                packaging_source = "image"
        return common.build_product_record(
            website=scraper.WEBSITE_NAME,
            url=url,
            product_name=scraper.extract_product_name(html_text) or "unknown-product",
            brand=scraper.extract_brand(html_text),
            ingredients_text=ingredients,
            packaging_text=packaging,
            packaging_source=packaging_source,
        )
    except Exception as exc:
        print(f"    parse failed {url}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return common.failed_product_record(scraper.WEBSITE_NAME, url, f"{type(exc).__name__}: {exc}")


def slug_for(brand_url):
    return BRAND.slug(brand_url)


def _ordered(retailer_value, brand_value):
    """(source1, source2) in the order analyse.SOURCE1 says."""
    return (retailer_value, brand_value) if SOURCE1 == "retailer" else (brand_value, retailer_value)


def not_comparable_result(gu, mu, method, reason, product_name=None):
    """Bundle/kit/set listings are excluded before scraping - a multi-product
    page has no single formula to compare, and analysing it anyway produced
    misleading not_scored/mismatched results in the Stage 7 audit. Also used
    when a scraped ingredient list turns out to cover several products."""
    empty_naughty = {"status": "not_scored", "colour": None, "reason": reason,
                     "categories": {}, "unmatched_ingredients": [], "skipped_claims": []}
    site1, site2 = _ordered(RETAILER.name, BRAND.name)
    url1, url2 = _ordered(mu, gu)
    return {
        "product": product_name or slug_for(gu).replace("-", " ").title(),
        "match_method": method,
        "sources": {"source1": {"site": site1, "url": url1, "status": "not_comparable"},
                    "source2": {"site": site2, "url": url2, "status": "not_comparable"}},
        "ingredients": {"comparison": "not_comparable", "reason": reason,
                        "only_in_source1": [], "only_in_source2": [], "close_matches": [], "raw_text_differences": []},
        "packaging": {"comparison": "not_comparable", "reason": reason,
                      "source1_materials": [], "source2_materials": [], "packaging_source": {"source1": None, "source2": None}},
        "naughty_list": {"source1": empty_naughty, "source2": empty_naughty, "colours_match": None,
                         "categories_only_in_source1": None, "categories_only_in_source2": None, "reason": reason},
    }


_KIT_PROMPT = """Is this ONE product, or a kit/set/bundle/routine/gift pack of several different products sold together?

Product title(s):
{titles}

Start of the ingredient text on each page:
{heads}

Answer "kit" ONLY if it clearly shows several DIFFERENT products sold together (a set, kit, bundle,
calendar, multi-step routine, gift pack). Different shades, sizes, refills or variants of ONE product
are "single". If you are not sure, answer "single".
Respond with JSON only: {{"verdict": "kit" | "single"}}"""
_kit_cache = {}


def llm_kit_verdict(titles, heads):
    """'kit' or 'single'. Anything unclear, any failure, or no API key -> 'single', so a
    problem here can only cause a product to be scored, never wrongly skipped."""
    key = (tuple(titles), tuple(heads))
    if key in _kit_cache:
        return _kit_cache[key]
    verdict = "single"
    if common.GEMINI_CLIENT is not None and common.types is not None and titles:
        try:
            config = common.types.GenerateContentConfig(response_mime_type="application/json", temperature=0)
            prompt = _KIT_PROMPT.format(titles="\n".join(f"- {a}: {b}" for a, b in titles),
                                        heads="\n".join(f"- {a}: {b}" for a, b in heads))
            def ask():
                result = common.GEMINI_CLIENT.models.generate_content(
                    model=common.GEMINI_MODEL, contents=[prompt], config=config)
                return (result.text or "").strip() or None

            raw = common.llm_cached(common.GEMINI_MODEL, "kit|" + prompt, ask)
            data, _ = json.JSONDecoder().raw_decode(raw) if raw else ({}, 0)
            if isinstance(data, dict) and str(data.get("verdict", "")).strip().lower() == "kit":
                verdict = "kit"
        except Exception as exc:
            print(f"    kit check failed ({type(exc).__name__}: {exc}) - scoring the product")
    _kit_cache[key] = verdict
    return verdict


def run_one(gu, mu, method, extractor):
    mecca_rec = scrape_record(RETAILER, mu)
    goto_rec = scrape_record(BRAND, gu)
    # structured product data (Shopify JSON etc.) from whichever sides have it
    kit_from_data = None
    for source, rec, url in ((BRAND, goto_rec, gu), (RETAILER, mecca_rec, mu)):
        raw, facts, kit = source.facts(url)
        rec.update(facts)
        if raw and raw.get("title") and rec.get("product_name") in (None, "unknown-product"):
            rec["product_name"] = str(raw["title"]).strip()
        kit_from_data = kit_from_data or kit
    name = goto_rec.get("product_name") or mecca_rec.get("product_name") or slug_for(gu).replace("-", " ").title()

    # Kit check, layered (the slug words were already checked in main()):
    #  1. the store's own product data (Shopify product_type/tags)  2. a title that names a kit
    #  3. an AI yes/no on titles + start of the list.
    # Each skips with a stated reason; anything unsure is SCORED, never skipped.
    titles = [(r["website"], r.get("product_name")) for r in (mecca_rec, goto_rec)
              if r.get("product_name") and r.get("product_name") != "unknown-product"]
    why = kit_from_data or kit_title_reason(titles)
    if not why:
        heads = [(r["website"], (r.get("ingredients_raw") or "")[:250]) for r in (mecca_rec, goto_rec)]
        if llm_kit_verdict(titles, heads) == "kit":
            why = "AI judged this a kit/set of several products from its title and ingredient list (" + "; ".join(f"{a}: {b}" for a, b in titles) + ")"
    if why:
        print(f"    kit detected: {why}")
        return not_comparable_result(gu, mu, method, f"not comparable - {why}", name)

    warnings = []
    for rec in (mecca_rec, goto_rec):
        pw = pooled_list_warning(rec.get("ingredients"))
        if pw:
            warnings.append(f"{rec['website']} {pw}")
        items, w = flatten_variants(rec.get("ingredients"))
        if w:
            print(f"    note: {rec['website']} {w}; list starts: {(rec.get('ingredients_raw') or '')[:150]!r}")
            rec["ingredients"] = items
            warnings.append(f"{rec['website']} {w}")

    rec1, rec2 = _ordered(mecca_rec, goto_rec)
    result = analyse_pair(name, rec1, rec2, extractor)
    result["warning"] = "; ".join(warnings)
    result["match_method"] = method
    result["product_type"] = goto_rec.get("product_type")
    result["variants"] = goto_rec.get("variants") or []
    return result


# ---------------------------------------------------------------------------
# COMBINED COLOUR: one colour, the worse of the two; "not found" for missing
# data, never blank
# ---------------------------------------------------------------------------
def _missing_note(block):
    """Why a side has no colour, from its scrape status."""
    return "page could not be fetched" if block["status"] == "scrape_failed" else "no ingredients list found"


def combine_colour(result):
    """Returns (colour, note). colour is one of RED/YELLOW/GREEN/NOT FOUND/
    NOT COMPARABLE. note explains a missing side, empty string otherwise."""
    if result["ingredients"]["comparison"] == "not_comparable":
        return "NOT COMPARABLE", result["ingredients"]["reason"].replace("not comparable - ", "", 1)

    nl = result["naughty_list"]
    s1, s2 = result["sources"]["source1"], result["sources"]["source2"]
    c1, c2 = nl["source1"]["colour"], nl["source2"]["colour"]

    if c1 is None and c2 is None:
        return "NOT FOUND", f"Source 1: {_missing_note(s1)}; Source 2: {_missing_note(s2)}"
    if c1 is None:
        return c2, f"Source 1: {_missing_note(s1)}"
    if c2 is None:
        return c1, f"Source 2: {_missing_note(s2)}"
    return (c1 if COLOUR_RANK[c1] >= COLOUR_RANK[c2] else c2), ""


def nf(value):
    """'not found' for any missing/empty value - the manager's answer to
    "blank vs not found" (not found won). Never mutates the underlying data,
    only how it's displayed in summary.csv / the dashboard."""
    if value is None:
        return NOT_FOUND
    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(v) for v in value) if value else NOT_FOUND
    if isinstance(value, dict):
        return "; ".join(value.keys()) if value else NOT_FOUND
    s = str(value).strip()
    return s if s else NOT_FOUND


# ---------------------------------------------------------------------------
# OUTPUT: summary.csv (Excel) + dashboard_data.js (the live dashboard)
# ---------------------------------------------------------------------------
def to_row(result):
    ing, pkg, nl = result["ingredients"], result["packaging"], result["naughty_list"]
    s1, s2 = result["sources"]["source1"], result["sources"]["source2"]
    colour, note = combine_colour(result)
    if result.get("warning"):
        note = "; ".join(x for x in (note, result["warning"]) if x)
    return {
        "product": result["product"],
        "colour": colour,
        "note": note,
        "match_method": result.get("match_method", ""),
        "product_type": nf(result.get("product_type")),
        "variants": nf(result.get("variants")),
        "source1_site": nf(s1["site"]), "source1_url": s1["url"] or "", "source1_status": s1["status"],
        "source2_site": nf(s2["site"]), "source2_url": s2["url"] or "", "source2_status": s2["status"],
        "ingredients_comparison": ing["comparison"], "ingredients_reason": ing["reason"],
        "only_in_source1": nf(ing["only_in_source1"]), "only_in_source2": nf(ing["only_in_source2"]),
        "close_matches": ing["close_matches"], "raw_text_differences": ing["raw_text_differences"],
        "packaging_comparison": pkg["comparison"], "packaging_reason": pkg["reason"],
        "source1_materials": nf(pkg["source1_materials"]), "source2_materials": nf(pkg["source2_materials"]),
        "source1_packaging_source": nf(pkg["packaging_source"]["source1"]),
        "source2_packaging_source": nf(pkg["packaging_source"]["source2"]),
        "source1_colour": nf(nl["source1"]["colour"]), "source2_colour": nf(nl["source2"]["colour"]),
        "source1_categories": nl["source1"]["categories"], "source2_categories": nl["source2"]["categories"],
        "source1_unmatched_ingredients": nf(nl["source1"]["unmatched_ingredients"]),
        "source2_unmatched_ingredients": nf(nl["source2"]["unmatched_ingredients"]),
        "naughty_reason": nl["reason"],
        "source1_hit_ingredients": _hits(nl["source1"]["categories"]),
        "source2_hit_ingredients": _hits(nl["source2"]["categories"]),
    }


def _hits(cats):
    """'Silicones: Dimethicone, Cyclopentasiloxane | PEGs: PEG-8' - which ingredient caused each hit."""
    return " | ".join(f"{c}: {', '.join(v)}" for c, v in (cats or {}).items()) if isinstance(cats, dict) else ""


SUMMARY_FIELDS = [
    "product", "colour", "note", "match_method", "product_type", "variants",
    "source1_site", "source1_url", "source1_status",
    "source2_site", "source2_url", "source2_status",
    "ingredients_comparison", "ingredients_reason",
    "only_in_source1", "only_in_source2",
    "packaging_comparison", "packaging_reason",
    "source1_materials", "source2_materials",
    "source1_packaging_source", "source2_packaging_source",
    "source1_colour", "source2_colour",
    "source1_categories", "source2_categories",
    "source1_unmatched_ingredients", "source2_unmatched_ingredients",
    "naughty_reason", "source1_hit_ingredients", "source2_hit_ingredients",
]


def write_summary(rows):
    with SUMMARY_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            flat["source1_categories"] = nf(row["source1_categories"])
            flat["source2_categories"] = nf(row["source2_categories"])
            writer.writerow(flat)
    print(f"{SUMMARY_FILE.name}: {len(rows)} products")


# ---------------------------------------------------------------------------
# INGREDIENT AUDIT: one line per distinct ingredient across the whole run, with
# the category it hit (if any) and the products it appears in. Sorted so every
# flagged ingredient is at the top - a wrong regex shows up as an obviously
# harmless name with a category next to it.
# ---------------------------------------------------------------------------
AUDIT_FILE = ROOT / "ingredient_audit.csv"
AUDIT_FIELDS = ["ingredient", "category", "site", "product_count", "products"]


def build_audit(results):
    """results: the per-product analysis dicts (not the flattened rows)."""
    seen = {}
    for r in results:
        if r["ingredients"]["comparison"] == "not_comparable":
            continue
        for side in ("source1", "source2"):
            site = r["sources"][side]["site"]
            nl = r["naughty_list"][side]
            hits = {ing: cat for cat, ings in (nl.get("categories") or {}).items() for ing in ings}
            for ing in list(hits) + list(nl.get("unmatched_ingredients") or []):
                key = (ing.strip().lower(), hits.get(ing, ""), site)
                entry = seen.setdefault(key, {"ingredient": ing.strip(), "category": hits.get(ing, ""),
                                              "site": site, "products": []})
                if r["product"] not in entry["products"]:
                    entry["products"].append(r["product"])
    out = []
    for e in seen.values():
        out.append({**e, "product_count": len(e["products"]), "products": "; ".join(e["products"])})
    out.sort(key=lambda e: (e["category"] == "", e["category"], -e["product_count"], e["ingredient"].lower()))
    return out


def write_audit(audit):
    with AUDIT_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS)
        w.writeheader()
        w.writerows(audit)
    flagged = sum(1 for a in audit if a["category"])
    print(f"{AUDIT_FILE.name}: {len(audit)} distinct ingredient/site lines, {flagged} flagged")


def write_dashboard_data(rows, audit=None, previous=None, changes=None, stats=None):
    """A JS file (not JSON) so dashboard.html can load it with a plain
    <script src="dashboard_data.js"> tag - opening dashboard.html directly as
    a file:// URL cannot fetch() a local JSON file, but a <script> include
    works with no server needed. Overwritten fresh every run, so refreshing
    the dashboard after a new run always shows the latest data."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "products": rows,
        "audit": audit or [],
        "stats": stats or {},
        "llm_usage": common.LLM_USAGE,
        "cache_stats": common.CACHE_STATS,
        "previous_run": ({"generated_at": previous.get("generated_at"), "stamp": previous.get("stamp"),
                          "stats": previous.get("stats", {})} if previous else None),
        "changes": changes or [],
    }
    DASHBOARD_DATA_FILE.write_text(
        "window.PIPELINE_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(f"{DASHBOARD_DATA_FILE.name}: {len(rows)} products")


# ---------------------------------------------------------------------------
# RUN HISTORY: runs/<timestamp>.json keeps every run's rows, so the dashboard
# can show what changed since the last one, and runs/<timestamp>.log keeps
# everything printed. runs/in_progress.json is the checkpoint --resume reads.
# ---------------------------------------------------------------------------
def _run_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def previous_run(exclude=None):
    """The most recent saved run (dict) other than `exclude`, or None."""
    if not RUNS_DIR.exists():
        return None
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        if path.name == PROGRESS_FILE.name or path == exclude:
            continue
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _cats_of(row, side):
    c = row.get(f"{side}_categories")
    return sorted(c) if isinstance(c, dict) else [x.strip() for x in str(c or "").split(";") if x.strip() and x.strip() != NOT_FOUND]


def diff_runs(current_rows, previous):
    """What changed since `previous` (a saved run dict), product by product:
    colour changes, category changes, products new or gone."""
    if not previous:
        return []
    before = {r["product"]: r for r in previous.get("products", [])}
    after = {r["product"]: r for r in current_rows}
    changes = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        if b is None:
            changes.append({"product": name, "kind": "new", "before": None, "after": a["colour"], "detail": "not in previous run"})
        elif a is None:
            changes.append({"product": name, "kind": "removed", "before": b["colour"], "after": None, "detail": "not in this run"})
        else:
            details = []
            if a["colour"] != b["colour"]:
                details.append(f"colour {b['colour']} -> {a['colour']}")
            for side in ("source1", "source2"):
                cb, ca = set(_cats_of(b, side)), set(_cats_of(a, side))
                if cb != ca:
                    gained, lost = sorted(ca - cb), sorted(cb - ca)
                    details.append(f"{side} categories " + ", ".join(
                        ([f"+{g}" for g in gained] + [f"-{l}" for l in lost])))
            if details:
                changes.append({"product": name, "kind": "changed", "before": b["colour"], "after": a["colour"],
                                "detail": "; ".join(details)})
    return changes


def save_run(rows, audit, args, stats, stamp):
    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"{stamp}.json"
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "stamp": stamp,
               "brand": args.brand, "retailer": args.retailer,
               "cache_mode": common.CACHE_MODE, "stats": stats,
               "llm_usage": common.LLM_USAGE, "cache_stats": common.CACHE_STATS,
               "products": rows, "audit_flagged": sum(1 for a in audit if a["category"])}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    print(f"{path.relative_to(ROOT)}: run saved ({len(rows)} products)")
    return path


class _Tee:
    """Everything printed also goes to runs/<stamp>.log."""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text):
        self.stream.write(text)
        self.log.write(text)
        self.log.flush()

    def flush(self):
        self.stream.flush()
        self.log.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _checkpoint(rows, pairs_done):
    """Written after every product so --resume can pick up here."""
    RUNS_DIR.mkdir(exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps({"saved_at": datetime.now(timezone.utc).isoformat(),
                                         "brand": BRAND.key, "retailer": RETAILER.key,
                                         "pairs_done": sorted(pairs_done), "rows": rows},
                                        ensure_ascii=False), "utf-8")


def _load_checkpoint():
    if not PROGRESS_FILE.exists():
        return None
    try:
        cp = json.loads(PROGRESS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if cp.get("brand") != BRAND.key or cp.get("retailer") != RETAILER.key:
        print(f"ignoring checkpoint for a different pair ({cp.get('brand')} vs {cp.get('retailer')})")
        return None
    return cp


def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Scrape, match, compare and score one brand's products on its own store vs a retailer")
    ap.add_argument("--brand", default=DEFAULT_BRAND, choices=sorted(SOURCES),
                    help=f"the store whose product list drives the run (default {DEFAULT_BRAND})")
    ap.add_argument("--retailer", default=DEFAULT_RETAILER, choices=sorted(SOURCES),
                    help=f"the store it is matched against (default {DEFAULT_RETAILER})")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--refresh", action="store_true",
                      help="ignore the cache: fetch every page and ask Gemini again (default: reuse anything "
                           f"cached in the last {common.CACHE_MAX_AGE_HOURS:g}h)")
    mode.add_argument("--offline", action="store_true",
                      help="never touch the network or Gemini: run entirely from the cache/ folder")
    ap.add_argument("--resume", action="store_true",
                    help="continue the last interrupted run from runs/in_progress.json instead of starting over")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    set_sources(args.brand, args.retailer)
    common.set_cache_mode("refresh" if args.refresh else "offline" if args.offline else "use")
    stamp = _run_stamp()
    RUNS_DIR.mkdir(exist_ok=True)
    log = (RUNS_DIR / f"{stamp}.log").open("w", encoding="utf-8")
    sys.stdout, sys.stderr = _Tee(sys.__stdout__, log), _Tee(sys.__stderr__, log)
    try:
        return _run(args, stamp)
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        log.close()


def _run(args, stamp):
    print(f"run {stamp}: {BRAND.name} vs {RETAILER.name} (cache {common.CACHE_MODE})")
    extractor = LLMMaterialExtractor()
    pairs = find_pairs()

    rows, results, failures, skipped, done = [], [], 0, 0, set()
    cp = _load_checkpoint() if args.resume else None
    if cp:
        rows, done = cp["rows"], set(cp["pairs_done"])
        skipped = sum(1 for r in rows if r["colour"] == "NOT COMPARABLE")
        print(f"resuming: {len(done)} of {len(pairs)} products already done (checkpoint {cp['saved_at']})")
    elif PROGRESS_FILE.exists():
        print("note: runs/in_progress.json exists from an interrupted run; starting over (use --resume to continue it)")

    for i, (gu, mu, method) in enumerate(pairs, 1):
        slug = slug_for(gu)
        if gu in done:
            continue
        if has_review_flag(BRAND.slug(gu)) or has_review_flag(RETAILER.slug(mu)):
            rows.append(to_row(not_comparable_result(
                gu, mu, method, "not comparable - bundle/kit/set listing, not a single product")))
            skipped += 1
            print(f"[{i}/{len(pairs)}] skip {slug} (bundle/kit/set - not comparable)")
            done.add(gu)
            _checkpoint(rows, done)
            continue
        try:
            result = run_one(gu, mu, method, extractor)
        except Exception as exc:  # last resort: a bug in analysis shouldn't lose the whole run
            failures += 1
            print(f"[{i}/{len(pairs)}] FAIL {slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        row = to_row(result)
        rows.append(row)
        results.append(result)
        statuses = (result["sources"]["source1"]["status"], result["sources"]["source2"]["status"])
        if "scrape_failed" in statuses:
            failures += 1
            print(f"[{i}/{len(pairs)}] PART {slug} ({row['colour']}; {row['note']})")
        elif row["colour"] == "NOT COMPARABLE":
            skipped += 1
            print(f"[{i}/{len(pairs)}] skip {slug} ({row['note']})")
        else:
            print(f"[{i}/{len(pairs)}] ok   {slug} ({row['colour']})")
        done.add(gu)
        _checkpoint(rows, done)

    # the audit needs the full analysis dicts, which a checkpoint doesn't keep:
    # after a --resume it covers only the products scored in this session
    audit = build_audit(results)
    if cp:
        print(f"note: ingredient_audit.csv covers the {len(results)} products scored after resuming")
    stats = {"total": len(rows), "scored": len(rows) - skipped, "not_comparable": skipped, "failed_pages": failures}
    prev = previous_run()
    changes = diff_runs(rows, prev)
    write_summary(rows)
    write_audit(audit)
    write_dashboard_data(rows, audit, prev, changes, stats)
    save_run(rows, audit, args, stats, stamp)
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
    if prev:
        print(f"since last run ({prev.get('stamp', '?')}): {len(changes)} product(s) changed"
              + (" - " + "; ".join(f"{c['product']}: {c['detail']}" for c in changes[:8]) if changes else "")
              + (" ..." if len(changes) > 8 else ""))
    print(common.cache_summary())
    print(common.llm_usage_summary())
    print(f"done {len(rows) - skipped} scored, not comparable {skipped}, with a failed page {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
