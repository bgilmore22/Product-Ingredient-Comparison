"""evaluate.py: label sheet and scoring. Offline. python3 -m pytest -q test_evaluate.py"""
import csv

import evaluate as ev


def _summary(rows):
    out = []
    for name, colour, c1, c2, hits in rows:
        out.append({"product": name, "colour": colour, "source1_categories": c1, "source2_categories": c2,
                    "source1_hit_ingredients": hits, "source2_hit_ingredients": "", "note": "",
                    "source1_url": f"https://m/{name}", "source2_url": f"https://g/{name}"})
    return out


SUMMARY = _summary([
    ("Face Hero", "RED", "PEGs; Silicones", "PEGs", "PEGs: PEG-8 | Silicones: Dimethicone"),
    ("Cleanser", "GREEN", "not found", "not found", ""),
    ("Balm", "YELLOW", "Silicones", "Silicones", "Silicones: Dimethicone"),
    ("Starter Kit", "NOT COMPARABLE", "not found", "not found", ""),
    ("Swipeys", "RED", "PEGs; Synthetic fragrance/parfum", "not found", "PEGs: Polysorbate 20"),
])


def _labels(*rows):
    return [{"product": p, "expected_colour": c, "expected_categories": cats, "notes": ""} for p, c, cats in rows]


def test_scores_agreement_direction_and_kits():
    res = ev.evaluate(SUMMARY, _labels(
        ("Face Hero", "red", "PEGs; Silicones"), ("Cleanser", "G", "none"), ("Balm", "GREEN", ""),
        ("Starter Kit", "KIT", ""), ("Swipeys", "YELLOW", "Synthetic fragrance/parfum"), ("Gone", "RED", "")))
    text, acc = ev.report(res)
    assert acc == 3 / 5                                   # Face Hero, Cleanser, Kit agree; Balm, Swipeys do not
    assert "COLOUR AGREEMENT: 3/5 = 60%" in text
    assert "too lenient (pipeline greener than you): 0   too strict (redder): 2" in text
    assert "precision 1/1" in text and "recall 1/1" in text
    assert "not found 1" in text and "Gone" in text
    assert "Swipeys: you YELLOW, pipeline RED (strict)" in text
    assert "hit ingredients: PEGs: Polysorbate 20" in text


def test_per_category_counts():
    res = ev.evaluate(SUMMARY, _labels(("Face Hero", "RED", "PEGs"), ("Cleanser", "GREEN", "Parabens"), ("Balm", "YELLOW", "")))
    k = res["category"]
    assert k["PEGs"]["tp"] == 1 and k["Silicones"]["fp"] == 1 and k["Parabens"]["fn"] == 1
    assert "Balm" not in str(k)                           # blank expected_categories = not scored per category


def test_unlabelled_and_bad_rows_are_reported_not_scored():
    res = ev.evaluate(SUMMARY, _labels(("Face Hero", "", ""), ("Balm", "orange", "")))
    assert res["labelled"] == 0 and len(res["problems"]) == 1
    text, acc = ev.report(res)
    assert acc is None and "nothing to score" in text


def test_template_hides_our_colour_covers_every_group_and_is_repeatable(tmp_path):
    many = _summary([(f"R{i}", "RED", "", "", "") for i in range(20)] + [(f"G{i}", "GREEN", "", "", "") for i in range(20)]
                    + [("Kit1", "NOT COMPARABLE", "", "", ""), ("Kit2", "NOT COMPARABLE", "", "", "")])
    path = tmp_path / "labels.csv"
    assert ev.write_template(many, path, 10, seed=3) == 10
    rows = list(csv.DictReader(path.open()))
    assert set(rows[0]) == set(ev.LABEL_FIELDS) and "colour" not in rows[0]
    assert all(r["expected_colour"] == "" for r in rows)
    names = {r["product"] for r in rows}
    assert any(n.startswith("R") for n in names) and any(n.startswith("G") for n in names) and \
        any(n.startswith("Kit") for n in names)
    path2 = tmp_path / "labels2.csv"
    ev.write_template(many, path2, 10, seed=3)
    assert path.read_text() == path2.read_text()


def test_main_refuses_to_overwrite_labels_and_fails_below(tmp_path, capsys):
    summ, labels = tmp_path / "summary.csv", tmp_path / "labels.csv"
    with summ.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(SUMMARY[0]))
        w.writeheader()
        w.writerows(SUMMARY)
    base = ["--summary", str(summ), "--labels", str(labels)]
    assert ev.main(base + ["--template", "--sample", "5"]) == 0
    assert ev.main(base + ["--template"]) == 2            # would overwrite
    labels.write_text("product,expected_colour\nFace Hero,GREEN\nCleanser,GREEN\n")
    assert ev.main(base) == 0
    assert ev.main(base + ["--fail-below", "0.9"]) == 1   # 1 of 2 right
    assert ev.main(base + ["--fail-below", "0.4"]) == 0
