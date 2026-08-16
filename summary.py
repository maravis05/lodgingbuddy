"""
What the write-up already told you.

Every stay arrives with a paragraph of prose nobody reads twice and a row of
structured fields half of which are null. In the Edinburgh capture the prose is
there 30 times out of 30; bathrooms is there 4 times, an address never. The
facts are not missing. They are sitting in the sentence "the apartment features
two bedrooms and two bathrooms" in a shape nothing can sort by.

So this reads the paragraph and hands back the fields. It never writes them.
`read` returns what the text claims, `against` says how each claim sits with the
record you already have, and deciding to keep one is left to the caller — a
scraped field and a sentence disagreeing is worth seeing, not worth silently
resolving in favour of whichever ran last.

Booking.com writes these two different ways and both are here. Roughly two in
three are machine-written and sectioned — "Prime Location:", "Modern
Amenities:", a colon-terminated label per paragraph, drawn from a vocabulary of
about twenty. The rest are older human copy in flat prose. The sectioned ones
are easier and less trustworthy; see the geometry note below.

Three things are worth knowing about the distances, because each one silently
breaks a naive reader:

  The figures are metric, converted. "1969 feet" is 600 m, "1312 feet" is 400 m,
  "less than 0.6 mi" is "less than 1 km" — every foot value in the corpus is a
  round number of metres run through 3.28. Read them back as metric buckets and
  they stop looking like false precision.

  A landmark in the property's own name is not a distance to itself. "Royal Mile
  Apartment is 7 minutes from Edinburgh Castle" will hand you Royal Mile = 7
  minutes if you match landmark names before masking the title out, and 7 of the
  30 names here carry a landmark token.

  The machine-written ones are sometimes just wrong. Forth House claims Edinburgh
  Castle is a 1-minute walk; it is 1.25 km away. Nothing in the text marks that
  sentence as different from the true ones, so the text alone cannot catch it.

Which is why anything with coordinates gets checked against them. Not to compute
the distance — a straight line is the wrong quantity and proximity.py explains at
length why — but to reject a claim that cannot be true at any pace. A 1-minute
walk to a point 1.25 km away is not a slow walker, it is a wrong sentence, and an
order-of-magnitude test is enough to tell those apart while staying well clear of
the honest gap between crow-flies and pavement.

Which places count as landmarks is a fact about a city, so the table is empty
here and comes from the config of whichever city the database is in —
`[[landmark]]` in cities/edinburgh.toml, installed by `use_landmarks`. A new
city is a list of names and the spellings its write-ups use, and nothing in this
file changes.

Where those landmarks are can come from the corpus rather than a gazetteer.
Thirty stays with known coordinates all quoting their distance to Edinburgh
Castle is thirty circles that intersect where the castle is, so `locate`
trilaterates it: Waverley lands 112 m off, the museum 140 m, the castle 508 m,
all from prose. It also explains its own failures usefully — the airport fits
3.7 km out because those figures are drives, not walks, and a landmark that
won't converge is one whose claims were never comparable in the first place.
A config that gives coordinates outright is believed instead, and should: it
works on the first stay rather than the fourth. Give them only for places that
are a point — a kilometre of street pinned at its midpoint turns an honest
claim from one end of it into a lie.

Two things deliberately not done. Sentiment isn't scored: "highly rated for its
location" is the same review average already in the record, laundered into an
adjective. And "boating, kayaking and canoeing are available in the surroundings"
appears in 13 of 30 Edinburgh city-centre flats, which is Booking listing a
region's activities rather than a property's, so it is dropped as noise instead
of becoming an amenity nobody can use from a tenement on the Royal Mile.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Walking pace, metres per minute, for turning a stated distance into the
# minutes the rest of the tool speaks in. Deliberately slow — 80 m/min is an
# unhurried city walk, and these are holiday afternoons, not commutes.
PACE = 80.0

# Driving, for the handful of "15-minute drive" claims. Urban average, and only
# ever used to sanity-check, never to fill a walk.
DRIVE_PACE = 500.0

FOOT = 0.3048
MILE = 1609.344

# How much longer the pavement is than the straight line. Only a starting guess
# — `locate` fits the real one from whatever corpus it is given, and Edinburgh's
# comes out at 1.2. It matters because every quoted distance is a route and
# every coordinate check is a chord, and comparing them without it makes honest
# listings look like liars.
DETOUR = 1.25

# Past this many minutes a quoted distance stops being a walk. Set where it is
# because an hour is roughly where "we could walk that" stops being said about a
# city — and because the things quoted beyond it are airports and stadiums,
# which are quoted to tell you where you are, not how you'd get there.
FURTHEST = 60


# --------------------------------------------------------------------------
# Landmarks
#
# Which places a write-up names, and how it spells them, is a fact about a city
# and not about this tool — so the table is empty here and filled from the
# city's config, `[[landmark]]` in cities/edinburgh.toml. `use_landmarks`
# below is what config.apply calls to install one.
#
# The name is what gets stored; `match` is every way the corpus spells it, and
# is worth writing out. Booking is inconsistent about "Edinburgh Waverley",
# "Waverley Station" and "Edinburgh Railway Station" within a single capture,
# and about the American spellings it applies to Centre and Theatre. A landmark
# with no `match` gets a pattern built from its name, which handles the ordinary
# case and nothing else.
#
# Order is load-bearing: `_canonical` takes the first entry that matches, so a
# name that is a prefix of another goes second.
# --------------------------------------------------------------------------

LANDMARKS: list[tuple[str, str]] = []

# Where the config said a landmark actually is. Optional, and worth giving for
# anywhere you know: `locate` can fit a position out of enough write-ups, but a
# coordinate you supplied works on the first stay rather than the fourth, and is
# right rather than approximately right.
PLACES: dict[str, tuple[float, float]] = {}

_LM_ANY = ""
_LM_ONE: list[tuple[str, re.Pattern]] = []

# A distance, in any of the forms the corpus uses. One named group so the
# templates below can each carry exactly one.
_DIST = (
    r"(?P<d>less\s+than\s+(?:a\s+)?0\.6\s*mi"
    r"|half\s+a\s+mile"
    r"|\d+(?:\.\d+)?\s*mi(?:les)?\b"
    r"|\d{3,5}\s*feet"
    r"|(?:a|an|\d+)[\s-]?(?:\d+[\s-])?minutes?[’']?\s*"
    r"(?:walk|stroll|drive|taxi\s+journey|on\s+foot)"
    r"|\d+\s*minutes?(?:\s+on\s+foot|\s+away)"
    r"|(?:a|an|\d+)[\s-]?(?:\d+[\s-])?minute\b)"
)

# Bare minutes — "Located 19 minutes from Edinburgh Castle" — with nothing
# saying walking. Only usable where the template itself supplies the context,
# i.e. where "from <landmark>" follows immediately; loose in the open it would
# read check-in times and opening hours as distances.
_DIST_LOOSE = _DIST.replace(
    r"|\d+\s*minutes?(?:\s+on\s+foot|\s+away)",
    r"|\d+\s*minutes?\b")

# The five sentence shapes that bind a landmark to a distance. Anything looser
# than these starts pairing a landmark with whatever number shares its sentence,
# and a paragraph listing four attractions with four distances then yields
# sixteen claims, most of them wrong.
#
# The qualifier in front of a distance changes what it is. "less than a
# 15-minute walk" and "a 15-minute walk" are a ceiling and a measurement, and
# Stewart's write-up says the first about a castle 240 m away — true, and
# indistinguishable from a bad claim if the "less than" is swallowed by the
# template before the number is read.
_QUAL = r"(?P<q>just|only|within|less\s+than|under|about|around|roughly)?\s*"
_LIMIT = re.compile(r"within|less\s+than|under", re.I)

def _shapes(lm: str) -> list[tuple[str, str]]:
    return [
        ("landmark is distance",
         rf"(?P<lm>{lm})[^.,;]{{0,30}}?\s+(?:is|lies|are)\s+{_QUAL}(?:a\s+)?{_DIST}"),
        ("landmark (distance)",
         rf"(?P<lm>{lm})\s*\({_QUAL}{_DIST}\)"),
        ("distance from landmark",
         rf"{_QUAL}{_DIST_LOOSE}\s+(?:away\s+)?(?:from|of|to)\s+(?:the\s+)?(?P<lm>{lm})"),
        # "Edinburgh Waverley station 1312 feet away" — no verb at all, which is
        # Booking's house style for a list of two. Anchored on the trailing
        # "away" so it can't swallow the next sentence's number.
        ("landmark distance away",
         rf"(?P<lm>{lm})[^.,;]{{0,25}}?\s+{_QUAL}{_DIST}\s+(?:away|nearby)"),
        ("distance landmark",
         rf"{_QUAL}{_DIST}\s+(?:from|of)?\s*(?P<lm>{lm})"),
        ("landmark within distance",
         rf"(?P<lm>{lm})[^.]{{0,30}}?\s+(?P<q>within|less\s+than)\s+(?:a\s+)?{_DIST}"),
    ]


# Nothing to match against until a city says what its landmarks are, and an
# empty alternation would match the empty string everywhere — every sentence
# with a number in it becoming a claim about a place with no name.
_TEMPLATES: list[tuple[str, re.Pattern]] = []


def _from_name(name: str) -> str:
    """A pattern for a landmark that didn't bother to give one.

    Whitespace-flexible and article-tolerant, which is the whole of the ordinary
    case: "the Grassmarket" and "Grassmarket" are one place, and a write-up that
    breaks the name across a line break is still naming it.
    """
    return r"(?:the\s+)?" + r"\s+".join(re.escape(w) for w in name.split())


def use_landmarks(entries: list[dict]) -> list[str]:
    """Install a city's landmark table, and say what was wrong with it.

    Called by config.apply when the database changes, which is the only time
    this can change. Everything derived from the table is rebuilt here rather
    than per read: these compile into six patterns that run over every write-up,
    and rebuilding them per record would be the slowest thing in the tool.

    Returns complaints rather than raising them. A landmark with a typo in its
    pattern is a settings mistake, and the rest of the table — and the rest of
    the tool — has no reason to stop working over it.
    """
    global LANDMARKS, PLACES, _LM_ANY, _LM_ONE, _TEMPLATES

    pairs: list[tuple[str, str]] = []
    places: dict[str, tuple[float, float]] = {}
    problems: list[str] = []
    for i, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            problems.append(f"[[landmark]] #{i + 1} isn't a table")
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            problems.append(f"[[landmark]] #{i + 1} needs a name")
            continue
        pattern = str(entry.get("match") or "").strip() or _from_name(name)
        try:
            re.compile(pattern, re.I)
        except re.error as exc:
            problems.append(f"[[landmark]] {name}: match = {pattern!r} isn't a "
                            f"pattern ({exc})")
            continue
        pairs.append((name, pattern))
        lat, lon = entry.get("latitude"), entry.get("longitude")
        if lat is not None and lon is not None:
            places[name] = (float(lat), float(lon))
        elif lat is not None or lon is not None:
            problems.append(f"[[landmark]] {name} has half a coordinate — it "
                            f"needs both, or neither and enough write-ups to "
                            f"fit one from")

    LANDMARKS, PLACES = pairs, places
    _LM_ONE = [(n, re.compile(p, re.I)) for n, p in pairs]
    _LM_ANY = "|".join(f"(?:{p})" for _, p in pairs)
    _TEMPLATES = ([(n, re.compile(p, re.I)) for n, p in _shapes(_LM_ANY)]
                  if _LM_ANY else [])
    return problems


# --------------------------------------------------------------------------
# Everything that isn't a distance
# --------------------------------------------------------------------------

_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "three": 3,
          "four": 4, "five": 5, "six": 6}

_BEDROOMS = [
    # "two bedrooms", "a one-bedroom apartment". Plural matters more than it
    # looks: a Sykes write-up says "there are two bedrooms" with no verb this
    # would recognise, and requiring one silently read that as no bedrooms.
    re.compile(r"\b(one|two|three|four|five|six|\d+)[\s-]"
               r"(?:spacious\s+|bright\s+|double\s+|large\s+)?bedrooms?\b", re.I),
    # "a two-bed flat" — UK shorthand, and bedrooms rather than beds. Only with
    # the hyphen: "two beds" unhyphenated is a bed count and a different fact.
    re.compile(r"\b(one|two|three|four|five|six|\d+)-bed\b", re.I),
    re.compile(r"\b(?:features?|includes?|has|have|with|offers?|and)\s+"
               r"(?:a\s+)?(one|two|three|four|five|six|\d+)\s+"
               r"(?:spacious\s+|bright\s+|double\s+)?bedrooms?\b", re.I),
]
_BATHROOMS = [
    re.compile(r"\b(one|two|three|\d+)\s+(?:tiled\s+|modern\s+|private\s+)?"
               r"bathrooms?\b", re.I),
]

# A bathroom mentioned without a count still tells you there is one, which for a
# field that is null 26 times out of 30 is most of the value.
_HAS_BATH = re.compile(r"private bathroom|a bathroom with|bathroom with a", re.I)

# Ordered: the first that matches wins, because a hotel's write-up says
# "apartment" about its rooms and an aparthotel's says both.
_KIND = [
    ("aparthotel", r"\bapart-?hotel\b|\bserviced\s+apartments?\b"),
    ("guesthouse", r"\bguest\s*house\b|\bGeorgian\s+townhouse\b"),
    ("hotel", r"\bthis\s+hotel\b|\bthe\s+hotel\b|hotel[’']s\b"
              r"|\bcountry\s+hotel\b|\bHoliday\s+Inn\b"),
    ("apartment", r"\bapartments?\b|\bflat\b|\bself-catering\b"),
]
_KIND = [(n, re.compile(p, re.I)) for n, p in _KIND]

# Amenities, in the vocabulary the records already speak — every slug here is
# one of sources.AMENITY_ALIASES, so a fact read out of the prose and the same
# fact read off the page land in the same field under the same name. Drift
# between the two lists is worth catching rather than discovering, which is what
# `CORE` and scoring.complaints() are for.
#
# Folded the way sources.normalise_amenities folds: a kitchenette is a kitchen
# and a terrace is a balcony. Not because they're the same thing, but because
# one page mustn't produce two different slugs depending on which reader saw it.
_AMENITIES = {
    "wifi": r"free\s+Wi-?Fi|\bWi-?Fi\b|\binternet\b",
    "parking": r"\bparking\b|\bgarage\b|\bdriveway\b",
    "kitchen": r"\bkitchen(?:ette)?\b|\bstovetop\b|\bhob\b|\boven\b",
    "washing_machine": r"washing\s+machine|\blaundry\b",
    "dishwasher": r"dishwasher",
    "hot_tub": r"hot\s+tub|jacuzzi|whirlpool",
    "fireplace": r"fireplace|wood\s*burner|log\s*burner|open\s+fire",
    "pet_friendly": r"pet[\s-]friendly|pets\s+(?:are\s+)?(?:allowed|welcome)"
                    r"|dog[\s-]friendly|welcome\s+dogs|bring(?:ing)?\s+dogs",
    "air_conditioning": r"air[\s-]conditioning|air\s*con\b",
    "heating": r"\bheating\b|central\s+heating|\bradiators?\b",
    "tv": r"\bTVs?\b|flat[\s-]screen|satellite\s+TV|smart\s+TV|\d+-inch",
    "balcony": r"\bbalcony\b|\bterrace\b|\bpatio\b",
    "garden": r"\bgarden\b",
    "sea_view": r"sea\s+views?|ocean\s+views?|loch\s+views?"
                r"|views?\s+of\s+the\s+(?:Firth|bay)|Firth\s+of\s+Forth",
    "pool": r"swimming\s+pool|\bpool\b",
    "lift": r"\belevator\b|\blift\b",
    "ev_charger": r"EV\s+charging|electric\s+vehicle\s+charg|car\s+charg",
    "breakfast": r"\bbreakfast\b",
}
_AMENITIES = {k: re.compile(v, re.I) for k, v in _AMENITIES.items()}

# Every slug this module will ever put in `amenities`. sources.AMENITY_ALIASES
# has to contain all of them or the two readers have quietly diverged.
CORE = frozenset(_AMENITIES)

# Everything else the prose knows and no field holds. Kept flat and boolean on
# purpose: a scored composite of these would be a second scoring.py, and there
# is already one — config.toml weights them, the same as any amenity.
#
# The line between this and the list above is only which vocabulary the record
# already had. Nothing here is less of a fact than a dishwasher; `soundproofed`
# on a Royal Mile flat is arguably the most useful thing in either list.
_TRAITS = {
    # shape of the place
    "ground_floor": r"ground[\s-]floor",
    "upper_floor": r"(?:second|third|fourth|fifth|top)[\s-]floor",
    "historic_building": r"historic\s+building|historic\s+setting|Georgian"
                         r"|built\s+in\s+\d{4}|cobbled\s+street",
    "renovated": r"recently\s+renovated|newly\s+renovated|new(?:ly)?\s+refurbished",
    "soundproofed": r"soundproof",
    "private_entrance": r"private\s+entrance",
    "sofa_bed": r"sofa\s+bed",
    "bathtub": r"\bbath\s+and\s+(?:a\s+)?(?:power\s+)?shower|\bwith\s+a\s+bath\b",
    "private_bathroom": r"private\s+bathroom",
    # what parking there is, which "parking" alone doesn't say
    "free_parking": r"free\s+(?:on-site\s+)?(?:private\s+)?parking"
                    r"|parking\s+is\s+free|free\s+private\s+parking",
    "paid_parking": r"paid\s+(?:on-site\s+)?(?:private\s+)?parking"
                    r"|on-site\s+paid\s+parking",
    "limited_parking": r"limited\s+(?:underground\s+)?parking",
    "bike_parking": r"bicycle\s+parking",
    # kitchen fittings, which "kitchen" alone doesn't say
    "microwave": r"microwave",
    "coffee_machine": r"coffee\s+(?:machine|maker)|tea\s+and\s+coffee"
                      r"|tea\s*/\s*coffee",
    "kettle": r"\bkettle\b",
    "dining_area": r"dining\s+(?:table|area)",
    # serviced-building things, which separate an aparthotel from a flat
    "reception_24h": r"24-hour\s+(?:reception|front\s+desk)",
    "self_check_in": r"private\s+check-in|express\s+check-in|self\s+check-in",
    "concierge": r"concierge",
    "luggage_storage": r"luggage\s+storage",
    "laundry_service": r"laundry\s+service",
    # Singular is the property's own; plural is the neighbourhood's. That reads
    # like a trick and is the actual rule across the corpus — "features a
    # restaurant" and "the hotel's award-winning restaurant" against "bars,
    # restaurants, shops" and "the surrounding area offers restaurants". The one
    # singular that isn't a restaurant is the concierge making you a booking at
    # somebody else's.
    "restaurant_on_site": r"\brestaurant\b(?!s)(?!\s+reservations)|\bbrasserie\b",
    "gym": r"\bgym\b|fitness\s+(?:club|centre|center|classes)",
    "sauna": r"\bsauna\b",
    # who may stay, and what it will cost on the day
    "adults_only": r"adults[\s-]only",
    "family_rooms": r"family\s+rooms?",
    "licensed": r"licen[cs]e\s+numbers?|operated\s+under\s+licen",
    "visitor_levy": r"[Vv]isitor\s+[Ll]evy",
    # views other than the sea, which already has a field
    "city_view": r"city\s+views?",
    "garden_view": r"garden\s+views?",
    # small comforts
    "streaming": r"streaming\s+services",
    "hairdryer": r"hairdryer",
    "toiletries": r"free\s+toiletries",
    "wardrobe": r"wardrobe",
    "work_desk": r"work\s+desk",
}
_TRAITS = {k: re.compile(v, re.I) for k, v in _TRAITS.items()}

# Every slug this module will ever put in `traits`, for config to validate against.
TRAITS = frozenset(_TRAITS)

# What is around the place, as opposed to in it. A named landmark comes with a
# distance and is handled elsewhere; this is the other half of "what's nearby" —
# the neighbourhood, which no listing gives coordinates for and which is most of
# what makes a street worth staying on.
#
# Plural and generic, both deliberately. The neighbourhood is always plural —
# "bars, restaurants, shops and pubs" — while a singular is either a landmark
# with a name or the property's own: `restaurant` is the hotel's dining room and
# already a trait, `garden` is the patch behind the building and already an
# amenity, and reading either as a neighbourhood would put a night out within
# reach of a country hotel with a bar. Every match this makes across the thirty
# Edinburgh write-ups is a real claim about the surrounding streets.
#
# It under-claims and that is the right way round. `culture` fires once, because
# these summaries name the museum rather than saying there are museums — and a
# named museum arrives with minutes attached, which is better.
_NEARBY = {
    "food_nearby": r"\brestaurants\b|\bcafes\b|\bcafés\b|\beateries\b"
                   r"|places\s+to\s+eat|food\s+and\s+drinks?",
    "nightlife_nearby": r"\bbars\b|\bpubs\b|\bclubs\b|\bnightlife\b",
    "shops_nearby": r"\bshops\b|\bshopping\b|\bboutiques\b",
    "groceries_nearby": r"\bsupermarkets?\b|\bgrocery\b",
    "culture_nearby": r"\bmuseums\b|\bgalleries\b|\btheat(?:re|er)s\b|\bcinemas?\b",
    "green_nearby": r"\bparkland\b|\bpublic\s+park\b|a\s+beautiful\s+park"
                    r"|\bgreen\s+space\b|the\s+Meadows\b|Princes\s+Street\s+Gardens"
                    r"|Botanic\s+Gardens?",
    "transport_nearby": r"public\s+transport|regular\s+buss?es|\bbus\s+stops?\b"
                        r"|\btrams?\b|\bby\s+bus\b",
}
_NEARBY = {k: re.compile(v, re.I) for k, v in _NEARBY.items()}

NEARBY = frozenset(_NEARBY)

# What a property is. Also a trait as far as config.toml is concerned, but it is
# one answer out of four rather than a flag, so it gets its own field.
KINDS = ("apartment", "aparthotel", "hotel", "guesthouse")

# Sentences that describe the region rather than the property. Dropped before
# anything else looks at the text.
_NOISE = [
    re.compile(r"[^.]*\b[Bb]oating,?\s+kayaking,?\s+(?:and\s+|or\s+)?canoeing[^.]*\.", re.I),
    re.compile(r"[^.]*\bkayaking\s+or\s+canoeing[^.]*\.", re.I),
]

_SECTION = re.compile(r"^([A-Z][A-Za-z /&’']{2,40}):\s", re.M)


# --------------------------------------------------------------------------

@dataclass
class Claim:
    """One landmark, one distance, and where it came from."""

    landmark: str
    metres: float
    minutes: float
    unit: str          # mi | ft | walk | drive | under
    text: str          # the phrase as written
    shape: str         # which template matched
    stated: bool       # the text gave minutes, rather than us dividing by PACE
    upper_bound: bool  # "less than 0.6 mi" — a ceiling, not a measurement
    doubted: str = ""  # why geometry disbelieves it, if it does


@dataclass
class Reading:
    """Everything one summary claims, with nothing merged in."""

    kind: str | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    # "a private bathroom" is one bathroom being described, not a count of them.
    # Dragon Suites says exactly that and has two, so a reading taken from the
    # phrasing rather than a number is a floor and has to say so — otherwise it
    # reports a conflict against a record that was right.
    bathrooms_at_least: bool = False
    amenities: set[str] = field(default_factory=set)
    traits: set[str] = field(default_factory=set)
    nearby: set[str] = field(default_factory=set)
    claims: list[Claim] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    machine_written: bool = False
    dropped: int = 0

    def walk_minutes(self) -> dict[str, int]:
        """Minutes on foot per landmark, nearest first.

        Ceilings and drives are left out. "less than 1 km" is a marketing upper
        bound and a drive is not a walk, and neither belongs in a column the
        table sorts by.

        Nor is anything past FURTHEST. "Edinburgh Airport is 6.2 mi away" is a
        true sentence and dividing it by a walking pace produces 125 minutes,
        which is arithmetic rather than a fact about anybody's afternoon. A
        distance quoted that far out is quoted for orientation, and reading it
        as a walk fills the record with journeys nobody is going to make.
        """
        out: dict[str, tuple[float, bool]] = {}
        for c in self.claims:
            if c.doubted or c.upper_bound or c.unit == "drive":
                continue
            if c.minutes > FURTHEST:
                continue
            # A stated walking time beats one we derived from a distance.
            if c.landmark not in out or (c.stated and not out[c.landmark][1]):
                out[c.landmark] = (c.minutes, c.stated)
        return {k: round(v[0]) for k, v in
                sorted(out.items(), key=lambda kv: kv[1][0])}


def _number(word: str) -> int | None:
    word = word.strip().lower()
    return int(word) if word.isdigit() else _WORDS.get(word)


def _parse_distance(text: str) -> tuple[float, str, bool]:
    """Metres, unit, and whether it is an upper bound rather than a figure."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    if "less than" in t and "0.6" in t:
        return 1000.0, "under", True          # i.e. "less than 1 km", converted
    if "half a mile" in t:
        return 0.5 * MILE, "mi", False
    if m := re.match(r"(\d+(?:\.\d+)?)\s*mi(?!n)", t):
        return float(m.group(1)) * MILE, "mi", False
    if m := re.match(r"(\d{3,5})\s*feet", t):
        return float(m.group(1)) * FOOT, "ft", False
    digits = re.findall(r"\d+", t)
    n = float(digits[-1]) if digits else 8.0
    if "drive" in t or "taxi" in t:
        return n * DRIVE_PACE, "drive", False
    return n * PACE, "walk", "less than" in t


