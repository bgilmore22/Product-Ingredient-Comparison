"""Disk cache (Scrapers/common.py): pages and Gemini answers are saved under
cache/ and re-used on the next run. Run: python3 -m pytest -q test_cache.py"""
import httpx
import pytest

from Scrapers import common


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CACHE_DIR", tmp_path)
    for k in common.CACHE_STATS:
        common.CACHE_STATS[k] = 0
    common.set_cache_mode("use")
    yield
    common.set_cache_mode("use")


def _fake_network(monkeypatch, body=b"<html>hi</html>", calls=None):
    calls = calls if calls is not None else []

    def fake(url, **kw):
        calls.append(url)
        return httpx.Response(200, content=body, headers={"content-type": "text/html"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(common, "_fetch_network", fake)
    return calls


def test_second_fetch_is_served_from_disk(monkeypatch):
    calls = _fake_network(monkeypatch)
    a = common.fetch("https://example.com/p/1")
    b = common.fetch("https://example.com/p/1")
    assert calls == ["https://example.com/p/1"]
    assert a.text == b.text == "<html>hi</html>"
    assert isinstance(b, common.CachedResponse)
    assert b.content == a.content and b.status_code == 200
    assert common.CACHE_STATS == {"http_hit": 1, "http_miss": 1, "llm_hit": 0, "llm_miss": 0}


def test_refresh_mode_ignores_cache(monkeypatch):
    calls = _fake_network(monkeypatch)
    common.fetch("https://example.com/p/2")
    common.set_cache_mode("refresh")
    common.fetch("https://example.com/p/2")
    assert len(calls) == 2


def test_offline_mode_never_touches_network(monkeypatch):
    calls = _fake_network(monkeypatch)
    common.fetch("https://example.com/p/3")
    common.set_cache_mode("offline")
    assert common.fetch("https://example.com/p/3").text == "<html>hi</html>"
    with pytest.raises(common.CacheMiss):
        common.fetch("https://example.com/never-fetched")
    assert len(calls) == 1


def test_stale_entry_is_refetched(monkeypatch):
    calls = _fake_network(monkeypatch)
    common.fetch("https://example.com/p/4")
    monkeypatch.setattr(common, "CACHE_MAX_AGE_HOURS", 0)   # everything is now too old
    import time
    time.sleep(0.01)
    common.fetch("https://example.com/p/4")
    assert len(calls) == 2


def test_llm_answers_cached_and_failures_not(monkeypatch):
    n = {"calls": 0}

    def ok():
        n["calls"] += 1
        return '{"verdict": "same"}'

    def fail():
        n["calls"] += 1
        return None
    assert common.llm_cached("m", "prompt A", ok) == '{"verdict": "same"}'
    assert common.llm_cached("m", "prompt A", ok) == '{"verdict": "same"}'
    assert n["calls"] == 1
    assert common.llm_cached("m", "prompt B", fail) is None
    assert common.llm_cached("m", "prompt B", fail) is None
    assert n["calls"] == 3                       # a failure is asked again next time
    common.set_cache_mode("offline")
    assert common.llm_cached("m", "prompt A", ok) == '{"verdict": "same"}'
    assert common.llm_cached("m", "prompt C", ok) is None   # never answered -> caller's failure path
    assert n["calls"] == 3


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        common.set_cache_mode("sometimes")


def test_llm_config_is_deterministic_and_takes_extras():
    cfg = common.llm_config(system_instruction="be brief")
    assert cfg.temperature == 0 and cfg.seed == common.LLM_SEED
    assert cfg.response_mime_type == "application/json" and cfg.system_instruction == "be brief"
