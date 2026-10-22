"""
naughty_list.py  (project root)

The naughty list: 15 categories, each with a list of regex patterns.
The 15 categories and the scoring are the manager's and are final. This file
only spells out which ingredient names belong in each category, reading the
category names literally.

This file is DATA ONLY. The matching code lives elsewhere and imports it:

    from naughty_list import NAUGHTY_LIST

How the matcher is meant to use it:
  - Run each pattern with re.search against the LOWERCASED raw ingredient
    text (not the normalised form, which can strip what a pattern needs).
  - A hit is a CATEGORY, not an ingredient. Two ingredients that hit the
    same category count as one hit.
  - One ingredient counts towards ONE category only. If its name matches
    patterns in two (PEG-12 dimethicone: PEGs and Silicones), it is filed
    under whichever comes first in CATEGORY_PRIORITY below, so a single
    ingredient can never push a product from YELLOW to RED on its own.
  - Free-from claims ("Paraben-free", "No sulfates") are skipped by the
    matcher (analyse.check_naughty) before these patterns run, so a pattern
    here never needs its own negation handling.

How to edit it:
  - New term: add an r"..." string to the right category's list.
  - New category: add a new key. The comments after each pattern are notes.
  - Keep the r prefix so backslashes are read as regex, not Python escapes.
  - Then run  python3 naughty_list.py  - it checks every pattern compiles and
    runs the SHOULD_HIT / SHOULD_NOT_HIT examples at the bottom. Add an
    example for anything you add or change.

Principles used when choosing patterns (so the next person can keep them):
  - Match the INCI name as it appears on a label, lowercase, plus the common
    English/pharma/trade names that turn up on supplement and nail labels.
  - Word boundaries (\\b) wherever a short pattern could sit inside a longer,
    unrelated name (methylal vs methylalanine, paraffin vs isoparaffin).
  - Related-but-not-the-same chemicals stay out: hyaluronic acid is not
    sodium hyaluronate, phenoxyethanol is not ethanol, PPG is not PEG.
  - Items marked "judgement" are defensible either way; they are the ones to
    raise with the client first.
"""

