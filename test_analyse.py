import hashlib
import json

import pytest

import analyse
from analyse import (check_naughty, compare_ingredients, compare_naughty,
                     compare_packaging, normalise)


# ---- naughty list: colours -------------------------------------------------
def test_green_no_hits():
    r = check_naughty(["water", "sodium hyaluronate", "panthenol"])
    assert (r["status"], r["colour"], r["categories"]) == ("scored", "GREEN", {})
    assert r["unmatched_ingredients"] == ["water", "sodium hyaluronate", "panthenol"]


def test_yellow_one_category():
    r = check_naughty(["water", "Mineral Oil"])
    assert r["colour"] == "YELLOW"
    assert r["categories"] == {"Mineral oil/petrolatum": ["Mineral Oil"]}
    assert r["unmatched_ingredients"] == ["water"]


def test_several_ingredients_same_category_count_once():
    r = check_naughty(["propanediol", "pentylene glycol", "caprylyl glycol"])
    assert r["colour"] == "YELLOW"
    assert list(r["categories"]) == ["Synthetic alcohols"]
    assert len(r["categories"]["Synthetic alcohols"]) == 3


def test_red_two_ingredients_two_categories():
    r = check_naughty(["mineral oil", "dimethicone"])
    assert r["colour"] == "RED"
    assert set(r["categories"]) == {"Mineral oil/petrolatum", "Silicones"}


def test_red_one_ingredient_hits_two_categories():
    r = check_naughty(["PEG-12 Dimethicone"])
    assert r["colour"] == "RED"
    assert set(r["categories"]) == {"PEGs", "Silicones"}


def test_missing_list_is_not_scored_never_green():
    for empty in ([], None):
        r = check_naughty(empty)
        assert r["status"] == "not_scored" and r["colour"] is None and r["reason"]


def test_naughty_compare_and_not_scored():
    a = check_naughty(["mineral oil"])
    b = check_naughty(["mineral oil", "dimethicone"])
    c = compare_naughty(a, b)
    assert c["colours_match"] is False
    assert c["categories_only_in_source1"] == []
    assert c["categories_only_in_source2"] == ["Silicones"]
    n = compare_naughty(a, check_naughty([]))
    assert n["colours_match"] is None and "source 2" in n["reason"]


# ---- normalisation ---------------------------------------------------------
def test_aqua_slash_water_normalises_to_water():
    assert normalise("Aqua/water") == "water"
    assert normalise("Aqua (Water)") == "water"
    assert normalise("Water/Aqua/Eau") == "water"


def test_normalise_rules():
    assert normalise("Niacinamide (5%)") == "niacinamide"
    assert normalise("Glycerin*") == "glycerin"
    assert normalise("Organic Aloe Vera") == "aloe vera"
    assert normalise("PEG-40 Hydrogenated Castor Oil") == "peg 40 hydrogenated castor oil"
    assert normalise("Parfum") == "fragrance"
    assert normalise("Vitamin C (Ascorbic Acid) (0.5%)") == "vitamin c"


# ---- ingredient comparison -------------------------------------------------
def test_ingredients_same_ignores_order_and_case():
    c = compare_ingredients(["Water", "Glycerin"], ["glycerin", "Aqua"])
    assert c["comparison"] == "same" and c["reason"]


def test_ingredients_different():
    c = compare_ingredients(["water", "glycerin"], ["water", "panthenol"])
    assert c["comparison"] == "different"
    assert c["only_in_source1"] == ["glycerin"] and c["only_in_source2"] == ["panthenol"]


@pytest.mark.parametrize("a,b", [([], ["water"]), (["water"], None), ([], [])])
def test_ingredients_cannot_compare(a, b):
    c = compare_ingredients(a, b)
    assert c["comparison"] == "cannot_compare" and c["reason"]


def test_hyaluronic_acid_vs_sodium_hyaluronate_close_not_merged():
    c = compare_ingredients(["Hyaluronic Acid"], ["Sodium Hyaluronate"])
    assert c["comparison"] == "different"
    assert c["only_in_source1"] == ["Hyaluronic Acid"]
    assert c["only_in_source2"] == ["Sodium Hyaluronate"]
    assert len(c["close_matches"]) == 1
    assert c["close_matches"][0]["source1"] == "Hyaluronic Acid"


def test_unrelated_ingredients_not_flagged_close():
    c = compare_ingredients(["sodium lactate"], ["sodium hydroxide"])
    assert c["close_matches"] == []


def test_bracket_difference_is_same_but_reported():
    c = compare_ingredients(["Hyaluronic Acid (0.8%)"],
                            ["Hyaluronic Acid (Sodium Hyaluronate) (0.8%)"])
    assert c["comparison"] == "same"
    assert len(c["raw_text_differences"]) == 1


# ---- packaging comparison --------------------------------------------------
def test_packaging_same():
    assert compare_packaging(["glass", "cardboard"], ["cardboard", "glass"])["comparison"] == "same"


