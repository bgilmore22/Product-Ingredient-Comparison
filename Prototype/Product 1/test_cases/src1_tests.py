"""
Tests for src1_scraper.py (The Iconic product page scraper).

Run with: pytest src1_tests.py -v

Fixtures live in test_cases/src1_cases/:
  - iconic_full_details.html       real rendered DOM, ingredients present, no packaging text
  - iconic_structure_changed.html  same page, class="product-description" renamed
                                    to class="product-description-info" (all occurrences)

These tests are written first. src1_scraper.py does not exist yet — it must be
implemented to satisfy this contract:

    extract_ingredients(html: str) -> str | None
    extract_packaging_text(html: str) -> str | None
"""
import pathlib
import sys

import pytest

# scrapers live one directory up, in Product 1/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src1_scraper import extract_ingredients, extract_packaging_text

FIXTURES = pathlib.Path(__file__).parent / "src1_cases"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def full_details_html() -> str:
    return _load("iconic_full_details.html")


@pytest.fixture(scope="module")
def structure_changed_html() -> str:
    return _load("iconic_structure_changed.html")


# --- Happy path: real page, unchanged structure -----------------------------

def test_extract_ingredients_finds_correct_text(full_details_html):
    result = extract_ingredients(full_details_html)
    assert result is not None
    assert "Hydrolysed Bioactive Collagen Peptides (bovine) (98.7%)" in result
    assert "Hyaluronic Acid" in result
    assert "Vitamin C (Ascorbic Acid) (0.5%)" in result


def test_extract_ingredients_does_not_include_unrelated_boilerplate(full_details_html):
    # Sanity check: we grabbed the ingredients list, not the whole product
    # description div dumped as one blob including directions/precautions text
    # that happens to also live under product-description.
    result = extract_ingredients(full_details_html)
    assert "Ingredients:" not in result  # label itself stripped, not just present
    assert "Suggested use" not in result


def test_extract_packaging_text_returns_none_when_absent(full_details_html):
    # The Iconic's product-description div has no packaging info in text form.
    # This None is the signal that triggers the image-vision fallback in
    # common.py / prototype.py — it is not a failure case.
    assert extract_packaging_text(full_details_html) is None


# --- Structure-changed fixture: must degrade gracefully, never crash -------

def test_extract_ingredients_returns_none_on_structure_change(structure_changed_html):
    # class="product-description" was renamed everywhere. The scraper's
    # selector should no longer match anything. This must come back as None,
    # not raise, so prototype.py can flag "scraper broke" rather than crash
    # the whole batch on one changed page.
    result = extract_ingredients(structure_changed_html)
    assert result is None


def test_extract_packaging_text_returns_none_on_structure_change(structure_changed_html):
    assert extract_packaging_text(structure_changed_html) is None


def test_scraper_never_raises_on_structure_changed(structure_changed_html):
    # Belt and braces: explicitly assert no exception propagates, in case a
    # future implementation swaps the None-return for something that throws.
    try:
        extract_ingredients(structure_changed_html)
        extract_packaging_text(structure_changed_html)
    except Exception as exc:  # noqa: BLE001 - intentional broad catch for this test
        pytest.fail(f"Scraper raised on structure-changed fixture: {exc!r}")