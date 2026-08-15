# lodgingbuddy

Collate lodging options you pick while browsing, so comparing them doesn't mean
copy-pasting into a spreadsheet.

Paste a link and it captures what it can — name, place, dates, sleeps, bedrooms,
review score, and a price where the site will give one up. Fill the gaps with
`set`; `list` puts everything side by side on one comparable number.

Requires Python 3.11+ (for `tomllib`). No dependencies.

## Use

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

| | |
|---|---|
| sykescottages.co.uk | full scrape, schema.org JSON-LD |
| cottages.com, hoseasons.co.uk | Next.js `__NEXT_DATA__`; AWS WAF trips intermittently |
| booking.com | URL parameters only — the page itself is WAF-locked |

A Booking.com `/Share-` link redirects to a URL carrying dates and party size;
that first hop is the data path.

Another brand on a platform already supported is a `[[source]]` entry in
`config.toml`. A new platform needs an adapter in `sources.py`.

## Bookmarklet

For pages a script can't reach. `python3 build_bookmarklet.py` writes
`bookmarklet.txt`; save that as a bookmark's URL and click it on a listing. The
browser has already rendered the page and cleared the bot wall, so it reads
what `add` can't. It parses the DOM only — no network calls, no storage.

`refresh` never trades good data for bad: a site that answers with a bot wall
keeps what it already had.
