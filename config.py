"""
Settings, in three layers: the tool, the city, then this particular trip.

The dict below is the full set of defaults, so the tool runs with no config file
at all and a file that sets three keys overrides exactly three keys. Set
LODGINGBUDDY_CONFIG to read from somewhere other than config.toml next to this
file.

The middle layer is the one worth having. Most of what this tool gets told is
not a preference but a fact about a place: what the write-ups there call the
castle, which places are worth walking to, what tax is charged and in what
currency. None of that is about the trip — it is as true of the fortnight you
spent pricing Edinburgh last year as of the weekend you price tomorrow — so it
lives in cities/edinburgh.toml, under the city's name, and is worth keeping and
worth sharing. Point a second database at the same city and it arrives knowing
the place.

The trip is the thin layer on top: <db>.toml beside <db>.json, which says which
city this database is in and holds anything true of this trip alone — how many
ways the bill splits, a must-have that only matters this time.

Which means every name below can change while the process is running, and
nothing may take a copy at import: read `config.X` at the point of use, the way
every module here already does, and `db` moves the landmarks, the destinations
and the VAT rate together.

Constants that aren't settings — the record schema, the status values, the URL
patterns each site uses — stay in sources.py, where changing one means changing
the code that reads it.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATH = Path(os.environ.get("LODGINGBUDDY_CONFIG") or HERE / "config.toml")

# A name that has to be able to be a filename, for both databases and cities.
# Path tricks and hidden files are refused rather than sanitised: a file quietly
# renamed under you is worse than one that won't open.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

DEFAULTS = {
    # `cities` sits beside the code rather than beside the stays: a city config
    # is knowledge about a place, shared and committed, where the stays are
    # yours and are not.
    "storage": {"file": "stays.json", "cities": "cities"},
    "http": {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "timeout_seconds": 25,
        "accept_language": "en-GB,en;q=0.9",
    },
    "tax": {"vat_rate": 0.20},
    "split": {"shares": 2, "label": "share"},
    "currency": {
        "base": "GBP",
        "quote": "USD",
        "default_rate": 0,
        "native_default": "GBP",
    },
    "display": {
        "default_sort": "value",
        "column_gap": "  ",
        "rule_char": "─",
        "name_width": 30,
        "source_width": 11,
        "where_width": 20,
        "columns": ["name", "source", "space", "slp", "walk", "all_in",
                    "share_nt", "score", "points", "value"],
        # What the write-up said, under each row. "line" is a second indented
        # line per stay; "off" is columns only. There are `kind` and `traits`
        # columns too, for a table you'd rather keep one row per stay.
        "facts": "lines",
        # An upper bound on the traits that line will name, not a target — the
        # width of the terminal is what actually decides, since the line is
        # fitted to it and never wraps.
        "facts_traits": 8,
        "status_marks": {"ok": " ", "needs_price": "·", "blocked": "!"},
        "tax_marks": {"inclusive": "", "added": "+", "computed": "=",
                      "unknown": "?"},
        "gate_marks": {"fail": "✗", "unknown": "?"},
    },
    "booking": {"min_price": 10, "max_price": 100_000},
    # Desirability, with price deliberately left out — see scoring.py.
    "scoring": {
        "price_unit": 25,
        "tiers": {
            "walk_minutes": {"direction": "lower", "steps": [
                {"max": 10, "points": 25},
                {"max": 20, "points": 15},
                {"max": 35, "points": 6},
            ]},
            "guest_score": {"direction": "higher", "steps": [
                {"min": 90, "points": 20},
                {"min": 80, "points": 12},
                {"min": 70, "points": 5},
            ]},
            "reviews": {"direction": "higher", "steps": [
                {"min": 200, "points": 6},
                {"min": 50, "points": 4},
                {"min": 10, "points": 2},
            ]},
            "cleanliness": {"direction": "higher", "steps": [
                {"min": 90, "points": 12},
                {"min": 80, "points": 7},
                {"min": 70, "points": 2},
            ]},
            "look": {"direction": "higher", "steps": [
                {"min": 100, "points": 15},
                {"min": 80, "points": 10},
                {"min": 60, "points": 4},
            ]},
            "spare_beds": {"direction": "higher", "steps": [
                {"min": 2, "points": 8},
                {"min": 1, "points": 5},
            ]},
            # Shares that don't get a bedroom to themselves. Privacy, which
            # spare_beds — a measure of elbow room — cannot stand in for.
            "shares_without_a_door": {"direction": "lower", "steps": [
                {"max": 0, "points": 25},
                {"max": 1, "points": 6},
            ]},
        },
        "bonuses": {
            "wifi": 4, "parking": 6, "kitchen": 5, "washing_machine": 3,
            "second_bathroom": 5, "hot_tub": 4, "fireplace": 3,
            # Read out of the write-up. Weighted here on the same footing as
            # anything scraped from a feature list, because they are the same
            # kind of claim by the same seller.
            "soundproofed": 4, "free_parking": 3, "adults_only": 2,
            "lift": 2, "self_check_in": 2,
            "visitor_levy": -3, "limited_parking": -2,
            # What's around it. The neighbourhood has no landmark to measure
            # to and decides as much as the walk to one does.
            "food_nearby": 5, "nightlife_nearby": 3, "groceries_nearby": 3,
            "shops_nearby": 2, "green_nearby": 2, "culture_nearby": 2,
            "transport_nearby": 2,
        },
    },
    # Gates, not preferences. A stay that fails one is kept and marked, never
    # dropped — you captured it, so it stays captured.
    "filters": {
        "room_per_share": True,
        "max_walk_minutes": 0,
        "require": [],
    },
    "maps": {
        # `walk` is the one command that sends a stay's location to a third
        # party. It ships on, because with OSRM it needs no key and nothing to
        # sign up for, and a walk time is most of what decides a stay. Set this
        # false and the tool never opens a connection to a routing service.
        "enabled": True,
        # Measure a stay as it's captured, so a row arrives finished. Costs
        # about half a second and one routing call per new stay.
        "on_capture": True,
        # Where the router has no answer for a destination, use the walk the
        # listing claims to it. Only ever fills a hole — a measured figure is
        # never replaced — and only claims that survive the geometry check in
        # summary.py. False keeps the walk column strictly measured.
        "trust_claimed_walk": True,
        # "osrm" walks OpenStreetMap data and needs no key. "google" is the
        # Distance Matrix and does.
        "provider": "osrm",
        # OSM's services ask to be told who is calling, and refuse traffic that
        # pretends to be a browser. Deliberately not the scraping user agent.
        "user_agent": "lodgingbuddy (+https://github.com/maravis05/lodgingbuddy)",
        # The demo server at router.project-osrm.org answers /foot/ with car
        # timings — same distance, same duration, 26 km/h. FOSSGIS runs a real
        # pedestrian profile, which is the one worth asking.
        "osrm_host": "routing.openstreetmap.de",
        "osrm_profile": "routed-foot",
        # Addresses become coordinates here, because OSRM only speaks lon/lat.
        "geocoder_host": "nominatim.openstreetmap.org",
        # Both services ask for no more than one call a second.
        "min_interval_seconds": 1.0,
        "api_key_env": "GOOGLE_MAPS_API_KEY",
        "host": "maps.googleapis.com",
        "mode": "walking",
    },
    # Where you actually want to be, per trip. Empty means `walk` has nothing
    # to measure against and says so.
    "destination": [],
    # The places a city's write-ups name, and how each is spelled. Empty,
    # because there is no such thing as a default city — see summary.py, which
    # compiles these into the patterns that read a distance out of a sentence.
    "landmark": [],
    "source": [
        {"name": "booking.com", "domain": "booking.com", "parser": "booking",
         "currency": "GBP", "tax_included": False, "score_scale": 10},
        {"name": "sykes", "domain": "sykescottages.co.uk", "parser": "sykes",
         "currency": "GBP", "tax_included": True, "score_scale": 5},
        {"name": "cottages.com", "domain": "cottages.com", "parser": "awaze",
         "currency": "GBP", "tax_included": True, "score_scale": 5},
        {"name": "hoseasons", "domain": "hoseasons.co.uk", "parser": "awaze",
         "currency": "GBP", "tax_included": True, "score_scale": 5},
    ],
    "bookmarklet": {
        "source": "bookmarklet.js",
        "output": "bookmarklet.txt",
        "bookmark_file": "bookmarklet.html",
        "install_page": "install-bookmarklet.html",
        "title": "Grab this stay",
        "max_url_bytes": 60_000,
    },
}


def _merge(base: dict, over: dict) -> dict:
    """Overlay `over` on `base`, section by section.

    Tables merge key by key so a config file can set one glyph without having to
    restate the other two. A list — the sites — replaces wholesale: half a list
    of sources merged into another list of sources is nobody's intent.
    """
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out




def _read(path: Path) -> dict:
    """One TOML file, or nothing where there isn't one."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        # Bad settings should read as bad settings, not as a stack trace out of
        # whichever command happened to import this first.
        sys.exit(f"Can't read {path}: {exc}")


