"""
Tests for src2_scraper.py (Inca Organics product page scraper).

Run with: pytest src2_tests.py -v

Fixtures live in test_cases/src2_cases/:
  - inca_full_details.html       real page source, ingredients in an accordion,
                                  found by matching the "MINIMAL INGREDIENTS"
                                  <summary> label, no packaging text
  - inca_structure_changed.html  same page, the ingredients accordion's content
                                  div class="collapsible__content" renamed to
                                  "collapsible__content_CHANGED" (summary label
                                  text left untouched)

Same contract as src1_scraper.py:

    extract_ingredients(html: str) -> str | None
    extract_packaging_text(html: str) -> str | None
"""
import pathlib
import sys

import pytest

# scrapers live one directory up, in Product 1/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src2_scraper import extract_ingredients, extract_packaging_text

FIXTURES = pathlib.Path(__file__).parent / "src2_cases"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def full_details_html() -> str:
    return _load("inca_full_details.html")


@pytest.fixture(scope="module")
def structure_changed_html() -> str:
    return _load("inca_structure_changed.html")


# --- Happy path: real page, unchanged structure -----------------------------

def test_extract_ingredients_finds_correct_text(full_details_html):
    result = extract_ingredients(full_details_html)
    assert result is not None
    assert "Hydrolysed Bioactive Collagen Peptides (bovine) (98.7%)" in result
    assert "Hyaluronic Acid (Sodium Hyaluronate) (0.8%)" in result
    assert "Vitamin C (Ascorbic Acid) (0.5%)" in result


def test_extract_ingredients_picks_the_right_accordion(full_details_html):
    # The page has several accordions (THE BASICS, MINIMAL INGREDIENTS,
    # HOW TO USE IT...). Confirm we got the ingredients one specifically,
    # not e.g. the "how to use" directions text.
    result = extract_ingredients(full_details_html)
    assert "Mix 1-3 heaped teaspoons" not in result
    assert "Halal and Kosher friendly" in result  # part of the same accordion


def test_extract_packaging_text_returns_none_when_absent(full_details_html):
    # Confirmed by manual search: no jar/pouch/tub/tin/bottle/resealable
    # wording anywhere on this page. None here triggers the image fallback.
    assert extract_packaging_text(full_details_html) is None


# --- Structure-changed fixture: must degrade gracefully, never crash -------

def test_extract_ingredients_returns_none_on_structure_change(structure_changed_html):
    # class="collapsible__content" was renamed on the ingredients accordion's
    # content div only. The <summary>MINIMAL INGREDIENTS</summary> label text
    # is untouched, so a naive "search all text for the label" approach could
    # still find it by accident - this test guards against that: the real
    # scraper must rely on the class-based selector, and lose the content
    # when that selector breaks, same as it would in production.
    result = extract_ingredients(structure_changed_html)
    assert result is None


def test_extract_packaging_text_returns_none_on_structure_change(structure_changed_html):
    assert extract_packaging_text(structure_changed_html) is None


def test_scraper_never_raises_on_structure_changed(structure_changed_html):
    try:
        extract_ingredients(structure_changed_html)
        extract_packaging_text(structure_changed_html)
    except Exception as exc:  # noqa: BLE001 - intentional broad catch for this test
        pytest.fail(f"Scraper raised on structure-changed fixture: {exc!r}")