#!/usr/bin/env python3
"""
lodgingbuddy — collate lodging you've picked while browsing, so comparing them
doesn't mean copy-pasting into a spreadsheet.

Paste a link from any supported site and it captures what it can: name, place,
dates, sleeps, bedrooms, review score, and price where the site will give one
up. Anything missing you fill in with `set`. Then `list` puts them side by side
and sorts them however you like.

    python3 lodgingbuddy.py add https://www.sykescottages.co.uk/cottage/...
    python3 lodgingbuddy.py add https://www.booking.com/Share-LjP6kp --price 480
    python3 lodgingbuddy.py set the-distillers-den --price 480 --note "free whisky"
    python3 lodgingbuddy.py list --sort share
    python3 lodgingbuddy.py refresh

(`python` rather than `python3` on Windows. Always name the interpreter rather
than running the script by bare path; see RUN below for why.)

Run it with no arguments for a prompt that holds open across a browsing
session. `db` keeps more than one set of stays apart, since a trip you're
booking and a pile of examples shouldn't share a table.

Sites: Booking.com, Sykes Cottages, cottages.com, Hoseasons.

Settings — where the store lives, which also names the default database and
the folder the rest sit in; the VAT rate; the currency pair; the table's
shape; which domains route where — are in config.toml.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sys
import textwrap

import config
import database
import proximity
import scoring
import sources
import summary


# How to tell someone to run this, in the spelling that works where they are.
# Never `./lodgingbuddy.py`: Windows resolves a bare script path through file
# associations, so it opens the file in whatever owns .py — frequently an
# editor — and reports no error while doing nothing. Naming the interpreter is
# the only form that behaves the same everywhere. Which name, though, differs:
# `python3` is absent from most Windows installs, and `python` is not reliably
# Python 3 on older Unixes. One each.
# From __file__ rather than sys.argv[0], which is "-c" under `python -c` and a
# module path when something imports us — neither of which anyone can type.
RUN = ("python " if os.name == "nt" else "python3 ") + os.path.basename(__file__)


# Colour, and only where there's someone to see it. Down a pipe or into a file
# the table is the same characters it always was, so `list > shortlist.txt` and
# `list | grep Leith` still read — what the colour carries is emphasis, and
# nothing is only said by it. NO_COLOR is honoured because it's the convention,
# and `[display] colour` overrides both ways for the terminal that lies.
def _painted() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if config.COLOUR == "always":
        return True
    return config.COLOUR != "never" and sys.stdout.isatty()


def paint(code: str):
    """A style, applied only when styling is on at the moment of printing."""
    return lambda text: f"\033[{code}m{text}\033[0m" if _painted() else text


# Dim for what's secondary — the write-up, the units, the rules. Bold for the
# header. Lead for the few rows at the top, which is what you came to find.
DIM, BOLD, LEAD = paint("2"), paint("1"), paint("32")


# ──────────────────────────────── storage ──────────────────────────────────

def load() -> list[dict]:
    # Asked for on every read rather than resolved once at import, so that
    # switching databases at the prompt takes effect on the next line rather
    # than the next process.
    path = database.path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save(stays: list[dict]) -> None:
    database.path().write_text(json.dumps(stays, indent=2) + "\n",
                               encoding="utf-8")


def key_of(rec: dict) -> str:
    """Stable identifier: source + code, or the URL tail as a fallback."""
    if rec.get("code"):
        return f"{rec['source']}:{rec['code']}".lower()
    return (rec.get("url") or "?").rstrip("/").rsplit("/", 1)[-1].lower()


def find_exact(stays: list[dict], key: str) -> dict | None:
    for rec in stays:
        if key_of(rec) == key:
            return rec
    return None


def merge_over(existing: dict | None, fresh: dict) -> dict:
    """Fold a new capture onto what we already hold, keeping confirmed facts.

    Re-capturing a property you're still browsing must not wipe a price you
    read off its booking page. A site's "from" price never overwrites a total
    you confirmed, and hand-typed notes always survive.
    """
    if not existing:
        return fresh

    confirmed = existing.get("price_basis") == "quoted"
    incoming_is_soft = fresh.get("price_basis") != "quoted"
    if confirmed and incoming_is_soft:
        for field in ("price", "currency", "price_basis", "tax_included",
                      "native_price", "native_currency"):
            fresh[field] = existing.get(field)

    # A capture adds knowledge; it never subtracts it. Anything the new one is
    # silent about — dates from an earlier visit, a hand-typed note — is kept.
    for field, value in existing.items():
        if fresh.get(field) is None and value is not None:
            fresh[field] = value
    return fresh


def find(stays: list[dict], needle: str) -> dict | None:
    needle = needle.lower()
    for rec in stays:
        if key_of(rec) == needle or needle in (rec.get("code") or "").lower():
            return rec
    for rec in stays:  # last resort: name substring
        if needle in (rec.get("name") or "").lower():
            return rec
    return None


# ─────────────────────────────── derived math ──────────────────────────────

def with_stated_charges(price: float, cur: str, rec: dict) -> float | None:
    """The checkout total, where the page said what it was leaving out.

    Booking.com prints the rates under each room block and then makes you click
    through to see them applied. Reading them off saves that click:

        Included: £78 Cleaning fee per stay
        Excluded: 20 % VAT, 5 % City tax

    Taxes compound rather than summing, and an included fee is not taxed. Both
    checked against real checkouts — 602.14 with 20% and 5% comes to 758.70,
    and 673.24 with a £78 cleaning fee comes to 828.00, each to the penny, and
    no other arrangement of the same numbers does.

    None when the page didn't say, which leaves the flat-VAT estimate to it.
    A fee in a currency that isn't the price's is not subtracted from it, since
    that would be arithmetic on two different units; it makes the whole sum
    unanswerable and returns None rather than something plausible.
    """
    taxes = rec.get("taxes")
    if not taxes:
        return None

    fees = rec.get("fees_included") or []
    fee_total = 0.0
    for fee in fees:
        amount = fee.get("amount")
        if not amount:
            continue
        if fee.get("currency") and cur and fee["currency"] != cur:
            return None
        fee_total += amount

    # A fee bigger than the price it is supposedly inside means one of the two
    # was misread, and the sum below would go negative and look deliberate.
    if fee_total >= price:
        return None

    total = price - fee_total
    for tax in taxes:
        rate = tax.get("rate")
        if rate is None or not 0 <= rate < 1:
            return None
        total *= 1 + rate
    return round(total + fee_total, 2)


def stated_charges_note(rec: dict) -> str:
    """The sum in words: which rates went on, and what wasn't taxed."""
    parts = [f"{t.get('rate', 0):.0%} {t.get('label') or 'tax'}"
             for t in rec.get("taxes") or []]
    note = "the page's own rates applied: " + " then ".join(parts)
    fees = [f for f in rec.get("fees_included") or [] if f.get("amount")]
    if fees:
        note += ", on top of " + ", ".join(
            f"{f['amount']:g} {f.get('label') or 'fee'}" for f in fees
        ) + " which isn't taxed"
    return note


def all_in(rec: dict) -> tuple[float | None, str, str]:
    """The price with VAT actually in it, as (amount, currency, estimated).

    Sites quote differently and the difference is 20%: UK consumer sites like
    Sykes must show VAT-inclusive totals, while Booking.com shows a US booker
    the ex-VAT figure and adds tax at checkout. Comparing the raw numbers puts
    Booking flatteringly 20% under everything else.

    Where we have the property's own currency price we prefer it, since it
    hasn't been through the OTA's exchange rate.
    """
    price = rec.get("price")
    cur = rec.get("currency") or ""
    if rec.get("native_price") and rec.get("native_currency"):
        price, cur = rec["native_price"], rec["native_currency"]
    if not price:
        return None, cur, "inclusive"

    included = rec.get("tax_included")
    if included is False:
        computed = with_stated_charges(price, cur, rec)
        if computed is not None:
            return computed, cur, "computed"
        return price * (1 + (rec.get("vat_rate") or config.VAT_RATE)), cur, "added"
    if included is None:
        # Nobody has told us. Report the number untouched and say so — that is
        # a different thing from having added tax, and must not look the same.
        return price, cur, "unknown"
    return price, cur, "inclusive"


def from_mark(rec: dict, tax: str) -> str:
    """`~` where the figure is a "from" price rather than a total for the dates.

    The mark makes two claims — that this is a "from" price, and that it isn't
    a quote for your dates — and neither survives the page having stated its
    own rates. Where the sum was finished from them the number is what the
    checkout charges for the dates in the link, to the penny, and hedging it
    would be false. Where it wasn't, the hedge stays: it is the thing that gets
    the real total typed in, which is worth more than a tidier table.

    Takes the tax path rather than reading it back off the record, so it can't
    disagree with the arithmetic that produced the number beside it.
    """
    return "~" if rec.get("price_basis") == "indicative" and tax != "computed" else ""


def converted(amount: float | None, cur: str, rate: float | None) -> tuple[float | None, str]:
    if amount and rate and cur == config.BASE_CURRENCY:
        return amount * rate, config.QUOTE_CURRENCY
    return amount, cur


def per_share_night(rec: dict, rate: float | None = None) -> float | None:
    """All-in cost divided by nights, then by however many ways the bill splits.

    The `÷ nights` is the point: it makes a 2-night stay comparable to a 3-night
    one without doing the arithmetic in your head. The `÷ shares` only picks the
    unit — what one side pays a night, rather than what the whole party does.

    Shares are not heads. Three people who split a bill down the middle, a
    couple against a singleton, are two shares; dividing by three would describe
    a payment nobody makes.
    """
    amount, cur, _ = all_in(rec)
    nights = rec.get("nights")
    shares = shares_of(rec)
    if not (amount and nights and shares):
        return None
    amount, _ = converted(amount, cur, rate)
    return amount / nights / shares


def per_share_total(rec: dict, rate: float | None = None) -> float | None:
    """What one share pays for the whole stay — the figure you actually transfer."""
    amount, cur, _ = all_in(rec)
    shares = shares_of(rec)
    if not (amount and shares):
        return None
    amount, _ = converted(amount, cur, rate)
    return amount / shares


def shares_of(rec: dict) -> int | None:
    """How many ways this stay's bill divides.

    Lives in `scoring` now that the same number decides who gets a bedroom as
    well as who pays for it, and one definition is the point: a party that
    splits three ways for the bill and two ways for the beds is a party this
    tool would describe wrongly twice.
    """
    return scoring.shares_of(rec)


def heads_of(rec: dict) -> int | None:
    """How many people are staying: your party, or capacity if we weren't told.

    Your party, not the property's capacity — a cottage that sleeps six costs
    the same whether or not you fill it.
    """
    return rec.get("adults") or rec.get("sleeps")


def scored(rec: dict) -> scoring.Breakdown:
    """This stay's desirability and its value ratio.

    Recomputed on every read rather than stored, for the same reason `shares_of`
    reads config fresh: editing the weights in config.toml has to re-rank the
    whole list, not just whatever gets captured next.

    Deliberately blind to `--rate`. Value is a property of the stay, and asking
    to see the table in dollars must not change it — converting every stay by
    the same factor leaves the ranking alone but silently moves every number,
    which reads like the stays changed.
    """
    return scoring.evaluate(rec, per_share_night(rec))


def ruled_out(rec: dict) -> list[tuple[str, str]]:
    return scoring.gates(rec)


def glean(rec: dict, stays: list[dict]):
    """Read this stay's write-up, checked against everything else we hold.

    The corpus is passed in because the geometry check is the one part of this
    that a single record can't do alone: knowing that "Edinburgh Castle is a
    1-minute walk" is false means knowing roughly where the castle is, and that
    comes from the other thirty stays that quoted a distance to it. Fitted here
    on every capture rather than cached, because it costs a few milliseconds and
    a cache that goes stale as stays are added would be wrong in the direction
    that matters — trusting a claim it should have caught.
    """
    places, detour = summary.locate(stays)
    return summary.apply(rec, places, detour)


def measure_walk(rec: dict) -> None:
    """Walk a stay to your destinations as it's captured, in place.

    The lookup was always going to happen. Leaving it to `walk` meant a second
    command to remember and a column that stayed empty until you did — and a
    row with a hole in it is a row you can't compare, which is the one thing
    this table is for. Half a second at capture buys a finished row.

    Only ever on a stay that hasn't got one, so re-capturing a price doesn't
    spend somebody else's routing service on an answer we already hold. Moving
    a destination is still `walk --again`.

    Every way this can go wrong is quiet, and deliberately so. A router that's
    down, a stay whose page gave no coordinates, a destination list belonging
    to the other leg — none of them is a reason for the capture not to have
    happened, and every one of them leaves a stay that `walk` can pick up
    later. The one thing it must never do is lose you the record.
    """
    if rec.get("walk_minutes") or not config.MAPS_ON_CAPTURE:
        return
    try:
        proximity.check_enabled()
    except proximity.Disabled:
        return
    # A map pin or an address the page actually stated, and nothing weaker.
    # `origin_of` will fall back to the property's name, which is a fair last
    # resort when you typed `walk` and can read what came back — but Nominatim
    # answers "Harbour View" with *a* Harbour View, and the one it picks may be
    # in Cornwall. That guess is fine to offer and wrong to make silently: it
    # arrives as a plausible number in a column you compare on. Asked for
    # "Nowhere In Particular" it returned a 22,016-minute walk, which is
    # fifteen days and the honest version of the same mistake.
    located = rec.get("address") or (rec.get("latitude") is not None
                                     and rec.get("longitude") is not None)
    wanted = config.destinations_for(database.current())
    if not wanted or not located:
        return
    origin = proximity.origin_of(rec)
    try:
        minutes, _ = proximity.walk_times(origin, wanted)
    except (proximity.MapsError, proximity.NoKey, proximity.Unlocatable, OSError):
        return
    if minutes:
        rec["walk_minutes"] = minutes


