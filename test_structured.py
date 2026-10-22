"""Shopify product JSON helpers (Scrapers/go-to_skincare.py) and the
ingredient audit (run_pipeline.py). Offline. python3 -m pytest -q test_structured.py"""
import importlib.util
import json
from pathlib import Path

import pytest

from Scrapers import common
import run_pipeline as rp

_spec = importlib.util.spec_from_file_location("goto", Path("Scrapers") / "go-to_skincare.py")
goto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(goto)


def test_product_json_url():
    assert goto.product_json_url("https://gotoskincare.com/products/face-hero?variant=1#x") == \
        "https://gotoskincare.com/products/face-hero.json"
    assert goto.product_json_url("https://gotoskincare.com/products/face-hero/") == \
        "https://gotoskincare.com/products/face-hero.json"


def test_kit_signals_from_type_and_tags():
    assert "product_type" in goto.kit_signals({"product_type": "Gift Sets", "tags": []})
    assert "tag" in goto.kit_signals({"product_type": "Skincare", "tags": "bestseller, bundle"})
    assert goto.kit_signals({"product_type": "Moisturiser", "tags": ["face", "mini"]}) is None
    assert goto.kit_signals(None) is None


def test_variant_titles_skip_default():
    p = {"variants": [{"title": "Default Title"}]}
    assert goto.variant_titles(p) == []
    p = {"variants": [{"title": "Rose"}, {"title": "Peach"}]}
    assert goto.variant_titles(p) == ["Rose", "Peach"]


def test_fetch_product_json_fails_open(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("404")
    monkeypatch.setattr(common, "fetch", boom)
    assert goto.fetch_product_json("https://gotoskincare.com/products/x") is None

    class R:
        text = json.dumps({"product": {"title": "X", "product_type": "Kit", "variants": []}})
    monkeypatch.setattr(common, "fetch", lambda url, **kw: R())
    assert goto.fetch_product_json("https://gotoskincare.com/products/x")["title"] == "X"
    monkeypatch.setattr(common, "fetch", lambda url, **kw: type("R", (), {"text": "<html>"})())
    assert goto.fetch_product_json("https://gotoskincare.com/products/x") is None


def _result(name, s1_cats, s1_unmatched, s2_cats, s2_unmatched):
    return {"product": name,
            "ingredients": {"comparison": "same"},
            "sources": {"source1": {"site": "MECCA"}, "source2": {"site": "Go-To Skincare"}},
            "naughty_list": {"source1": {"categories": s1_cats, "unmatched_ingredients": s1_unmatched},
                             "source2": {"categories": s2_cats, "unmatched_ingredients": s2_unmatched}}}


def test_audit_counts_and_orders_flagged_first():
    results = [
        _result("A", {"Silicones": ["Dimethicone"]}, ["Aqua"], {"Silicones": ["Dimethicone"]}, ["Aqua"]),
        _result("B", {}, ["Aqua", "Glycerin"], {"Silicones": ["Dimethicone"]}, ["Aqua"]),
        {"product": "Kit", "ingredients": {"comparison": "not_comparable"}},
    ]
    audit = rp.build_audit(results)
    assert audit[0]["category"] == "Silicones"
    flagged = [(a["ingredient"], a["site"], a["product_count"]) for a in audit if a["category"]]
    assert ("Dimethicone", "Go-To Skincare", 2) in flagged
    assert ("Dimethicone", "MECCA", 1) in flagged
    aqua = [a for a in audit if a["ingredient"] == "Aqua" and a["site"] == "MECCA"][0]
    assert aqua["category"] == "" and aqua["product_count"] == 2 and aqua["products"] == "A; B"
    assert all(a["category"] == "" for a in audit[len(flagged):])


def test_audit_written_and_in_dashboard_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "AUDIT_FILE", tmp_path / "audit.csv")
    monkeypatch.setattr(rp, "DASHBOARD_DATA_FILE", tmp_path / "d.js", raising=False)
    audit = rp.build_audit([_result("A", {"PEGs": ["PEG-8"]}, ["Aqua"], {}, ["Aqua"])])
    rp.write_audit(audit)
    text = (tmp_path / "audit.csv").read_text()
    assert text.splitlines()[0] == "ingredient,category,site,product_count,products"
    assert "PEG-8,PEGs,MECCA,1,A" in text
