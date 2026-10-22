"""
audit_hints.py (project root) - "this ingredient matched nothing, but look at it".

The naughty-list patterns only recognise names someone thought of. An
ingredient spelled a way nobody expected passes silently as clean. For every
ingredient that matched NO category, review_hint() checks the name against
looser word stems per category and, if one fits, says which category it
resembles. The audit table then sorts those lines straight after the flagged
ones, so a missed spelling is a few lines to read rather than 400.

A hint is a prompt to look, not a verdict: nothing here changes a colour.
Several hints are deliberate exclusions the client may want to revisit
(soluble polymers, fatty alcohols, PPG) - they show up as "review" lines on
purpose. To stop a hint recurring for something you have decided is fine,
tighten its stem here.
"""
import re

HINT_STEMS = {
    "Parabens": r"parab|hydroxybenzoate",
    "Phthalates": r"phthal",
    # not when the label says "natural": the list treats natural fragrance as fine
    "Synthetic fragrance/parfum": r"^(?!.*\bnatural\b).*(?:parfum|fragran|perfum|\baroma\b|\bmusk|ionone|cinnamal|cinnamyl|propional|carboxaldehyde)",
    "SLS/SLES": r"(?<!magnesium )(?<!zinc )(?<!calcium )(?<!copper )(?<!aluminum )(?<!metho)sul(?:f|ph)ate|\bsl[es]s\b",
    "Formaldehyde & releasers": r"formal|hydantoin|methylol|oxazolidin|bronopol|glyox|diazolidinyl|methenamine|hydroxymethyl",
    "Mineral oil/petrolatum": r"paraffin|petrol|mineral|vaselin|ceresin|ozokerite|isododecane|isohexadecane|polyisobutene|polydecene",
    "Silicones": r"silox|silan|silicon|silyl|silsesq|cone\b|methicon|dimeticon",
    "PEGs": r"\bpeg|ethoxyl|polyoxy|oxynol|[a-z]eth[\s-]?\d|poloxam|polysorb|macrogol",
    "Oxybenzone & octinoxate": r"benzophenon|methoxycinnam|oxybenzone|octinox",
    "Carbon black": r"\bcarbon\b|ci ?7726\d|lampblack",
    "Triclosan": r"triclo|irgasan|chlorophenol",
    "Toluene": r"toluen|toluol|xylen|\bbenzene\b",
    "Synthetic alcohols": r"denat|\bsd\b|propanol|methanol|\balcohol\s*\d",
    "Microplastics/polymers": r"polyethyl|polypropyl|polystyr|methacryl|nylon|\bpvc\b|polyvinyl|glitter|acrylate|polyurethane|polyamide|polyester|crosspolymer|copolymer",
    "PFAS": r"fluor(?!phlogopite)|ptfe|teflon",
}
_COMPILED = {cat: re.compile(p, re.IGNORECASE) for cat, p in HINT_STEMS.items()}


def review_hint(name):
    """'possible PEGs' for an unmatched ingredient that resembles a category, else ''."""
    for cat, rx in _COMPILED.items():
        if rx.search(name or ""):
            return f"possible {cat}"
    return ""