# What every database and every city shares, and the floor the other two sit on.
BASE = _merge(DEFAULTS, _read(PATH))

# storage
# Taken from BASE alone and never overlaid. This is the setting that says where
# the other settings files are — letting one of them move the folder it was
# found in is a question with no answer.
STORE = Path(BASE["storage"]["file"])
if not STORE.is_absolute():
    STORE = HERE / STORE
# Databases are named files in one folder — stays.json is "stays" — and `db`
# switches between them. Derived from `file` rather than set separately so an
# existing store needs no migration and new ones land beside it.
STORE_DIR = STORE.parent
DEFAULT_DB = STORE.stem

# Cities are named files in a folder of their own, beside the code rather than
# beside the stays. What is in them is knowledge about a place — worth keeping,
# worth committing, worth someone else's having — where the stays are a private
# list of what you might pay for.
CITIES_DIR = Path(BASE["storage"]["cities"])
if not CITIES_DIR.is_absolute():
    CITIES_DIR = HERE / CITIES_DIR


def name_of(text: str, what: str = "name") -> str:
    """A file-safe name, or a complaint about why that isn't one."""
    name = (text or "").strip()
    if name.endswith(".toml"):
        name = name[: -len(".toml")]
    if not NAME.fullmatch(name):
        raise ValueError(f"{text!r} can't be a {what} — letters, digits, dot, "
                         f"dash and underscore, starting with a letter or digit.")
    return name


