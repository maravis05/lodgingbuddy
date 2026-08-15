#!/usr/bin/env python3
"""
lodgingbuddy — collate lodging you've picked while browsing, so comparing them
doesn't mean copy-pasting into a spreadsheet.

Paste a link from any supported site and it captures what it can: name, place,
dates, sleeps, bedrooms, review score, and price where the site will give one
up. Anything missing you fill in with `set`. Then `list` puts them side by side
and sorts them however you like.

    ./lodgingbuddy.py add https://www.sykescottages.co.uk/cottage/...
    ./lodgingbuddy.py add https://www.booking.com/Share-LjP6kp --price 480
    ./lodgingbuddy.py set the-distillers-den --price 480 --note "free whisky"
    ./lodgingbuddy.py list --sort share
    ./lodgingbuddy.py refresh

Sites: Booking.com, Sykes Cottages, cottages.com, Hoseasons.

Settings — where the store lives, the VAT rate, the currency pair, the table's
shape, which domains route where — are in config.toml.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shlex
import sys

import config
import sources


# ──────────────────────────────── storage ──────────────────────────────────

def load() -> list[dict]:
    if not config.STORE.exists():
        return []
    return json.loads(config.STORE.read_text())


def save(stays: list[dict]) -> None:
    config.STORE.write_text(json.dumps(stays, indent=2) + "\n")


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
        return price * (1 + (rec.get("vat_rate") or config.VAT_RATE)), cur, "added"
    if included is None:
        # Nobody has told us. Report the number untouched and say so — that is
        # a different thing from having added tax, and must not look the same.
        return price, cur, "unknown"
    return price, cur, "inclusive"


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

    Read fresh from config every time rather than stamped onto the record at
    capture, so editing config.toml re-costs the whole list. A stay that splits
    differently from the rest of the trip carries its own `shares`.
    """
    return rec.get("shares") or config.SHARES


def heads_of(rec: dict) -> int | None:
    """How many people are staying: your party, or capacity if we weren't told.

    Your party, not the property's capacity — a cottage that sleeps six costs
    the same whether or not you fill it.
    """
    return rec.get("adults") or rec.get("sleeps")


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
                  "shares", "bedrooms", "bathrooms", "sleeps"):
        val = getattr(args, field, None)
        if val is not None:
            rec[field] = val
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
    return 0


SORTS = {
    "share": lambda r, rate: (per_share_night(r, rate) is None, per_share_night(r, rate) or 0),
    # The old name for the same idea, kept so muscle memory and an unedited
    # config.toml both still work.
    "pppn": lambda r, rate: (per_share_night(r, rate) is None, per_share_night(r, rate) or 0),
    "price": lambda r, rate: (r.get("price") is None, r.get("price") or 0),
    "score": lambda r, rate: (r.get("score") is None, -(r.get("score") or 0)),
    "sleeps": lambda r, rate: (r.get("sleeps") is None, -(r.get("sleeps") or 0)),
    "checkin": lambda r, rate: (r.get("checkin") or "9999",),
    "name": lambda r, rate: ((r.get("name") or "").lower(),),
}


