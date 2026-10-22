"""
capture_fixture.py (project root) - save real product pages so the scrapers can be tested offline.

A scraper breaks silently when a site changes its HTML: the page still loads,
the extractor just returns the wrong thing. A fixture is a saved page plus
what the extractors should return from it. test_scraper_fixtures.py replays
every fixture on each test run, so a scraper change that alters a result
fails a test instead of quietly changing a colour.

Capture (needs the site, or a cached copy of the page in cache/):
    python3 capture_fixture.py mecca <product-url> clarifying-face-hero
    python3 capture_fixture.py goto  <product-url> much-plumper-skin
        -> tests/fixtures/<site>/<name>.html          the page
           tests/fixtures/<site>/<name>.expected.json  what the extractors returned
    READ the .expected.json before committing it: it records what the scraper
    does today, so if the ingredients in it are wrong, the test locks in the bug.

Good pages to capture: a normal single product on each site; a product whose
page lists shades; a kit; a supplement; a Mecca page with the "free from" tail;
a page with no packaging text. One of each is enough.

Check the live sites still match (run now and then, or before a demo):
    python3 capture_fixture.py --check-live
        re-downloads every fixture's page and reports any extractor whose result changed.
    python3 capture_fixture.py --list

Fixtures are about 1 MB each; a handful is plenty.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from Scrapers import common
from sources import SOURCES

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "tests" / "fixtures"
EXTRACTORS = ("extract_ingredients", "extract_packaging_text", "extract_product_name", "extract_brand", "extract_image_urls")


def snapshot(scraper, html):
    """What every extractor returns for this page, as plain JSON-able values."""
    return {name: getattr(scraper, name)(html) for name in EXTRACTORS}


def differences(expected, actual):
    """['extract_ingredients: expected ... got ...', ...] - empty when they match."""
    out = []
    for name in EXTRACTORS:
        if expected.get(name) != actual.get(name):
            out.append(f"{name}: expected {_short(expected.get(name))} got {_short(actual.get(name))}")
    return out


def _short(v, n=90):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "..."


def fixtures(root=FIXTURE_DIR):
    """[(site_key, name, html_path, expected_path)] for every saved fixture."""
    out = []
    for html in sorted(Path(root).glob("*/*.html")):
        out.append((html.parent.name, html.stem, html, html.with_suffix(".expected.json")))
    return out


def capture(site_key, url, name, root=FIXTURE_DIR, force=False, allow_empty=False, html=None):
    source = SOURCES[site_key]
    folder = Path(root) / site_key
    html_path, exp_path = folder / f"{name}.html", folder / f"{name}.expected.json"
    if html_path.exists() and not force:
        raise SystemExit(f"{html_path} exists - pick another name or pass --force")
    if html is None:
        html = common.fetch(url, timeout=20).text
    snap = snapshot(source.scraper, html)
    if not snap["extract_ingredients"] and not allow_empty:
        raise SystemExit("the scraper found NO ingredients on this page, so the fixture would lock that in. "
                         "Fix the scraper first, or pass --allow-empty if 'no ingredients' is the right answer for this page.")
    folder.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    exp_path.write_text(json.dumps({"url": url, "captured": datetime.now(timezone.utc).isoformat(),
                                    "expected": snap}, ensure_ascii=False, indent=2), encoding="utf-8")
    return html_path, exp_path, snap


def check_live(root=FIXTURE_DIR, fetch=None):
    """Re-fetch each fixture's page from the live site (never the cache) and compare it with the
    saved expectation. Returns [(site, name, [differences])] for the fixtures that drifted;
    a page that cannot be fetched counts as drift."""
    fetch = fetch or (lambda url: common.fetch(url, timeout=20).text)
    common.set_cache_mode("refresh")
    drifted = []
    for site_key, name, _html, exp_path in fixtures(root):
        meta = json.loads(exp_path.read_text("utf-8"))
        try:
            live = snapshot(SOURCES[site_key].scraper, fetch(meta["url"]))
        except Exception as exc:
            drifted.append((site_key, name, [f"could not fetch {meta['url']}: {type(exc).__name__}: {exc}"]))
            continue
        diffs = differences(meta["expected"], live)
        if diffs:
            drifted.append((site_key, name, diffs))
    return drifted


def main(argv=None):
    ap = argparse.ArgumentParser(description="Save product pages as scraper test fixtures (see the top of this file)")
    ap.add_argument("site", nargs="?", choices=sorted(SOURCES), help="which scraper reads this page")
    ap.add_argument("url", nargs="?")
    ap.add_argument("name", nargs="?", help="short name for the fixture, e.g. clarifying-face-hero")
    ap.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    ap.add_argument("--allow-empty", action="store_true", help="save the fixture even though no ingredients were found")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check-live", action="store_true", help="re-fetch every fixture's page and report scraper drift")
    args = ap.parse_args(argv)

    if args.list:
        for site, name, _h, _e in fixtures():
            print(f"{site}/{name}")
        return 0
    if args.check_live:
        found = fixtures()
        if not found:
            print("no fixtures saved yet")
            return 0
        drifted = check_live()
        for site, name, diffs in drifted:
            print(f"DRIFT {site}/{name}")
            for d in diffs:
                print(f"    {d}")
        print(f"{len(found) - len(drifted)} of {len(found)} fixtures still match the live site")
        return 1 if drifted else 0
    if not (args.site and args.url and args.name):
        ap.error("need: site url name   (or --list / --check-live)")
    html_path, exp_path, snap = capture(args.site, args.url, args.name, force=args.force, allow_empty=args.allow_empty)
    print(f"saved {html_path.relative_to(ROOT)} and {exp_path.name}")
    print(f"  product : {snap['extract_product_name']}")
    print(f"  brand   : {snap['extract_brand']}")
    print(f"  packaging text: {snap['extract_packaging_text']}")
    print(f"  ingredients: {(snap['extract_ingredients'] or '')[:300]}")
    print("Check those ingredients against the live page before you commit the fixture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
