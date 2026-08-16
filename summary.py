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

The landmarks come from the corpus rather than a gazetteer. Thirty stays with
known coordinates all quoting their distance to Edinburgh Castle is thirty
circles that intersect where the castle is, so `locate` trilaterates it: Waverley
lands 112 m off, the museum 140 m, the castle 508 m, all from prose. It also
explains its own failures usefully — the airport fits 3.7 km out because those
figures are drives, not walks, and a landmark that won't converge is one whose
claims were never comparable in the first place.

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


# --------------------------------------------------------------------------
# Landmarks
#
# The name on the left is what gets stored; the pattern on the right is every
# way the corpus spells it. Booking is inconsistent about "Edinburgh Waverley",
# "Waverley Station" and "Edinburgh Railway Station" within a single capture,
# and about the American spellings it applies to Centre and Theatre.
# --------------------------------------------------------------------------

LANDMARKS: list[tuple[str, str]] = [
    ("Edinburgh Castle", r"Edinburgh\s+Castle"),
    ("Waverley", r"(?:Edinburgh\s+)?Waverley(?:\s+(?:Railway\s+)?[Ss]tation)?"
                 r"|Edinburgh\s+Rail(?:way)?\s+Station"),
    ("Royal Mile", r"(?:the\s+)?Royal\s+Mile"),
    ("National Museum of Scotland", r"(?:the\s+)?National\s+Museum\s+of\s+Scotland"),
    ("Edinburgh Playhouse", r"Edinburgh\s+Playhouse"),
    ("Edinburgh Airport", r"Edinburgh\s+(?:International\s+)?Airport"),
    ("EICC", r"Edinburgh\s+International\s+Conference\s+Cent(?:er|re)|EICC"),
    ("Arthur's Seat", r"Arthur[’']?s?\s+Seat"),
    ("Murrayfield", r"Murrayfield(?:\s+Stadium)?"),
    ("Royal Yacht Britannia", r"(?:the\s+)?Royal\s+Yacht\s+Britannia"),
    ("Camera Obscura", r"Camera\s+Obscura(?:\s+and\s+World\s+of\s+Illusions)?"),
    ("Princes Street", r"Prince?s+[’']?s?\s+Street|Princes\s+St\b"),
    ("Mary King's Close", r"(?:The\s+)?Real\s+Mary\s+King[’']s\s+Close"),
    ("Grassmarket", r"(?:the\s+)?Grassmarket"),
    ("Usher Hall", r"Usher\s+Hall"),
    ("Edinburgh Zoo", r"Edinburgh\s+Zoo"),
    ("Royal Botanic Garden", r"(?:the\s+)?Royal\s+Botanic\s+Gardens?"),
    ("St Giles Cathedral", r"St\s+Giles[’']?\s*Cathedral"),
    ("The Meadows", r"(?:the\s+)?Meadows"),
    ("University of Edinburgh", r"(?:The\s+)?University\s+of\s+Edinburgh"),
    ("George Street", r"George\s+Street"),
    ("Holyrood", r"(?:the\s+)?Palace\s+of\s+Holyrood\s*[Hh]ouse|Holyrood"),
]

_LM_ANY = "|".join(f"(?:{p})" for _, p in LANDMARKS)
_LM_ONE = [(n, re.compile(p, re.I)) for n, p in LANDMARKS]

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

_TEMPLATES = [
    ("landmark is distance",
     rf"(?P<lm>{_LM_ANY})[^.,;]{{0,30}}?\s+(?:is|lies|are)\s+{_QUAL}(?:a\s+)?{_DIST}"),
    ("landmark (distance)",
     rf"(?P<lm>{_LM_ANY})\s*\({_QUAL}{_DIST}\)"),
    ("distance from landmark",
     rf"{_QUAL}{_DIST}\s+(?:away\s+)?(?:from|of|to)\s+(?:the\s+)?(?P<lm>{_LM_ANY})"),
    ("distance landmark",
     rf"{_QUAL}{_DIST}\s+(?:from|of)?\s*(?P<lm>{_LM_ANY})"),
    ("landmark within distance",
     rf"(?P<lm>{_LM_ANY})[^.]{{0,30}}?\s+(?P<q>within|less\s+than)\s+(?:a\s+)?{_DIST}"),
]
_TEMPLATES = [(n, re.compile(p, re.I)) for n, p in _TEMPLATES]