def city_path(city: str) -> Path:
    """Where one city's settings live: cities/edinburgh.toml."""
    return CITIES_DIR / (name_of(city, "city") + ".toml")


def db_path(db: str) -> Path:
    """One database's own settings: stays.toml, beside stays.json.

    Thin on purpose. Its job is to name the city — that is the association, and
    a file beside the database is the one place that can't be separated from it
    — and to hold whatever is true of this trip and not of the place.
    """
    return STORE_DIR / (name_of(db, "database") + ".toml")


def cities() -> list[str]:
    """Every city there is a config for."""
    try:
        return sorted(p.stem for p in CITIES_DIR.glob("*.toml"))
    except OSError:
        return []


def city_of(db: str) -> str | None:
    """Which city a database says it is in, if it says.

    Read straight off the file rather than out of CONFIG, because `db` asks it
    about every database at once and only one of them is the one in force.
    """
    named = _read(db_path(db)).get("city")
    return str(named) if named else None


def city_counts(city: str) -> tuple[int, int]:
    """What a city config knows: how many destinations, how many landmarks."""
    conf = _read(city_path(city))
    return len(conf.get("destination") or []), len(conf.get("landmark") or [])


# What's in force right now.
CONFIG = BASE
# Which database these settings are for, so `apply` can tell a switch from a
# repeat — `current()` asks on every read.
DB: str | None = None
# The city in force, and the two files it came with. Either may be None: a
# database needn't name a city, and a city needn't have a file yet.
CITY: str | None = None
CITY_FILE: Path | None = None
TRIP_FILE: Path | None = None
# Settings problems worth saying out loud. Collected rather than printed:
# this module is imported before anything exists to print with, and a bad
# weight shouldn't stand between you and the stays you already have.
# scoring.complaints() is what reports them.
PROBLEMS: list[str] = []


