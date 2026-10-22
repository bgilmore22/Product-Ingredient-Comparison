"""Extraction sanity warnings (sanity.py), audit review hints (audit_hints.py) and how
run_pipeline wires them in. Offline. python3 -m pytest -q test_sanity.py"""
import pytest

import run_pipeline as rp
import sources
from Scrapers import common
from audit_hints import review_hint
from sanity import compare_warnings, list_warnings


def _items(text):
    return common.split_ingredients(text)


def test_a_normal_list_has_no_warnings():
    assert list_warnings(_items("Aqua, Glycerin, Niacinamide, Sodium Hyaluronate, Panthenol, Allantoin")) == []
    assert list_warnings([]) == [] and list_warnings(None) == []


def test_short_list_is_flagged():
    assert "only 2 ingredients" in list_warnings(["Aqua", "Glycerin"])[0]
    assert "only 1 ingredient " in list_warnings(["Aqua"])[0]


def test_supplement_dose_table_is_flagged():
    w = list_warnings(_items("Magnesium 300 mg, Vitamin B6 5 mg, Zinc 10 mg, Vitamin D3 25 mcg, Rice flour"))
    assert any("supplement facts table" in x for x in w)


def test_prose_html_and_placeholder_items():
    base = ["Aqua", "Glycerin", "Niacinamide", "Panthenol"]
    sentence = "This lightweight formula is enriched with plant extracts to hydrate and soften skin every day"
    assert any("reads like a sentence" in x for x in list_warnings(base + [sentence]))
    assert any("HTML" in x for x in list_warnings(base + ["Aqua&nbsp;(Water)"]))
    assert any("HTML" in x for x in list_warnings(base + ["<b>Glycerin</b>"]))
    assert any("no real ingredient list" in x for x in list_warnings(base + ["Ingredients coming soon"]))


def test_free_from_tail_is_not_treated_as_ingredients():
    tail = ["No silicones", "parabens", "sulfates (SLS/SLES)",
            "For a full explanation of each ingredient visit the site and read the long explanation of everything we leave out"]
    assert list_warnings(["Aqua", "Glycerin", "Niacinamide", "Panthenol"] + tail) == []


def test_count_mismatch_between_sources():
    long_, short = [f"Ingredient {i}" for i in range(30)], [f"Ingredient {i}" for i in range(10)]
    w = compare_warnings(short, long_, "MECCA", "Go-To")
    assert w and "MECCA 10 vs Go-To 30" in w[0]
    assert compare_warnings(long_, long_[:20], "MECCA", "Go-To") == []      # 20 vs 30: normal
    assert compare_warnings([], long_, "MECCA", "Go-To") == []              # a missing list is reported elsewhere
    assert compare_warnings(["a", "b"], ["a", "b", "c", "d", "e"], "A", "B") == []   # tiny lists: gap too small


@pytest.mark.parametrize("name,hint", [
    ("Polysorbate-80", "possible PEGs"),
    ("Dimethiconol Stearate", "possible Silicones"),
    ("Sodium Cocoyl Sulphate", "possible SLS/SLES"),
    ("Sodium Fluoride", "possible PFAS"),
    ("Isoparaffin C13-14", "possible Mineral oil/petrolatum"),
    ("Octyl Methoxycinnamate", "possible Oxybenzone & octinoxate"),
    ("Sodium Cetearyl Sulfate", "possible SLS/SLES"),
    ("Hydrogenated Polyisobutene", "possible Mineral oil/petrolatum"),
    # real names from the audit that must NOT be hinted
    ("Aqua", ""), ("Glycerin", ""), ("Niacinamide", ""), ("Silica", ""), ("Sodium Hyaluronate", ""),
    ("Magnesium Sulfate", ""), ("Zinc Sulfate", ""), ("Calcium Carbonate", ""), ("Dicaprylyl Carbonate", ""),
    ("Synthetic Fluorphlogopite", ""), ("Natural Fragrance (contains rose and geranium oils)", ""),
    ("Fragrance (natural mandarin and lemon)", ""),
    ("Diethylamino Hydroxybenzoyl Hexyl Benzoate", ""),
    ("Bis-(Isostearoyl/Oleoyl Isopropyl) Dimonium Methosulfate", ""),
])
def test_review_hint(name, hint):
    assert review_hint(name) == hint


def _result(product, site, cats, unmatched):
    side = {"categories": cats, "unmatched_ingredients": unmatched}
    return {"product": product, "ingredients": {"comparison": "compared"},
            "sources": {"source1": {"site": site}, "source2": {"site": site}},
            "naughty_list": {"source1": side, "source2": {"categories": {}, "unmatched_ingredients": []}}}


def test_audit_sorts_flagged_then_possible_misses_then_clean():
    audit = rp.build_audit([_result("P", "MECCA", {"PEGs": ["PEG-8"]}, ["Aqua", "Polysorbate 60", "Silicon Dioxide Beads", "Glycerin"])])
    assert [a["ingredient"] for a in audit][0] == "PEG-8"
    hinted = [a for a in audit if a["review_hint"]]
    assert {a["ingredient"] for a in hinted} == {"Polysorbate 60", "Silicon Dioxide Beads"}
    # every hinted line comes before every plain line
    flags = [bool(a["category"]) or bool(a["review_hint"]) for a in audit]
    assert flags == sorted(flags, reverse=True)
    assert all(a["review_hint"] == "" for a in audit if a["category"])


def test_run_one_adds_shape_warnings_to_the_note_but_not_the_colour(monkeypatch):
    def rec(site):
        return common.build_product_record(website=site, url="u", product_name="Face Hero", brand="Go-To",
                                           ingredients_text="Aqua, Glycerin", packaging_text=None,
                                           packaging_source="not_found")
    monkeypatch.setattr(rp, "scrape_record", lambda source, url: rec(source.name))
    monkeypatch.setattr(rp, "BRAND", sources.SOURCE_GOTO)
    monkeypatch.setattr(sources.SOURCE_GOTO, "product_json", None)
    row = rp.to_row(rp.run_one("https://gotoskincare.com/products/face-hero", "https://www.mecca.com/x-I-1/",
                               "exact", lambda text: []))
    assert row["colour"] == "GREEN"
    assert "only 2 ingredients" in row["note"]