def test_packaging_different():
    c = compare_packaging(["glass"], ["glass", "cardboard"])
    assert c["comparison"] == "different" and "cardboard" in c["reason"]


def test_packaging_one_side_missing_is_cannot_compare_not_different():
    c = compare_packaging(["glass"], [], "text", "not_found")
    assert c["comparison"] == "cannot_compare"
    assert "source 2" in c["reason"]
    assert c["packaging_source"] == {"source1": "text", "source2": "not_found"}


def test_packaging_both_missing():
    c = compare_packaging([], [], "not_found", "not_found")
    assert c["comparison"] == "cannot_compare" and c["reason"]


# ---- end to end ------------------------------------------------------------
def _write(path, rec):
    path.write_text(json.dumps(rec))


def test_end_to_end_shape_source_swap_and_data_untouched(tmp_path):
    ings = ["Aloe vera leaf juice", "water", "propanediol", "sodium hyaluronate crosspolymer"]
    _write(tmp_path / "ret.json", {"website": "MECCA", "url": "u1", "ingredients": ings,
                                   "packaging": [], "packaging_source": "not_found"})
    _write(tmp_path / "brand.json", {"website": "Go-To", "url": "u2", "ingredients": ings,
                                     "packaging": ["Glass bottle, polypropylene dropper"],
                                     "packaging_source": "text"})
    before = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    product = {"slug": "x", "name": "X", "retailer": "ret.json", "brand": "brand.json"}
    stub = lambda text: ["glass", "polypropylene"]

    r = analyse.analyse_product(product, tmp_path, stub, source1="retailer")
    assert r["sources"]["source1"] == {"site": "MECCA", "url": "u1", "status": "ok"}
    assert r["ingredients"]["comparison"] == "same"
    assert r["packaging"]["comparison"] == "cannot_compare"
    assert r["packaging"]["packaging_source"] == {"source1": "not_found", "source2": "text"}
    nl = r["naughty_list"]
    assert nl["source1"]["colour"] == nl["source2"]["colour"] == "RED"
    assert set(nl["source1"]["categories"]) == {"Synthetic alcohols", "Microplastics/polymers"}
    assert nl["colours_match"] is True

    swapped = analyse.analyse_product(product, tmp_path, stub, source1="brand")
    assert swapped["sources"]["source1"]["site"] == "Go-To"

    assert before == {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}


def test_missing_data_file_is_reported_not_crash(tmp_path):
    _write(tmp_path / "brand.json", {"website": "B", "url": "u", "ingredients": ["water"],
                                     "packaging": [], "packaging_source": "not_found"})
    product = {"slug": "x", "name": "X", "retailer": "nope.json", "brand": "brand.json"}
    r = analyse.analyse_product(product, tmp_path, lambda t: [])
    assert r["sources"]["source1"]["status"] == "missing_file"
    assert r["ingredients"]["comparison"] == "cannot_compare"
    assert r["naughty_list"]["source1"]["colour"] is None


def test_scraper_provided_materials_skip_the_llm(tmp_path):
    _write(tmp_path / "a.json", {"website": "A", "ingredients": ["water"],
                                 "packaging_materials": ["Glass"], "packaging_source": "text"})
    _write(tmp_path / "b.json", {"website": "B", "ingredients": ["water"],
                                 "packaging_materials": ["glass"], "packaging_source": "image"})
    product = {"slug": "x", "name": "X", "retailer": "a.json", "brand": "b.json"}

    def boom(text):
        raise AssertionError("LLM should not be called")
    assert analyse.analyse_product(product, tmp_path, boom)["packaging"]["comparison"] == "same"

def test_analyse_pair_takes_records_directly_no_files():
    """This is the function the Stage 5 runner will call with freshly scraped
    records — it must not touch the filesystem at all."""
    rec1 = {"website": "A", "url": "u1", "ingredients": ["mineral oil"],
            "packaging": [], "packaging_source": "not_found"}
    rec2 = {"website": "B", "url": "u2", "ingredients": ["mineral oil", "dimethicone"],
            "packaging": [], "packaging_source": "not_found"}
    r = analyse.analyse_pair("X", rec1, rec2, lambda t: [])
    assert r["product"] == "X"
    assert r["sources"]["source1"] == {"site": "A", "url": "u1", "status": "ok"}
    assert r["naughty_list"]["source1"]["colour"] == "YELLOW"
    assert r["naughty_list"]["source2"]["colour"] == "RED"


def test_analyse_pair_handles_none_record_missing_scrape():
    """A failed scrape (Stage 5) is a None record, same as a missing file today."""
    rec = {"website": "A", "url": "u1", "ingredients": ["water"],
           "packaging": [], "packaging_source": "not_found"}
    r = analyse.analyse_pair("X", rec, None, lambda t: [])
    assert r["sources"]["source2"]["status"] == "missing_file"
    assert r["ingredients"]["comparison"] == "cannot_compare"