def _bind() -> None:
    """Point every name below at whatever CONFIG now says.

    Names rather than lookups because that is what every caller already reads,
    and rebinding them is what makes `db <name>` change the weights, the tax
    rate and the landmarks rather than only the file the stays land in.
    """
    globals().update(
        # http
        USER_AGENT=CONFIG["http"]["user_agent"],
        TIMEOUT=CONFIG["http"]["timeout_seconds"],
        ACCEPT_LANGUAGE=CONFIG["http"]["accept_language"],
        # tax
        VAT_RATE=CONFIG["tax"]["vat_rate"],
        # how the bill splits
        SHARES=CONFIG["split"]["shares"],
        SHARE_LABEL=CONFIG["split"]["label"],
        # currency
        BASE_CURRENCY=CONFIG["currency"]["base"],
        QUOTE_CURRENCY=CONFIG["currency"]["quote"],
        DEFAULT_RATE=CONFIG["currency"]["default_rate"] or None,
        NATIVE_CURRENCY=CONFIG["currency"]["native_default"],
        # display
        DEFAULT_SORT=CONFIG["display"]["default_sort"],
        COLUMN_GAP=CONFIG["display"]["column_gap"],
        RULE_CHAR=CONFIG["display"]["rule_char"],
        NAME_WIDTH=CONFIG["display"]["name_width"],
        SOURCE_WIDTH=CONFIG["display"]["source_width"],
        WHERE_WIDTH=CONFIG["display"]["where_width"],
        COLUMNS=CONFIG["display"]["columns"],
        FACTS=CONFIG["display"]["facts"],
        FACTS_TRAITS=CONFIG["display"]["facts_traits"],
        STATUS_MARKS=CONFIG["display"]["status_marks"],
        TAX_MARKS=CONFIG["display"]["tax_marks"],
        GATE_MARKS=CONFIG["display"]["gate_marks"],
        # booking
        BOOKING_MIN_PRICE=CONFIG["booking"]["min_price"],
        BOOKING_MAX_PRICE=CONFIG["booking"]["max_price"],
        # scoring
        PRICE_UNIT=CONFIG["scoring"]["price_unit"],
        TIERS=CONFIG["scoring"]["tiers"],
        BONUSES=CONFIG["scoring"]["bonuses"],
        # hard gates
        REQUIRE_ROOM_PER_SHARE=CONFIG["filters"]["room_per_share"],
        MAX_WALK_MINUTES=CONFIG["filters"]["max_walk_minutes"] or None,
        REQUIRED_AMENITIES=CONFIG["filters"]["require"],
        # proximity
        MAPS_ENABLED=CONFIG["maps"]["enabled"],
        MAPS_ON_CAPTURE=CONFIG["maps"]["on_capture"],
        TRUST_CLAIMED_WALK=CONFIG["maps"]["trust_claimed_walk"],
        MAPS_PROVIDER=CONFIG["maps"]["provider"],
        MAPS_USER_AGENT=CONFIG["maps"]["user_agent"],
        OSRM_HOST=CONFIG["maps"]["osrm_host"],
        OSRM_PROFILE=CONFIG["maps"]["osrm_profile"],
        GEOCODER_HOST=CONFIG["maps"]["geocoder_host"],
        MAPS_MIN_INTERVAL=CONFIG["maps"]["min_interval_seconds"],
        MAPS_KEY_ENV=CONFIG["maps"]["api_key_env"],
        MAPS_HOST=CONFIG["maps"]["host"],
        MAPS_MODE=CONFIG["maps"]["mode"],
        DESTINATIONS=CONFIG["destination"],
        # the city's own vocabulary
        LANDMARKS=CONFIG["landmark"],
        # sites, in the order they're tried
        SOURCES=CONFIG["source"],
        # bookmarklet build
        BOOKMARKLET_SRC=HERE / CONFIG["bookmarklet"]["source"],
        BOOKMARKLET_OUT=HERE / CONFIG["bookmarklet"]["output"],
        BOOKMARKLET_HTML=HERE / CONFIG["bookmarklet"]["bookmark_file"],
        BOOKMARKLET_INSTALL=HERE / CONFIG["bookmarklet"]["install_page"],
        BOOKMARKLET_TITLE=CONFIG["bookmarklet"]["title"],
        BOOKMARKLET_MAX_BYTES=CONFIG["bookmarklet"]["max_url_bytes"],
    )


