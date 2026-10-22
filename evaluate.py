"""
evaluate.py (project root) - how often is the pipeline right?

Compares summary.csv with a hand-labelled answer file (labels.csv) and reports
agreement, which direction the mistakes lean, and every disagreement with the
ingredients that caused it. Standard library only; no network, no Gemini.

STEP 1 - make the label sheet (once):
    python3 evaluate.py --template            # 30 products, picked to cover every colour
    python3 evaluate.py --template --sample 40 --seed 2

  It writes labels.csv with the product name and both page links, and NOT the
  pipeline's colour, so your answer is not nudged. The order is shuffled.

STEP 2 - fill in labels.csv by reading the two ingredient lists yourself:
    expected_colour      GREEN / YELLOW / RED / NOT COMPARABLE (a kit or set)
                         G, Y, R and KIT also work. This is the colour YOU would give:
                         0 categories hit = GREEN, 1 = YELLOW, 2 or more = RED, taking
                         the worse of the two stores.
    expected_categories  optional. The naughty-list categories you found, separated
                         by ";" (e.g. "PEGs; Silicones"), or the word none. Fill this
                         in as well and the report also scores each category.
    notes                optional.

STEP 3 - score a run:
    python3 evaluate.py                       # summary.csv vs labels.csv
    python3 evaluate.py --fail-below 0.9      # exit code 1 if agreement is under 90%

Re-run it after every change to naughty_list.py or the matching rules: a rule
change should move the agreement figure up, and the disagreement list shows
exactly which products it moved.
"""
import argparse
import csv
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RANK = {"GREEN": 1, "YELLOW": 2, "RED": 3}
SCORED = ("GREEN", "YELLOW", "RED")
NC = "NOT COMPARABLE"
ALIASES = {"G": "GREEN", "Y": "YELLOW", "R": "RED", "KIT": NC, "NC": NC, "N/C": NC,
           "NOT_COMPARABLE": NC, "NOT COMPARABLE": NC}
LABEL_FIELDS = ["product", "source1_url", "source2_url", "expected_colour", "expected_categories", "notes"]


def norm_name(s):
    return " ".join((s or "").lower().split())


def norm_colour(s):
    c = (s or "").strip().upper()
    return ALIASES.get(c, c)


def split_categories(s):
    return {c.strip() for c in (s or "").replace("|", ";").split(";") if c.strip() and c.strip().lower() not in ("none", "not found")}


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# STEP 1: the label sheet
# ---------------------------------------------------------------------------
def pick_sample(rows, n, seed):
    """n rows covering every colour: at least min(3, size) from each colour group, the
    rest spread in proportion. Deterministic for a given seed. Shuffled."""
    rng = random.Random(seed)
    groups = defaultdict(list)
    for r in rows:
        groups[r["colour"]].append(r)
    for g in groups.values():
        rng.shuffle(g)
    chosen = []
    for g in groups.values():
        chosen += g[:min(3, len(g))]
    rest = [r for g in groups.values() for r in g[min(3, len(g)):]]
    rng.shuffle(rest)
    chosen += rest[:max(0, n - len(chosen))]
    chosen = chosen[:n] if len(chosen) > n else chosen
    rng.shuffle(chosen)
    return chosen


def write_template(summary_rows, path, n, seed):
    sample = pick_sample(summary_rows, n, seed)
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
        w.writeheader()
        for r in sample:
            w.writerow({"product": r["product"], "source1_url": r.get("source1_url", ""),
                        "source2_url": r.get("source2_url", ""),
                        "expected_colour": "", "expected_categories": "", "notes": ""})
    return len(sample)


# ---------------------------------------------------------------------------
# STEP 3: score
# ---------------------------------------------------------------------------
def our_categories(row):
    cats = set()
    for side in ("source1_categories", "source2_categories"):
        cats |= split_categories(row.get(side))
    return cats


def evaluate(summary_rows, label_rows):
    by_name = {norm_name(r["product"]): r for r in summary_rows}
    res = {"labelled": 0, "problems": [], "missing": [], "pairs": [], "category": defaultdict(Counter)}
    for lab in label_rows:
        name = lab.get("product", "")
        exp = norm_colour(lab.get("expected_colour"))
        if not exp:
            continue                                   # not labelled yet
        if exp not in SCORED and exp != NC:
            res["problems"].append(f"{name}: expected_colour '{lab.get('expected_colour')}' is not GREEN/YELLOW/RED/NOT COMPARABLE")
            continue
        res["labelled"] += 1
        row = by_name.get(norm_name(name))
        if row is None:
            res["missing"].append(name)
            continue
        ours = row["colour"]
        res["pairs"].append({"product": row["product"], "expected": exp, "ours": ours, "row": row,
                             "expected_categories": lab.get("expected_categories"), "notes": lab.get("notes", "")})
        if (lab.get("expected_categories") or "").strip():
            exp_c, our_c = split_categories(lab["expected_categories"]), our_categories(row)
            for c in exp_c | our_c:
                res["category"][c]["tp" if c in exp_c and c in our_c else "fn" if c in exp_c else "fp"] += 1
    return res


def _pct(a, b):
    return f"{a}/{b} = {100 * a / b:.0f}%" if b else "n/a"


