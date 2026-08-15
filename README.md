# lodgingbuddy

Collate lodging options you pick while browsing, so comparing them doesn't mean
copy-pasting into a spreadsheet.

Paste a link and it captures what it can — name, place, dates, sleeps, bedrooms,
review score, and a price where the site will give one up. Fill the gaps with
`set`; `list` puts everything side by side on one comparable number.

## Prerequisites

Python 3.11 or newer on your `PATH` (3.11 is where `tomllib` arrived). Nothing
to install — it's standard library only. Check with `python3 --version`.

## Use

Run it with no arguments and it holds a prompt open, so a browsing session is
one process rather than one per link. Paste a link — or the bookmarklet's
output — and press enter. Add a space and the total you're paying to record it:

```console
$ ./lodgingbuddy.py
lodgingbuddy — 4 stays on file. `help` for what this takes.
link> https://www.booking.com/Share-LjP6kp 480
  Strathisla Oban [booking.com] · 3 nts · 480 GBP all-in · 80/share/nt
link> ./lodgingbuddy.py paste '{"source":"sykes",...}' 582
  Heather Island View [sykes] · 3 nts · 582 GBP all-in · 97/share/nt
link> list
```

The total is optional — leave it off and the stay is captured with whatever the
site gave up. A number typed here is taken as the **final** price: quoted for
your dates, tax included, and authoritative over anything scraped. Anything
that isn't a link runs as a command. Ctrl-D quits.

Every command also works as a one-shot:

```console
$ ./lodgingbuddy.py add https://www.sykescottages.co.uk/cottage/Argyll-and-Bute-Kilbowie/...
$ ./lodgingbuddy.py set heather --price 582 --incl-tax
$ ./lodgingbuddy.py list
   Property             Source       Where                 Check-in        Nts  Slp  All-in   Share/nt  Score
─────────────────────────────────────────────────────────────────────────────────────────────────────────────
   Strathisla Oban      booking.com                        2026-10-09 Fri  3    3    380 GBP  63        —
   The Distillers Den   booking.com                        2026-10-12 Mon  2    3    364 GBP  91        —
   Heather Island View  sykes        Near Oban, Argyll an  2026-10-09 Fri  3    4    582 GBP  97        —
   Harbour View         cottages.co  Oban                  2026-10-09 Fri  3    3    671 GBP  112       4
```

| command | |
|---|---|
| *(none)* | hold a prompt open for pasted links |
| `add <url>` | capture a stay from a URL |
| `set <id> --price N …` | fill in or correct a field |
| `list [--sort K] [--rate N]` | everything side by side |
| `show <id> [--json]` | one stay in full, with the arithmetic shown |
| `refresh [id]` | re-fetch prices |
| `paste [json]` | take a record from the bookmarklet |
| `rm <id>…` | drop stays you've ruled out |

`<id>` is a property code or part of a name — `1198632`, `heather`.

## The numbers

Two adjustments make prices from different sites comparable.

**VAT.** UK consumer sites quote inclusive totals; Booking.com shows some
bookers the ex-VAT figure and adds tax at checkout — a 20% gap that would put
Booking flatteringly under everything else. Records carry `tax_included`, and
`list` grosses up the ones flagged pre-tax (`+`). Where no site said either way
the number is shown untouched and marked `?`, which is not the same claim.

**Nights and shares.** `Share/nt` is all-in ÷ nights ÷ shares, so a 2-night stay
compares with a 4-night one. Shares are not heads: three people splitting a bill
down the middle — a couple and a singleton — are two shares, and dividing by
three would describe a payment nobody makes. Set `shares` in `config.toml`, or
per stay with `set <id> --shares N`.

Where a property's own price is known it beats the OTA's converted one, which
has FX markup baked in. A "from" price is marked `~` and is not a quote for your
dates — click through and `set` the real total.

## Config

Everything tunable lives in `config.toml`: store path, VAT rate, currency pair,
the split, table shape, and which domains route to which adapter. Every key has
a built-in default, so a partial file — or none — still runs. Set
`LODGINGBUDDY_CONFIG` to use a different file.

## Sources

Verified against live pages on 2026-08-15. What each site gives up varies a
lot, so this is what actually came back rather than what the adapters hope for:

| site | how it's read | what came back |
|---|---|---|
| **sykescottages.co.uk** | schema.org JSON-LD, no bot wall | name, location, region, sleeps, bedrooms, bathrooms, review score, and a "from" price |
| **booking.com** — `/Share-` link | the first 301 carries the query string | property, dates, party size |
| **booking.com** — property URL | URL parameters | name, dates, nights, party, rooms |
| **cottages.com** | Next.js `__NEXT_DATA__` | name, location, review score, nights |
| **hoseasons.co.uk** | same platform as cottages.com | name and property code |

**No site hands over a usable total for your dates.** Sykes publishes a "from"
figure (shown `~`, and a 3-night stay advertised "from £1090" billed at £582).
Booking.com only reveals the real number once you click through to book. The
Awaze pair load pricing in a client-side widget that isn't in the page source.
So the workflow is: capture the stay, then type the total you see at checkout —
that number is taken as final.

The two Awaze sites sit behind an AWS WAF that trips intermittently; when it
does, the adapter falls back to what the URL alone says and keeps whatever it
already had rather than overwriting good data with nothing. Booking.com's
property pages are WAF-locked outright, which is why only the URL is parsed.

Another brand on a platform already supported is a `[[source]]` entry in
`config.toml`. A new platform needs an adapter in `sources.py`.

## Bookmarklet

A bookmarklet is an ordinary browser bookmark whose URL, instead of starting
with `https:`, starts with `javascript:`. Clicking it doesn't navigate anywhere
— the browser runs that code against the page you're already looking at. No
extension, no install, no permissions dialog. It's been in every browser for
about twenty-five years and is still the shortest path from "this page has data
I want" to "I have the data".

That sidesteps the whole scraping problem. A script fetching cottages.com gets
an AWS WAF challenge, because it *is* a robot. Your browser gets the page,
because it is a browser that has already solved the challenge, already run the
site's JavaScript, and already has your dates and currency applied. The data is
sitting rendered in front of you; this just picks it up.

It also crosses machines, which is why it exists here: the browser can be on
your laptop while the script runs on a headless box over SSH. The clipboard is
the bridge. (A Chrome extension can't do that — Native Messaging only talks to
the same machine.)

**Build and install.**

```console
$ python3 build_bookmarklet.py
source    11,209 bytes
stripped   8,011 bytes
encoded   14,990 bytes  ->  bookmarklet.txt
```

Create a new bookmark, and paste the contents of `bookmarklet.txt` into its
**URL** field — not the name. Keep it on the bookmarks bar. Note that most
browsers strip `javascript:` if you paste it into the address bar directly, so
it has to go in via the bookmark editor. Rebuild after editing
`bookmarklet.js`, and re-paste.

**Use.** Open a listing, click the bookmark. It reads the page's schema.org
JSON-LD, its `__NEXT_DATA__` blob, and failing those the visible text, then
copies a ready-to-run `./lodgingbuddy.py paste '{...}'` command to your
clipboard and tells you what it found. Paste that at the prompt, with the total
after a space if you have it.

It only reads the DOM — no network calls, no cookies, no storage — and the
extractor is split from the browser plumbing so it can be tested against saved
HTML under node (`test_bookmarklet.js`).

Where a page shows several plausible amounts and none is clearly the total, it
reports them as candidates rather than guessing. Booking.com only reveals the
real total once you click through to book, so that number is worth typing in by
hand — it's taken as final.

`refresh` never trades good data for bad: a site that answers with a bot wall
keeps what it already had.
