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
import shlex
import sys
import textwrap

import config
import database
import proximity
import scoring
import sources


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


# ──────────────────────────────── storage ──────────────────────────────────

def load() -> list[dict]:
    # Asked for on every read rather than resolved once at import, so that
    # switching databases at the prompt takes effect on the next line rather
    # than the next process.
    path = database.path()
    if not path.exists():
        return []
    return json.loads(path.read_text())


def save(stays: list[dict]) -> None:
    database.path().write_text(json.dumps(stays, indent=2) + "\n")


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
    save(stays)

    amount, cur, estimated = all_in(rec)
    print(f"Updated {rec['name']}")
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


def _pct(value: float | None) -> str:
    return f"{value:.0f}%" if value is not None else "—"


# Each column as (header, how to render one row). Which of them `list` prints,
# and in what order, is `columns` in config.toml — the table got wide enough
# that "all of them" stopped being a sensible default for every trip.
COLUMNS = {
    "name":     ("Property", lambda r, ctx: (r.get("name") or "?")[:config.NAME_WIDTH]),
    "source":   ("Source", lambda r, ctx: (r.get("source") or "")[:config.SOURCE_WIDTH]),
    "where":    ("Where", lambda r, ctx: (r.get("location") or r.get("region") or "")[:config.WHERE_WIDTH]),
    "checkin":  ("Check-in", lambda r, ctx: f"{r['checkin']} {weekday(r['checkin'])}" if r.get("checkin") else ""),
    "nts":      ("Nts", lambda r, ctx: str(r.get("nights") or "")),
    "slp":      ("Slp", lambda r, ctx: str(r.get("sleeps") or r.get("adults") or "")),
    "space":    ("Space", lambda r, ctx: " ".join(
                    x for x in (f"{r['bedrooms']}br" if r.get("bedrooms") else
                                (f"{r['rooms']}rm" if r.get("rooms") else None),
                                f"{r['bathrooms']:g}ba" if r.get("bathrooms") else None) if x)),
    "all_in":   ("All-in", lambda r, ctx: ctx["money"](r)),
    "share_nt": (None, lambda r, ctx: (f"{per_share_night(r, ctx['rate']):,.0f}"
                                       if per_share_night(r, ctx["rate"]) else "—")),
    # Normalised, so one column can hold a 5-star site and a 10-point one.
    "score":    ("Guest", lambda r, ctx: _pct(scoring.guest_score(r))),
    "reviews":  ("Revs", lambda r, ctx: str(r.get("reviews") or "—")),
    "clean":    ("Clean", lambda r, ctx: _pct(scoring.cleanliness(r))),
    "look":     ("Look", lambda r, ctx: _pct(scoring.look(r))),
    "walk":     ("Walk", lambda r, ctx: (f"{scoring.walk_minutes(r):.0f}m"
                                         if scoring.walk_minutes(r) is not None else "—")),
    "points":   ("Pts", lambda r, ctx: f"{ctx['score'](r).points:g}"),
    "value":    ("Value", lambda r, ctx: (f"{ctx['score'](r).value:g}"
                                          if ctx["score"](r).value is not None else "—")),
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

    def money(rec: dict) -> str:
        amount, cur, tax = all_in(rec)
        amount, cur = converted(amount, cur, rate)
        if not amount:
            return "—"
        return (from_mark(rec, tax)
                + f"{amount:,.0f} {cur}".strip() + config.TAX_MARKS.get(tax, ""))

    ctx = {"rate": rate, "money": money, "score": lambda r: marks[key_of(r)]}

    chosen = [c for c in config.COLUMNS if c in COLUMNS]
    if unknown := [c for c in config.COLUMNS if c not in COLUMNS]:
        print(f"{config.PATH}: no such column {', '.join(unknown)} — "
              f"have: {', '.join(COLUMNS)}", file=sys.stderr)

    rows = [[leading_mark(rec, gates[key_of(rec)])]
            + [COLUMNS[c][1](rec, ctx) for c in chosen] for rec in stays]
    # The per-share column is the one you compare on, so it says whose money it
    # is — a bare "P/p/nt" invited the assumption that it was split three ways.
    headers = [""] + [COLUMNS[c][0] or f"{config.SHARE_LABEL.title()}/nt"
                      for c in chosen]

    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    sep = config.COLUMN_GAP
    print(sep.join(h.ljust(w) for h, w in zip(headers, widths)))
    print(config.RULE_CHAR * (sum(widths) + len(sep) * (len(widths) - 1)))
    for row in rows:
        print(sep.join(c.ljust(w) for c, w in zip(row, widths)))

    footnotes(stays, marks, gates, seen_tax, seen_cur, rate)
    return 0


def footnotes(stays, marks, gates, seen_tax, seen_cur, rate) -> None:
    """What the table couldn't say in a column.

    Every mark in it gets explained here, and every explanation names the
    command that clears it — a glyph you have to go and look up is worse than
    no glyph.
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
    if "added" in seen_tax:
        print(f"\n{config.TAX_MARKS.get('added', '')}  VAT added by us at "
              f"{config.VAT_RATE:.0%} — the site quoted a pre-tax price.")
    if "computed" in seen_tax:
        print(f"{config.TAX_MARKS.get('computed', '')}  the page stated its tax "
              "rates and fees, so that is the arithmetic done rather than a flat "
              "VAT estimate. `show <id>` names the rates.")
    if "unknown" in seen_tax:
        print(f"{config.TAX_MARKS.get('unknown', '')}  tax status unknown; shown "
              "as quoted. Mark it with `set <id> --incl-tax` or `--excl-tax`.")
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
    wanted = config.destinations_for(database.current())
    if not wanted:
        elsewhere = " (the ones in it name other databases)" if config.DESTINATIONS else ""
        print(f"No destinations for {database.current()} in config.toml{elsewhere}, "
              f"so there's nothing to measure against.\nAdd a [[destination]] "
              f"with a label and an address, and `db = \"{database.current()}\"` "
              f"if it belongs to this leg alone.", file=sys.stderr)
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
    stays = [s for s in stays if key_of(s) != key_of(rec)]
    stays.append(rec)
    save(stays)

    print(f"{rec['name'] or '?'}  [{rec['source']}]")
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
        # The pointer moved, but this run didn't. Saying so beats letting the
        # next command look like it ignored you. On stdout, and after the line
        # it qualifies — a caveat that turns up first reads as a failure.
        if pinned := database.forced():
            print(f"  {database.ENV}={pinned} is set though, so that's what "
                  f"still gets read until you unset it.")
        return 0

    if args.new:
        print("`db <name> --new` needs a name.", file=sys.stderr)
        return 1

    here = database.current()
    width = max(len(n) for n in database.names())
    for name in database.names():
        pinned = database.ENV if name == here and database.forced() else ""
        print(f"{'→' if name == here else ' '} {name.ljust(width)}  {tally(name)}"
              + (f"   ({pinned}, for this run only)" if pinned else ""))
    print(f"\nIn {config.STORE_DIR}. `db <name>` switches and remembers it; "
          f"`db <name> --new` starts one.")
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
    row("Walk", proximity.describe(rec))
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

  Commands: add, paste, list, show, set, walk, refresh, rm, db.
  `set <id> --look 4 --clean 5` marks the things no site can tell you.
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
        print(f"{config.PATH}: {complaint}", file=sys.stderr)
    l.add_argument("--sort", choices=sorted(SORTS), default=config.DEFAULT_SORT)
    l.add_argument("--viable", action="store_true",
                   help="hide stays that fail a must-have in [filters]")
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