def apply(db: str) -> None:
    """Work from `db`'s settings from here on: the tool's, its city's, its own.

    Called by database.current(), so the thing that asks which database it is in
    is also what loads that database's settings — there is no third place to
    forget. Cheap on the repeat, which matters because that question gets asked
    once per record.

    The trip file is read first because it is what names the city, and applied
    last because it is the narrowest thing said: the city knows where the castle
    is, this trip knows there are three of you.
    """
    global CONFIG, DB, CITY, CITY_FILE, TRIP_FILE, PROBLEMS
    if db == DB:
        return

    trip_file = db_path(db)
    # A database called "config" would otherwise take the global file as its own
    # and merge it onto itself.
    itself = trip_file.resolve() == PATH.resolve()
    trip = {} if itself else _read(trip_file)
    problems = []
    if itself and trip_file.exists():
        problems.append(f"a database called {db} would take {PATH.name} as its "
                        f"own settings — ignored, so it holds the global ones")

    city = trip.pop("city", None)
    city_file = None
    city_conf = {}
    if city:
        try:
            city_file = city_path(str(city))
        except ValueError as exc:
            problems.append(f"{trip_file.name}: {exc}")
            city = None
        else:
            city_conf = _read(city_file)
            if not city_file.exists():
                problems.append(f"{trip_file.name} names city {city!r}, and "
                                f"there is no {city_file.name} in {CITIES_DIR} "
                                f"— nothing was loaded for it")

    for path, conf in ((city_file, city_conf), (trip_file, trip)):
        if path is not None and conf.pop("storage", None):
            problems.append(f"[storage] in {path.name} was ignored — only "
                            f"{PATH.name} says where the files live")

    CONFIG = BASE
    for conf in (city_conf, trip):
        if conf:
            CONFIG = _merge(CONFIG, conf)
    DB, CITY = db, (str(city) if city else None)
    CITY_FILE = city_file if city_conf else None
    TRIP_FILE = trip_file if trip_file.exists() and not itself else None
    _bind()

    # The landmarks are regexes over the write-ups, so they are compiled here,
    # once, rather than per record. Imported inside the function because summary
    # is a reader of settings like everything else and importing it at module
    # scope would make this file the bottom of a cycle.
    import summary
    problems += summary.use_landmarks(CONFIG["landmark"])
    PROBLEMS = problems


_bind()


def where() -> str:
    """The settings files in force, for a message that has to name them."""
    parts = [PATH.name]
    if CITY_FILE:
        parts.append(f"{CITIES_DIR.name}/{CITY_FILE.name}")
    if TRIP_FILE:
        parts.append(TRIP_FILE.name)
    return " + ".join(parts)