NAUGHTY_LIST = {
    "Parabens": [
        r"paraben",  # every INCI '-paraben': methyl, ethyl, propyl, butyl, isobutyl, isopropyl, benzyl, phenyl + sodium/potassium/calcium salts
        r"\b(methyl|ethyl|propyl|butyl|isobutyl|isopropyl|benzyl|phenyl)[\s-]*(p-|4-|para-)?\s*hydroxybenzoate",  # chemical names of the same esters (supplement/pharma labels)
        r"\be\s?21[4-9]\b",  # food-additive E-numbers E214-E219 (also written "E 216")
    ],
    "Phthalates": [
        r"\b(dibutyl|diethyl|dimethyl|diethylhexyl|dioctyl|dicyclohexyl|diisobutyl|diisononyl|diisodecyl|dihexyl|dipropyl|dipentyl|butyl benzyl|benzyl butyl|bis\(2-ethylhexyl\)|di\(2-ethylhexyl\))\s*phthalate",  # plasticiser phthalates: DBP, DEP, DMP, DEHP, BBP etc.
        r"\b(dbp|dehp|bbp|dinp|didp|dmp)\b",  # common abbreviations (DEP left out: too ambiguous as a token)
        r"(?<!tere)(?<!iso)(?<!acetate )(?<!hypromellose )(?<!methylcellulose )(?<!succinate )phthalate",  # any other phthalate ester; skips terephthalate (PET, see Microplastics), isophthalate and the tablet-coating polymers
    ],
    "Synthetic fragrance/parfum": [
        r"(?<!natural )\b(parfum|fragrance|perfume)\b(?!\s*\([^)]*natural[^)]*\))",  # counts as synthetic UNLESS the label itself says "natural fragrance/parfum" or "fragrance (natural ...)" - naughty-list review Sept 2026
        r"hexamethylindanopyran|acetyl hexamethyl tetralin|musk (xylene|ketone)|ethylene brassylate|\bgalaxolide\b|\btonalide\b",  # synthetic musks (galaxolide, tonalide, musk xylene, musk ketone, ethylene brassylate)
        r"\b(hhcb|ahtn)\b|musk (ambrette|tibetene|moskene)",  # more synthetic musks: abbreviations and older musks
        r"butylphenyl methylpropional|hydroxyisohexyl 3-cyclohexene carboxaldehyde|\blilial\b|\blyral\b",  # Lilial and Lyral: BANNED in the EU (reprotoxic/sensitizer)
        # Iso E Super, Hedione and ethyl vanillin removed after review (Sept 2026) -
        # not banned or restricted anywhere found, not on the EU mandatory-
        # disclosure allergen list; being synthetic alone isn't what this
        # category is meant to catch.
        # Synthetic aroma chemicals (EU mandatory-disclosure allergens) that do
        # not occur in essential oils, so their presence means a synthetic
        # fragrance was added. Linalool, limonene, citronellol, geraniol,
        # eugenol, citral, farnesol, coumarin, benzyl benzoate, benzyl salicylate
        # and the other allergens that essential oils contain naturally are
        # deliberately NOT here. (Benzyl salicylate removed Oct 2026: it occurs
        # naturally in ylang-ylang and jasmine oils - it flagged Things Will Calm
        # Down Now, whose Mecca list declares it next to Cananga Odorata oil.)
        r"\bhexyl cinnamal\b",
        r"\bamyl cinnamal\b",
        r"\bamylcinnamyl alcohol\b",
        r"\balpha-isomethyl ionone\b",
        r"\bhydroxycitronellal\b",
        r"\bmethyl 2-octynoate\b",
    ],
    "SLS/SLES": [
        r"sodium\s+(lauryl|laureth(-\d+)?|lauryl ether|dodecyl)\s+sul(f|ph)ate",  # SLS and SLES incl. 'sulphate' (AU/UK spelling) and sodium dodecyl sulfate
        r"\bsles\b|\bsls\b",  # abbreviations
        r"(lauryl|laureth|myreth)[\w-]*[\s-]+sul(f|ph)ate",  # same sulfate class: ammonium/TEA/magnesium/potassium lauryl or laureth sulfate, sodium myreth sulfate
        # NOT here: sodium coco-sulfate (same class, different name - add
        # r"coco[\s-]?sul(f|ph)ate" if the client wants it), sodium lauryl
        # sulfoacetate (SLSA), sarcosinates, isethionates, olefin sulfonates -
        # different chemistry, commonly confused with SLS on "sulfate-free" labels.
    ],
    "PEGs": [
        r"\bpeg[\s-]?\d+",  # PEG-N names incl. bis-PEG, PPG-N-PEG-N, PEG-14M. Also hits Silicones when the name has dimethicone (PEG-12 dimethicone)
        r"\bpeg/ppg|\bppg[\s-]?\d+[\s-]?peg",  # PEG/PPG copolymers and PEG/PPG dimethicones, either order
        r"\bpolyethylene glycol\b",  # full name (also 'polyethylene glycol 400')
        r"\bpolyethylene oxide\b",  # same polymer under its other name
        r"\bmacrogol\b",  # pharma/supplement name for polyethylene glycol
        r"\w*eth[\s-]?\d+(?!\d|\s*sul(f|ph)ate)",  # ethoxylates (laureth-, ceteareth-, steareth-, oleth-, sorbeth-, glycereth-, C12-15 pareth-N). Laureth-N sulfate is left to SLS/SLES
        r"ethoxylated",  # any name that says it is ethoxylated
        r"polysorbate[\s-]?\d+",  # PEG-sorbitan esters (ethoxylated)
        r"\bpolyoxyethylene\b|\bpolyoxyl\s?\d+",  # chemical / pharma names for a PEG chain (polyoxyl 40 stearate)
        r"(nonoxynol|octoxynol)-?\d+",  # ethoxylated surfactants
        r"poloxamer[\s-]?\d+",  # PEG-PPG-PEG block copolymers
        # NOT here: PPG-N on its own (polypropylene glycol - different polymer),
        # polyglyceryl-N esters, glycerin, propylene glycol.
    ],
    "Formaldehyde & releasers": [
        # Source for the releaser names: Washington WAC 173-339 formaldehyde-releaser list
        r"formaldehyde",  # direct name; also paraformaldehyde, tosylamide/formaldehyde resin (nail), urea polymer with formaldehyde
        r"\bformalin\b|methylene glycol",  # formaldehyde in water / hydrate form
        r"dmdm[\s-]?hydantoin|dimethylol[\w,\-\s]*hydantoin",  # DMDM hydantoin and its chemical name 1,3-dimethylol-5,5-dimethylhydantoin
        r"\bmdm hydantoin",
        r"imid(az|iaz)olidinyl urea",  # the WAC list misspells it 'imidiazolidinyl'; both matched
        r"diazolidinyl urea",
        r"quaternium-15|\bdowicil\b",  # quaternium-15 and its trade name
        r"bronopol|2-bromo-2-nitropropane-1,3-diol|bromonitropropanediol",
        r"5-bromo-5-nitro-1,3-dioxane|bronidox",
        r"hydroxymethylglycinate",  # sodium hydroxymethylglycinate
        r"polyoxymethylene",
        r"\bmethenamine\b|hexamethylenetetramine",
        r"\bmethylal\b",  # word-bounded so methylalanine etc. don't hit
        r"benzylhemiformal",
        r"tris\(?hydroxymethyl\)?\s?nitromethane",
        r"7-ethylbicyclo-?oxazolidine|bioban cs-?1246",
        r"dimethylol (urea|glycol)|tetramethylol\s?glycoluril|dimethyl oxazolidine",
    ],
    "Mineral oil/petrolatum": [
        r"mineral oil|paraffinum liquidum|liquid paraffin|paraffin oil|\bpetrolatum liquid",  # INCI name is paraffinum liquidum
        r"petrolatum|petroleum jelly|vaseline|\bpetroleum\b|\bvaselinum\b|\bsoft paraffin\b",  # petrolatum family incl. pharma names
        r"\bparaffin(um)?\b",  # paraffin wax; the word-start boundary keeps isoparaffin out
        r"\bcera microcristallina\b",  # microcrystalline wax (INCI Latin), petroleum-derived like paraffin
        r"\bmicrocrystalline wax\b",  # petroleum-derived wax
        r"\bceresin\b|\bozokerite\b",  # mineral waxes from petroleum-bearing deposits; ceresin is refined ozokerite, so they go together
        # NOT here: isododecane, isohexadecane, hydrogenated polyisobutene,
        # C13-14 isoparaffin, squalane - petrochemical-derived liquids that
        # are not mineral oil. Add them only if the client widens the category.
    ],
    "Silicones": [
        r"methicon(e|ol)",  # all -methicone / -dimethicone / -dimethiconol / cyclomethicone names
        r"\b(dimeticone|simeticone|simethicone)\b",  # INN spelling used in pharma/supplements
        r"silicone",  # the word 'silicone' and polysilicone-N
        r"siloxane|siloxy|\bpdms\b",  # cyclopentasiloxane, polydimethylsiloxane, trimethylsiloxysilicate
        r"silsesquioxane",  # silicone resin beads
        r"silanol|silane\b|silylate",  # other organosilicon names: dimethylsilanol, triethoxycaprylylsilane (pigment coating), silica silylate
        # NOT here: silica, silica dimethyl silylate is caught by 'silylate',
        # hydrated silica, silk - not silicones.
    ],
    "Oxybenzone & octinoxate": [
        r"oxybenzone|benzophenone[-\s]?3\b|\bbp-?3\b",  # oxybenzone (benzophenone-3)
        r"octinoxate|(ethylhexyl|octyl) methoxycinnamate|2-ethylhexyl 4-methoxycinnamate|\bomc\b",  # octinoxate incl. older 'octyl' name and the OMC abbreviation
    ],
    "Carbon black": [
        r"carbon black",
        r"c\.?i\.?\s?77266",  # Colour Index number, all spacing variants
        r"d\s?&\s?c black no\.?\s?2|\bblack 2\b",  # US colour name for carbon black
        r"pigment black 7",  # Colour Index generic name for carbon black
        # NOT here: vegetable carbon (CI 77268:1) - carbon black from plant
        # sources. Add r"\bvegetable carbon\b|c\.?i\.?\s?77268" if the client counts it.
    ],
    "Triclosan": [
        r"triclosan|irgasan",  # Irgasan is a trade name for triclosan
        r"5-chloro-2-\(2,4-dichlorophenoxy\)phenol",  # triclosan's chemical name
        # NOT here: triclocarban - a different (if related) antibacterial.
        # Add r"triclocarban" if the client wants the pair treated together.
    ],
    "Toluene": [
        r"(?<![a-z0-9-])(toluene|toluol|methylbenzene)(?![a-z0-9-]|\s*sul(f|ph))",  # solvent (nail products); lookarounds skip toluenesulfonate/toluenesulfonamide (nail resin), toluene-2,5-diamine (hair dye) and butylated hydroxytoluene (BHT)
    ],
    "Synthetic alcohols": [
        # Read literally: solvent/drying alcohols only - the ones people mean by
        # "synthetic alcohol" (drying, irritating). Glycols/diols, phenoxyethanol,
        # benzyl alcohol and the rose-scent aromatic alcohols (phenethyl/
        # phenylethyl alcohol, 2-phenylethanol) were removed after review (Sept
        # 2026): near-universal humectants/preservatives/fragrance alcohols,
        # not banned or restricted anywhere found, not what this category
        # is meant to catch.
        r"alcohol denat|denatured alcohol|\bsd alcohol|\balcohol\s+\d+%",  # denatured (industrial) alcohol; also "Alcohol 20%"
        r"isopropyl alcohol|isopropanol|\bipa\b",
        r"\bmethanol\b|methylated spirit|methyl alcohol",
        r"\bethanol\b|\bethyl alcohol\b|^\s*alcohol\s*(?:\(|$)|\bethyl alcohol\b",  # plain 'alcohol' on a label means ethanol; also "alcohol (ethanol)"
        # --- OFF BY DEFAULT: alcohols that are usually plant-derived; the label can't say natural vs synthetic.
        # Uncomment any of these to count them.
        # r"\b(cetyl|cetearyl|stearyl|behenyl|myristyl|lauryl|arachidyl|oleyl) alcohol\b",  # fatty alcohols
        # r"\bglycerin\b|\bglycerol\b",
        # r"\b(sorbitol|xylitol|mannitol|erythritol|inositol)\b",  # sugar alcohols
    ],
    "Microplastics/polymers": [
        # Read literally: solid/particulate microplastics and the polymer types
        # EU Reg. 2023/2055 actually targets - not any water-soluble gelling or
        # conditioning agent that happens to contain "poly-"/"polymer". The bare
        # 'polymer' catch-all, plus carbomer, polyquaternium-N, polyacrylates,
        # acrylates copolymer, PVP/PVA and the polybutene-family liquid
        # emollients, were removed after review (Sept 2026): these are soluble
        # thickeners/film-formers, not insoluble plastic particles.
        r"polyethylene\b(?!\s*(glycol|oxide))",  # also catches polyethylene terephthalate (PET glitter) and oxidized polyethylene wax. 'polyethylene glycol/oxide' are counted under PEGs
        r"polybutylene terephthalate|\bpbt\b",  # PBT glitter
        r"polypropylene\b(?!\s*glycol)",  # plastic; polypropylene glycol (PPG) is a humectant, not counted here
        r"polystyrene",  # microbeads
        r"polymethyl methacrylate|\bpmma\b|methyl methacrylate crosspolymer",  # PMMA beads
        r"nylon[-\s]?\d+",  # nylon-12, -6, -66, -11 powder - documented exfoliating microbeads. (INCI polyamide-N are film-formers/gellants, not nylon powder - left out)
        r"polyvinyl chloride|\bpvc\b",  # PVC particles
        # --- OFF BY DEFAULT (Oct 2026 review): mostly soluble film-formers in
        # nail polish and mascara, not particles. Uncomment to count them.
        # r"polyurethane|polyamide|polyester|polyimide",
        # r"polylactic acid|polylactide|\bpla\b",  # PLA - marketed biodegradable, marine persistence contested
        # NOT here: PTFE/fluoropolymers - counted once, under PFAS.
    ],
    "PFAS": [
        r"perfluoro|polyfluoro",  # perfluorodecalin, perfluorononyl dimethicone, perfluorohexane, etc.
        r"\bptfe\b|polytetrafluoroethylene|hexafluoropropylene|\bteflon\b",  # PTFE/Teflon and fluoropolymers. Counts here only, not also Microplastics
        r"fluoroalcohol|fluorotelomer|octafluoropentyl|pentafluoropropane",  # e.g. C9-15 fluoroalcohol phosphate
        r"\b(pfoa|pfos|pfna|pfda|pfhxa|pfhxs|pfbs)\b",  # named PFAS acronyms
        r"(?<!mono)fluoro(?!phlogopite|acetyl tripeptide)|fluorinated",  # any other 'fluoro' name; skips fluoride, monofluorophosphate, fluorphlogopite (synthetic mica) and trifluoroacetyl tripeptide-2 (a peptide with one CF3 group, not a PFAS surfactant)
    ],
}