def cmd_list(args) -> int:
    stays = load()
    if not stays:
        print("Nothing captured yet.\n  ./lodgingbuddy.py add <url>")
        return 0

    rate = args.rate
    stays.sort(key=lambda r: SORTS[args.sort](r, rate))

    rows = []
    seen_tax = set()
    for rec in stays:
        amount, cur, tax = all_in(rec)
        amount, cur = converted(amount, cur, rate)
        psn = per_share_night(rec, rate)
        if amount:
            seen_tax.add(tax)
        rows.append([
            config.STATUS_MARKS.get(rec.get("status"), " "),
            (rec.get("name") or "?")[:config.NAME_WIDTH],
            (rec.get("source") or "")[:config.SOURCE_WIDTH],
            (rec.get("location") or rec.get("region") or "")[:config.WHERE_WIDTH],
            f"{rec['checkin']} {weekday(rec['checkin'])}" if rec.get("checkin") else "",
            str(rec.get("nights") or ""),
            str(rec.get("sleeps") or rec.get("adults") or ""),
            (("~" if rec.get("price_basis") == "indicative" else "")
             + f"{amount:,.0f} {cur}".strip()
             + config.TAX_MARKS.get(tax, "")) if amount else "—",
            f"{psn:,.0f}" if psn else "—",
            f"{rec['score']:g}" if rec.get("score") else "—",
        ])

    # The per-share column is the one you compare on, so it says whose money it
    # is — a bare "P/p/nt" invited the assumption that it was split three ways.
    headers = ["", "Property", "Source", "Where", "Check-in", "Nts",
               "Slp", "All-in", f"{config.SHARE_LABEL.title()}/nt", "Score"]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    sep = config.COLUMN_GAP
    print(sep.join(h.ljust(w) for h, w in zip(headers, widths)))
    print(config.RULE_CHAR * (sum(widths) + len(sep) * (len(widths) - 1)))
    for row in rows:
        print(sep.join(c.ljust(w) for c, w in zip(row, widths)))

    if any(r.get("price_basis") == "indicative" for r in stays):
        print("\n~  a \"from\" price, not a quote for these dates — click through "
              "and set the real total with `set <id> --price`.")
    if "added" in seen_tax:
        print(f"\n{config.TAX_MARKS.get('added', '')}  VAT added by us at "
              f"{config.VAT_RATE:.0%} — the site quoted a pre-tax price.")
    if "unknown" in seen_tax:
        print(f"{config.TAX_MARKS.get('unknown', '')}  tax status unknown; shown "
              "as quoted. Mark it with `set <id> --incl-tax` or `--excl-tax`.")
    if rate:
        print(f"{config.BASE_CURRENCY} converted at {rate} "
              f"{config.QUOTE_CURRENCY}/{config.BASE_CURRENCY}.")
    pending = [r["name"] for r in stays if r.get("status") != sources.OK]
    if pending:
        print(f"·/! needs a price: {', '.join(p or '?' for p in pending)}")
    return 0


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
        # Keep anything typed by hand; only fill gaps and refresh price.
        for field, value in fresh.items():
            if value is not None and (rec.get(field) is None or field == "price"):
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
            print(f"    ./lodgingbuddy.py set {rec['code']} --price <total> --incl-tax")
    elif candidates:
        # Several plausible amounts on the page and no way to tell which is the
        # total — offering the list beats guessing wrong.
        print("  couldn't tell which amount is the total. Candidates:")
        print("    " + ", ".join(f"{c:g}" for c in candidates))
        print(f"  set it with:  ./lodgingbuddy.py set {key_of(rec)} --price <n>")
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

    if not (rec["name"] or rec["code"]):
        # Search and region pages parse fine but describe no single property.
        return None, candidates, ("That page doesn't identify one property — it "
                                  "looks like a search or landing page. Open a "
                                  "specific listing and click again.")

    rec["captured_at"] = dt.datetime.now().isoformat(timespec="seconds")
    rec["status"] = sources.OK if rec["price"] else sources.NEEDS_PRICE
    return rec, candidates, None


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
             f"{rec['bathrooms']} bath" if rec.get("bathrooms") else None]
    row("Property", ", ".join(s for s in shape if s))
    if rec.get("score"):
        n = f" from {rec['reviews']} reviews" if rec.get("reviews") else ""
        row("Score", f"{rec['score']:g}{n}")
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

    row("Note", rec.get("note"))
    row("Status", rec.get("status"))
    row("Captured", rec.get("captured_at"))
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

