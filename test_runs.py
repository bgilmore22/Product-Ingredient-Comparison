"""Run history, change detection, logging, Gemini usage tally and --resume
(run_pipeline.py). Everything runs in a temp folder with fake sites.
python3 -m pytest -q test_runs.py"""
import json

import pytest

import run_pipeline as rp
from Scrapers import common


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(common, "GEMINI_CLIENT", None)
    for k in common.CACHE_STATS:
        common.CACHE_STATS[k] = 0
    common.LLM_USAGE.clear()
    monkeypatch.setattr(rp, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(rp, "PROGRESS_FILE", tmp_path / "runs" / "in_progress.json")
    monkeypatch.setattr(rp, "SUMMARY_FILE", tmp_path / "summary.csv")
    monkeypatch.setattr(rp, "AUDIT_FILE", tmp_path / "audit.csv")
    monkeypatch.setattr(rp, "DASHBOARD_DATA_FILE", tmp_path / "dashboard_data.js")
    monkeypatch.setattr(rp, "ROOT", tmp_path)


def _row(product, colour, s1=None, s2=None):
    return {"product": product, "colour": colour, "source1_categories": s1 or {}, "source2_categories": s2 or {}}


def test_diff_runs_reports_colour_category_new_and_removed():
    prev = {"products": [_row("A", "GREEN"), _row("B", "YELLOW", {"PEGs": ["PEG-8"]}), _row("Gone", "RED")]}
    now = [_row("A", "YELLOW", {"Silicones": ["Dimethicone"]}), _row("B", "YELLOW", {"PEGs": ["PEG-8"]}), _row("New", "GREEN")]
    changes = {c["product"]: c for c in rp.diff_runs(now, prev)}
    assert changes["A"]["detail"] == "colour GREEN -> YELLOW; source1 categories +Silicones"
    assert "B" not in changes
    assert changes["Gone"]["kind"] == "removed" and changes["New"]["kind"] == "new"
    assert rp.diff_runs(now, None) == []


def test_diff_handles_csv_style_category_strings():
    prev = {"products": [{"product": "A", "colour": "RED", "source1_categories": "PEGs; Silicones", "source2_categories": "not found"}]}
    now = [_row("A", "YELLOW", {"PEGs": ["x"]})]
    assert rp.diff_runs(now, prev)[0]["detail"] == "colour RED -> YELLOW; source1 categories -Silicones"


def _fake_pipeline(monkeypatch, pairs, colours, fail_on=None):
    monkeypatch.setattr(rp, "find_pairs", lambda: pairs)
    monkeypatch.setattr(rp, "LLMMaterialExtractor", lambda: (lambda t: []))
    calls = []

    def fake_run_one(gu, mu, method, extractor):
        calls.append(gu)
        if fail_on and gu == fail_on:
            raise KeyboardInterrupt
        name = gu.rsplit("/", 1)[-1]
        colour = colours[name]
        cats = {} if colour == "GREEN" else {"Silicones": ["Dimethicone"]}
        return {"product": name, "match_method": method,
                "sources": {"source1": {"site": "MECCA", "url": mu, "status": "ok"},
                            "source2": {"site": "Go-To Skincare", "url": gu, "status": "ok"}},
                "ingredients": {"comparison": "same", "reason": "", "only_in_source1": [], "only_in_source2": [],
                                "close_matches": [], "raw_text_differences": []},
                "packaging": {"comparison": "cannot_compare", "reason": "", "source1_materials": [], "source2_materials": [],
                              "packaging_source": {"source1": None, "source2": None}},
                "naughty_list": {"source1": {"colour": colour, "categories": cats, "unmatched_ingredients": ["Aqua"]},
                                 "source2": {"colour": colour, "categories": cats, "unmatched_ingredients": ["Aqua"]},
                                 "colours_match": True, "categories_only_in_source1": [], "categories_only_in_source2": [],
                                 "reason": ""}}
    monkeypatch.setattr(rp, "run_one", fake_run_one)
    return calls


PAIRS = [(f"https://gotoskincare.com/products/{n}", f"https://www.mecca.com/en-au/go-to/{n}-I-1/", "exact")
         for n in ("a", "b", "c")]


def test_run_saves_history_log_and_reports_changes(tmp_path, monkeypatch):
    _fake_pipeline(monkeypatch, PAIRS, {"a": "GREEN", "b": "GREEN", "c": "YELLOW"})
    assert rp.main([]) == 0
    runs = sorted((tmp_path / "runs").glob("*.json"))
    assert len(runs) == 1 and not (tmp_path / "runs" / "in_progress.json").exists()
    assert (tmp_path / "runs" / (runs[0].stem + ".log")).read_text().count("ok   ") == 3
    payload = json.loads((tmp_path / "dashboard_data.js").read_text().split("=", 1)[1].rstrip(";\n"))
    assert payload["previous_run"] is None and payload["changes"] == []
    assert payload["stats"] == {"total": 3, "scored": 3, "not_comparable": 0, "failed_pages": 0}

    _fake_pipeline(monkeypatch, PAIRS, {"a": "GREEN", "b": "YELLOW", "c": "YELLOW"})
    import time; time.sleep(1.1)                 # a new timestamp for the second run
    rp.main([])
    payload = json.loads((tmp_path / "dashboard_data.js").read_text().split("=", 1)[1].rstrip(";\n"))
    assert payload["previous_run"]["stamp"] == runs[0].stem
    assert [c["product"] for c in payload["changes"]] == ["b"]
    assert payload["changes"][0]["detail"].startswith("colour GREEN -> YELLOW")


def test_resume_skips_finished_products(tmp_path, monkeypatch):
    calls = _fake_pipeline(monkeypatch, PAIRS, {"a": "GREEN", "b": "GREEN", "c": "GREEN"}, fail_on=PAIRS[2][0])
    with pytest.raises(KeyboardInterrupt):
        rp.main([])
    cp = json.loads((tmp_path / "runs" / "in_progress.json").read_text())
    assert len(cp["pairs_done"]) == 2 and len(cp["rows"]) == 2

    calls = _fake_pipeline(monkeypatch, PAIRS, {"a": "GREEN", "b": "GREEN", "c": "GREEN"})
    assert rp.main(["--resume"]) == 0
    assert calls == [PAIRS[2][0]]                          # only the unfinished one was run
    assert not (tmp_path / "runs" / "in_progress.json").exists()
    assert (tmp_path / "summary.csv").read_text().count("\n") == 4   # header + 3 rows


def test_resume_ignores_checkpoint_for_a_different_pair(tmp_path, monkeypatch):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "in_progress.json").write_text(json.dumps({"brand": "x", "retailer": "y", "pairs_done": [], "rows": []}))
    calls = _fake_pipeline(monkeypatch, PAIRS[:1], {"a": "GREEN"})
    rp.main(["--resume"])
    assert calls == [PAIRS[0][0]]


def test_llm_usage_tally():
    common.llm_cached("m", "kit|p1", lambda: '{"verdict":"single"}')
    common.llm_cached("m", "kit|p1", lambda: '{"verdict":"single"}')
    common.llm_cached("m", "verify|p2", lambda: None)
    assert common.LLM_USAGE == {"kit": {"called": 1, "cached": 1, "failed": 0},
                                "verify": {"called": 1, "cached": 0, "failed": 1}}
    assert "2 billable calls" in common.llm_usage_summary()