def direction(expected, ours):
    """'lenient' = we scored greener than the label; 'strict' = redder; 'other' = involves kit/not found."""
    if expected in RANK and ours in RANK:
        return "lenient" if RANK[ours] < RANK[expected] else "strict"
    return "other"


def report(res):
    out = []
    pairs = res["pairs"]
    agree = [p for p in pairs if p["expected"] == p["ours"]]
    out.append(f"labelled {res['labelled']} | found in summary.csv {len(pairs)} | not found {len(res['missing'])}")
    for m in res["missing"]:
        out.append(f"  not in summary.csv (renamed? not matched this run?): {m}")
    for p in res["problems"]:
        out.append(f"  label problem - {p}")
    if not pairs:
        out.append("nothing to score yet - fill in expected_colour in labels.csv")
        return "\n".join(out), None
    acc = len(agree) / len(pairs)
    out.append(f"\nCOLOUR AGREEMENT: {_pct(len(agree), len(pairs))}")
    wrong = [p for p in pairs if p["expected"] != p["ours"]]
    d = Counter(direction(p["expected"], p["ours"]) for p in wrong)
    out.append(f"  too lenient (pipeline greener than you): {d['lenient']}   too strict (redder): {d['strict']}   "
               f"kit/not-found mix-ups: {d['other']}")

    cols = list(SCORED) + [NC, "NOT FOUND"]
    out.append("\nrows = your label, columns = pipeline")
    out.append(f"  {'':<16}" + "".join(f"{c:>16}" for c in cols))
    for e in list(SCORED) + [NC]:
        out.append(f"  {e:<16}" + "".join(f"{sum(1 for p in pairs if p['expected'] == e and p['ours'] == c):>16}" for c in cols))

    kit_exp = [p for p in pairs if p["expected"] == NC]
    kit_got = [p for p in pairs if p["ours"] == NC]
    tp = sum(1 for p in kit_got if p["expected"] == NC)
    out.append(f"\nKIT DETECTION: precision {_pct(tp, len(kit_got))} (of products skipped, how many really were kits), "
               f"recall {_pct(tp, len(kit_exp))} (of real kits, how many were skipped)")

    if res["category"]:
        out.append("\nPER CATEGORY (only products where you filled in expected_categories)")
        out.append(f"  {'category':<28}{'right':>7}{'missed':>8}{'false alarm':>13}")
        for c, k in sorted(res["category"].items()):
            out.append(f"  {c:<28}{k['tp']:>7}{k['fn']:>8}{k['fp']:>13}")

    if wrong:
        out.append(f"\nDISAGREEMENTS ({len(wrong)})")
        for p in sorted(wrong, key=lambda p: (direction(p["expected"], p["ours"]), p["product"])):
            r = p["row"]
            hits = "; ".join(x for x in (r.get("source1_hit_ingredients"), r.get("source2_hit_ingredients")) if x and x != "not found")
            out.append(f"  {p['product']}: you {p['expected']}, pipeline {p['ours']} ({direction(p['expected'], p['ours'])})")
            out.append(f"      pipeline categories: {'; '.join(sorted(our_categories(r))) or 'none'}")
            if hits:
                out.append(f"      hit ingredients: {hits}")
            if r.get("note"):
                out.append(f"      pipeline note: {r['note']}")
            if p["expected_categories"]:
                out.append(f"      your categories: {p['expected_categories']}")
            if p["notes"]:
                out.append(f"      your note: {p['notes']}")
    return "\n".join(out), acc


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score summary.csv against hand-labelled answers (see the top of this file)")
    ap.add_argument("--summary", default=str(ROOT / "summary.csv"))
    ap.add_argument("--labels", default=str(ROOT / "labels.csv"))
    ap.add_argument("--template", action="store_true", help="write a blank label sheet from summary.csv, then stop")
    ap.add_argument("--sample", type=int, default=30, help="products in the label sheet (default 30)")
    ap.add_argument("--seed", type=int, default=1, help="same seed = same sample")
    ap.add_argument("--force", action="store_true", help="let --template overwrite an existing labels file")
    ap.add_argument("--fail-below", type=float, default=None, metavar="FRACTION",
                    help="exit 1 if colour agreement is below this (e.g. 0.9)")
    args = ap.parse_args(argv)

    if not Path(args.summary).exists():
        print(f"{args.summary} not found - run run_pipeline.py first")
        return 2
    summary = read_csv(args.summary)

    if args.template:
        if Path(args.labels).exists() and not args.force:
            print(f"{args.labels} already exists - it may hold your labels. Use --force to overwrite it.")
            return 2
        n = write_template(summary, args.labels, args.sample, args.seed)
        print(f"{Path(args.labels).name}: {n} products to label (colours hidden, shuffled). "
              "Fill in expected_colour, then run: python3 evaluate.py")
        return 0

    if not Path(args.labels).exists():
        print(f"{args.labels} not found - make one with: python3 evaluate.py --template")
        return 2
    text, acc = report(evaluate(summary, read_csv(args.labels)))
    print(text)
    if args.fail_below is not None and (acc is None or acc < args.fail_below):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
