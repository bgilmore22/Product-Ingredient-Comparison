"""The retailer interface (sources.py) and the parts of run_pipeline.py that
use it: matching and a full product run, with fake sites and no network.
python3 -m pytest -q test_sources.py"""
import dataclasses

import pytest

import run_pipeline as rp
import sources
from Scrapers import common


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(common, "GEMINI_CLIENT", None)
    monkeypatch.setattr(rp, "BRAND", sources.SOURCE_GOTO)
    monkeypatch.setattr(rp, "RETAILER", sources.SOURCE_MECCA)


def _with(source, **changes):
    return dataclasses.replace(source, **changes)


def test_registry_and_cli_defaults():
    assert set(sources.SOURCES) >= {"goto", "mecca"}
    args = rp.parse_args([])
    assert (args.brand, args.retailer) == ("goto", "mecca")
    with pytest.raises(SystemExit):
        rp.set_sources("goto", "goto")


def test_slugs():
    assert sources.goto_slug("https://gotoskincare.com/products/face-hero") == "face-hero"
    assert sources.mecca_slug("https://www.mecca.com/en-au/go-to/face-hero-I-062443/") == "face-hero"


def test_find_pairs_uses_source_crawl_and_slug(monkeypatch):
    goto = ["https://gotoskincare.com/products/face-hero", "https://gotoskincare.com/products/the-removalist"]
    mecca = ["https://www.mecca.com/en-au/go-to/face-hero-V-2/", "https://www.mecca.com/en-au/go-to/face-hero-I-1/",
             "https://www.mecca.com/en-au/go-to/removalist-I-3/"]
    monkeypatch.setattr(rp, "BRAND", _with(sources.SOURCE_GOTO, crawl=lambda: goto))
    monkeypatch.setattr(rp, "RETAILER", _with(sources.SOURCE_MECCA, crawl=lambda: mecca))
    titles = {goto[1]: "The Removalist", mecca[2]: "Go-To Removalist"}
    monkeypatch.setattr(rp, "fetch_title", lambda u: titles.get(u))
    seen = []

    def fake_verify(t1, t2, *site_args):
        seen.append(site_args)
        return "same"
    monkeypatch.setattr(rp, "llm_verify", fake_verify)

    pairs = rp.find_pairs()
    assert (goto[0], mecca[1], "exact") in pairs          # -I- preferred over -V- for the same slug
    assert (goto[1], mecca[2], "llm") in pairs
    assert seen == [("Go-To Skincare", "the brand's own store", "MECCA", "a multi-brand retailer")]


def _rec(site, name, ingredients):
    return common.build_product_record(website=site, url="u", product_name=name, brand="Go-To",
                                       ingredients_text=ingredients, packaging_text=None,
                                       packaging_source="not_found")


def test_run_one_scores_a_single_product_and_uses_structured_kit_signal(monkeypatch):
    recs = {"MECCA": _rec("MECCA", "Face Hero", "Aqua, Dimethicone"),
            "Go-To Skincare": _rec("Go-To Skincare", "Face Hero", "Aqua, Dimethicone")}
    monkeypatch.setattr(rp, "scrape_record", lambda source, url: dict(recs[source.name]))
    brand = _with(sources.SOURCE_GOTO, product_json=lambda url: {"title": "Face Hero", "product_type": "Oil",
                                                                  "variants": [{"title": "Default Title"}]})
    monkeypatch.setattr(rp, "BRAND", brand)
    row = rp.to_row(rp.run_one("https://gotoskincare.com/products/face-hero", "https://www.mecca.com/x-I-1/",
                               "exact", lambda text: []))
    assert row["colour"] == "YELLOW" and row["product_type"] == "Oil" and row["variants"] == "not found"

    kit = _with(brand, product_json=lambda url: {"title": "Face Hero Duo", "product_type": "Gift Sets"})
    monkeypatch.setattr(rp, "BRAND", kit)
    row = rp.to_row(rp.run_one("https://gotoskincare.com/products/face-hero", "https://www.mecca.com/x-I-1/",
                               "exact", lambda text: []))
    assert row["colour"] == "NOT COMPARABLE" and "product_type 'Gift Sets'" in row["note"]


def test_a_source_without_structured_data_is_fine():
    raw, facts, kit = sources.SOURCE_MECCA.facts("https://www.mecca.com/x-I-1/")
    assert (raw, facts, kit) == (None, {}, None)
