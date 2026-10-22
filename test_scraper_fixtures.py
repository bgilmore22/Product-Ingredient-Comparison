"""Replays every saved page in tests/fixtures/ through its scraper and checks the result is
what was recorded. Add pages with capture_fixture.py. The harness itself is tested with a
fake scraper, so this file passes (with the replay skipped) before any real page is saved.
python3 -m pytest -q test_scraper_fixtures.py"""
import json
import types

import pytest

import capture_fixture as cf
from sources import SOURCES

FIXTURES = cf.fixtures()


@pytest.mark.parametrize("site,name,html_path,exp_path", FIXTURES or [pytest.param(None, None, None, None, marks=pytest.mark.skip(
    reason="no saved pages yet - run capture_fixture.py to add some"))], ids=lambda v: str(v) if isinstance(v, str) else "")
def test_saved_page_still_extracts_the_same(site, name, html_path, exp_path):
    expected = json.loads(exp_path.read_text("utf-8"))["expected"]
    actual = cf.snapshot(SOURCES[site].scraper, html_path.read_text("utf-8"))
    assert cf.differences(expected, actual) == []


# ---- the harness ----------------------------------------------------------
def _fake_scraper():
    def ing(html):
        return "Aqua, Glycerin" if "INGREDIENTS" in html else None
    return types.SimpleNamespace(
        WEBSITE_NAME="Fake", extract_ingredients=ing, extract_packaging_text=lambda h: None,
        extract_product_name=lambda h: "Face Hero", extract_brand=lambda h: "Go-To", extract_image_urls=lambda h: [])


@pytest.fixture
def fake_site(monkeypatch):
    monkeypatch.setitem(SOURCES, "fake", types.SimpleNamespace(scraper=_fake_scraper()))


def test_capture_then_replay_then_detect_a_layout_change(tmp_path, fake_site):
    page = "<html>INGREDIENTS Aqua, Glycerin</html>"
    html_path, exp_path, snap = cf.capture("fake", "https://x/p", "face-hero", root=tmp_path, html=page)
    assert snap["extract_ingredients"] == "Aqua, Glycerin"
    found = cf.fixtures(tmp_path)
    assert [(s, n) for s, n, _h, _e in found] == [("fake", "face-hero")]
    expected = json.loads(exp_path.read_text())["expected"]
    assert cf.differences(expected, cf.snapshot(SOURCES["fake"].scraper, page)) == []
    # the site changes its markup: the extractor now finds nothing
    drift = cf.differences(expected, cf.snapshot(SOURCES["fake"].scraper, "<html>redesigned</html>"))
    assert len(drift) == 1 and drift[0].startswith("extract_ingredients: expected")


def test_capture_refuses_to_overwrite_or_to_lock_in_an_empty_result(tmp_path, fake_site):
    cf.capture("fake", "u", "a", root=tmp_path, html="<html>INGREDIENTS</html>")
    with pytest.raises(SystemExit):
        cf.capture("fake", "u", "a", root=tmp_path, html="<html>INGREDIENTS</html>")
    cf.capture("fake", "u", "a", root=tmp_path, html="<html>INGREDIENTS</html>", force=True)
    with pytest.raises(SystemExit, match="NO ingredients"):
        cf.capture("fake", "u", "b", root=tmp_path, html="<html>nothing</html>")
    cf.capture("fake", "u", "b", root=tmp_path, html="<html>nothing</html>", allow_empty=True)


def test_check_live_reports_only_drifted_pages(tmp_path, fake_site):
    cf.capture("fake", "https://x/ok", "ok", root=tmp_path, html="<html>INGREDIENTS</html>")
    cf.capture("fake", "https://x/changed", "changed", root=tmp_path, html="<html>INGREDIENTS</html>")
    cf.capture("fake", "https://x/down", "down", root=tmp_path, html="<html>INGREDIENTS</html>")

    def live(url):
        if url.endswith("/down"):
            raise ConnectionError("503")
        return "<html>INGREDIENTS</html>" if url.endswith("/ok") else "<html>new layout</html>"
    drifted = {name: diffs for _s, name, diffs in cf.check_live(tmp_path, fetch=live)}
    assert set(drifted) == {"changed", "down"}
    assert "could not fetch" in drifted["down"][0]