def as_number(text: str | None) -> float | None:
    """A typed total, or None if that isn't what this is.

    Tolerates the shapes a price arrives in when you've just copied one off a
    booking page: 1,234.50 and £480 are both numbers.
    """
    text = (text or "").strip().replace(",", "").lstrip("£$€")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def parse_entry(line: str) -> tuple[str, object, float | None] | None:
    """Read one line of the prompt into (kind, payload, total), or None.

    Three things get pasted here and all of them are welcome: a bare URL, raw
    JSON, or the whole `./lodgingbuddy.py paste '{...}'` command line the
    bookmarklet puts on the clipboard. Any of them may be followed by a space
    and the total you're paying.
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
        return ("json", obj, as_number(line[end:]))

    if line.startswith("http"):
        url, _, tail = line.partition(" ")
        return ("url", url, as_number(tail))

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
                return ("json", json.loads(token), as_number(tail))
            except json.JSONDecodeError:
                return None
        if token.startswith("http"):
            return ("url", token, as_number(tail))
    return None


def apply_total(rec: dict, total: float) -> None:
    """Record a total typed at the prompt, and let it win.

    You typed it off the booking page, so it is the final number: quoted for
    these dates and with tax already in it. It also clears any scraped native
    price, which `all_in` would otherwise prefer — leaving that in place would
    show you a number you didn't type and can't account for.
    """
    rec["price"] = total
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
        soft = "~" if rec.get("price_basis") == "indicative" else ""
        bits.append(f"{soft}{amount:,.0f} {cur}".strip()
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
    elif not amount:
        out.append("    no price — paste it again with the total after a space")
    elif rec.get("price_basis") == "indicative":
        out.append('    ~ is a "from" price, not a quote — paste again with the'
                   " total after a space")
    return "\n".join(out)


def capture_entry(kind: str, payload, total: float | None, rate: float | None) -> None:
    """Fold one pasted entry into the store and say what happened."""
    candidates: list = []
    if kind == "url":
        try:
            rec = sources.capture(payload)
        except (ValueError, OSError) as exc:
            print(f"  {exc}")
            return
    else:
        rec, candidates, problem = record_from_payload(dict(payload))
        if problem:
            print(f"  {problem}")
            return

    stays = load()
    rec = merge_over(find_exact(stays, key_of(rec)), rec)
    # After the merge, so a typed total beats anything the merge restored.
    if total is not None:
        apply_total(rec, total)
    elif rec.get("price") or rec.get("native_price"):
        rec["status"] = sources.OK
    stays = [s for s in stays if key_of(s) != key_of(rec)]
    stays.append(rec)
    save(stays)
    print(confirm(rec, candidates, rate))


PROMPT_HELP = """\
  Paste a link, or the bookmarklet's output, and press enter.
  Add a space and the total you're paying to record it:

      https://www.booking.com/Share-abc123 480
      ./lodgingbuddy.py paste '{...}' 582

  A total typed here is taken as the final price, tax included.
  Anything else runs as a command — list, show, set, rm, refresh.
  Ctrl-D quits, Ctrl-C clears the line."""


def cmd_watch(args) -> int:
    """Hold a prompt open so a browsing session isn't one process per link."""
    try:
        import readline  # noqa: F401 — arrow keys and history, where available
    except ImportError:
        pass

    # Bare `./lodgingbuddy.py` parses no subcommand, so there is no --rate.
    rate = getattr(args, "rate", None) or config.DEFAULT_RATE
    stays = load()
    print(f"lodgingbuddy — {len(stays)} stays on file. `help` for what this takes.")
    while True:
        try:
            line = input("link> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue

        if not line:
            continue
        if line in ("quit", "exit", "q"):
            return 0
        if line in ("help", "h", "?"):
            print(PROMPT_HELP)
            continue

        if entry := parse_entry(line):
            capture_entry(*entry, rate)
            continue

        # A paste that didn't parse should say so, rather than being reported as
        # an unknown command — a truncated clipboard is the likely cause.
        if line.startswith(("{", "http")) or "lodgingbuddy.py" in line:
            print("  that looks like a paste but didn't parse — copy it again, "
                  "or check the whole line arrived")
            continue

        # Not a link, so treat it as a command — but only if it names one. A
        # mistyped paste shouldn't answer with a screenful of argparse usage.
        head = line.split()[0]
        if head not in getattr(args, "commands", ()):
            print(f"  not a link, and no command called {head!r} — try `help`")
            continue

        # argparse exits on a bad command line, which must not take the prompt
        # down with it.
        try:
            sub_args = args.parser.parse_args(shlex.split(line))
        except (SystemExit, ValueError):
            continue
        if func := getattr(sub_args, "func", None):
            try:
                func(sub_args)
            except (ValueError, OSError, KeyError) as exc:
                print(f"  {exc}")


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
    l.add_argument("--sort", choices=sorted(SORTS), default=config.DEFAULT_SORT)
    l.add_argument("--rate", type=float, default=config.DEFAULT_RATE,
                   metavar=f"{config.QUOTE_CURRENCY}_PER_{config.BASE_CURRENCY}",
                   help=f"convert {config.BASE_CURRENCY} prices to "
                        f"{config.QUOTE_CURRENCY} at this rate")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("refresh", help="re-fetch prices for captured stays")
    r.add_argument("id", nargs="?")
    r.set_defaults(func=cmd_refresh)

    pa = sub.add_parser("paste", help="accept a record from the browser bookmarklet")
    pa.add_argument("json", nargs="?", help="JSON payload (or pipe it on stdin)")
    pa.add_argument("--price", type=float)
    pa.add_argument("--nights", type=int)
    pa.add_argument("--adults", type=int)
    pa.set_defaults(func=cmd_paste)

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
