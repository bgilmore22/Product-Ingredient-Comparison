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
    r = check_naughty(["dimethicone", "cyclopentasiloxane", "dimethiconol"])
    assert r["colour"] == "YELLOW"
    assert list(r["categories"]) == ["Silicones"]
    assert len(r["categories"]["Silicones"]) == 3


def test_glycols_and_fatty_alcohols_are_not_synthetic_alcohols():
    # removed from the list after review: near-universal humectants/emulsifiers
    r = check_naughty(["propanediol", "pentylene glycol", "caprylyl glycol", "cetearyl alcohol"])
    assert r["colour"] == "GREEN"


def test_red_two_ingredients_two_categories():
    r = check_naughty(["mineral oil", "dimethicone"])
    assert r["colour"] == "RED"
    assert set(r["categories"]) == {"Mineral oil/petrolatum", "Silicones"}


def test_one_ingredient_counts_in_one_category_only():
    # manager's rule: one ingredient is one hit, it can never make a product RED by itself
    r = check_naughty(["PEG-12 Dimethicone"])
    assert r["colour"] == "YELLOW"
    assert set(r["categories"]) == {"Silicones"}


def test_free_from_claims_are_skipped_not_scored():
    # Mecca appends "No silicones; parabens; ..." after the list - must not false-flag
    r = check_naughty(["Aqua", "Bakuchiol", "No silicones", "parabens", "sulfates (SLS/SLES)",
                       "For a full explanation of each ingredient visit gotoskincare.com/ouringredients"])
    assert r["colour"] == "GREEN"
    assert len(r["skipped_claims"]) == 4


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
def test_end_to_end_shape_and_source_swap():
    ings = ["Aloe vera leaf juice", "water", "propanediol", "dimethicone"]
    ret = {"website": "MECCA", "url": "u1", "ingredients": ings, "packaging": [], "packaging_source": "not_found"}
    brand = {"website": "Go-To", "url": "u2", "ingredients": ings,
             "packaging": ["Glass bottle, polypropylene dropper"], "packaging_source": "text"}
    stub = lambda text: ["glass", "polypropylene"]

    r = analyse.analyse_pair("X", ret, brand, stub)
    assert r["sources"]["source1"] == {"site": "MECCA", "url": "u1", "status": "ok"}
    assert r["ingredients"]["comparison"] == "same"
    assert r["packaging"]["comparison"] == "cannot_compare"
    assert r["packaging"]["packaging_source"] == {"source1": "not_found", "source2": "text"}
    nl = r["naughty_list"]
    assert nl["source1"]["colour"] == nl["source2"]["colour"] == "YELLOW"
    assert set(nl["source1"]["categories"]) == {"Silicones"}
    assert nl["colours_match"] is True

    swapped = analyse.analyse_pair("X", brand, ret, stub)
    assert swapped["sources"]["source1"]["site"] == "Go-To"


def test_scraper_provided_materials_skip_the_llm():
    a = {"website": "A", "ingredients": ["water"], "packaging_materials": ["Glass"], "packaging_source": "text"}
    b = {"website": "B", "ingredients": ["water"], "packaging_materials": ["glass"], "packaging_source": "image"}

    def boom(text):
        raise AssertionError("LLM should not be called")
    assert analyse.analyse_pair("X", a, b, boom)["packaging"]["comparison"] == "same"


def test_generic_plastic_vs_specific_is_cannot_compare_not_different():
    r = compare_packaging(["plastic"], ["cardboard", "polypropylene"], "text", "text")
    assert r["comparison"] == "cannot_compare"
    assert "too generic" in r["reason"]


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
    assert r["sources"]["source2"]["status"] in ("missing_file", "scrape_failed", "missing")
    assert r["ingredients"]["comparison"] == "cannot_compare"

# ---- kit detection ------------------------------------------------------------
@pytest.mark.parametrize("title", ["Sunday Night Kit", "Nifty Fifty Gift Set", "12 Days of Go-To",
                                   "The Skintroduction Discovery Set", "Skin Trio"])
def test_kit_titles_are_detected(title):
    assert analyse.kit_title_reason([("MECCA", title)]) is not None


@pytest.mark.parametrize("title", ["Easy Breezy Magneezy", "Properly Clean Mini", "Glowy Blush",
                                   "Matte Pop Blush", "Face Hero Extra"])
def test_single_product_titles_are_not_kits(title):
    assert analyse.kit_title_reason([("MECCA", title)]) is None


def test_shade_variants_are_merged_and_scored_not_skipped():
    items, note = analyse.flatten_variants(["Rose: Aqua", "Glycerin", "Peach: Aqua", "Talc"])
    assert items == ["Aqua", "Glycerin", "Talc"]
    assert "variants" in note
    assert analyse.flatten_variants(["Aqua", "Glycerin"]) == (["Aqua", "Glycerin"], "")


def test_pooled_product_headers_only_warn():
    kit = ["Go-To Face Hero: Aqua", "Glycerin", "Go-To Very Useful Face Cream: Aqua"]
    assert "may be several products" in analyse.pooled_list_warning(kit)
    assert analyse.pooled_list_warning(["Rose: Mica", "Water", "Peach: Mica", "Water"]) is None
