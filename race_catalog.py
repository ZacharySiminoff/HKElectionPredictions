"""The races in the game and where their odds come from.

This is the source of truth for races. After editing it, open Admin ->
Races -> "Sync race list" to copy it into the database.

Winner races list options as "Name (P)"; the party letter (D/R/I) drives
colours. `source.match` maps each option to substrings that identify its
Polymarket market (checked against the market's title and question, case-
insensitive). Polymarket names some markets by party ("Will the Democrats
win...") and some by candidate, so both kinds of match strings are used.

Senate races included: every race whose Polymarket favourite was below 90%
on 2026-11-02, which also covers Mississippi and Nebraska.
"""

from __future__ import annotations


def _winner(race_id, title, short, category, slug, options, sort_order):
    return {
        "id": race_id,
        "title": title,
        "short": short,
        "category": category,
        "race_type": "winner",
        "options": [label for label, _ in options],
        "min_value": None,
        "max_value": None,
        "sort_order": sort_order,
        "source": {
            "polymarket": slug,
            "match": {label: list(match) for label, match in options},
        },
    }


DEM = ("Democrat",)
REP = ("Republican",)

_SENATE = [
    ("ak_senate", "Alaska Senate", "AK-Sen", "alaska-senate-election-winner",
     [("Mary Peltola (D)", ("Mary Peltola",)), ("Dan Sullivan (R)", ("Dan Sullivan",))]),
    ("fl_senate", "Florida Senate (special)", "FL-Sen", "florida-senate-election-winner",
     [("Angie Nixon (D)", DEM + ("Angie Nixon",)), ("Ashley Moody (R)", REP + ("Ashley Moody",))]),
    ("ia_senate", "Iowa Senate", "IA-Sen", "iowa-senate-election-winner",
     [("Josh Turek (D)", DEM + ("Josh Turek",)), ("Ashley Hinson (R)", REP + ("Ashley Hinson",))]),
    ("ks_senate", "Kansas Senate", "KS-Sen", "kansas-senate-election-winner",
     [("Adam Hamilton (D)", DEM + ("Adam Hamilton",)), ("Roger Marshall (R)", REP + ("Roger Marshall",))]),
    ("la_senate", "Louisiana Senate", "LA-Sen", "louisiana-senate-election-winner",
     [("Jamie Davis (D)", DEM + ("Jamie Davis",)), ("Julia Letlow (R)", REP + ("Julia Letlow",))]),
    ("me_senate", "Maine Senate", "ME-Sen", "maine-senate-election-winner",
     [("Troy Jackson (D)", DEM + ("Troy Jackson",)), ("Susan Collins (R)", REP + ("Susan Collins",))]),
    ("mi_senate", "Michigan Senate", "MI-Sen", "michigan-senate-election-winner",
     [("Abdul El-Sayed (D)", DEM + ("El-Sayed",)), ("Mike Rogers (R)", REP + ("Mike Rogers",))]),
    ("ms_senate", "Mississippi Senate", "MS-Sen", "mississippi-senate-election-winner",
     [("Scott Colom (D)", DEM + ("Scott Colom",)), ("Cindy Hyde-Smith (R)", REP + ("Hyde-Smith",))]),
    ("ne_senate", "Nebraska Senate", "NE-Sen", "nebraska-senate-election-winner",
     [("Dan Osborn (I)", ("independent", "Dan Osborn")), ("Pete Ricketts (R)", REP + ("Pete Ricketts",))]),
    ("oh_senate", "Ohio Senate (special)", "OH-Sen", "ohio-senate-election-winner",
     [("Sherrod Brown (D)", DEM + ("Sherrod Brown",)), ("Jon Husted (R)", REP + ("Jon Husted",))]),
    ("sc_senate", "South Carolina Senate", "SC-Sen", "south-carolina-senate-election-winner",
     [("Annie Andrews (D)", DEM + ("Annie Andrews",)),
      ("Darline Graham Nordone (R)", REP + ("Nordone",))]),
    ("tx_senate", "Texas Senate", "TX-Sen", "texas-senate-election-winner",
     [("James Talarico (D)", DEM + ("Talarico",)), ("Ken Paxton (R)", REP + ("Ken Paxton",))]),
]

_GOVERNOR = [
    ("ga_governor", "Georgia Governor", "GA-Gov", "georgia-governor-winner-2026",
     [("Keisha Lance Bottoms (D)", DEM + ("Bottoms",)), ("Rick Jackson (R)", REP + ("Rick Jackson",))]),
    ("ia_governor", "Iowa Governor", "IA-Gov", "iowa-governor-winner-2026",
     [("Rob Sand (D)", DEM + ("Rob Sand",)), ("Zach Lahn (R)", REP + ("Zach Lahn",))]),
    ("ks_governor", "Kansas Governor", "KS-Gov", "kansas-governor-winner-2026",
     [("Cindy Holscher (D)", DEM + ("Holscher",)), ("Ty Masterson (R)", REP + ("Masterson",))]),
    ("oh_governor", "Ohio Governor", "OH-Gov", "ohio-governor-winner-2026",
     [("Amy Acton (D)", DEM + ("Amy Acton",)), ("Vivek Ramaswamy (R)", REP + ("Ramaswamy",))]),
    ("tx_governor", "Texas Governor", "TX-Gov", "texas-governor-winner-2026",
     [("Gina Hinojosa (D)", DEM + ("Hinojosa",)), ("Greg Abbott (R)", REP + ("Greg Abbott",))]),
]

_HOUSE = [
    ("co08_house", "Colorado 8th District", "CO-08", "co-08-house-election-winner",
     [("Manny Rutinel (D)", DEM + ("Rutinel",)), ("Gabe Evans (R)", REP + ("Gabe Evans",))]),
    ("mi07_house", "Michigan 7th District", "MI-07", "mi-07-house-election-winner",
     [("William Lawrence (D)", DEM + ("William Lawrence",)), ("Tom Barrett (R)", REP + ("Tom Barrett",))]),
]


def _build() -> list[dict]:
    races = []
    order = 0
    for category, group in (("Senate", _SENATE), ("Governor", _GOVERNOR), ("House", _HOUSE)):
        for race_id, title, short, slug, options in group:
            order += 1
            races.append(_winner(race_id, title, short, category, slug, options, order))
    races.append({
        "id": "dem_house_seats",
        "title": "Total Democratic House seats",
        "short": "House D",
        "category": "House total",
        "race_type": "number",
        "options": None,
        "min_value": 0,
        "max_value": 435,
        "sort_order": order + 1,
        "source": {
            # Kalshi: 4-seat brackets (primary) + "at least N" ladder (shapes the upper tail).
            "kalshi_brackets": "KXDHOUSESEATS-27",
            "kalshi_ladder": "KXDHOUSEWON-26NOV03",
            # Polymarket Republican-seat brackets, used only if Kalshi fails.
            "polymarket_r_brackets": "republican-house-seats-after-the-2026-midterm-elections",
            "house_size": 435,
        },
    })
    return races


RACES: list[dict] = _build()


def party_of(label: str) -> str | None:
    """'James Talarico (D)' -> 'D'."""
    label = label.strip()
    if label.endswith(")") and "(" in label:
        return label[label.rindex("(") + 1 : -1].strip() or None
    return None


def short_name(label: str) -> str:
    """'James Talarico (D)' -> 'Talarico'."""
    name = label.split("(")[0].strip()
    return name.split()[-1] if name else label