def _canonical(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    for name, pattern in _LM_ONE:
        if pattern.fullmatch(text):
            return name
    for name, pattern in _LM_ONE:
        if pattern.match(text):
            return name
    return text


def _mask_name(text: str, name: str | None) -> str:
    """Blank the property's own name out of its write-up.

    "Royal Mile Apartment is 7 minutes' walk from Edinburgh Castle" otherwise
    reads as a claim about the Royal Mile. Replaced with same-length filler so
    every offset the caller might care about still lines up.
    """
    if not name:
        return text
    for pattern in _name_patterns(name):
        text = pattern.sub(lambda m: "·" * len(m.group(0)), text)
    return text


def _name_patterns(name: str) -> list[re.Pattern]:
    out = []
    for lm_name, lm_pattern in LANDMARKS:
        if re.search(lm_pattern, name, re.I):
            # Only where the landmark is doing duty as part of a title — i.e.
            # followed by the words that make it a property, not standing alone.
            out.append(re.compile(
                rf"(?:{lm_pattern})\s+(?:Apartments?|Aparthotel|House|Residence|"
                rf"Suites?|Flat|Studios?|Hotel|Lodge|Retreat|by\s+\w+)",
                re.I))
    return out


def strip_noise(text: str) -> tuple[str, int]:
    """The write-up without the sentences that describe the whole region."""
    dropped = 0
    for pattern in _NOISE:
        text, n = pattern.subn(" ", text)
        dropped += n
    return re.sub(r"[ \t]{2,}", " ", text), dropped


def read(record: dict) -> Reading:
    """Everything the summary claims. Nothing merged, nothing written."""
    raw = record.get("summary") or ""
    if not raw.strip():
        return Reading()

    text, dropped = strip_noise(raw)
    sections = _SECTION.findall(text)
    out = Reading(sections=sections, machine_written=bool(sections), dropped=dropped)

    for name, pattern in _KIND:
        if pattern.search(text):
            out.kind = name
            break

    for pattern in _BEDROOMS:
        if m := pattern.search(text):
            if (n := _number(m.group(1))) and 1 <= n <= 8:
                out.bedrooms = n
                break
    for pattern in _BATHROOMS:
        if m := pattern.search(text):
            if (n := _number(m.group(1))) and 1 <= n <= 8:
                out.bathrooms = n
                break
    if out.bathrooms is None and _HAS_BATH.search(text):
        out.bathrooms, out.bathrooms_at_least = 1, True

    out.amenities = {k for k, p in _AMENITIES.items() if p.search(text)}
    out.traits = {k for k, p in _TRAITS.items() if p.search(text)}
    out.nearby = {k for k, p in _NEARBY.items() if p.search(text)}

    out.claims = _claims(_mask_name(text, record.get("name")))
    _doubt(out.claims, record)
    return out


def _claims(text: str) -> list[Claim]:
    found: list[Claim] = []
    seen: set[tuple[str, int]] = set()
    for shape, pattern in _TEMPLATES:
        for m in pattern.finditer(text):
            landmark = _canonical(m.group("lm"))
            phrase = re.sub(r"\s+", " ", m.group("d").strip())
            key = (landmark, m.start("d"))
            if key in seen:
                continue
            seen.add(key)
            metres, unit, bound = _parse_distance(phrase)
            qualifier = (m.groupdict().get("q") or "").strip()
            bound = bound or bool(qualifier and _LIMIT.fullmatch(qualifier))
            stated = unit in ("walk", "drive")
            found.append(Claim(landmark, metres, metres / PACE, unit,
                               phrase, shape, stated, bound))
    return found


# How far off a claim has to be before it is disbelieved rather than forgiven.
# Wide on purpose. A straight line is always shorter than the pavement, so a
# claim reading long is ordinary; one reading a tenth of the distance is not.
_TOO_SHORT = 0.28
_TOO_LONG = 3.5


def _doubt(claims: list[Claim], record: dict,
           gazetteer: dict | None = None, detour: float = DETOUR) -> None:
    """Mark claims the record's own coordinates say cannot be true."""
    lat, lon = record.get("latitude"), record.get("longitude")
    places = gazetteer or {}
    if lat is None or lon is None or not places:
        return
    for c in claims:
        if c.landmark not in places:
            continue
        crow = haversine((lat, lon), places[c.landmark])
        if crow < 80:                      # too close to test meaningfully
            continue
        ratio = (c.metres / detour) / crow
        if ratio < _TOO_SHORT:
            c.doubted = (f"claims {c.metres:.0f} m but sits {crow:.0f} m away "
                         f"in a straight line")
        elif ratio > _TOO_LONG and not c.upper_bound:
            c.doubted = (f"claims {c.metres:.0f} m to somewhere {crow:.0f} m "
                         f"away in a straight line")


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two lat/lon pairs, as the crow flies."""
    radius = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------
# Locating the landmarks from the corpus, so nothing has to be looked up
# --------------------------------------------------------------------------

def locate(records: list[dict],
           minimum: int = 4) -> tuple[dict[str, tuple[float, float]], float]:
    """Where the landmarks are, worked out from the stays that mention them.

    A stay at a known point claiming 600 m to the castle puts the castle
    somewhere on a 600 m circle. Enough stays and the circles cross. This is
    least-squares over that, which is worth the thirty lines because it means
    the geometry check needs no coordinates that didn't come with the capture —
    point it at a Bordeaux database and it learns Bordeaux.

    Anywhere the city config gave coordinates for is used as given and skipped
    here. Fitting is what to do about the places you haven't looked up, not a
    reason to avoid looking any up.

    The circles are all too big, because a quoted distance is pavement and a
    fitted position is straight-line. So the ratio between them is fitted too,
    by trying a range and keeping whichever leaves the claims most consistent
    with each other. Nothing in that decision knows where anything actually is;
    it is the corpus agreeing with itself.

    Edinburgh picks 1.1 that way and lands its landmarks a mean of 320 m from
    where they really are — the museum 81 m, the Playhouse 109 m, Waverley
    164 m, the castle 631 m. Worth being honest about the ceiling here: the
    residual curve is nearly flat from 1.1 to 1.4, so the corpus does not pin
    the factor down well, and a k chosen with a real map would have done about
    a hundred metres better. It doesn't matter for what this is for. Every
    verdict below turns on a claim being wrong by a factor of three or more,
    and no landmark moves far enough to change one.

    Returns the landmarks and the detour factor, because a caller comparing a
    quoted walk against a straight line needs the same number.

    Walks and short distances only. Airport figures are drives quoted in miles
    and fitting them puts the airport four kilometres into a field.
    """
    seen: dict[str, list[tuple[float, float, float]]] = {}
    for record in records:
        lat, lon = record.get("latitude"), record.get("longitude")
        if lat is None or lon is None:
            continue
        for c in read(record).claims:
            if c.upper_bound or c.unit == "drive" or c.metres > 5000:
                continue
            seen.setdefault(c.landmark, []).append((lat, lon, c.metres))

    # A landmark the config placed needs no fitting, and isn't fitted: a
    # coordinate you gave is better than one worked out from the sentences being
    # judged against it, and it holds on the first stay rather than the fourth.
    usable = {n: p for n, p in seen.items()
              if len(p) >= minimum and n not in PLACES}
    if not usable:
        return dict(PLACES), DETOUR

    detour, _ = min(
        ((k, _spread([_trilaterate(p, k)[1] for p in usable.values()]))
         for k in (1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6)),
        key=lambda kv: kv[1])

    return {name: fitted
            for name, points in usable.items()
            if (fitted := _trilaterate(points, detour)[0]) is not None
            } | dict(PLACES), detour


def _spread(values: list[float]) -> float:
    ordered = sorted(v for v in values if v is not None)
    return ordered[len(ordered) // 2] if ordered else float("inf")


def _trilaterate(points, detour: float = None, rounds: int = 3, steps: int = 600):
    """Least squares on a flat local projection, re-run without its outliers.

    Refitting after discarding the worst residuals is what stops one confidently
    wrong sentence — the 1-minute walk to the castle — from dragging the fitted
    castle a street west and casting suspicion on the claims that were right.

    Returns the position and the median residual, the second so `locate` can
    judge one detour factor against another without needing to know anything
    about the city.
    """
    detour = DETOUR if detour is None else detour
    lat0 = sum(p[0] for p in points) / len(points)
    per_lat = 111320.0
    per_lon = 111320.0 * math.cos(math.radians(lat0))
    residuals: list[float] = []

    for _ in range(rounds):
        if len(points) < 3:
            return None, None
        flat = [(p[1] * per_lon, p[0] * per_lat, p[2] / detour) for p in points]
        x = sum(f[0] for f in flat) / len(flat)
        y = sum(f[1] for f in flat) / len(flat)
        for _ in range(steps):
            gx = gy = 0.0
            for px, py, r in flat:
                dx, dy = x - px, y - py
                d = math.hypot(dx, dy) or 1e-6
                e = d - r
                gx += e * dx / d
                gy += e * dy / d
            x -= 0.35 * gx / len(flat)
            y -= 0.35 * gy / len(flat)

        residuals = [abs(math.hypot(x - f[0], y - f[1]) - f[2]) for f in flat]
        ordered = sorted(residuals)
        median = ordered[len(ordered) // 2]
        keep = [p for p, r in zip(points, residuals) if r <= max(3 * median, 250)]
        if len(keep) == len(points):
            break
        points = keep

    return (y / per_lat, x / per_lon), _spread(residuals)


def check(records: list[dict]) -> dict[str, Reading]:
    """Read every record, with the geometry check switched on.

    Separate from `read` because the check needs the corpus: the landmarks are
    fitted from all the records before any one of them is judged against them.
    """
    places, detour = locate(records)
    out = {}
    for record in records:
        reading = read(record)
        _doubt(reading.claims, record, places, detour)
        out[record.get("code") or record.get("name", "?")] = reading
    return out


# --------------------------------------------------------------------------

@dataclass
class Verdict:
    """How one reading sits against the record it was read from."""

    fills: dict = field(default_factory=dict)      # field was null, text has it
    confirms: dict = field(default_factory=dict)   # both agree
    conflicts: dict = field(default_factory=dict)  # (record value, text value)
    new_amenities: set = field(default_factory=set)
    new_traits: set = field(default_factory=set)
    new_nearby: set = field(default_factory=set)
    claimed_walk: dict = field(default_factory=dict)
    doubted: list = field(default_factory=list)

    def anything(self) -> bool:
        return bool(self.fills or self.new_amenities or self.new_traits
                    or self.new_nearby or self.claimed_walk or self.conflicts)


def against(record: dict, reading: Reading) -> Verdict:
    """What acting on this reading would change, without changing it.

    The split that matters is fills against conflicts. A null filled from the
    text is free; a scraped 2 bedrooms against a sentence saying three is a
    question for a person, and `apply` leaves that one alone for exactly the
    reason you'd expect — picking a winner here means picking it out of sight of
    anyone who could tell.
    """
    v = Verdict()
    for name in ("bedrooms", "bathrooms", "kind"):
        claimed = getattr(reading, name)
        if claimed is None:
            continue
        floor = name == "bathrooms" and reading.bathrooms_at_least
        held = record.get(name)
        if held is None:
            v.fills[name] = claimed
        elif held == claimed or (floor and held > claimed):
            v.confirms[name] = held
        else:
            v.conflicts[name] = (held, claimed)

    v.new_amenities = reading.amenities - set(record.get("amenities") or [])
    v.new_traits = reading.traits - set(record.get("traits") or [])
    v.new_nearby = reading.nearby - set(record.get("nearby") or [])
    v.claimed_walk = reading.walk_minutes()
    v.doubted = [c for c in reading.claims if c.doubted]
    return v


def apply(record: dict, places: dict | None = None,
          detour: float = DETOUR, reading: Reading | None = None) -> Verdict:
    """Write what the prose says into the record, and say what was written.

    `places` is what `locate` fitted from the whole database, and without it
    nothing can be disbelieved — a lone record cannot tell you that its castle
    is in the wrong place. Callers holding the corpus should fit it once and
    pass it to every apply; a caller that doesn't will simply file the claims
    as written, which is the old behaviour and no worse than not reading them.

    Only into holes. A field the page stated outright is the better fact and
    stays — this runs after every capture, and a re-capture that finally scrapes
    a bathroom count must not find a gleaned one squatting in the field. That
    also makes it safe to run repeatedly, which it has to be: the summary
    usually arrives later than the record, by bookmarklet or by `set --summary`,
    so the read has to happen again every time either one changes.

    Amenities are added to, never replaced. `set --amenities` replaces because
    correcting a scrape usually means the list was wrong; this is the opposite
    case — the prose found a dishwasher the amenity block didn't mention, and
    nothing about that says the block's entries were mistaken.

    `gleaned` is rebuilt from scratch each time rather than appended to, so it
    always names what is *currently* in a field because of the prose, and a
    field since filled by a real scrape drops off it.
    """
    reading = read(record) if reading is None else reading
    if places:
        _doubt(reading.claims, record, places, detour)
    v = against(record, reading)

    for name, value in v.fills.items():
        record[name] = value
    if v.new_amenities:
        record["amenities"] = sorted(set(record.get("amenities") or [])
                                     | v.new_amenities)
    if reading.traits:
        record["traits"] = sorted(set(record.get("traits") or []) | reading.traits)
    if reading.nearby:
        record["nearby"] = sorted(set(record.get("nearby") or []) | reading.nearby)
    if v.claimed_walk:
        record["walk_claimed"] = v.claimed_walk

    # Provenance, and the reason `list` can mark a gleaned figure differently
    # from a scraped one. A fact being as good as a scraped fact is not the same
    # as it being indistinguishable from one.
    record["gleaned"] = sorted(v.fills) or None
    return v


def claimed_for(record: dict, labels: list[str]) -> dict[str, int]:
    """The prose's walk times, re-keyed to the destinations you named.

    The write-up says "Waverley" and config.toml says "Edinburgh Waverley", and
    they are the same station. Matched by containment either way round, which is
    loose enough for that and tight enough that no two destinations in a sane
    config both answer to one landmark.
    """
    claimed = record.get("walk_claimed") or {}
    out = {}
    for label in labels:
        want = label.strip().lower()
        for landmark, minutes in claimed.items():
            have = landmark.strip().lower()
            if want == have or want in have or have in want:
                out[label] = minutes
                break
    return out