def weekday(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return dt.date.fromisoformat(iso).strftime("%a")
    except ValueError:
        return ""


# ──────────────────────────────── commands ─────────────────────────────────

def cmd_add(args) -> int:
    try:
        rec = sources.capture(args.url)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.price is not None:
        rec["price"] = args.price
    if args.currency:
        rec["currency"] = args.currency
    if args.note:
        rec["note"] = args.note
    if args.nights:
        rec["nights"] = args.nights
    if args.adults:
        rec["adults"] = args.adults
    if rec["price"]:
        rec["status"] = sources.OK

    stays = load()
    rec = merge_over(find_exact(stays, key_of(rec)), rec)
    # Status has to be judged after the merge: a price recovered from the
    # previous capture still counts as a price.
    if rec.get("price") or rec.get("native_price"):
        rec["status"] = sources.OK
    glean(rec, stays)
    measure_walk(rec)
    stays = [s for s in stays if key_of(s) != key_of(rec)]
    stays.append(rec)
    save(stays)

    print(f"{rec['name'] or '?'}  [{rec['source']}]")
    where = " / ".join(x for x in (rec["location"], rec["region"]) if x)
    if where:
        print(f"  {where}")
    if rec["checkin"]:
        print(f"  {rec['checkin']} {weekday(rec['checkin'])} → "
              f"{rec['checkout']} {weekday(rec['checkout'])}  ({rec['nights']} nights)")
    shape = [f"sleeps {rec['sleeps']}" if rec["sleeps"] else None,
             f"{rec['bedrooms']} bed" if rec["bedrooms"] else None,
             f"{rec['bathrooms']} bath" if rec["bathrooms"] else None]
    shape = [s for s in shape if s]
    if shape:
        print("  " + ", ".join(shape))
    if walk := proximity.describe(rec):
        print(f"  {walk}")
    if rec["score"]:
        n = f" from {rec['reviews']} reviews" if rec["reviews"] else ""
        print(f"  scored {rec['score']}{n}")
    if rec["price"]:
        basis = ' ("from" price, not a quote for your dates)' \
            if rec.get("price_basis") == "indicative" else ""
        print(f"  {rec['price']:g} {rec['currency'] or ''}".rstrip() + basis)
    elif rec["status"] == sources.BLOCKED:
        print("  site refused the page (bot wall) — add a price with `set`")
    else:
        print("  no price found — add one with `set`")
    return 0


def cmd_set(args) -> int:
    stays = load()
    rec = find(stays, args.id)
    if not rec:
        print(f"No stay matching {args.id!r}. Try `list`.", file=sys.stderr)
        return 1
    for field in ("price", "nights", "adults", "rooms", "note", "currency",
                  "score", "native_price", "native_currency", "offer",
                  "shares", "bedrooms", "bathrooms", "sleeps",
                  "score_scale", "look", "clean", "address", "summary"):
        val = getattr(args, field, None)
        if val is not None:
            rec[field] = val
    if args.amenities is not None:
        # Replaces rather than adds: correcting a scrape usually means the list
        # was wrong, not short, and "how do I remove one" should not need a flag.
        rec["amenities"] = sources.normalise_amenities(
            [a for a in args.amenities.split(",") if a.strip()]) or None
    if args.price is not None or args.native_price is not None:
        rec["price_basis"] = "quoted"
    if args.incl_tax:
        rec["tax_included"] = True
    if args.excl_tax:
        rec["tax_included"] = False
    if rec.get("native_price") and not rec.get("native_currency"):
        rec["native_currency"] = config.NATIVE_CURRENCY
    if rec.get("price") or rec.get("native_price"):
        rec["status"] = sources.OK
    # The write-up usually turns up after the record does — pasted from the
    # bookmarklet, or typed here. Reading it on the way past is what stops
    # `set --summary` being a thing you then have to remember to follow with
    # something else. Anything typed on this same line was set above and so is
    # no longer a hole for it to fill.
    gleaned = glean(rec, stays)
    save(stays)

    amount, cur, estimated = all_in(rec)
    print(f"Updated {rec['name']}")
    if args.summary is not None:
        for line in (facts_rows(rec, verdict=gleaned)
                     or ["  nothing new in the write-up"]):
            print(line)
    if amount:
        vat = rec.get("vat_rate") or config.VAT_RATE
        tail = f"  (VAT added at {vat:.0%})" if estimated == "added" else ""
        print(f"  all-in {amount:,.2f} {cur}{tail}")
    mark = scored(rec)
    if mark.points:
        value = f", value {mark.value:g}" if mark.value is not None else ""
        print(f"  {mark.points:g} points{value}")
    return 0


SORTS = {
    "share": lambda r, rate: (per_share_night(r, rate) is None, per_share_night(r, rate) or 0),
    # The old name for the same idea, kept so muscle memory and an unedited
    # config.toml both still work.
    "pppn": lambda r, rate: (per_share_night(r, rate) is None, per_share_night(r, rate) or 0),
    "price": lambda r, rate: (r.get("price") is None, r.get("price") or 0),
    # On the normalised percentage, never the raw number. Sorting those put a
    # 4.8-out-of-5 below a 9.0-out-of-10.
    "score": lambda r, rate: (scoring.guest_score(r) is None, -(scoring.guest_score(r) or 0)),
    "sleeps": lambda r, rate: (r.get("sleeps") is None, -(r.get("sleeps") or 0)),
    "walk": lambda r, rate: (scoring.walk_minutes(r) is None, scoring.walk_minutes(r) or 0),
    "points": lambda r, rate: (-scored(r).points,),
    # What you're really asking when you open this table: which of these gives
    # me the most of what I want per pound.
    "value": lambda r, rate: (scored(r).value is None, -(scored(r).value or 0)),
    "checkin": lambda r, rate: (r.get("checkin") or "9999",),
    "name": lambda r, rate: ((r.get("name") or "").lower(),),
}

# Which column holds the figure a sort ordered the table by, where one does.
# The top rows get it picked out, and picking out the wrong column would be
# worse than picking out none — `--sort price` ranks on the all-in total, and
# `--sort name` ranks on the thing already heading its own pane.
SORT_COLUMN = {
    "share": "share_nt", "pppn": "share_nt", "price": "all_in",
    "score": "score", "sleeps": "slp", "walk": "walk", "points": "points",
    "value": "value", "checkin": "checkin", "name": None,
}


def _pct(value: float | None) -> str:
    # Bare, because the "%" is in the unit row under the header. Thirty of them
    # down a column is thirty characters saying the same thing.
    return f"{value:.0f}" if value is not None else "—"


# Traits read better as English than as slugs, and a few of them read badly
# either way. Only the awkward ones are named here; the rest just lose their
# underscores.
TRAIT_WORDS = {
    "reception_24h": "24h reception",
    "self_check_in": "self check-in",
    "restaurant_on_site": "restaurant",
    "visitor_levy": "visitor levy!",
    "bathtub": "bath",
    "adults_only": "adults only",
    "upper_floor": "upstairs",
    "ground_floor": "ground floor",
}

# The tie-break when two traits are equally rare, and the whole order when
# there is no table to be rare within. Roughly: things that would change your
# mind, then things that describe the building, then fittings.
TRAIT_ORDER = [
    "adults_only", "visitor_levy", "soundproofed", "ground_floor",
    "upper_floor", "historic_building", "renovated", "free_parking",
    "paid_parking", "limited_parking", "private_entrance", "private_bathroom",
    "bathtub", "sofa_bed", "family_rooms", "reception_24h", "self_check_in",
    "concierge", "restaurant_on_site", "gym", "sauna", "city_view",
    "garden_view", "licensed",
]

# Property kinds, shortened. Only one of them is long enough to be worth it and
# it's the one two thirds of the table says.
KIND_WORDS = {"apartment": "apt"}


def how_rare(stays: list[dict]) -> dict[str, int]:
    """How many of these stays carry each trait.

    The line under a row has space for about four of them, and which four is
    worth arguing about: `microwave` is true of eleven of the thirty Edinburgh
    flats and tells you nothing, while `adults_only` is true of one and is the
    most useful thing on that row. Frequency across the table you're actually
    looking at answers that better than any fixed order can, and it re-answers
    it as the table changes — a trait every stay in a shortlist shares stops
    being worth the space the moment they all share it.
    """
    counts: dict[str, int] = {}
    for rec in stays:
        for trait in rec.get("traits") or []:
            counts[trait] = counts.get(trait, 0) + 1
    return counts


def notable(traits: list[str], rarity: dict[str, int] | None = None) -> list[str]:
    """A stay's traits, the ones worth the space first.

    Rare and important are not the same thing and the order needs both. On the
    Edinburgh set `kettle` is rarer than `free parking` — three stays mention
    one, seven the other — and rarity alone put the kettle first, which is
    nobody's idea of the useful fact. So TRAIT_ORDER decides the tier and
    rarity decides the order within it: the traits that could change your mind,
    least common first, and then the fittings on the same terms.
    """
    rank = {name: i for i, name in enumerate(TRAIT_ORDER)}
    return sorted(traits, key=lambda t: (t not in rank,
                                         (rarity or {}).get(t, 0),
                                         rank.get(t, len(rank)), t))


def trait_words(traits: list[str], limit: int | None = None,
                rarity: dict | None = None) -> str:
    ordered = notable(traits, rarity)
    shown = ordered if limit is None else ordered[:limit]
    words = [TRAIT_WORDS.get(t, t.replace("_", " ")) for t in shown]
    if limit is not None and len(ordered) > limit:
        words.append(f"+{len(ordered) - limit}")
    return ", ".join(words)


# Booking writes American and the trip is Scottish. "1 full" is a double and
# "2 twin" is two singles, and the row that answers "how are we sleeping" should
# say it the way you'd say it out loud.
BED_WORDS = {"full": "double", "twin": "single", "sofa": "sofa bed",
             "double": "double", "single": "single", "queen": "queen",
             "king": "king", "bunk": "bunk"}


def _beds(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def sleeping_words(rec: dict) -> str:
    """How the party actually divides up, in words rather than counts.

    The one question the Space column cannot answer. "1br" is the same two
    characters whether the third person gets a room or the sofa, and which of
    those it is decides the stay — so what survives every squeeze below is the
    sofa and the room it's in.

    Beds behind a door first, then the ones that aren't, because that is the
    order they get taken in. Falls back to a bedroom count where the site never
    published a layout, which is 7 of the 30: "2 bedrooms" is less than the
    truth but it isn't a guess at it.
    """
    beds = rec.get("beds")
    if not beds:
        rooms = rec.get("bedrooms")
        return _beds(rooms, "bedroom") if rooms else ""

    private: dict[str, int] = {}
    shared: list[str] = []
    for bed in beds:
        word = BED_WORDS.get((bed.get("type") or "").lower(),
                             bed.get("type") or "bed")
        count = bed.get("count") or 1
        # Absent means private: a flat bed list with no rooms in it is a list of
        # bedrooms, and only a site that names rooms can tell us otherwise.
        if bed.get("private", True):
            private[word] = private.get(word, 0) + count
        else:
            where = (bed.get("room") or "").lower()
            # "1 sofa bed in the living room" — the 1 is noise, there is one.
            phrase = word if count == 1 else _beds(count, word)
            shared.append(f"{phrase} in the {where}" if where else phrase)

    parts = [", ".join(_beds(n, w) for w, n in private.items())] if private else []
    return " + ".join(parts + shared)


# What's around the place, said the way you'd say it. The slug carries "_nearby"
# so config.toml reads unambiguously; the row it prints on is already headed
# "nearby" and doesn't need it twice.
NEARBY_WORDS = {
    "food_nearby": "restaurants", "nightlife_nearby": "bars & pubs",
    "shops_nearby": "shops", "groceries_nearby": "a supermarket",
    "culture_nearby": "museums & cinemas", "green_nearby": "a park",
    "transport_nearby": "public transport",
}
# Most worth knowing first, for when the row runs out of room.
NEARBY_ORDER = ["food_nearby", "nightlife_nearby", "groceries_nearby",
                "shops_nearby", "green_nearby", "culture_nearby",
                "transport_nearby"]


def nearby_words(rec: dict) -> str:
    rank = {name: i for i, name in enumerate(NEARBY_ORDER)}
    found = sorted(rec.get("nearby") or [], key=lambda n: rank.get(n, len(rank)))
    return ", ".join(NEARBY_WORDS.get(n, n.replace("_nearby", "")) for n in found)


def place_brief(name: str) -> str:
    """A landmark's name with the half you already know taken off."""
    return {"National Museum of Scotland": "the Museum"}.get(
        name, name.removeprefix("Edinburgh "))


# The words a listing title is made of besides the name of the place. Sellers
# write to a formula — an adjective, the size, the kind of building, the town —
# and the table has a column for every one of those. What's left is the half
# you can't read anywhere else, which is the half worth the width.
#
# Named here for the same reason TRAIT_WORDS is: the general rule below finds
# most of them by counting, but counting alone puts "Fabulous" above "Roseburn"
# in a table where only one stay is fabulous.
TITLE_FILLER = set(
    "a an and the of in on at to by with for from near close nearby amp".split())
TITLE_GENERIC = set("""
beautiful lovely stunning gorgeous charming cosy cozy comfy comfortable comfort
spacious bright airy sunny peaceful quiet stylish chic elegant modern contemporary
new newly built renovated luxury luxurious deluxe premium executive perfect ideal
fabulous fantastic amazing wonderful great superb excellent pleasant delightful
apartment apartments apt flat flats studio home house residence property place
accommodation stay retreat getaway escape rooms room suite lodge
bed beds bedroom bedrooms br one two three four five 1 2 3 4 5 1br 2br 3br 4br
min mins minute minutes walk
""".split())
# How many titles have to share a word before it stops telling any of them
# apart. Same argument as how_rare, counted over the same table.
TITLE_COMMON = 3


def _bare(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def title_rarity(stays: list[dict]) -> dict[str, int]:
    """How many of these stays use each word in their name."""
    counts: dict[str, int] = {}
    for rec in stays:
        for word in {_bare(w) for w in (rec.get("name") or "").split()}:
            if word:
                counts[word] = counts.get(word, 0) + 1
    return counts


def squeeze(name: str, rarity: dict[str, int], width: int, city: str = "") -> str:
    """A title with the words the rest of the table already said taken off.

    Booking.com titles are sixty characters of "Bright And Stylish Two Bedroom
    Apartment Near Granton Waterfront", and a column that truncates one lands
    on "Bright And Stylish Two Bedroo" — which names nothing, in a column whose
    only job is naming. Three things say a word isn't working: it's grammar,
    another column already says it, or every other title uses it too.

    Two of those are worth nothing at any width, so they go before the width is
    even consulted: a word the title has already used ("Peaceful 3 Bedroom
    Townhouse Edinburgh Edinburgh") and the name of the city every row is in.
    The rest go only when the column actually runs out, commonest first — the
    title is squeezed as far as it must be and no further.
    """
    town = {_bare(w) for w in city.split()}
    seen: set[str] = set()
    kept: list[list] = []
    for word in name.split():
        low = _bare(word)
        if not low or low in seen:
            continue
        seen.add(low)
        # A rank, not a verdict: filler first, then what a column already says,
        # then whatever the other titles are also using, commonest first.
        tier = (0 if low in TITLE_FILLER else
                1 if low in TITLE_GENERIC else 2)
        kept.append([word, tier, rarity.get(low, 0), low in town])
    # Never down to nothing: a stay actually called "Edinburgh" keeps the word.
    if any(not is_town for *_, is_town in kept):
        kept = [k for k in kept if not k[-1]]

    def out() -> str:
        return " ".join(word for word, *_ in kept).strip()

    while len(out()) > width and len(kept) > 1:
        worst = min(range(len(kept)),
                    key=lambda i: (kept[i][1], -kept[i][2], -i))
        if kept[worst][1] == 2 and kept[worst][2] < TITLE_COMMON:
            break          # nothing left but the words actually naming it
        kept.pop(worst)
    # What's left can still open with an orphaned "In" or "By", the noun it
    # was pointing at having gone.
    while len(kept) > 1 and kept[0][1] == 0:
        kept.pop(0)
    text = out() or (name.strip() or "?")
    return text if len(text) <= width else text[:width - 1] + "…"


# How far a claimed walk can be and still be worth naming on one line. The
# record keeps everything up to an hour, because a 50-minute walk is a fact
# about a stay two miles out; a line with room for two is a line that should
# spend them on somewhere you'd actually go before dinner.
WORTH_NAMING = 30


def claimed_brief(rec: dict, limit: int = 4) -> str:
    """The nearest places the listing bothered to time, in its own words."""
    claimed = rec.get("walk_claimed") or {}   # already nearest-first
    near = [(place, mins) for place, mins in claimed.items()
            if mins <= WORTH_NAMING]
    return ", ".join(f"{mins}m {place_brief(place)}"
                     for place, mins in near[:limit])


FACTS_SEP = " · "
# Wide enough for both labels and a space, so the two rows line up under each
# other and the eye can run down either one without reading the other.
FACTS_LABEL = 6


def _row(label: str, body: str) -> str:
    """One row, at whatever width it comes out. Fitting is the caller's job —
    trimming here would make every candidate measure as fitting, and the
    shrink-until-it-fits loop below would always accept its first, longest try.
    """
    return f"    ↳ {label:<{FACTS_LABEL}}  {body}"


def facts_rows(rec: dict, width: int = 100, rarity: dict | None = None,
               verdict=None) -> list[str]:
    """What the write-up adds to the row above, in the order you'd ask it.

    Two rows, because there are two questions and they don't compete for the
    same space: how are we sleeping, and what's around it. Cramming both onto
    one line meant the sofa bed and the walk to Waverley bidding against each
    other, and losing either to a microwave.

      inside  the layout first, then what kind of place it is and the traits
              that set this one apart from the others in the table
      nearby  the landmarks the listing timed, nearest first, then the
              neighbourhood — which has no landmark and no coordinates and is
              still most of what makes a street worth staying on

    Each row is one line, fitted to the terminal and never wrapped. What gets
    cut is always the least distinguishing thing on it, and the tail says how
    many went. A row with nothing to say isn't printed.

    Nothing here says where a fact came from. It all came off the same page,
    and flagging half of it would rank facts by which paragraph printed them.
    """
    rows = []

    kind = rec.get("kind") or ""
    inside = [x for x in (sleeping_words(rec), KIND_WORDS.get(kind, kind)) if x]
    if verdict is not None and verdict.conflicts:
        # Loud, and never cut: two sources disagreeing about the same property
        # outranks anything either of them agrees on.
        inside.append("disagrees with the page on "
                      + ", ".join(f"{k} ({held} vs {said})"
                                  for k, (held, said) in verdict.conflicts.items()))
    said = rec.get("traits") or []
    if "sofa" in " ".join(inside):
        # The layout already put somebody on it, and in a named room. Saying
        # "sofa bed" again a few words later is the same fact twice.
        said = [t for t in said if t != "sofa_bed"]
    traits = [TRAIT_WORDS.get(t, t.replace("_", " "))
              for t in notable(said, rarity)]
    if line := _fit("inside", inside, traits, width):
        rows.append(line)

    # Places first: a walk of six minutes to Waverley outranks the fact that
    # there are shops, and the categories are what gets cut when both won't fit.
    if line := _fit("nearby", [claimed_brief(rec)],
                    [w for w in nearby_words(rec).split(", ") if w], width):
        rows.append(line)
    return rows


def _fit(label: str, fixed: list[str], elastic: list[str], width: int) -> str:
    """One row, with as much of `elastic` on it as the terminal will take."""
    fixed = [x for x in fixed if x]

    def build(count: int) -> str:
        parts = list(fixed)
        if elastic:
            words = elastic[:count]
            if len(elastic) > count:
                words.append(f"+{len(elastic) - count}")
            parts.append(", ".join(words))
        return _row(label, FACTS_SEP.join(parts)) if parts else ""

    for count in range(min(config.FACTS_TRAITS, len(elastic)), -1, -1):
        line = build(count)
        if len(line) <= width:
            return line
    # Nothing fits, which takes a name longer than the terminal is wide. Cut it
    # rather than wrap it — this row's whole job is being one line.
    return build(0)[:width]


# The separator, glued to the word in front of it so a wrap can never open a
# line with one. "· free parking, work desk" at a left margin reads as a bullet
# in a list that isn't there. textwrap won't break on a non-breaking space, so
# the dot can only ever end a line; it goes back to an ordinary space after.
GLUE = "\u00a0" + FACTS_SEP.strip() + " "
# How far a continuation is indented under the line it continues. What tells
# one block from the next now that neither is labelled: flush with the margin
# starts something new, indented is the last one carrying on.
HANG = "  "


def prose(rec: dict, width: int, rarity: dict | None = None,
          cap: int = 4, halves: int = 2) -> list[str]:
    """What the write-up adds, wrapped into the title's column and no wider.

    This used to be one line fitted to the whole terminal, which put prose
    underneath the numbers on the rows that had any and not on the rows that
    didn't — so the numeric block had a left edge that moved as you read down
    it, and the eye had nothing straight to follow. Kept inside the column it
    belongs to, the numbers get a clean pane, and the prose gets as many lines
    as it has something to say for. Which is the better half of the trade: this
    is the only place a trait can be read in full rather than cut to "+6".

    Unlabelled. "inside" and "nearby" cost eight characters off the front of
    every line — a fifth of the column — to say what the order already says and
    what the words give away anyway: a list of beds is not a list of landmarks.
    A hanging indent marks where one block ends, which is what prose has always
    used for this, and it costs two characters instead of eight.

    What gets cut is still the least distinguishing thing on the row, and the
    tail still says how many went — but it counts facts now rather than lines,
    so "+2" is two traits and never two-thirds of a landmark's name.
    """
    blocks: list[tuple[str, list[str]]] = []

    kind = rec.get("kind") or ""
    head = [x for x in (sleeping_words(rec), KIND_WORDS.get(kind, kind)) if x]
    said = rec.get("traits") or []
    if "sofa" in " ".join(head):
        # The layout already put somebody on it, and in a named room.
        said = [t for t in said if t != "sofa_bed"]
    traits = [TRAIT_WORDS.get(t, t.replace("_", " "))
              for t in notable(said, rarity)]
    if head or traits:
        blocks.append((GLUE.join(head), traits))

    # Places first: a walk of six minutes to Waverley outranks the fact that
    # there are shops, and the categories are what gets cut when both won't fit.
    where = claimed_brief(rec)
    around = [w for w in nearby_words(rec).split(", ") if w]
    if where or around:
        blocks.append((where, around))
    # `facts = "line"` wants the first half, not the first line of it — cutting
    # by line ends the row on a dangling comma partway through a trait list.
    blocks = blocks[:max(halves, 0)]
    if not blocks:
        return []

    def lay(fixed: str, elastic: list[str], count: int) -> list[str]:
        shown = elastic[:count]
        if count < len(elastic):
            shown = shown + [f"+{len(elastic) - count}"]
        parts = [p for p in (fixed, ", ".join(shown)) if p]
        lines = textwrap.wrap(GLUE.join(parts), width,
                              subsequent_indent=HANG) or [""]
        return [line.replace("\u00a0", " ") for line in lines]

    # Rationed before either block is fitted, and every block that has anything
    # to say keeps a line before any block gets a second one. Spending the whole
    # ration in order dropped `nearby` off some stays without saying so, which
    # is the one failure a fitted line must not have.
    if cap <= 0:
        quota = [len(lay(f, e, len(e))) for f, e in blocks]
    else:
        quota = [1] * len(blocks)
        left = cap - len(blocks)
        for i, (fixed, elastic) in enumerate(blocks):
            take = max(min(len(lay(fixed, elastic, len(elastic))) - quota[i],
                           left), 0)
            quota[i] += take
            left -= take

    rows: list[str] = []
    for (fixed, elastic), room in zip(blocks, quota):
        if room <= 0:
            continue
        # Shrink until it fits, exactly as the single line used to.
        for count in range(len(elastic), -1, -1):
            lines = lay(fixed, elastic, count)
            if len(lines) <= room:
                break
        rows.extend(lines[:room])
    return rows


# The table is two panes. On the left, one column: the rank, the title, and the
# write-up wrapped underneath it. On the right, the numbers, sealed behind a
# seam that runs unbroken down every line of the table — including the prose
# lines, which is the whole point. Prose used to be fitted to the terminal and
# so ran underneath the figures on the stays that had any, giving the numeric
# block a left edge that moved as you read down it.
VERT, CROSS, CLOSE = "│", "┼", "┴"
SEAM = "  " + VERT + "  "
# Narrow enough to still name a place, wide enough to wrap prose into. Between
# these the title column is whatever the terminal has spare.
MIN_TITLE, MAX_TITLE = 28, 56
# How many rows off the top are worth picking out. However you sorted it, the
# answer is at the top, and a table you have to hunt the top of has wasted the
# sorting.
LEADERS = 3


def _header(col: str) -> str:
    # The per-share column is the one you compare on, so it says whose money it
    # is — a bare "P/p/nt" invited the assumption that it was split three ways.
    return COLUMNS[col][0] or config.SHARE_LABEL.title()


def _commonest(values) -> str | None:
    """Whichever of these there is most of, or None if there are none."""
    tally: dict[str, int] = {}
    for value in values:
        tally[value] = tally.get(value, 0) + 1
    return max(tally, key=tally.get) if tally else None


# Each column as (header, unit, how to render one row, which way it aligns).
# The unit is a second header line rather than a suffix on every figure: thirty
# rows of "GBP" is thirty rows of the same word, and it is what stopped the
# numbers lining up on anything. Numbers align right so the eye can compare
# magnitudes down the column without reading them; words align left.
#
# Which of these `list` prints, and in what order, is `columns` in config.toml —
# the table got wide enough that "all of them" stopped being a sensible default
# for every trip.
COLUMNS = {
    "name":     ("Property", "", lambda r, ctx: r.get("name") or "?", "<", "what"),
    # ^ kept so an unedited `columns` list still parses; it heads the left pane
    #   rather than being one of these, and cmd_list takes it out.
    "source":   ("Source", "", lambda r, ctx: (r.get("source") or "")[:config.SOURCE_WIDTH], "<", "what"),
    "where":    ("Where", "", lambda r, ctx: (r.get("location") or r.get("region") or "")[:config.WHERE_WIDTH], "<", "what"),
    "checkin":  ("Check-in", "", lambda r, ctx: f"{r['checkin']} {weekday(r['checkin'])}" if r.get("checkin") else "", "<", "what"),
    "nts":      ("Nts", "", lambda r, ctx: str(r.get("nights") or ""), ">", "what"),
    "slp":      ("Slp", "", lambda r, ctx: str(r.get("sleeps") or r.get("adults") or ""), ">", "what"),
    "space":    ("Space", "", lambda r, ctx: " ".join(
                    x for x in (f"{r['bedrooms']}br" if r.get("bedrooms") else
                                (f"{r['rooms']}rm" if r.get("rooms") else None),
                                f"{r['bathrooms']:g}ba" if r.get("bathrooms") else None) if x), "<", "what"),
    # The currency rides in the unit row rather than on thirty rows of figures,
    # so the column holds numbers you can compare down. Only a row in some other
    # currency says which — being the one that needs to.
    "all_in":   ("All-in", lambda ctx: ctx["unit_cur"], lambda r, ctx: ctx["money"](r), ">", "cost"),
    "share_nt": (None, "/nt", lambda r, ctx: (f"{per_share_night(r, ctx['rate']):,.0f}"
                                              if per_share_night(r, ctx["rate"]) else "—"), ">", "cost"),
    # Normalised, so one column can hold a 5-star site and a 10-point one.
    "score":    ("Guest", "%", lambda r, ctx: _pct(scoring.guest_score(r)), ">", "good"),
    "reviews":  ("Revs", "", lambda r, ctx: str(r.get("reviews") or "—"), ">", "good"),
    "clean":    ("Clean", "%", lambda r, ctx: _pct(scoring.cleanliness(r)), ">", "good"),
    "look":     ("Look", "%", lambda r, ctx: _pct(scoring.look(r)), ">", "good"),
    # "≈" says the figure is the listing's own claim rather than a routed one.
    # Same column because it answers the same question, marked because it was
    # answered by the seller.
    "walk":     ("Walk", "min", lambda r, ctx: (f"{scoring.walked(r)[1] and '≈' or ''}"
                                                f"{scoring.walk_minutes(r):.0f}"
                                                if scoring.walk_minutes(r) is not None else "—"), ">", "what"),
    "kind":     ("Kind", "", lambda r, ctx: r.get("kind") or "—", "<", "what"),
    "traits":   ("From the write-up", "", lambda r, ctx: trait_words(
                    r.get("traits") or [], config.FACTS_TRAITS,
                    ctx.get("rarity")) or "—", "<", "good"),
    "points":   ("Pts", "", lambda r, ctx: f"{ctx['score'](r).points:g}", ">", "good"),
    # One decimal on every row, so the column is a straight edge rather than a
    # ragged one — "23" beside "20.9" beside "18.5" doesn't line up on anything.
    "value":    ("Value", "", lambda r, ctx: (f"{ctx['score'](r).value:.1f}"
                                              if ctx["score"](r).value is not None else "—"), ">", "good"),
}


def leading_mark(rec: dict, gates: list[tuple[str, str]]) -> str:
    """The glyph in front of a row.

    A must-have it fails outranks everything: a place with too few bedrooms is
    out whether or not we also need a price for it. But a page we couldn't
    fetch outranks a gate we merely can't judge yet, because that one names
    something you can go and do about it.
    """
    if any(verdict == "fail" for _, verdict in gates):
        return config.GATE_MARKS.get("fail", "x")
    if rec.get("status") != sources.OK:
        return config.STATUS_MARKS.get(rec.get("status"), " ")
    if gates:
        return config.GATE_MARKS.get("unknown", "?")
    return config.STATUS_MARKS.get(sources.OK, " ")


def cmd_list(args) -> int:
    # `list` is where you go to see what you've got, so it's the right place to
    # be told you're looking at a different set than usual. Silent in the normal
    # case, because a banner on every run is a banner nobody reads.
    if database.current() != config.DEFAULT_DB:
        print(f"Showing {database.current()}, not {config.DEFAULT_DB}. "
              f"`db {config.DEFAULT_DB}` switches back.\n")

    stays = load()
    if not stays:
        print(f"Nothing captured yet.\n  {RUN} add <url>")
        return 0

    rate = args.rate
    # Scored once per stay and reused: three columns and the sort all ask, and
    # the answer can't change underneath them mid-table.
    marks = {key_of(r): scored(r) for r in stays}
    gates = {key_of(r): ruled_out(r) for r in stays}

    if getattr(args, "viable", False):
        stays = [r for r in stays
                 if not any(v == "fail" for _, v in gates[key_of(r)])]
        if not stays:
            print("Everything captured fails a must-have in [filters]. "
                  "`list` without --viable shows them and why.")
            return 0

    stays.sort(key=lambda r: SORTS[args.sort](r, rate))

    # Collected over the stays rather than as the price column renders, so that
    # dropping `all_in` from `columns` doesn't also drop the note explaining
    # the 20% that is still inside every per-share figure below it.
    seen_tax = {tax for r in stays for amount, _, tax in [all_in(r)] if amount}
    # On what's stored, not on what the column ends up printing: `--rate` puts
    # one unit in the table while leaving Value dividing by the other, so a
    # table that has gone mixed stays mixed however it's displayed.
    seen_cur = {cur for r in stays
                for amount, cur, _ in [all_in(r)] if amount and cur}

    # The currency and the tax basis the header can carry are whichever most of
    # the table is in. A mark every row wears distinguishes no row from any
    # other — it is thirty characters spent saying "as usual" — so the common
    # case is stated once underneath and only the exceptions are marked.
    common_cur = _commonest(cur for r in stays
                            for amount, cur, _ in [all_in(r)] if amount and cur)
    common_tax = _commonest(tax for r in stays
                            for amount, _, tax in [all_in(r)] if amount)
    shown_cur = converted(1, common_cur, rate)[1] if common_cur else ""

    def money(rec: dict) -> str:
        amount, cur, tax = all_in(rec)
        amount, cur = converted(amount, cur, rate)
        if not amount:
            return "—"
        unit = "" if cur == shown_cur else f" {cur}"
        mark = "" if tax == common_tax else config.TAX_MARKS.get(tax, "")
        return from_mark(rec, tax) + f"{amount:,.0f}{unit}".strip() + mark

    # Worked out over the stays being shown, so `--viable` and a filtered
    # shortlist re-rank what counts as worth mentioning — in the column and in
    # the prose under it, which have to agree.
    rarity = how_rare(stays)
    titles = title_rarity(stays)
    ctx = {"rate": rate, "money": money, "rarity": rarity,
           "unit_cur": shown_cur, "score": lambda r: marks[key_of(r)]}

    # `name` is not a column any more — it heads the left pane, with the prose
    # wrapped underneath it — so it is taken out of the list rather than
    # rejected, and an unedited config.toml still means what it meant.
    chosen = [c for c in config.COLUMNS if c in COLUMNS and c != "name"]
    if unknown := [c for c in config.COLUMNS if c not in COLUMNS]:
        print(f"{config.PATH}: no such column {', '.join(unknown)} — "
              f"have: {', '.join(COLUMNS)}", file=sys.stderr)

    # A column holding one value thirty times isn't a column, it's a caption in
    # the wrong place — and this one sat between the name and the numbers,
    # pushing everything right for no information at all. Said once underneath
    # instead. Only worth doing where there are enough rows for "every row" to
    # mean something, and never to the two columns you came to read.
    fixed: list[tuple[str, str]] = []
    if len(stays) >= 3:
        for col in list(chosen):
            if col in ("points", "value"):
                continue
            seen = {COLUMNS[col][2](rec, ctx) for rec in stays}
            if len(seen) == 1 and (only := seen.pop()).strip() not in ("", "—"):
                fixed.append((_header(col), only))
                chosen.remove(col)

    data = [[COLUMNS[c][2](rec, ctx) for c in chosen] for rec in stays]
    heads = [_header(c) for c in chosen]
    units = [u(ctx) if callable(u := COLUMNS[c][1]) else u for c in chosen]
    widths = [max(len(heads[i]), len(units[i]), *(len(row[i]) for row in data))
              for i in range(len(chosen))]
    align = [COLUMNS[c][3] for c in chosen]
    # Four questions, not ten columns: what the place is, what it costs, how
    # good it is. A rule where the question changes gives the eye somewhere to
    # stop, and costs one character each.
    groups = [COLUMNS[c][4] for c in chosen]

    def pane(cells: list[str], styles: list | None = None) -> str:
        out: list[str] = []
        for i, (cell, w, a) in enumerate(zip(cells, widths, align)):
            if i and groups[i] != groups[i - 1]:
                out.append(VERT)
            text = cell.rjust(w) if a == ">" else cell.ljust(w)
            # After padding, never before: an escape sequence is no width on
            # the screen and several characters to ljust, and the column would
            # come out short by exactly as much as the styling cost.
            out.append(styles[i](text) if styles and styles[i] else text)
        return config.COLUMN_GAP.join(out)

    marked = {key_of(r): leading_mark(r, gates[key_of(r)]) for r in stays}
    mark_w = 1 if any(m.strip() for m in marked.values()) else 0
    rank_w = len(str(len(stays)))
    # "12 " before the title, or "12 ✗ " where anything in the table is marked.
    lead_w = rank_w + 1 + (mark_w + 1 if mark_w else 0)

    # The numbers are as wide as the numbers are; the title column gets what's
    # left. So widening the window widens the only column that can use it,
    # rather than leaving a number in config.toml to guess at.
    pane_w = len(pane(heads))       # exact: the separators are in it too
    room = max(shutil.get_terminal_size((120, 24)).columns, 60)
    title_w = config.TITLE_WIDTH
    if not title_w:
        title_w = max(MIN_TITLE,
                      min(room - lead_w - len(SEAM) - pane_w, MAX_TITLE))

    head = " " * lead_w + "Property".ljust(title_w) + SEAM + pane(heads)
    unit = " " * (lead_w + title_w) + SEAM + pane(units)
    full = len(head)
    # Where the verticals are, so a horizontal can cross them rather than run
    # over them. Read off the printed header rather than recomputed from the
    # widths, which is the version that can drift from what actually got drawn.
    seams = {i for i, ch in enumerate(head) if ch == VERT}

    def rule(cross: str = CROSS) -> str:
        return DIM("".join(cross if i in seams else config.RULE_CHAR
                           for i in range(full)))

    print(BOLD(head.rstrip()))
    if any(units):
        print(DIM(unit.rstrip()))
    print(rule())

    facts = config.FACTS if not getattr(args, "no_facts", False) else "off"
    cap = config.FACTS_LINES        # 0 is "as many as it has something for"
    # The few rows off the top are what you opened the table to find, so the
    # figure they were sorted into this order by is the one that carries it.
    # Costs no width, which the bar this replaced could not say for itself.
    top = [LEAD if c == SORT_COLUMN.get(args.sort) else None for c in chosen]
    for place, rec in enumerate(stays, 1):
        title = squeeze(rec.get("name") or "?", titles, title_w, config.CITY or "")
        lead = f"{str(place).rjust(rank_w)} "
        if mark_w:
            lead += marked[key_of(rec)][:1] + " "
        print(lead + title.ljust(title_w) + SEAM
              + pane(data[place - 1], top if place <= LEADERS else None))
        if facts != "off":
            # "line" is what the place is; "lines" adds what's around it, which
            # is the other half of the question and worth the room to anyone
            # who cares where they're standing.
            for text in prose(rec, title_w, rarity, cap,
                              1 if facts == "line" else 2):
                # The seam carries on down over an empty pane, and stops there:
                # it is the left edge of the numbers, not a box around nothing.
                print(DIM(" " * lead_w + text.ljust(title_w) + SEAM.rstrip()))
        if config.RULE_EVERY and place % config.RULE_EVERY == 0 \
                and place != len(stays):
            print(rule())
    print(rule(CLOSE))

    if fixed:
        print("\n" + DIM("The same on every row, so not in the table: "
                         + ", ".join(f"{h.lower()} {v}" for h, v in fixed) + "."))
    footnotes(stays, marks, gates, seen_tax, seen_cur, rate, common_tax)
    asked = getattr(args, "links", None)
    links(stays, args.sort, config.LINKS if asked is None else asked)
    return 0


def links(stays: list[dict], sort: str, count: int) -> None:
    """The way back to the stays at the top of the table.

    A sorted table answers "which one", and then leaves you holding a name you
    have to go and find again. These are the same rows, in the same order, as
    something you can open — which is the point of having ranked them.

    Printed in full and unwrapped. A Booking.com link carries the dates, the
    party size and the identifier of the room block whose price is in the row
    above it, so a URL folded to the terminal's width, or trimmed to look
    tidier, is one that opens a different quote than the one you're reading.

    Under the footnotes rather than beside the rows for the same reason the
    facts line isn't a column: there is no width at which this fits one, and
    the table has to stay scannable.
    """
    shown = [r for r in stays if r.get("url")][:max(count, 0)]
    if not shown:
        return
    # Named after the sort because that is what makes them the top three rather
    # than three of them, and the sort is a flag you may well have set on this
    # run and not the last one.
    print(f"\nTop {len(shown)} by {sort}:" if len(shown) > 1
          else f"\nTop by {sort}:")
    for place, rec in enumerate(shown, 1):
        print(f"  {place}. {rec.get('name') or '?'}")
        print(f"     {rec['url']}")
    # Only when there are others to fetch. A pointer to a command for getting
    # what is already on the screen reads as noise.
    if len(shown) < sum(1 for r in stays if r.get("url")):
        print(f"  {RUN} url <id>   for any of the rest")


TAX_NOTES = {
    "added": lambda: (f"VAT added by us at {config.VAT_RATE:.0%} — the site "
                      f"quoted a pre-tax price."),
    "computed": lambda: ("the page stated its tax rates and fees, so that is "
                         "the arithmetic done rather than a flat VAT estimate. "
                         "`show <id>` names the rates."),
    "unknown": lambda: ("tax status unknown; shown as quoted. Mark it with "
                        "`set <id> --incl-tax` or `--excl-tax`."),
}


def footnotes(stays, marks, gates, seen_tax, seen_cur, rate,
              common_tax=None) -> None:
    """What the table couldn't say in a column.

    Every mark in it gets explained here, and every explanation names the
    command that clears it — a glyph you have to go and look up is worse than
    no glyph. The basis most of the table shares wears no glyph and is said
    here as a plain sentence, since a mark that is on every row is one you
    stop seeing by the third row.
    """
    # First, and without a glyph, because it isn't a footnote to one number —
    # it says the ordering of the whole table is arithmetic across two units.
    # The currencies are already printed in the column, so there is nothing to
    # go and look up.
    if len(seen_cur) > 1:
        print("\n" + textwrap.fill(
            f"{' and '.join(sorted(seen_cur))} in one table — the share "
            f"column, the sort and Value are all arithmetic across both, so "
            f"the rows above are ordered against each other in units that "
            f"don't match. `set <id> --currency {config.BASE_CURRENCY}` files "
            f"a price under what it's actually in; `--rate` only converts the "
            f"column for display, and doesn't reach Value.", width=76))
    # Same predicate as the column, so the note can't turn up explaining a mark
    # that isn't in the table above it.
    if any(from_mark(r, all_in(r)[2]) for r in stays):
        print("\n~  a \"from\" price, not a quote for these dates — click through "
              "and set the real total with `set <id> --price`.")
    for basis, note in TAX_NOTES.items():
        if basis not in seen_tax:
            continue
        if basis == common_tax:
            # No glyph, because no row is wearing one: this is what the column
            # means unless a row says otherwise.
            print("\n" + textwrap.fill(f"Throughout: {note()}", width=76))
        else:
            print(f"\n{config.TAX_MARKS.get(basis, '')}  {note()}")
    if any(scoring.walked(r)[1] for r in stays):
        print("\n" + textwrap.fill(
            "≈  a walk the listing claims, not one we measured — the router had "
            "no answer for that destination. Checked against the stay's own "
            "coordinates, so nothing wildly wrong got in, but it is the seller's "
            "figure. `walk --again` measures it properly; "
            "`[maps] trust_claimed_walk = false` stops using them at all.",
            width=76))
    if rate:
        print(f"{config.BASE_CURRENCY} converted at {rate} "
              f"{config.QUOTE_CURRENCY}/{config.BASE_CURRENCY}.")

    def with_verdict(verdict: str) -> list[tuple[dict, list]]:
        return [(r, gates[key_of(r)]) for r in stays
                if any(v == verdict for _, v in gates[key_of(r)])]

    failed = with_verdict("fail")
    failed_keys = {key_of(r) for r, _ in failed}
    # A stay that already fails something outright is listed once, under the
    # failure — being also unsure about a second gate changes nothing.
    unsure = [(r, gs) for r, gs in with_verdict("unknown")
              if key_of(r) not in failed_keys]
    if failed:
        print(f"\n{config.GATE_MARKS.get('fail', 'x')}  fails a must-have, so no "
              f"score can buy it back:")
        for rec, gs in failed:
            why = "; ".join(w for w, v in gs if v == "fail")
            print(f"     {rec.get('name') or '?'} — needs {why}")
    if unsure:
        print(f"\n{config.GATE_MARKS.get('unknown', '?')}  can't tell whether it "
              f"clears a must-have — held back rather than ruled out:")
        for rec, gs in unsure:
            why = "; ".join(w for w, v in gs if v == "unknown")
            print(f"     {rec.get('name') or '?'} — unknown: {why}")

    # Points resting on absent data are the one number here that can quietly
    # mislead: a place nobody has reviewed scores like a place everybody
    # disliked, and only this line tells them apart.
    thin = [(r, marks[key_of(r)]) for r in stays if marks[key_of(r)].unknown]
    if thin:
        print("\n   scored on partial data — these factors had no number:")
        for rec, mark in thin:
            print(f"     {rec.get('name') or '?'} — {', '.join(mark.unknown)}")

    pending = [r["name"] for r in stays if r.get("status") != sources.OK]
    if pending:
        print(f"\n·/! needs a price: {', '.join(p or '?' for p in pending)}")


def cmd_refresh(args) -> int:
    stays = load()
    changed = 0
    for i, rec in enumerate(stays):
        if args.id and key_of(rec) != args.id.lower():
            continue
        if not rec.get("url"):
            continue
        try:
            fresh = sources.capture(rec["url"])
        except (ValueError, OSError) as exc:
            print(f"  {rec['name']}: {exc}")
            continue
        # Keep anything typed by hand; only fill gaps and refresh price. The
        # price is the one field a re-fetch is allowed to overwrite — except
        # with a "from" price over a total you confirmed, which turned £582
        # back into the £1090 the listing advertises.
        confirmed = rec.get("price_basis") == "quoted"
        soft = fresh.get("price_basis") != "quoted"
        for field, value in fresh.items():
            if value is None:
                continue
            if rec.get(field) is None:
                rec[field] = value
            elif field == "price" and not (confirmed and soft):
                rec[field] = value
        stays[i] = rec
        # Report what the *fetch* did, not whether a price happens to be on
        # file — a hand-typed price must not make a blocked page look fetched.
        if fresh["status"] == sources.BLOCKED:
            print(f"  {rec['name']}: blocked (bot wall) — kept existing data")
        else:
            changed += 1
            got = "price updated" if fresh.get("price") else "details only, no price"
            print(f"  {rec['name']}: {got}")
    save(stays)
    print(f"Refreshed {changed} of {len(stays)}.")
    return 0


def cmd_glean(args) -> int:
    """Re-read every stay's write-up and file what it says.

    Capture does this already, so this is for the stays that were captured
    before it did — and for the day summary.py learns to read something new,
    which is the same command either way.

    Cheap enough to be worth just running: no network, no rate limit, nothing
    to sign up for, a millisecond a stay. That is the whole difference between
    this and `walk`, which is otherwise its twin — there is no reason to be
    sparing with it, so `--again` isn't a flag here. Every run re-reads
    everything and rewrites what the prose currently supports, which is also
    how a fact drops off `gleaned` once a real scrape supplies it.
    """
    stays = load()
    if args.id:
        one = find(stays, args.id)
        if not one:
            print(f"No stay matching {args.id!r}", file=sys.stderr)
            return 1
        stays_to_read = [one]
    else:
        stays_to_read = stays

    # Fitted from every stay we hold, not just the ones being re-read, so
    # `glean <id>` judges that one against the same map as a whole-database run.
    places, detour = summary.locate(stays)
    width = max(shutil.get_terminal_size((100, 24)).columns, 60)
    rarity = how_rare(stays)

    read = nothing = 0
    conflicts, doubted = [], []
    for rec in stays_to_read:
        if not (rec.get("summary") or "").strip():
            nothing += 1
            continue
        verdict = summary.apply(rec, places, detour)
        if verdict.conflicts:
            conflicts.append((rec, verdict))
        doubted += [(rec, c) for c in verdict.doubted]
        if verdict.anything():
            read += 1
            print(f"  {rec.get('name') or '?'}")
            for line in facts_rows(rec, width, rarity, verdict):
                print(line)
    save(stays)

    print(f"\nRead {read} write-up{'s' if read != 1 else ''}.", end="")
    if nothing:
        print(f" {nothing} stay{'s' if nothing != 1 else ''} haven't got one — "
              f"paste it with `set <id> --summary '…'`.", end="")
    print()
    if conflicts:
        # Loud, and never resolved on our own. Two sources disagreeing about how
        # many bedrooms a place has is the most useful thing either of them said.
        print("\nThe prose disagrees with the record on these. Nothing was "
              "overwritten — check the listing and settle it with `set`:")
        for rec, verdict in conflicts:
            for name, (held, said) in verdict.conflicts.items():
                print(f"  {rec.get('name') or '?'}: {name} is {held} on the "
                      f"record, the write-up says {said}")
    if doubted:
        print("\nClaims thrown out as impossible against the stay's own "
              "coordinates:")
        for rec, claim in doubted:
            print(f"  {rec.get('name') or '?'}: \"{claim.landmark} is "
                  f"{claim.text}\" — {claim.doubted}")
    return 0


def cmd_walk(args) -> int:
    """Measure how long it takes to walk from each stay to the places you named.

    One call per stay rather than per destination, because both providers take
    a list of destinations and answer them together — which keeps us inside
    OSRM's fair-use rate limit, and off Google's per-element bill.
    """
    # Before the destination check, and before anything is loaded: if the
    # answer is "you haven't turned this on", that's the whole reply.
    try:
        proximity.check_enabled()
    except proximity.Disabled as exc:
        print(exc, file=sys.stderr)
        return 1

    # The current database's destinations, so a config holding two legs at once
    # measures the Edinburgh stays against Edinburgh and leaves Oban alone.
    db = database.current()
    wanted = config.destinations_for(db)
    if not wanted:
        elsewhere = " (the ones there name other databases)" if config.DESTINATIONS else ""
        fix = (f"Add a [[destination]] with a label and an address to "
               f"{config.CITIES_DIR.name}/{config.CITY}{config.CITY_SUFFIX}, "
               f"and every database in {config.CITY} gets it." if config.CITY else
               f"{db} isn't in a city yet — `db {db} --city <name>` names one, "
               f"and its destinations come with it.")
        print(f"No destinations for {db} in {config.where()}{elsewhere}, so "
              f"there's nothing to measure against.\n{fix}", file=sys.stderr)
        return 1

    stays = load()
    if args.id:
        one = find(stays, args.id)
        if not one:
            print(f"No stay matching {args.id!r}", file=sys.stderr)
            return 1
        targets = [one]
    else:
        targets = stays

    done = already = unlocatable = 0
    try:
        for rec in targets:
            if rec.get("walk_minutes") and not args.again:
                already += 1
                continue
            origin = proximity.origin_of(rec)
            if not origin:
                unlocatable += 1
                print(f"  {rec.get('name') or '?'}: nothing to measure from — "
                      f"add one with `set {key_of(rec)} --address '…'`")
                continue
            try:
                minutes, problems = proximity.walk_times(origin, wanted)
            except proximity.Unlocatable:
                # One stay the geocoder couldn't place. Every other stay still
                # can be, so this is a skip and a tally, not the end of the run.
                unlocatable += 1
                print(f"  {rec.get('name') or '?'}: couldn't place {origin!r} — "
                      f"try `set {key_of(rec)} --address '…'` with something "
                      f"more specific")
                continue
            except OSError as exc:
                print(f"  {rec.get('name') or '?'}: {exc}")
                continue

            # Merged, not replaced: a destination this run couldn't reach keeps
            # whatever an earlier run learned about it.
            rec["walk_minutes"] = {**(rec.get("walk_minutes") or {}), **minutes}
            done += 1
            print(f"  {rec.get('name') or '?'}: {proximity.describe(rec)}")
            for problem in problems:
                print(f"    {problem}")
    except (proximity.Disabled, proximity.NoKey, proximity.MapsError) as exc:
        # Whatever was measured before the service refused us is still worth
        # keeping, so this saves on the way out rather than discarding the run.
        save(stays)
        print(exc, file=sys.stderr)
        return 1

    save(stays)
    tail = []
    if already:
        tail.append(f"{already} already measured (`walk --again` redoes them)")
    if unlocatable:
        tail.append(f"{unlocatable} with no address or coordinates")
    print(f"Measured {done} of {len(targets)}."
          + (f" Skipped {', '.join(tail)}." if tail else ""))
    return 0


def cmd_paste(args) -> int:
    """Take a record captured by the browser bookmarklet.

    The browser has already rendered the page and cleared any bot wall, so this
    path works on sites our own fetches can't reach.
    """
    raw = args.json if args.json else sys.stdin.read()
    try:
        incoming = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"That isn't valid JSON ({exc}).", file=sys.stderr)
        return 1

    rec, candidates, problem = record_from_payload(incoming)
    if problem:
        print(problem, file=sys.stderr)
        return 1

    if args.price is not None:
        rec["price"] = args.price
        rec["price_basis"] = "quoted"
    rec["nights"] = args.nights or rec["nights"]
    rec["adults"] = args.adults or rec["adults"]

    stays = load()
    rec = merge_over(find_exact(stays, key_of(rec)), rec)
    # Status has to be judged after the merge: a price recovered from the
    # previous capture still counts as a price.
    if rec.get("price") or rec.get("native_price"):
        rec["status"] = sources.OK
    glean(rec, stays)
    measure_walk(rec)
    stays = [s for s in stays if key_of(s) != key_of(rec)]
    stays.append(rec)
    save(stays)

    print(f"{rec['name'] or '?'}  [{rec['source']}]")
    if walk := proximity.describe(rec):
        print(f"  {walk}")
    if rec["price"]:
        print(f"  {rec['price']:g} {rec['currency'] or ''}".rstrip())
        if rec.get("price_basis") == "indicative":
            print('  that is a "from" price, not a quote for these dates — '
                  "click through to book and set the real total:")
            print(f"    {RUN} set {rec['code']} --price <total> --incl-tax")
    elif candidates:
        # Several plausible amounts on the page and no way to tell which is the
        # total — offering the list beats guessing wrong.
        print("  couldn't tell which amount is the total. Candidates:")
        print("    " + ", ".join(f"{c:g}" for c in candidates))
        print(f"  set it with:  {RUN} set {key_of(rec)} --price <n>")
    else:
        print("  no price on the page — add one with `set`")
    return 0


def record_from_payload(incoming: dict) -> tuple[dict | None, list, str | None]:
    """Turn a bookmarklet payload into a record, or say why it isn't one.

    Returns (record, price candidates, complaint). Shared by `paste` and the
    prompt so both judge a page the same way.
    """
    rec = sources.blank_record()
    candidates = incoming.pop("price_candidates", []) or []
    for field, value in incoming.items():
        if field in rec and value is not None:
            rec[field] = value

    # The bookmarklet sends amenities in the site's own words — "Free WiFi",
    # "Parking on site" — because the alias table belongs in one language, not
    # kept in step across two.
    if isinstance(rec.get("amenities"), list):
        rec["amenities"] = sources.normalise_amenities(rec["amenities"]) or None

    if not (rec["name"] or rec["code"]):
        # Search and region pages parse fine but describe no single property.
        return None, candidates, ("That page doesn't identify one property — it "
                                  "looks like a search or landing page. Open a "
                                  "specific listing and click again.")

    rec["captured_at"] = dt.datetime.now().isoformat(timespec="seconds")
    rec["status"] = sources.OK if rec["price"] else sources.NEEDS_PRICE
    return rec, candidates, None


def tally(name: str) -> str:
    n = database.count(name)
    if n is None:
        return "not readable"
    return "1 stay" if n == 1 else f"{n} stays"


def ask_city(db: str) -> str | None:
    """Which city a new database is in, asked once, while it's being started.

    Asked rather than left to be discovered, because a database with no city is
    a database whose write-ups go unread and whose walks go unmeasured, and
    nothing about the empty table it prints says so. One question at the one
    moment there is nothing else to be doing.

    Silence is an answer: a database can perfectly well have no city, and a
    pipe that can't be asked mustn't be waited on — the caller says what to do
    about a database that ends up without one, and says it the same way whether
    the question was declined or never put.
    """
    if not sys.stdin.isatty():
        return None
    if known := config.cities():
        print(f"  Cities already configured: {', '.join(known)}.")
    print(f"  Which city is {db} in? A new name starts a config for it; blank "
          f"for none.")
    try:
        return input("  city> ").strip() or None
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def attach_city(db: str, city: str) -> None:
    """Point a database at a city, starting that city's config if it's new."""
    try:
        city = config.name_of(city, "city")
    except ValueError as exc:
        print(f"  {exc}", file=sys.stderr)
        return
    fresh = not config.city_path(city).exists()
    path = config.start_city(city)
    config.set_city(db, city)
    where = f"{config.CITIES_DIR.name}/{path.name}"
    if fresh:
        print(f"  {db} is in {city}. Started {where} — empty, with the shape "
              f"commented in. Fill in its destinations and landmarks and every "
              f"database in {city} gets them.")
    else:
        dests, marks = config.city_counts(city)
        print(f"  {db} is in {city}, so it takes {where}: {dests} "
              f"destination{'' if dests == 1 else 's'}, {marks} "
              f"landmark{'' if marks == 1 else 's'}.")


def cmd_db(args) -> int:
    """Which set of stays we're working in, and how to be in a different one.

    Switching is sticky, so the flag you'd otherwise type on every capture is
    typed once. The cost of that is a mode you can forget you're in, which is
    why both branches here end by naming the way back.
    """
    if args.name:
        try:
            name = database.name_of(args.name)
            if args.new:
                database.start(name)
            elif not database.path_of(name).exists():
                print(f"No database called {name}. There is: "
                      f"{', '.join(database.names())}.\n"
                      f"  `db {name} --new` starts one.", file=sys.stderr)
                return 1
            was = database.current()
            database.use(name)
        except (ValueError, OSError) as exc:
            print(exc, file=sys.stderr)
            return 1

        back = f" `db {was}` goes back to {tally(was)}." if was != name else ""
        print(f"{'Started and now in' if args.new else 'Now in'} {name} — "
              f"{tally(name)}.{back}")

        # The city, which is most of what the settings are and the one thing
        # nothing else can work out for itself.
        city = args.city or (ask_city(name) if args.new else None)
        if city:
            attach_city(name, city)
            # The pointer moved and so did the file under it; re-resolve so the
            # rest of this run, and the prompt this may have been typed at,
            # reads the city we just attached rather than the one it had.
            config.DB = None
            database.current()
        elif in_city := config.city_of(name):
            city_file = config.city_path(in_city)
            if not city_file.exists():
                print(f"  In {in_city}, but there is no "
                      f"{config.CITIES_DIR.name}/{city_file.name} — nothing "
                      f"came with it. `db {name} --city {in_city}` starts one.")
            else:
                dests, marks = config.city_counts(in_city)
                print(f"  In {in_city} — {dests} destination"
                      f"{'' if dests == 1 else 's'}, {marks} "
                      f"landmark{'' if marks == 1 else 's'} from "
                      f"{config.CITIES_DIR.name}/{city_file.name}.")
        else:
            known = config.cities()
            print(f"  No city — {config.PATH.name} alone. `db {name} --city "
                  f"<city>` names one, which is what brings the landmarks and "
                  f"the destinations with it."
                  + (f" There is: {', '.join(known)}." if known else ""))
        # The pointer moved, but this run didn't. Saying so beats letting the
        # next command look like it ignored you. On stdout, and after the line
        # it qualifies — a caveat that turns up first reads as a failure.
        if pinned := database.forced():
            print(f"  {database.ENV}={pinned} is set though, so that's what "
                  f"still gets read until you unset it.")
        return 0

    if args.new or args.city:
        flag = "--new" if args.new else "--city"
        print(f"`db <name> {flag} ...` needs a name.", file=sys.stderr)
        return 1

    here = database.current()
    names = database.names()
    width = max(len(n) for n in names)
    counts = {n: tally(n) for n in names}
    count_width = max(len(c) for c in counts.values())
    for name in names:
        pinned = database.ENV if name == here and database.forced() else ""
        line = (f"{'→' if name == here else ' '} {name.ljust(width)}  "
                f"{counts[name].ljust(count_width)}"
                + f"  {config.city_of(name) or ''}"
                + (f"   ({pinned}, for this run only)" if pinned else ""))
        print(line.rstrip())
    spare = [c for c in config.cities()
             if c not in {config.city_of(n) for n in names}]
    print(f"\nIn {config.STORE_DIR}. `db <name>` switches and remembers it; "
          f"`db <name> --new` starts one and asks which city it's in. The city "
          f"is what carries the landmarks, the destinations and the rates, from "
          f"{config.CITIES_DIR}"
          + (f" — also configured there: {', '.join(spare)}." if spare else "."))
    return 0


def cmd_rm(args) -> int:
    """Drop stays you've ruled out."""
    stays = load()
    removed = []
    for needle in args.ids:
        rec = find(stays, needle)
        if not rec:
            print(f"No stay matching {needle!r}", file=sys.stderr)
            continue
        stays = [s for s in stays if key_of(s) != key_of(rec)]
        removed.append(rec["name"] or key_of(rec))
    if removed:
        save(stays)
        print("Removed: " + ", ".join(removed))
    return 0 if removed else 1


def describe(rec: dict, rate: float | None = None) -> str:
    """One stay as a block you can paste into a message.

    The same facts `show` used to dump as JSON, minus the nulls and the quoting.
    The per-person line shows its own arithmetic — the figure is only worth
    anything if you can see which price, how many nights and how many people
    went into it.
    """
    lines = [rec.get("name") or "?"]
    tag = " ".join(x for x in (rec.get("source"), rec.get("code")) if x)
    if tag:
        lines.append(tag)
    if rec.get("url"):
        lines.append(rec["url"])
    lines.append("")

    def row(label: str, value: str | None) -> None:
        if value:
            lines.append(f"  {label:<9} {value}")

    # Sites often file a small town as both its location and its region, and
    # "Oban / Oban" reads like a bug.
    where = list(dict.fromkeys(x for x in (rec.get("location"), rec.get("region")) if x))
    row("Where", " / ".join(where))
    if rec.get("checkin"):
        row("When", f"{rec['checkin']} {weekday(rec['checkin'])} → "
                    f"{rec.get('checkout') or '?'} {weekday(rec.get('checkout'))}"
                    + (f"  ({rec['nights']} nights)" if rec.get("nights") else ""))
    def count(n: int, noun: str, plural: str = "") -> str:
        return f"{n} {noun}" if n == 1 else f"{n} {plural or noun + 's'}"

    party = [count(rec["adults"], "adult") if rec.get("adults") else None,
             count(rec["children"], "child", "children") if rec.get("children") else None,
             # Not the property's layout: an apartment books as one "room".
             count(rec["rooms"], "room") + " booked" if rec.get("rooms") else None]
    row("Party", ", ".join(p for p in party if p))
    shape = [f"sleeps {rec['sleeps']}" if rec.get("sleeps") else None,
             f"{rec['bedrooms']} bed" if rec.get("bedrooms") else None,
             f"{rec['bathrooms']} bath" if rec.get("bathrooms") else None,
             f"{rec['rooms_total']} rooms" if rec.get("rooms_total") else None]
    row("Property", ", ".join(s for s in shape if s))
    # The bed list, spelled out, because "sleeps 4" counts a sofa bed the same
    # as a double behind a door and only one of those settles an argument.
    row("Beds", sources.describe_beds(rec.get("beds")))
    row("Has", ", ".join(rec.get("amenities") or []))
    row("Kind", rec.get("kind"))
    # In full here, not the handful the table has room for — `show` is where you
    # go when the row didn't settle it. Not marked as read from the description
    # rather than the feature list: it all came off the same page, and saying
    # which paragraph would rank facts by their typesetting. `--json` still
    # carries `gleaned` for the day you need to ask.
    row("Says", trait_words(rec.get("traits") or []))
    row("Walk", proximity.describe(rec))
    if claimed := rec.get("walk_claimed"):
        row("", "the listing claims " + ", ".join(
            f"{m} min to {place}" for place, m in claimed.items()))
    if rec.get("score"):
        n = f" from {rec['reviews']} reviews" if rec.get("reviews") else ""
        pct = scoring.guest_score(rec)
        scale = f"/{scoring.scale_of(rec):g}" if scoring.scale_of(rec) else ""
        as_pct = f"  = {pct:.0f}%" if pct is not None else \
            "  — scale unknown, so it can't be compared with other sites"
        row("Score", f"{rec['score']:g}{scale}{n}{as_pct}")
    for name, value in (rec.get("subscores") or {}).items():
        row("", f"{name} {value:g}")
    marks = [f"look {rec['look']}/5" if rec.get("look") else None,
             f"clean {rec['clean']}/5" if rec.get("clean") else None]
    row("Yours", ", ".join(m for m in marks if m))
    row("Offer", rec.get("offer"))

    if rec.get("price"):
        basis = {"quoted": " — quoted for these dates",
                 "indicative": ' — a "from" price, not a quote for these dates'}
        row("Price", f"{rec['price']:g} {rec.get('currency') or ''}".rstrip()
                     + basis.get(rec.get("price_basis"), ""))
    if rec.get("native_price"):
        row("Native", f"{rec['native_price']:g} "
                      f"{rec.get('native_currency') or ''}".rstrip()
                      + " — the property's own price, before any FX markup")

    amount, cur, tax = all_in(rec)
    amount, cur = converted(amount, cur, rate)
    if amount:
        vat = rec.get("vat_rate") or config.VAT_RATE
        note = {"added": f" — VAT added by us at {vat:.0%}",
                "computed": " — " + stated_charges_note(rec),
                "unknown": " — VAT status unknown, shown as quoted",
                "inclusive": " — VAT included"}[tax]
        row("All-in", f"{amount:,.2f} {cur}".strip() + note)

    label = config.SHARE_LABEL
    nights, shares = rec.get("nights"), shares_of(rec)
    stay_share = per_share_total(rec, rate)
    if stay_share:
        row(f"Per {label}", f"{stay_share:,.2f} {cur}".strip()
                            + f" for the stay   = {amount:,.2f} / "
                            + count(shares, label))
    psn = per_share_night(rec, rate)
    if psn:
        row(f"{label.title()}/nt", f"{psn:,.2f} {cur}".strip()
                                   + f"   = {stay_share:,.2f} / {count(nights, 'night')}")
    elif amount and not nights:
        row(f"{label.title()}/nt", f"— no nights on file; set with "
                                   f"`set {key_of(rec)} --nights N`")

    # Points last, and never as a bare number. A composite you can't take apart
    # is one you end up trusting or ignoring wholesale, and neither is useful.
    mark = scored(rec)
    if mark.points or mark.unknown:
        row("Points", f"{mark.points:g}   {mark.summary()}")
    if mark.value is not None:
        # Always shown against the unconverted figure, because that is what it
        # was computed from — quoting it against a converted one would be
        # arithmetic that doesn't check out.
        base = per_share_night(rec)
        _, base_cur, _ = all_in(rec)
        row("Value", f"{mark.value:g}   = {mark.points:g} points / "
                     f"({base:,.0f} {base_cur} per {label} a night / "
                     f"{config.PRICE_UNIT})")

    for what, verdict in ruled_out(rec):
        row("Must-have", f"{what} — "
            + ("not met" if verdict == "fail" else "can't tell from what we hold"))

    row("Note", rec.get("note"))
    row("Status", rec.get("status"))
    row("Captured", rec.get("captured_at"))

    # Last, and wrapped rather than in the label column, because it's the one
    # field with no fixed width — a paragraph squeezed into a two-column layout
    # takes the whole block down with it.
    if rec.get("summary"):
        lines.append("")
        lines.append("  Summary")
        for para in rec["summary"].split("\n"):
            lines.append(textwrap.fill(para, width=76, initial_indent="    ",
                                       subsequent_indent="    ")
                         if para.strip() else "")

    if rate:
        lines.append("")
        lines.append(f"  {config.BASE_CURRENCY} converted at {rate} "
                     f"{config.QUOTE_CURRENCY}/{config.BASE_CURRENCY}.")
    return "\n".join(lines)


def cmd_show(args) -> int:
    rec = find(load(), args.id)
    if not rec:
        print(f"No stay matching {args.id!r}", file=sys.stderr)
        return 1
    print(json.dumps(rec, indent=2) if args.json else describe(rec, args.rate))
    return 0


def cmd_url(args) -> int:
    """The link back to a listing, on a line with nothing else on it.

    `show` prints the URL too, in among everything else we hold about the stay.
    This is the form you can hand to something: pipe it, open it, put it on the
    clipboard, without first cutting a line up.

        python3 lodgingbuddy.py url aberlady | xargs xdg-open

    With no id it's every stay, in the order `list` would have put them, which
    is what opening the shortlist again actually means.
    """
    stays = load()
    if args.id:
        rec = find(stays, args.id)
        if not rec:
            print(f"No stay matching {args.id!r}", file=sys.stderr)
            return 1
        # Distinguished from "no such stay", because the two want different
        # things done about them: one is a typo, the other is a record that
        # arrived by hand and can be given a URL with `set`.
        if not rec.get("url"):
            print(f"{rec.get('name') or args.id} has no URL stored.",
                  file=sys.stderr)
            return 1
        print(rec["url"])
        return 0

    # The configured rate rather than a flag of our own: `value` and `share`
    # divide by it, so the order has to be arrived at the same way `list`
    # arrives at it when you don't pass `--rate` either.
    stays.sort(key=lambda r: SORTS[args.sort](r, config.DEFAULT_RATE))
    found = [r["url"] for r in stays if r.get("url")]
    if not found:
        print("No URLs stored in this database.", file=sys.stderr)
        return 1
    for url in found:
        print(url)
    return 0


# ────────────────────────────── the prompt ─────────────────────────────────

# What a sign in front of a typed total means. Booking pages print the sign
# rather than the code, so that is the form a copied price arrives in.
CURRENCY_SIGNS = {"£": "GBP", "$": "USD", "€": "EUR"}


def as_total(text: str | None) -> tuple[float | None, str | None]:
    """A typed total and the currency it was typed in, or (None, None).

    Tolerates the shapes a price arrives in when you've just copied one off a
    booking page: 1,234.50, £480 and 758.70 GBP are all totals.

    The sign is the reason this returns two things rather than one. Booking.com
    shows a US-signed-in booker dollars and then, on the same panel, names the
    pounds the property will actually charge — so both numbers are in front of
    you and only you know which one you copied. Filed under the wrong one, a
    total doesn't look wrong. It looks cheap, and it sorts like it.

    A bare number stays bare: it says nothing about its unit, so it doesn't get
    to overrule what the site was quoting in. "It's dollars" and "nobody said"
    are different claims and mustn't arrive here as the same one.
    """
    text = (text or "").strip().replace(",", "")
    if not text:
        return None, None

    currency = None
    for sign, code in CURRENCY_SIGNS.items():
        if sign in text:
            currency, text = code, text.replace(sign, "")
    # "758.70 GBP" and "GBP 758.70" both get typed, so the code is taken from
    # wherever in the line it turns up rather than from a fixed side of it.
    rest = []
    for word in text.split():
        if len(word) == 3 and word.isalpha():
            currency = word.upper()
        else:
            rest.append(word)

    # What's left has to be the one word. Two numbers on a line is a sentence
    # we haven't understood rather than a price, and running them together
    # would answer it with a number nobody typed.
    if len(rest) != 1:
        return None, None
    try:
        return float(rest[0]), currency
    except ValueError:
        return None, None


def parse_entry(line: str) -> tuple[str, object, float | None, str | None] | None:
    """Read one line of the prompt into (kind, payload, total, currency).

    Three things get pasted here and all of them are welcome: a bare URL, the
    raw JSON the bookmarklet puts on the clipboard, or a whole
    `... paste '{...}'` command line — which older builds of the bookmarklet
    copied, and which still arrives from anyone who typed it. Any of them may
    be followed by a space and the total you're paying, in whatever currency
    you're reading it in.
    """
    line = line.strip()
    if not line:
        return None

    # Raw JSON. raw_decode finds where the object ends, so whatever follows is
    # the total — no counting braces by hand.
    if line.startswith("{"):
        try:
            obj, end = json.JSONDecoder().raw_decode(line)
        except json.JSONDecodeError:
            return None
        return ("json", obj, *as_total(line[end:]))

    if line.startswith("http"):
        url, _, tail = line.partition(" ")
        return ("url", url, *as_total(tail))

    # The bookmarklet's command line. shlex undoes its shell quoting, including
    # the '\'' dance it does for apostrophes in property names.
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    for i, token in enumerate(tokens):
        tail = " ".join(tokens[i + 1:])
        if token.startswith("{"):
            try:
                return ("json", json.loads(token), *as_total(tail))
            except json.JSONDecodeError:
                return None
        if token.startswith("http"):
            return ("url", token, *as_total(tail))
    return None


def apply_total(rec: dict, total: float, currency: str | None = None) -> None:
    """Record a total typed at the prompt, and let it win.

    You typed it off the booking page, so it is the final number: quoted for
    these dates and with tax already in it. It also clears any scraped native
    price, which `all_in` would otherwise prefer — leaving that in place would
    show you a number you didn't type and can't account for.

    The currency comes from the same place the number did — you — and only when
    you gave one. Where you didn't, the site's own currency stands: it is a
    guess about a figure you typed off a checkout page, but the alternative is a
    price with no unit at all, and `list` says when a table has ended up holding
    two of them.
    """
    rec["price"] = total
    if currency:
        rec["currency"] = currency
    rec["price_basis"] = "quoted"
    rec["tax_included"] = True
    rec["native_price"] = None
    rec["native_currency"] = None
    rec["status"] = sources.OK


def confirm(rec: dict, candidates: list, rate: float | None) -> str:
    """One line back, because you're about to paste another one."""
    amount, cur, tax = all_in(rec)
    amount, cur = converted(amount, cur, rate)
    psn = per_share_night(rec, rate)

    bits = [f"{rec.get('name') or '?'} [{rec.get('source')}]"]
    if rec.get("nights"):
        bits.append(f"{rec['nights']} nts")
    # The walk, in the one figure the tier scores, rather than every
    # destination — the line is a receipt for the paste, not a report, and
    # `show` breaks it out. Rounded the way the column rounds it.
    if (mins := scoring.walk_minutes(rec)) is not None:
        bits.append(f"{mins:.0f}m walk")
    if amount:
        bits.append(f"{from_mark(rec, tax)}{amount:,.0f} {cur}".strip()
                    + config.TAX_MARKS.get(tax, "") + " all-in")
    if psn:
        bits.append(f"{psn:,.0f}/{config.SHARE_LABEL}/nt")
    out = ["  " + " · ".join(bits)]

    if rec.get("status") == sources.BLOCKED:
        out.append("    the site refused the page — paste the bookmarklet output"
                   " instead, or add a total after the link")
    elif not amount and candidates:
        out.append("    couldn't tell which amount is the total. Candidates: "
                   + ", ".join(f"{c:g}" for c in candidates))
        out.append("    type the right one on the next line")
    elif not amount:
        out.append("    no price — type the total on the next line")
    elif from_mark(rec, tax):
        # Same predicate as the mark it names. Explaining a `~` that isn't on
        # the line above is worse than saying nothing: it tells you the number
        # is soft when the whole point of `=` is that this one isn't.
        out.append('    ~ is a "from" price, not a quote — type the real total'
                   " on the next line")
    return "\n".join(out)


def attach_summary(key: str, text: str) -> tuple[str, int] | None:
    """Add the listing's own words to a stay we already hold.

    Appended, not replaced: a description copied out of a browser arrives as
    however many lines the terminal decided to send it in, and each of them
    reaches us as a separate paste. `set <id> --summary` is the one that
    replaces, for when the text itself was wrong rather than short.

    Returns (name, words so far), or None if that stay has gone — which it has
    if you changed database between capturing it and pasting this.
    """
    stays = load()
    rec = find_exact(stays, key)
    if rec is None:
        return None
    rec["summary"] = ((rec.get("summary") or "") + "\n" + text).strip()
    save(stays)
    return rec.get("name") or "?", len(rec["summary"].split())


def price_stored(key: str, total: float, currency: str | None,
                 rate: float | None) -> bool:
    """Put a total onto the stay just captured, a line later than usual."""
    stays = load()
    rec = find_exact(stays, key)
    if rec is None:
        return False
    apply_total(rec, total, currency)
    save(stays)
    print(confirm(rec, [], rate))
    return True


def capture_entry(kind: str, payload, total: float | None,
                  currency: str | None, rate: float | None) -> str | None:
    """Fold one pasted entry into the store and say what happened.

    Hands back the key of what it captured, so the lines after it — a total, a
    summary — know what they're about.
    """
    candidates: list = []
    if kind == "url":
        try:
            rec = sources.capture(payload)
        except (ValueError, OSError) as exc:
            print(f"  {exc}")
            return None
    else:
        rec, candidates, problem = record_from_payload(dict(payload))
        if problem:
            print(f"  {problem}")
            return None

    stays = load()
    rec = merge_over(find_exact(stays, key_of(rec)), rec)
    # After the merge, so a typed total beats anything the merge restored.
    if total is not None:
        apply_total(rec, total, currency)
    elif rec.get("price") or rec.get("native_price"):
        rec["status"] = sources.OK
    glean(rec, stays)
    measure_walk(rec)
    stays = [s for s in stays if key_of(s) != key_of(rec)]
    stays.append(rec)
    save(stays)
    print(confirm(rec, candidates, rate))
    return key_of(rec)


PROMPT_HELP = """\
  Paste a link, or the bookmarklet's output, and press enter.
  Add a space and the total you're paying to record it:

      https://www.booking.com/Share-abc123 480
      {"source":"booking.com",...} £582

  A total typed here is taken as the final price, tax included. On its own
  line it does the same, to whatever you captured last — which is the usual
  way round, since the real total only shows up once you click through:

      {"source":"booking.com",...}
      £582

  Type its currency with it — £582, $789 or 582 GBP — whenever the page is
  showing you two. Booking.com quotes a US-signed-in booker in dollars and
  then names the pounds the property will charge; a bare number is filed
  under whatever the site was quoting in, which is right until you copy the
  other one.

  Anything else that isn't a command is filed as that stay's summary. The
  bookmarklet already brings the listing's write-up over, so this is for
  topping it up — a site whose markup moved, or the paragraph a page keeps
  behind a "read more". It appends: blank line when you've finished, and
  `set <id> --summary "..."` replaces instead.

  Commands: add, paste, list, show, url, set, walk, refresh, rm, db.
  `set <id> --look 4 --clean 5` marks the things no site can tell you.
  `list` ends with the links to its top few; `url <id>` fetches any one.
  The prompt is named after the database you're capturing into. `db` lists
  them, `db <name>` moves to another, `db <name> --new` starts one.
  `quit` leaves. Ctrl-C clears the line. Ctrl-D quits too, but only on
  Linux and macOS — on Windows, end-of-file is Ctrl-Z then enter."""


def cmd_watch(args) -> int:
    """Hold a prompt open so a browsing session isn't one process per link."""
    try:
        import readline  # noqa: F401 — arrow keys and history, where available
    except ImportError:
        pass

    # Run with no subcommand, argparse sets no --rate, so ask for it carefully.
    rate = getattr(args, "rate", None) or config.DEFAULT_RATE
    print(f"lodgingbuddy — {tally(database.current())} in "
          f"{database.current()}. `help` for what this takes.")

    # What the lines after a capture are about. One listing arrives as three
    # separate pastes — the bookmarklet's output, the total off the booking
    # page, the write-up — and only the first of them names the property.
    holding: str | None = None
    pending: tuple[str, int] | None = None

    def settled() -> None:
        """Say what a run of pasted prose came to, once it stops arriving.

        Held back rather than printed per line, because the terminal decides
        how many lines a pasted paragraph is and a receipt for each of them
        would bury the thing you were reading.
        """
        nonlocal pending
        if pending:
            print(f"  + summary on {pending[0]}, {pending[1]} words")
            pending = None

    while True:
        try:
            # Named after the database rather than after what it takes, because
            # the thing you can't otherwise tell by looking is where a paste is
            # about to land. Re-read each time: `db` at this prompt moves it.
            line = input(f"{database.current()}> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue

        # A blank line is how a paste ends, so it's the natural place to total
        # up what arrived rather than something to ignore.
        if not line:
            settled()
            continue
        if line in ("quit", "exit", "q"):
            settled()
            return 0
        # Ctrl-D is end-of-file on Linux and macOS, and input() raises above
        # before we ever see it. The Windows console has no meaning for it and
        # passes the character through, so it arrives as a line of its own
        # instead — which nothing else here does. Take it for what it meant.
        if line in ("\x04", "\x1a"):
            settled()
            print("  (not end-of-file on this platform — `quit` always works)")
            return 0
        if line in ("help", "h", "?"):
            settled()
            print(PROMPT_HELP)
            continue

        if entry := parse_entry(line):
            settled()
            holding = capture_entry(*entry, rate)
            continue

        # A bare total on its own line is the number you'd have put after the
        # link, typed a moment later — which is how it actually goes, since you
        # have to open the booking page to find out what it is.
        total, currency = as_total(line)
        if holding and total is not None:
            settled()
            if not price_stored(holding, total, currency, rate):
                print("  the stay that was for isn't in this database any more")
                holding = None
            continue

        # A paste that didn't parse should say so, rather than being reported as
        # an unknown command — a truncated clipboard is the likely cause.
        if line.startswith(("{", "http")) or "lodgingbuddy.py" in line:
            settled()
            print("  that looks like a paste but didn't parse — copy it again, "
                  "or check the whole line arrived")
            continue

        head = line.split()[0]
        if head in getattr(args, "commands", ()):
            settled()
            # argparse exits on a bad command line, which must not take the
            # prompt down with it.
            try:
                sub_args = args.parser.parse_args(shlex.split(line))
            except (SystemExit, ValueError):
                continue
            if func := getattr(sub_args, "func", None):
                try:
                    func(sub_args)
                except (ValueError, OSError, KeyError) as exc:
                    print(f"  {exc}")
            continue

        # Prose, then — the listing's own write-up. The bookmarklet brings it
        # over already, so this is the top-up path: a site whose markup moved,
        # a description that ran past the cap, the paragraph behind a "read
        # more". Hence appending rather than replacing.
        #
        # A single word is exempt: nothing describing a cottage is one word,
        # and a mistyped command answered by silently filing it as a
        # description would be worse than being told the command doesn't exist.
        if holding and " " in line:
            if got := attach_summary(holding, line):
                pending = got
                continue
            holding = None
            print("  that stay isn't in this database — paste the listing here "
                  "first, then its summary")
            continue

        if holding:
            print(f"  no command called {head!r}, and one word on its own isn't "
                  f"a summary — try `help`")
        else:
            print(f"  not a link, and no command called {head!r} — try `help`. "
                  f"Summary text goes after the listing it describes.")


def main() -> int:
    # Before anything asks for a file by name, since what this does is put the
    # files under the names everything else now asks for. Silent when there is
    # nothing left to rename, which is every run after the first.
    if moved := config.migrate():
        print(f"  Files are named by kind now — {config.DB_SUFFIX}, "
              f"{config.DB_CONF_SUFFIX}, {config.CITY_SUFFIX}:",
              file=sys.stderr)
        for line in moved:
            print(f"    {line}", file=sys.stderr)

    # Before argparse, because the parser below bakes settings into its own help
    # and defaults — the sort, the share label, the currency pair — and which
    # database we're in is what decides those. Everything after this point sees
    # the city's settings; asking first is the whole of what makes that true.
    database.current()

    p = argparse.ArgumentParser(description="Collate lodging options you pick while browsing.")
    p.set_defaults(parser=p)
    # Not required: with no command at all you get the prompt.
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="capture a stay from a URL")
    a.add_argument("url")
    a.add_argument("--price", type=float)
    a.add_argument("--currency")
    a.add_argument("--nights", type=int)
    a.add_argument("--adults", type=int)
    a.add_argument("--note")
    a.set_defaults(func=cmd_add)

    s = sub.add_parser("set", help="fill in or correct a field")
    s.add_argument("id")
    s.add_argument("--price", type=float)
    s.add_argument("--nights", type=int)
    s.add_argument("--adults", type=int)
    s.add_argument("--rooms", type=int,
                   help="units booked — on Booking.com an apartment is one room")
    s.add_argument("--bedrooms", type=int)
    s.add_argument("--bathrooms", type=int)
    s.add_argument("--sleeps", type=int)
    s.add_argument("--shares", type=int,
                   help=f"ways this stay's bill splits (default {config.SHARES})")
    s.add_argument("--score", type=float)
    s.add_argument("--score-scale", type=float, dest="score_scale",
                   help="what --score is out of, if not the site's usual (5 or 10)")
    s.add_argument("--look", type=int, choices=range(1, 6), metavar="1-5",
                   help="how it looks, out of 5 — the one thing no site can tell you")
    s.add_argument("--clean", type=int, choices=range(1, 6), metavar="1-5",
                   help="how clean it looks, out of 5; beats the site's sub-score")
    s.add_argument("--amenities", metavar="A,B,C",
                   help="comma-separated, e.g. 'parking,hot tub'; replaces the list")
    s.add_argument("--address", help="street address, for measuring the walk")
    s.add_argument("--summary",
                   help="the listing's own write-up; replaces what's there, "
                        "where pasting it at the prompt adds to it")
    s.add_argument("--currency")
    s.add_argument("--note")
    s.add_argument("--offer", help="which rate this is, e.g. '3 adults, free cancellation'")
    s.add_argument("--native-price", type=float, dest="native_price",
                   metavar="AMOUNT", help="price in the property's own currency")
    s.add_argument("--native-currency", dest="native_currency", default=None,
                   help=f"currency of --native-price (default {config.NATIVE_CURRENCY})")
    s.add_argument("--incl-tax", action="store_true", dest="incl_tax",
                   help="the quoted price already includes VAT")
    s.add_argument("--excl-tax", action="store_true", dest="excl_tax",
                   help="the quoted price excludes VAT; add it when comparing")
    s.set_defaults(func=cmd_set)

    l = sub.add_parser("list", help="show everything side by side")
    # argparse type-checks a default but not its membership in `choices`, so a
    # typo in the config would surface as a KeyError halfway through `list`.
    if config.DEFAULT_SORT not in SORTS:
        sys.exit(f"{config.PATH}: default_sort={config.DEFAULT_SORT!r} isn't one "
                 f"of: {', '.join(sorted(SORTS))}")
    # Warned about rather than fatal: a weight that can't fire is a settings
    # mistake, but it shouldn't stand between you and the four stays you've
    # already captured.
    for complaint in scoring.complaints():
        print(f"{config.where()}: {complaint}", file=sys.stderr)
    l.add_argument("--sort", choices=sorted(SORTS), default=config.DEFAULT_SORT)
    l.add_argument("--viable", action="store_true",
                   help="hide stays that fail a must-have in [filters]")
    l.add_argument("--no-facts", action="store_true",
                   help="drop the line under each stay summarising its write-up")
    # Defaulting to None rather than to config.LINKS so that "you didn't say"
    # and "you said 0" stay different answers — the second has to be able to
    # turn off a table that's configured to print them.
    l.add_argument("--links", type=int, default=None, metavar="N",
                   help="how many links to print under the table, from the top "
                        f"down (default {config.LINKS}; 0 for none)")
    l.add_argument("--rate", type=float, default=config.DEFAULT_RATE,
                   metavar=f"{config.QUOTE_CURRENCY}_PER_{config.BASE_CURRENCY}",
                   help=f"convert {config.BASE_CURRENCY} prices to "
                        f"{config.QUOTE_CURRENCY} at this rate")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("refresh", help="re-fetch prices for captured stays")
    r.add_argument("id", nargs="?")
    r.set_defaults(func=cmd_refresh)

    wk = sub.add_parser("walk", help="measure the walk to your destinations")
    wk.add_argument("id", nargs="?")
    wk.add_argument("--again", action="store_true",
                    help="re-measure stays already done, e.g. after editing "
                         "the destinations")
    wk.set_defaults(func=cmd_walk)

    gl = sub.add_parser("glean", help="re-read the write-ups and file what they say")
    gl.add_argument("id", nargs="?")
    gl.set_defaults(func=cmd_glean)

    pa = sub.add_parser("paste", help="accept a record from the browser bookmarklet")
    pa.add_argument("json", nargs="?", help="JSON payload (or pipe it on stdin)")
    pa.add_argument("--price", type=float)
    pa.add_argument("--nights", type=int)
    pa.add_argument("--adults", type=int)
    pa.set_defaults(func=cmd_paste)

    d = sub.add_parser("db", help="which set of stays to work in")
    d.add_argument("name", nargs="?",
                   help="switch to this database, and stay there until told "
                        "otherwise")
    d.add_argument("--new", action="store_true",
                   help="start it — required, so a typo can't silently open an "
                        "empty database instead of the one you meant")
    d.add_argument("--city", metavar="NAME",
                   help="which city it's in, which is where its landmarks, "
                        "destinations and rates come from; starts a config for "
                        "that city if there isn't one")
    d.set_defaults(func=cmd_db)

    rm = sub.add_parser("rm", help="remove one or more stays")
    rm.add_argument("ids", nargs="+")
    rm.set_defaults(func=cmd_rm)

    sh = sub.add_parser("show", help="print one stay in full")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true", help="dump the raw record instead")
    sh.add_argument("--rate", type=float, default=config.DEFAULT_RATE,
                    metavar=f"{config.QUOTE_CURRENCY}_PER_{config.BASE_CURRENCY}",
                    help=f"convert {config.BASE_CURRENCY} prices to "
                         f"{config.QUOTE_CURRENCY} at this rate")
    sh.set_defaults(func=cmd_show)

    u = sub.add_parser("url", help="print the link back to a listing")
    u.add_argument("id", nargs="?",
                   help="which stay; omit for all of them, one per line")
    u.add_argument("--sort", choices=sorted(SORTS), default=config.DEFAULT_SORT,
                   help="what order to print them in, when no id is given")
    u.set_defaults(func=cmd_url)

    w = sub.add_parser("watch", help="hold a prompt open for pasted links")
    w.add_argument("--rate", type=float, default=config.DEFAULT_RATE,
                   metavar=f"{config.QUOTE_CURRENCY}_PER_{config.BASE_CURRENCY}",
                   help=f"convert {config.BASE_CURRENCY} prices to "
                        f"{config.QUOTE_CURRENCY} at this rate")
    w.set_defaults(func=cmd_watch)

    p.set_defaults(commands=set(sub.choices))
    args = p.parse_args()
    return args.func(args) if getattr(args, "func", None) else cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
