"""
Which set of stays we're working in.

One tool, more than one reason to be using it. The places you're actually
choosing between and a pile of examples gathered to sharpen the scoring both
want capturing, and mixing them ruins both — invented candidates in the table
you're deciding from, real ones buried in practice data.

A database is a name and a file. `examples` is examples.db.json sitting beside
stays.db.json, and that's the whole scheme: every .db.json in the store folder
is one, so starting another means naming it and moving between them means
saying the name once.

Dates are the other reason to have more than one, and the one the tool enforces.
Move your stay a week and the site quotes you a different price, so a table
holding both weeks ranks two sets of quotes against each other and hands it to
whichever week was cheaper. That can't be seen further down — a price is a price
by the time it reaches the sort — so it is caught at capture: a database is for
one date range, settled by the first stay in it, and a quote for any other is
refused. lodgingbuddy.admit is where, config.SPAN is what it reads.

Once, rather than every time — the name is remembered in a pointer file next to
the databases. Which is exactly why the prompt is named after the database
you're in, and why `list` says so when it isn't the usual one. A sticky setting
you can't see is one that eventually surprises you, and here being surprised
means a capture landing somewhere you won't think to look for it.

Three answers to "which one", in order:

    LODGINGBUDDY_DB   for one run, and it doesn't move the pointer
    the pointer file  whatever `db <name>` last set
    storage.file      the default, when nothing else has an opinion
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import config

ENV = "LODGINGBUDDY_DB"
# Named in config.py with the other two, so the three kinds of file are decided
# in one place and `bare` knows about all of them.
SUFFIX = config.DB_SUFFIX

# The pointer lives with the databases rather than in config.toml. That file is
# yours and full of comments, and a tool that rewrote it every time you switched
# would sooner or later eat one of them.
POINTER = config.STORE_DIR / ".lodgingbuddy-db"

def name_of(text: str) -> str:
    """A database name from whatever was typed.

    The filename counts, since that's what's in the folder and what you'll have
    just been looking at — `examples.db.json` and `examples` are the same ask,
    and so is the `examples.dbconf.toml` beside it. The rule about what a name
    may contain is config.NAME, shared with the cities so that both halves of a
    trip can always be a file.
    """
    return config.name_of(text, "database name")


def path_of(name: str) -> Path:
    return config.STORE_DIR / (name_of(name) + SUFFIX)


def forced() -> str | None:
    """The name set for this run only, if there is one."""
    return (os.environ.get(ENV) or "").strip() or None


def _pointed() -> str | None:
    try:
        return POINTER.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _resolved() -> str:
    for where, raw in ((ENV, forced()), (POINTER, _pointed())):
        if raw:
            try:
                return name_of(raw)
            except ValueError as exc:
                # Same reasoning as a broken config.toml: a settings problem
                # should read as one, not as a stack trace out of `list`.
                sys.exit(f"{where}: {exc}")
    return config.DEFAULT_DB


def current() -> str:
    """The database everything reads and writes right now.

    Which also settles which settings are in force, so this is where that gets
    said. A database names the city it is in — stays.dbconf.toml beside
    stays.db.json —
    and the city is most of what the settings are; the alternative to loading
    them here is every caller remembering to, which is the same as some of them
    not.
    """
    name = _resolved()
    config.apply(name)
    return name


def path() -> Path:
    return path_of(current())


def names() -> list[str]:
    """Every database there is.

    The default and the current one are always in it. Both are real places to
    capture into before either has a file, and a list that omitted the database
    you're standing in would be a strange thing to read.
    """
    found = {config.bare(p.name) for p in config.STORE_DIR.glob("*" + SUFFIX)}
    return sorted(found | {config.DEFAULT_DB, current()})


def count(name: str) -> int | None:
    """How many stays are in one, or None if it can't be read."""
    try:
        return len(json.loads(path_of(name).read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None


def use(name: str) -> str:
    """Work in `name` from here on."""
    name = name_of(name)
    POINTER.write_text(name + "\n", encoding="utf-8")
    return name


def start(name: str) -> str:
    """Begin an empty database, so it's listable before anything is in it.

    Refusing a name that's taken is the point of asking for `--new` at all: it
    makes "switch to the one I mean" and "start a fresh one" two different
    requests, so a typo can't answer the first with the second.
    """
    name = name_of(name)
    target = path_of(name)
    if target.exists():
        raise ValueError(f"{name} already exists — `db {name}` switches to it.")
    target.write_text("[]\n", encoding="utf-8")
    return name
