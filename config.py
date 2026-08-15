"""
Settings, read once from config.toml.

The dict below is the full set of defaults, so the tool runs with no config file
at all and a file that sets three keys overrides exactly three keys. Set
LODGINGBUDDY_CONFIG to read from somewhere other than config.toml next to this
file.

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
        "default_sort": "share",
        "column_gap": "  ",
        "rule_char": "─",
        "name_width": 30,
        "source_width": 11,
        "where_width": 20,
        "status_marks": {"ok": " ", "needs_price": "·", "blocked": "!"},
        "tax_marks": {"inclusive": "", "added": "+", "unknown": "?"},
    },
    "booking": {"min_price": 10, "max_price": 100_000},
    "source": [
        {"name": "booking.com", "domain": "booking.com", "parser": "booking",
         "currency": "GBP", "tax_included": False},
        {"name": "sykes", "domain": "sykescottages.co.uk", "parser": "sykes",
         "currency": "GBP", "tax_included": True},
        {"name": "cottages.com", "domain": "cottages.com", "parser": "awaze",
         "currency": "GBP", "tax_included": True},
        {"name": "hoseasons", "domain": "hoseasons.co.uk", "parser": "awaze",
         "currency": "GBP", "tax_included": True},
    ],
    "bookmarklet": {
        "source": "bookmarklet.js",
        "output": "bookmarklet.txt",
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


def _load() -> dict:
    if not PATH.exists():
        return DEFAULTS
    try:
        with PATH.open("rb") as fh:
            return _merge(DEFAULTS, tomllib.load(fh))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        # Bad settings should read as bad settings, not as a stack trace out of
        # whichever command happened to import this first.
        sys.exit(f"Can't read {PATH}: {exc}")


CONFIG = _load()

# storage
STORE = Path(CONFIG["storage"]["file"])
if not STORE.is_absolute():
    STORE = HERE / STORE

# http
USER_AGENT = CONFIG["http"]["user_agent"]
TIMEOUT = CONFIG["http"]["timeout_seconds"]
ACCEPT_LANGUAGE = CONFIG["http"]["accept_language"]

# tax
VAT_RATE = CONFIG["tax"]["vat_rate"]

# how the bill splits
SHARES = CONFIG["split"]["shares"]
SHARE_LABEL = CONFIG["split"]["label"]

# currency
BASE_CURRENCY = CONFIG["currency"]["base"]
QUOTE_CURRENCY = CONFIG["currency"]["quote"]
DEFAULT_RATE = CONFIG["currency"]["default_rate"] or None
NATIVE_CURRENCY = CONFIG["currency"]["native_default"]

# display
DEFAULT_SORT = CONFIG["display"]["default_sort"]
COLUMN_GAP = CONFIG["display"]["column_gap"]
RULE_CHAR = CONFIG["display"]["rule_char"]
NAME_WIDTH = CONFIG["display"]["name_width"]
SOURCE_WIDTH = CONFIG["display"]["source_width"]
WHERE_WIDTH = CONFIG["display"]["where_width"]
STATUS_MARKS = CONFIG["display"]["status_marks"]
TAX_MARKS = CONFIG["display"]["tax_marks"]

# booking
BOOKING_MIN_PRICE = CONFIG["booking"]["min_price"]
BOOKING_MAX_PRICE = CONFIG["booking"]["max_price"]

# sites, in the order they're tried
SOURCES = CONFIG["source"]

# bookmarklet build
BOOKMARKLET_SRC = HERE / CONFIG["bookmarklet"]["source"]
BOOKMARKLET_OUT = HERE / CONFIG["bookmarklet"]["output"]
BOOKMARKLET_MAX_BYTES = CONFIG["bookmarklet"]["max_url_bytes"]
