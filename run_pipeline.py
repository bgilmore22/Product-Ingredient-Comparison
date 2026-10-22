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
from Scrapers import mecca as MECCA
from analyse import SOURCE1, LLMMaterialExtractor, analyse_pair, flatten_variants, kit_title_reason, pooled_list_warning
from crawl_sitemaps import crawl_mecca_products, crawl_shopify_products
from match_products import fetch_title, goto_slug, has_review_flag, llm_verify, mecca_slug, normalise

ROOT = Path(__file__).resolve().parent
SUMMARY_FILE = ROOT / "summary.csv"
DASHBOARD_DATA_FILE = ROOT / "dashboard_data.js"
DELAY_SECONDS = 1.0                      # pause after every product-page fetch
GOTO_SITEMAP = "https://gotoskincare.com/sitemap.xml"
MECCA_BRAND_PATH = "/go-to/"             # which brand's Mecca listings to keep
FUZZY_THRESHOLD = 0.4                    # SequenceMatcher ratio on normalised slugs
MAX_LLM_CANDIDATES = 3                   # per Go-To product, best-scored first

NOT_FOUND = "not found"
COLOUR_RANK = {"RED": 3, "YELLOW": 2, "GREEN": 1}


def _load_goto_scraper():
    """go-to_skincare.py has a hyphen, which a normal "import" statement can't
    use as an identifier - importlib.util works regardless of filename.
    The folder is "Scrapers" with a capital S: macOS ignores case, Linux
    doesn't, so the path must match exactly."""
    path = ROOT / "Scrapers" / "go-to_skincare.py"
    spec = importlib.util.spec_from_file_location("Scrapers.go_to_skincare", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


GOTO = _load_goto_scraper()


# ---------------------------------------------------------------------------
# STAGE 3 + 4: crawl and match, in memory
# ---------------------------------------------------------------------------
def _prefer_single_item(existing, new):
    """Mecca can list the same slug as -I-<id> (single item) and -V-<id>
    (variant group). Keep the -I- page: it is one SKU with one formula."""
    return new if ("-I-" in new and "-V-" in existing) else existing


def find_pairs():
    goto_urls = crawl_shopify_products(GOTO_SITEMAP)
    mecca_urls = [u for u in crawl_mecca_products() if MECCA_BRAND_PATH in u]
    print(f"crawled {len(goto_urls)} Go-To URLs, {len(mecca_urls)} Mecca {MECCA_BRAND_PATH.strip('/')} URLs")

    mecca_by_slug = {}
    for u in mecca_urls:
        s = mecca_slug(u)
        if s in mecca_by_slug:
            keep = _prefer_single_item(mecca_by_slug[s], u)
            print(f"WARNING duplicate Mecca slug '{s}': keeping {keep}")
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
            verdict = llm_verify(t1, t2)
            print(f"  llm {verdict:<9} {-neg_score:.2f}  {goto_slug(gu)}  <->  {mecca_slug(mu)}")
            if verdict == "same":
                pairs.append((gu, mu, "llm"))
                used_mecca.add(mu)
                llm_confirmed += 1
                break

    exact_n = sum(1 for p in pairs if p[2] == "exact")
    print(f"matched {len(pairs)} pairs ({exact_n} exact, {llm_confirmed} LLM-confirmed of {llm_checked} checked); "
          f"{len(remaining_goto) - llm_confirmed} Go-To products unmatched")
    return pairs


# ---------------------------------------------------------------------------
# STAGE 5: scrape both pages live and analyse
# ---------------------------------------------------------------------------
def fetch(url):
    resp = common.fetch(url, timeout=20)
    if not isinstance(resp, common.CachedResponse):
        time.sleep(DELAY_SECONDS)          # be polite to the live site; no need to wait for a cached page
    return resp.text


def scrape_record(scraper, url):
    """A product record for `url`, or a scrape_failed record if the page
    can't be fetched or parsed - never raises, so one bad page costs one
    side of one pair, not the whole pair."""
    try:
        html_text = fetch(url)
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


def slug_for(goto_url):
    return goto_slug(goto_url)


def _ordered(mecca_value, goto_value):
    """(source1, source2) in the order analyse.SOURCE1 says."""
    return (mecca_value, goto_value) if SOURCE1 == "retailer" else (goto_value, mecca_value)


def not_comparable_result(gu, mu, method, reason, product_name=None):
    """Bundle/kit/set listings are excluded before scraping - a multi-product
    page has no single formula to compare, and analysing it anyway produced
    misleading not_scored/mismatched results in the Stage 7 audit. Also used
    when a scraped ingredient list turns out to cover several products."""
    empty_naughty = {"status": "not_scored", "colour": None, "reason": reason,
                     "categories": {}, "unmatched_ingredients": [], "skipped_claims": []}
    site1, site2 = _ordered(MECCA.WEBSITE_NAME, GOTO.WEBSITE_NAME)
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
    mecca_rec = scrape_record(MECCA, mu)
    goto_rec = scrape_record(GOTO, gu)
    name = goto_rec.get("product_name") or mecca_rec.get("product_name") or slug_for(gu).replace("-", " ").title()

    # Kit check, layered (the slug words were already checked in main()):
    #  1. a title that names a kit  2. an AI yes/no on titles + start of the list.
    # Both skip with a stated reason; anything unsure is SCORED, never skipped.
    titles = [(r["website"], r.get("product_name")) for r in (mecca_rec, goto_rec)
              if r.get("product_name") and r.get("product_name") != "unknown-product"]
    why = kit_title_reason(titles)
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
    "product", "colour", "note", "match_method",
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


def write_dashboard_data(rows):
    """A JS file (not JSON) so dashboard.html can load it with a plain
    <script src="dashboard_data.js"> tag - opening dashboard.html directly as
    a file:// URL cannot fetch() a local JSON file, but a <script> include
    works with no server needed. Overwritten fresh every run, so refreshing
    the dashboard after a new run always shows the latest data."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "products": rows,
    }
    DASHBOARD_DATA_FILE.write_text(
        "window.PIPELINE_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(f"{DASHBOARD_DATA_FILE.name}: {len(rows)} products")


def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Scrape, match, compare and score Go-To products on Mecca vs gotoskincare.com")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--refresh", action="store_true",
                      help="ignore the cache: fetch every page and ask Gemini again (default: reuse anything "
                           f"cached in the last {common.CACHE_MAX_AGE_HOURS:g}h)")
    mode.add_argument("--offline", action="store_true",
                      help="never touch the network or Gemini: run entirely from the cache/ folder")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    common.set_cache_mode("refresh" if args.refresh else "offline" if args.offline else "use")
    extractor = LLMMaterialExtractor()
    pairs = find_pairs()

    rows, failures, skipped = [], 0, 0
    for i, (gu, mu, method) in enumerate(pairs, 1):
        slug = slug_for(gu)
        if has_review_flag(goto_slug(gu)) or has_review_flag(mecca_slug(mu)):
            rows.append(to_row(not_comparable_result(
                gu, mu, method, "not comparable - bundle/kit/set listing, not a single product")))
            skipped += 1
            print(f"[{i}/{len(pairs)}] skip {slug} (bundle/kit/set - not comparable)")
            continue
        try:
            result = run_one(gu, mu, method, extractor)
        except Exception as exc:  # last resort: a bug in analysis shouldn't lose the whole run
            failures += 1
            print(f"[{i}/{len(pairs)}] FAIL {slug}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        row = to_row(result)
        rows.append(row)
        statuses = (result["sources"]["source1"]["status"], result["sources"]["source2"]["status"])
        if "scrape_failed" in statuses:
            failures += 1
            print(f"[{i}/{len(pairs)}] PART {slug} ({row['colour']}; {row['note']})")
        elif row["colour"] == "NOT COMPARABLE":
            skipped += 1
            print(f"[{i}/{len(pairs)}] skip {slug} ({row['note']})")
        else:
            print(f"[{i}/{len(pairs)}] ok   {slug} ({row['colour']})")

    write_summary(rows)
    write_dashboard_data(rows)
    print(common.cache_summary())
    print(f"done {len(rows) - skipped} scored, not comparable {skipped}, with a failed page {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