# --------------------------------------------------------------------------
# Everything that isn't a distance
# --------------------------------------------------------------------------

_WORDS = {"a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "three": 3,
          "four": 4, "five": 5, "six": 6}

_BEDROOMS = [
    re.compile(r"\b(one|two|three|four|five|\d+)[\s-]bed(?:room)?\b", re.I),
    re.compile(r"\b(?:features?|includes?|has|have|with|offers?|and)\s+"
               r"(?:a\s+)?(one|two|three|four|five|\d+)\s+"
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

# Facts worth a column or a filter. Kept flat and boolean on purpose: a scored
# composite of these would be a second scoring.py, and there is already one.
_FLAGS = {
    "ground_floor": r"ground[- ]floor",
    "upper_floor": r"(?:second|third|fourth|fifth|top)[- ]floor",
    "lift": r"\belevator\b|\blift\b",
    "free_parking": r"free\s+(?:on-site\s+)?(?:private\s+)?parking"
                    r"|parking\s+is\s+free|free\s+private\s+parking",
    "paid_parking": r"paid\s+(?:on-site\s+)?(?:private\s+)?parking"
                    r"|on-site\s+paid\s+parking|[Cc]harges?\s+apply.*parking",
    "limited_parking": r"limited\s+(?:underground\s+)?parking",
    "historic_building": r"historic\s+building|historic\s+setting|Georgian"
                         r"|built\s+in\s+\d{4}|cobbled\s+street",
    "renovated": r"recently\s+renovated|newly\s+renovated|new(?:ly)?\s+refurbished",
    "soundproofed": r"soundproof",
    "adults_only": r"adults[\s-]only",
    "family_rooms": r"family\s+rooms?",
    "pets_allowed": r"pet[\s-]friendly|\bdogs?\s+(?:are|cannot|as)\b"
                    r"|welcome\s+dogs|bring\s+dogs",
    "breakfast_available": r"\bbreakfast\b",
    "restaurant_on_site": r"\brestaurant\b(?!\s+reservations)|\bbrasserie\b",
    "reception_24h": r"24-hour\s+(?:reception|front\s+desk)",
    "self_check_in": r"private\s+check-in|express\s+check-in|self\s+check-in",
    "sea_view": r"sea\s+views?|views?\s+of\s+the\s+(?:Firth|bay)|Firth\s+of\s+Forth",
    "city_view": r"city\s+views?",
    "garden_view": r"garden\s+views?",
    "licensed": r"licen[cs]e\s+numbers?|operated\s+under\s+licen",
    "visitor_levy": r"[Vv]isitor\s+[Ll]evy",
}
_FLAGS = {k: re.compile(v, re.I) for k, v in _FLAGS.items()}

# Amenities the structured field routinely omits. Names match the vocabulary
# already in the records where one exists, so a merge doesn't create a second
# spelling of the same thing.
_AMENITIES = {
    "wifi": r"free\s+Wi-?Fi",
    "kitchen": r"\bkitchen\b(?!ette)",
    "kitchenette": r"kitchenette",
    "washing_machine": r"washing\s+machine",
    "dishwasher": r"dishwasher",
    "microwave": r"microwave",
    "oven": r"\boven\b|\bstovetop\b|\bhob\b",
    "coffee_machine": r"coffee\s+(?:machine|maker)|tea\s+and\s+coffee|tea\s*/\s*coffee",
    "kettle": r"\bkettle\b",
    "tv": r"\bTV\b|flat-screen|satellite\s+TV|\d+-inch",
    "streaming": r"streaming\s+services",
    "hairdryer": r"hairdryer",
    "toiletries": r"free\s+toiletries",
    "wardrobe": r"wardrobe",
    "work_desk": r"work\s+desk",
    "dining_area": r"dining\s+(?:table|area)",
    "fireplace": r"fireplace",
    "air_conditioning": r"air[\s-]conditioning",
    "bathtub": r"\bbath\s+and\s+(?:a\s+)?(?:power\s+)?shower|\bwith\s+a\s+bath\b",
    "private_entrance": r"private\s+entrance",
    "private_bathroom": r"private\s+bathroom",
    "balcony": r"\bbalcony\b",
    "terrace": r"\bterrace\b",
    "garden": r"\bgarden\b",
    "concierge": r"concierge",
    "luggage_storage": r"luggage\s+storage",
    "laundry_service": r"laundry\s+service",
    "gym": r"\bgym\b|fitness\s+(?:club|centre|center|classes)",
    "sauna": r"\bsauna\b",
    "pool": r"swimming\s+pool|\bpool\b",
    "bike_parking": r"bicycle\s+parking",
    "ev_charging": r"EV\s+charging",
    "sofa_bed": r"sofa\s+bed",
    "parking": r"\bparking\b",
    "pet_friendly": r"pet[\s-]friendly",
    "heating": r"\bheating\b",
}
_AMENITIES = {k: re.compile(v, re.I) for k, v in _AMENITIES.items()}

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
    flags: set[str] = field(default_factory=set)
    claims: list[Claim] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    machine_written: bool = False
    dropped: int = 0

    def walk_minutes(self) -> dict[str, int]:
        """Minutes on foot per landmark, in the shape proximity.py writes.

        Ceilings and drives are left out. "less than 1 km" is a marketing
        upper bound and a drive is not a walk, and neither belongs in a column
        the table sorts by.
        """
        out: dict[str, float] = {}
        for c in self.claims:
            if c.doubted or c.upper_bound or c.unit == "drive":
                continue
            # A stated walking time beats one we derived from a distance.
            if c.landmark not in out or (c.stated and not out[c.landmark][1]):
                out[c.landmark] = (c.minutes, c.stated)
        return {k: round(v[0]) for k, v in out.items()}


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
    # "kitchenette" and "kitchen" are the same field to a filter, and the
    # write-ups use them interchangeably for the same galley.
    if "kitchenette" in out.amenities:
        out.amenities.add("kitchen")
    out.flags = {k for k, p in _FLAGS.items() if p.search(text)}
    if "free_parking" in out.flags or "paid_parking" in out.flags:
        out.amenities.add("parking")

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

    usable = {n: p for n, p in seen.items() if len(p) >= minimum}
    if not usable:
        return {}, DETOUR

    detour, _ = min(
        ((k, _spread([_trilaterate(p, k)[1] for p in usable.values()]))
         for k in (1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6)),
        key=lambda kv: kv[1])

    return {name: fitted
            for name, points in usable.items()
            if (fitted := _trilaterate(points, detour)[0]) is not None}, detour


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
    doubted: list = field(default_factory=list)


def against(record: dict, reading: Reading) -> Verdict:
    """What acting on this reading would change, without changing it.

    The split that matters is fills against conflicts. A null filled from the
    text is free; a scraped 2 bedrooms against a sentence saying three is a
    question for a person, and answering it here would mean picking a winner
    out of sight of anyone who could tell.
    """
    v = Verdict()
    for name in ("bedrooms", "bathrooms"):
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

    held_walk = record.get("walk_minutes") or {}
    for landmark, minutes in reading.walk_minutes().items():
        if landmark not in held_walk:
            v.fills.setdefault("walk_minutes", {})[landmark] = minutes

    v.new_amenities = reading.amenities - set(record.get("amenities") or [])
    v.doubted = [c for c in reading.claims if c.doubted]
    return v