# What a new city file says before anyone has filled it in. Every line of it is
# commented, so it is a valid config that changes nothing until you mean it to —
# and it is the shape of the thing rather than a blank page, because the useful
# half of a city config is knowing what can go in one.
CITY_TEMPLATE = '''\
# {title} — settings for anywhere you stay in {title}.
#
# Merged over config.toml whenever a database names this city, and under that
# database's own file. Anything config.toml holds can go here; what belongs here
# is what is true of the place rather than of the tool or of one trip.


# ── Where you want to be ──────────────────────────────────────────────────
#
# `walk` measures minutes on foot from each stay to each of these, and the walk
# tier scores the weighted mean. `weight` is that destination's share of it;
# they need not sum to anything. An address is geocoded on first use;
# latitude/longitude skip that, which is worth doing anywhere the geocoder is
# vague about — a terminal, a trailhead, a pedestrianised street it resolves to
# one end of.
#
# [[destination]]
# label = "Old town"
# address = "..."
# weight = 0.6


# ── What the write-ups name ───────────────────────────────────────────────
#
# summary.py reads a distance out of a sentence only for places listed here, so
# this table is the difference between "the station is an 8-minute walk" landing
# in the walk column and being a sentence nobody read. Build it by capturing a
# dozen listings and reading what they keep naming — `show <id>` prints the
# prose in full.
#
# `name` is what gets stored, and what a destination label is matched against.
# `match` is a regex for every way the write-ups spell it; leave it off and the
# name itself is used, article-tolerant and whitespace-flexible.
#
# latitude/longitude are optional and are what a claim gets checked against, so
# that "a 1-minute walk" to somewhere a kilometre away is thrown out. Give them
# only for places that are a point: a long street pinned at its midpoint makes
# an honest claim from one end read as a lie, and summary.locate() does better
# by fitting those from the corpus once four stays have quoted a distance.
#
# [[landmark]]
# name = "Cathedral"
# match = "(?:the\\\\s+)?Cathedral(?:\\\\s+of\\\\s+\\\\w+)?"
# latitude = 0.0
# longitude = 0.0


# ── What it costs to be there ─────────────────────────────────────────────
#
# [currency]
# base = "EUR"
#
# [tax]
# vat_rate = 0.10
'''


def start_city(city: str) -> Path:
    """Begin a city config, so there is something to open and fill in."""
    path = city_path(city)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(CITY_TEMPLATE.format(title=path.stem.replace("-", " ").title()))
    return path


def set_city(db: str, city: str) -> Path:
    """Say which city a database is in, in the database's own file.

    Edited as text rather than rewritten from parsed TOML, because that file is
    yours: it may hold this trip's overrides and the comments explaining them,
    and a tool that reformatted it every time you switched cities would sooner
    or later eat one.
    """
    path = db_path(db)
    city = name_of(city, "city")
    line = f'city = "{city}"'
    if not path.exists():
        path.write_text(
            f"# {db} — this database's own settings, over cities/{city}.toml and\n"
            f"# {PATH.name}. What belongs here is what's true of this trip rather\n"
            f"# than of the city: how many ways the bill splits, a must-have that\n"
            f"# only matters this time.\n\n{line}\n")
        return path

    text = path.read_text()
    # A bare key has to come before the first table header or it belongs to that
    # table, so a file that hasn't got one takes it at the top.
    if re.search(r"^\s*city\s*=.*$", text, re.M):
        text = re.sub(r"^\s*city\s*=.*$", line, text, count=1, flags=re.M)
    else:
        text = line + "\n" + text
    path.write_text(text)
    return path


def destinations_for(db: str) -> list[dict]:
    """The places worth walking to, for the database you're working in.

    Normally the whole answer is "whatever this database's own file said", since
    a city config is only ever read while its city is the one in use. The filter
    is for the other arrangement, which still works and needs no migrating: one
    global list where each `[[destination]]` names the database it belongs to.
    A destination that names none belongs to all of them.
    """
    return [d for d in DESTINATIONS if d.get("db") in (None, db)]