# When one ingredient name matches more than one category, it is filed under
# the first of these it matches - the categories with a regulatory basis
# first, the "clean beauty" ones after. Every key in NAUGHTY_LIST must appear
# here exactly once (the self-check enforces it).
CATEGORY_PRIORITY = [
    "PFAS",
    "Formaldehyde & releasers",
    "Phthalates",
    "Triclosan",
    "Toluene",
    "Oxybenzone & octinoxate",
    "Parabens",
    "Carbon black",
    "SLS/SLES",
    "Synthetic fragrance/parfum",
    "Mineral oil/petrolatum",
    "Silicones",
    "PEGs",
    "Synthetic alcohols",
    "Microplastics/polymers",
]


# ---------------------------------------------------------------------------
# SELF-CHECK:  python3 naughty_list.py
# Every pattern must compile; every SHOULD_HIT example must hit exactly the
# categories listed; every SHOULD_NOT_HIT example must hit nothing. Add a
# line here whenever you add or change a pattern above.
# ---------------------------------------------------------------------------
SHOULD_HIT = {
    "Methylparaben": ["Parabens"],
    "Sodium Propylparaben": ["Parabens"],
    "Propyl 4-hydroxybenzoate": ["Parabens"],
    "E 216": ["Parabens"],
    "Diethyl Phthalate": ["Phthalates"],
    "DEHP": ["Phthalates"],
    "Dimethyl Phthalate": ["Phthalates"],
    "Parfum": ["Synthetic fragrance/parfum"],
    "Fragrance (Parfum)": ["Synthetic fragrance/parfum"],
    "Hydroxycitronellal": ["Synthetic fragrance/parfum"],
    "Butylphenyl Methylpropional": ["Synthetic fragrance/parfum"],
    "Sodium Lauryl Sulfate": ["SLS/SLES"],
    "Sodium Laureth Sulphate": ["SLS/SLES"],
    "Sodium Laureth-3 Sulfate": ["SLS/SLES"],
    "Ammonium Lauryl Sulfate": ["SLS/SLES"],
    "SLES": ["SLS/SLES"],
    "PEG-100 Stearate": ["PEGs"],
    "PEG-40 Hydrogenated Castor Oil": ["PEGs"],
    "Ceteareth-20": ["PEGs"],
    "C12-15 Pareth-12": ["PEGs"],
    "Polysorbate 20": ["PEGs"],
    "Polyethylene Glycol 400": ["PEGs"],
    "Glycereth-26": ["PEGs"],
    "Bis-PEG-18 Methyl Ether Dimethyl Silane": ["Silicones"],
    "PEG-12 Dimethicone": ["Silicones"],
    "Polyoxyl 40 Stearate": ["PEGs"],
    "DMDM Hydantoin": ["Formaldehyde & releasers"],
    "1,3-Dimethylol-5,5-dimethylhydantoin": ["Formaldehyde & releasers"],
    "Imidazolidinyl Urea": ["Formaldehyde & releasers"],
    "Quaternium-15": ["Formaldehyde & releasers"],
    "Sodium Hydroxymethylglycinate": ["Formaldehyde & releasers"],
    "Tosylamide/Formaldehyde Resin": ["Formaldehyde & releasers"],
    "Methylal": ["Formaldehyde & releasers"],
    "Paraffinum Liquidum": ["Mineral oil/petrolatum"],
    "Mineral Oil": ["Mineral oil/petrolatum"],
    "Petrolatum": ["Mineral oil/petrolatum"],
    "Paraffin": ["Mineral oil/petrolatum"],
    "Cera Microcristallina": ["Mineral oil/petrolatum"],
    "Ozokerite": ["Mineral oil/petrolatum"],
    "Dimethicone": ["Silicones"],
    "Cyclopentasiloxane": ["Silicones"],
    "Phenyl Trimethicone": ["Silicones"],
    "Dimethicone/Vinyl Dimethicone Crosspolymer": ["Silicones"],
    "Trimethylsiloxysilicate": ["Silicones"],
    "Polysilicone-11": ["Silicones"],
    "Triethoxycaprylylsilane": ["Silicones"],
    "Oxybenzone": ["Oxybenzone & octinoxate"],
    "Benzophenone-3": ["Oxybenzone & octinoxate"],
    "Ethylhexyl Methoxycinnamate": ["Oxybenzone & octinoxate"],
    "Octinoxate": ["Oxybenzone & octinoxate"],
    "CI 77266": ["Carbon black"],
    "Carbon Black": ["Carbon black"],
    "Black 2": ["Carbon black"],
    "Triclosan": ["Triclosan"],
    "Toluene": ["Toluene"],
    "Alcohol Denat.": ["Synthetic alcohols"],
    "SD Alcohol 40-B": ["Synthetic alcohols"],
    "Alcohol": ["Synthetic alcohols"],
    "Alcohol (Ethanol)": ["Synthetic alcohols"],
    "Isopropyl Alcohol": ["Synthetic alcohols"],
    "Polyethylene": ["Microplastics/polymers"],
    "Polyethylene Terephthalate": ["Microplastics/polymers"],
    "Nylon-12": ["Microplastics/polymers"],
    "Polymethyl Methacrylate": ["Microplastics/polymers"],
    "Polypropylene": ["Microplastics/polymers"],
    "PTFE": ["PFAS"],
    "Perfluorodecalin": ["PFAS"],
    "C9-15 Fluoroalcohol Phosphate": ["PFAS"],
    "Perfluorononyl Dimethicone": ["PFAS"],
}

