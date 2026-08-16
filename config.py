"""
Settings, in two layers: config.toml, then the database's own file over it.

The dict below is the full set of defaults, so the tool runs with no config file
at all and a file that sets three keys overrides exactly three keys. Set
LODGINGBUDDY_CONFIG to read from somewhere other than config.toml next to this
file.

The second layer is a city. Most of what this tool is told is not a preference
but a fact about where you are going: what the write-ups there call the castle,
which places are worth walking to, what tax is charged and in what currency. One
global file can hold one city's worth of that, and a second trip either
overwrites it or is measured against the wrong town. So a database may carry its
own settings — edinbruh.toml beside edinbruh.json — which are merged over the
global ones whenever that database is the one in use.

Which means every name below can change while the process is running, and
nothing may take a copy at import: read `config.X` at the point of use, the way
every module here already does, and `db edinbruh` moves the landmarks, the
destinations and the VAT rate together.

Constants that aren't settings — the record schema, the status values, the URL
patterns each site uses — stay in sources.py, where changing one means changing
the code that reads it.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATH = Path(os.environ.get("LODGINGBUDDY_CONFIG") or HERE / "config.toml")

DEFAULTS = {
    "storage": {"file": "stays.json"},
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


# What every database shares, and the floor a city's own file sits on.
BASE = _merge(DEFAULTS, _read(PATH))

# storage
# Taken from BASE alone and never overlaid. This is the setting that says where
# the databases live, and a city config is one of the files in that folder —
# letting it move the folder it was found in is a question with no answer.
STORE = Path(BASE["storage"]["file"])
if not STORE.is_absolute():
    STORE = HERE / STORE
# Databases are named files in one folder — stays.json is "stays" — and `db`
# switches between them. Derived from `file` rather than set separately so an
# existing store needs no migration and new ones land beside it.
STORE_DIR = STORE.parent
DEFAULT_DB = STORE.stem


def city_path(db: str) -> Path:
    """The settings belonging to one database: edinbruh.toml, beside edinbruh.json.

    Same scheme as the databases themselves, because it is the same idea. What
    the write-ups in a city call the castle, which places are worth walking to,
    what tax is charged there — none of that is a preference about the tool, and
    all of it is wrong the moment you point it at the next town. Keeping it in a
    file named after the database makes a trip two files with one name, and
    makes going back to a city you priced last year a matter of having kept
    them.
    """
    return STORE_DIR / (db + ".toml")


# What's in force right now.
CONFIG = BASE
# Which database these settings are for, so `apply` can tell a switch from a
# repeat — `current()` asks on every read.
DB: str | None = None
# The city file in force, or None where the database hasn't got one.
CITY: Path | None = None
# Settings problems worth saying out loud. Collected rather than printed:
# this module is imported before anything exists to print with, and a bad
# weight shouldn't stand between you and the stays you already have.
# scoring.complaints() is what reports them.
PROBLEMS: list[str] = []


def _bind() -> None:
    """Point every name below at whatever CONFIG now says.

    Names rather than lookups because that is what every caller already reads,
    and rebinding them is what makes `db edinbruh` change the weights, the tax
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
    """Work from `db`'s settings from here on.

    Called by database.current(), so the thing that asks which database it is in
    is also what loads that database's settings — there is no third place to
    forget. Cheap on the repeat, which matters because that question gets asked
    once per record.
    """
    global CONFIG, DB, CITY, PROBLEMS
    if db == DB:
        return

    path = city_path(db)
    # A database called "config" would otherwise find the global file as its own
    # city file and merge it onto itself.
    itself = path.resolve() == PATH.resolve()
    overlay = {} if itself else _read(path)
    problems = []
    if itself and path.exists():
        problems.append(f"a database called {db} would take {PATH.name} as its "
                        f"own settings — ignored, so it holds the global ones")
    if overlay.pop("storage", None):
        problems.append(f"[storage] in {path.name} was ignored — a database's "
                        f"own settings can't move the folder it lives in")

    CONFIG = _merge(BASE, overlay) if overlay else BASE
    DB, CITY = db, (path if overlay else None)
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
    return PATH.name + (f" + {CITY.name}" if CITY else "")


def destinations_for(db: str) -> list[dict]:
    """The places worth walking to, for the database you're working in.

    Normally the whole answer is "whatever this database's own file said", since
    a city config is only ever read while its city is the one in use. The filter
    is for the other arrangement, which still works and needs no migrating: one
    global list where each `[[destination]]` names the database it belongs to.
    A destination that names none belongs to all of them.
    """
    return [d for d in DESTINATIONS if d.get("db") in (None, db)]
