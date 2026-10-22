"""
sanity.py (project root) - "does this scraped ingredient list look like one?"

The scrapers return whatever text sits where the ingredients should be. When a
site changes its layout, or a product is a supplement, the result can be a
truncated list, a dose table, a sentence or leftover HTML - and the pipeline
would still score it and print a confident colour. These checks catch the
shapes that give it away.

They only WARN. A warning is appended to the row's `note`; it never changes a
colour and never skips a product (a list that merely looks odd is still
scored - same rule as the kit check, where only deliberate signals skip).

    list_warnings(items)                 -> [str]   problems with one list
    compare_warnings(items1, items2, n1, n2) -> [str]  problems between the two lists
"""
import re

from analyse import _CLAIM_TAIL_START, _NEGATED_ITEM

MIN_ITEMS = 3            # fewer than this is almost always a cut-off list (simple supplements can have 3)
MAX_ITEMS = 100          # ... or longer
DOSE_ITEMS = 3           # this many "250 mg"-style items = a supplement facts table
PROSE_WORDS = 14         # an "ingredient" with this many words is a sentence
COUNT_RATIO = 0.5        # one list under half the length of the other ...
COUNT_GAP = 8            # ... and at least this many items shorter = suspicious

_DOSE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|ug|iu|kcal|kj)\b", re.IGNORECASE)
_HTML = re.compile(r"<[a-z/][^>]*>|&(?:amp|nbsp|lt|gt|quot|#\d+);| ", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"^\s*(?:see|refer to)\s+(?:pack|packag|label|box|bottle)|coming soon|not (?:yet )?available|^\s*n/?a\s*$|"
    r"^\s*(?:ingredients?\s*)?(?:to be confirmed|tbc|tba)\b", re.IGNORECASE)


def _ingredient_items(items):
    """The part of the list that is ingredients: everything before the first free-from claim."""
    out = []
    for raw in items or []:
        low = raw.lower()
        if _CLAIM_TAIL_START.search(low) or _NEGATED_ITEM.search(low):
            break
        out.append(raw)
    return out


def list_warnings(items):
    items = _ingredient_items(items)
    if not items:
        return []
    out = []
    n = len(items)
    if n < MIN_ITEMS:
        out.append(f"only {n} ingredient{'s' if n != 1 else ''} found - the list may be cut short")
    if n > MAX_ITEMS:
        out.append(f"{n} ingredients - unusually long, may include text that is not ingredients")
    dosed = [i for i in items if _DOSE.search(i)]
    if len(dosed) >= DOSE_ITEMS:
        out.append(f"{len(dosed)} items carry doses (e.g. '{dosed[0][:40]}') - looks like a supplement facts table, not an INCI list")
    prose = next((i for i in items if len(i.split()) >= PROSE_WORDS), None)
    if prose:
        out.append(f"an item reads like a sentence ('{prose[:50]}...') - page text may have been captured")
    if any(_HTML.search(i) for i in items):
        out.append("leftover HTML in the list - the scraper may need updating")
    placeholder = next((i for i in items if _PLACEHOLDER.search(i)), None)
    if placeholder:
        out.append(f"no real ingredient list on the page ('{placeholder[:40]}')")
    return out


def compare_warnings(items1, items2, name1, name2):
    n1, n2 = len(_ingredient_items(items1)), len(_ingredient_items(items2))
    if not n1 or not n2:
        return []
    short, long_ = min(n1, n2), max(n1, n2)
    if short < COUNT_RATIO * long_ and long_ - short >= COUNT_GAP:
        return [f"ingredient counts differ a lot ({name1} {n1} vs {name2} {n2}) - one list may be cut short, or the formulas differ"]
    return []