SHOULD_NOT_HIT = [
    "Aqua", "Glycerin", "Niacinamide", "Sodium Hyaluronate", "Hyaluronic Acid",
    "Phenoxyethanol", "Benzyl Alcohol", "Cetearyl Alcohol", "Cetyl Alcohol",
    "Propylene Glycol", "PPG-15 Stearyl Ether", "Polyglyceryl-4 Isostearate",
    "Caprylyl Glycol", "1,2-Hexanediol", "Ethylhexylglycerin",
    "Sodium Lauryl Sulfoacetate", "Sodium Lauroyl Sarcosinate", "Sodium Cocoyl Isethionate",
    "Sodium C14-16 Olefin Sulfonate", "Disodium Laureth Sulfosuccinate",
    "Natural Fragrance", "Fragrance (Natural)", "Parfum (natural essential oils)",
    "Linalool", "Limonene", "Citronellol", "Geraniol", "Benzyl Benzoate", "Benzyl Salicylate", "Coumarin",
    "Cellulose Acetate Phthalate", "Hypromellose Phthalate", "Dimethyl Isophthalate",
    "Tocopherol", "Tocopheryl Acetate", "Butylated Hydroxytoluene", "Toluene-2,5-Diamine",
    "Toluenesulfonamide", "Isoparaffin", "C13-14 Isoparaffin", "Isododecane", "Squalane",
    "Silica", "Hydrated Silica", "Hydrolyzed Silk", "Mica", "Synthetic Fluorphlogopite",
    "Sodium Fluoride", "Sodium Monofluorophosphate", "Titanium Dioxide", "Iron Oxides",
    "Zinc Oxide", "Non-nano Zinc Oxide", "Carbomer", "Polyquaternium-7", "Acrylates/C10-30 Alkyl Acrylate Crosspolymer",
    "Polyvinyl Alcohol", "Hydrogenated Polyisobutene", "Polyglutamic Acid", "Polyhydroxystearic Acid",
    "N-Methylalanine", "Methylpropanediol", "Ethanolamine", "Triethanolamine", "Methylisothiazolinone",
    "Dipotassium Glycyrrhizate", "Cetrimonium Chloride", "Sodium Benzoate", "Potassium Sorbate",
    "Lauryl Glucoside", "Coco-Glucoside", "Decyl Glucoside", "Lauryl Betaine",
    "Benzophenone-4", "Homosalate", "Octocrylene", "Avobenzone",
    "Ethylhexyl Palmitate", "Octyldodecanol", "Ceramide NP", "Panthenol",
    "Aroma", "Sodium Coco-Sulfate", "Vegetable Carbon", "Polyester-5", "Polyamide-5", "Polylactic Acid",
    "Trifluoroacetyl Tripeptide-2",
]


def self_check():
    import re
    problems = []
    compiled = {}
    for cat, pats in NAUGHTY_LIST.items():
        for p in pats:
            try:
                compiled.setdefault(cat, []).append(re.compile(p))
            except re.error as exc:
                problems.append(f"does not compile [{cat}]: {p!r}: {exc}")

    def hits(name):
        low = name.lower()
        for cat in CATEGORY_PRIORITY + [c for c in compiled if c not in CATEGORY_PRIORITY]:
            if any(p.search(low) for p in compiled.get(cat, [])):
                return [cat]
        return []

    missing = [c for c in NAUGHTY_LIST if c not in CATEGORY_PRIORITY]
    extra = [c for c in CATEGORY_PRIORITY if c not in NAUGHTY_LIST]
    if missing or extra:
        problems.append(f"CATEGORY_PRIORITY out of sync - missing {missing}, extra {extra}")

    for name, want in SHOULD_HIT.items():
        got = hits(name)
        if got != sorted(want):
            problems.append(f"{name!r}: expected {sorted(want)}, got {got}")
    for name in SHOULD_NOT_HIT:
        got = hits(name)
        if got:
            problems.append(f"{name!r}: should hit nothing, got {got}")

    n_pat = sum(len(v) for v in NAUGHTY_LIST.values())
    print(f"{len(NAUGHTY_LIST)} categories, {n_pat} patterns, "
          f"{len(SHOULD_HIT)} should-hit and {len(SHOULD_NOT_HIT)} should-not-hit examples")
    if problems:
        print(f"{len(problems)} PROBLEM(S):")
        for p in problems:
            print("  -", p)
        return 1
    print("all checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_check())