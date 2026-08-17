# lodgingbuddy

A command-line tool for collecting lodging listings you find while browsing and
putting them in one comparable table. You paste links (or bookmarklet output);
it stores a record per stay, normalises the prices, scores them, and prints a
sorted table.

Python 3.11+ (`tomllib`), standard library only. No install step, no
dependencies. Node is needed only to run the bookmarklet's tests.

```
python3 lodgingbuddy.py                 # interactive prompt
python3 lodgingbuddy.py add <url>
python3 lodgingbuddy.py set <id> --price 480 --incl-tax
python3 lodgingbuddy.py list --sort value
```

On Windows use `python` instead of `python3`. Always name the interpreter;
running the script by bare path opens it in whatever owns `.py`.

## Commands

| Command | What it does |
| --- | --- |
| `add <url>` | Fetch and store one stay. `--price --currency --nights --adults --note` |
| `paste [json]` | Store a record from the bookmarklet; reads stdin if no argument |
| `set <id> ...` | Fill in or correct fields (below) |
| `list` | The table. `--sort --viable --no-facts --links --rate` |
| `show <id>` | One stay in full. `--json` for the raw record, `--rate` to convert |
| `url [id]` | The listing's link, on a line of its own. No id gives all of them, in sort order |
| `refresh [id]` | Re-fetch listing pages and update prices |
| `walk [id]` | Measure walking time to your destinations. `--again` re-measures |
| `glean [id]` | Re-read stored write-ups and file what they say |
| `rm <id>...` | Delete stays |
| `db [name]` | Show or switch the active database. `--new` creates one and asks which city it's in; `--city <name>` sets that without asking |
| `watch` | The prompt; also what runs when no command is given |

`<id>` matches, in order: the record key (`source:code`), a substring of the
code, then a substring of the name.

`set` takes `--price --drop-price --native-price --native-currency --currency
--incl-tax --excl-tax --nights --adults --rooms --shares --bedrooms
--bathrooms --sleeps --score --score-scale --look --clean --amenities
--address --summary --offer --note`.

A `--price` you type is taken as the bill — quoted for these dates, everything
in it — because that is what typing one at a checkout page is for.
`--excl-tax` is there for the day you type a pre-tax figure on purpose, and
`--drop-price` forgets one and hands the stay back to what was captured.

`list --sort` takes `value` (default), `share`, `price`, `score`, `sleeps`,
`walk`, `points`, `checkin`, `name`.

## Getting back to a listing

Every stay keeps the URL it was captured from, and that URL is the one you were
looking at — for Booking.com that means the dates, the party size and the
identifier of the room block whose price ended up in the table. Opening it puts
you back on the same quote rather than on the property's front page for
whenever today is.

`list` ends with the links to its top few, taken from the top of whatever order
it just sorted into, so `--sort share` and `--sort walk` end with different
ones:

```
Top 3 by value:
  1. Peaceful 3 Bedroom Townhouse Edinburgh
     https://www.booking.com/hotel/gb/peaceful-3-bedroom-townhouse-…
  2. 30 Aberlady
     https://www.booking.com/hotel/gb/30-aberlady.html?…
  3. Km Granton Apartment
     https://www.booking.com/hotel/gb/km-granton-apartment.html?…
```

How many is `links` in `[display]` (default 3), or `list --links N` for one
run. `--links 0` turns them off. They print unwrapped, because a URL folded to
the width of the terminal is one you can't click, and a trimmed one opens a
different quote.

`url <id>` prints any single stay's link and nothing else, which is the form
you can hand to something:

```
python3 lodgingbuddy.py url aberlady | xargs xdg-open
python3 lodgingbuddy.py url aberlady | xclip -selection clipboard
```

With no id it prints them all, one per line, in the order `list` would have put
them — `--sort` picks which order. Both forms exit non-zero and say why if the
id matches nothing, or if the stay it matched was entered by hand and has no
URL to give.

## The prompt

Running with no command holds a prompt open, named after the current database.
It accepts, one per line:

- a listing URL, optionally followed by a total: `https://… 480` or `… £582`
- the bookmarklet's JSON, same trailing-total rule
- a bare total on its own line, which applies to the stay captured last
- anything else with a space in it, which is appended to that stay's summary
- any of the commands above
- `help`, `quit`

A total typed at the prompt is treated as final: quoted for those dates, tax
included, and it clears any scraped native price. A currency sign or code
(`£582`, `$789`, `582 GBP`) sets the currency; a bare number leaves the site's
currency in place. A blank line ends a run of pasted summary text.

## Sources

Four sites have adapters, and they yield different amounts:

| Site | Parser | What comes back |
| --- | --- | --- |
| sykescottages.co.uk | `sykes` | Full scrape from schema.org JSON-LD: name, coordinates, occupancy, bedrooms, beds, amenities, rating, price |
| cottages.com | `awaze` | `__NEXT_DATA__`, when the AWS WAF lets the page through; URL query otherwise |
| hoseasons.co.uk | `awaze` | Same platform, same parser |
| booking.com | `booking` | URL parameters only — the page is WAF-locked. Dates, party size, and the search's price block if the link carries one |

`add` on a `/Share-` link follows exactly one redirect, which is where
Booking.com's dates and party size live; the second redirect strips them.

A record that comes back without a price is stored with status `needs_price`;
one whose page was refused is `blocked`. Neither is an error — type the price
in with `set` or at the prompt.

## Bookmarklet

The browser has already rendered the page and passed any bot wall, so a
bookmarklet reaches data the fetchers cannot — Booking.com's map coordinates,
review sub-scores, room-by-room bed lists, stated tax rates, and the write-up.

```
python3 build_bookmarklet.py
```

writes three untracked files: `bookmarklet.txt` (the bare `javascript:` URL),
`bookmarklet.html` (a Netscape bookmark file to import), and
`install-bookmarklet.html` (a page with a draggable link). Open the install
page, drag the button to the bookmarks bar, then click it on a listing. It
copies a JSON record to the clipboard; paste that at the prompt or into
`paste`.

The bookmarklet recognises the same four hosts. `extract()` is separated from
the DOM accessors so it can run under Node:

```
node test_bookmarklet.js                        # unit checks
node test_bookmarklet.js saved.html <url>       # extract from a saved page
```

Amenities cross the boundary in the site's own words; the alias table that
turns them into slugs lives only in `sources.py`.

## Prices and tax

Sites quote differently, so raw prices are not comparable. Each `[[source]]`
declares `tax_included`, and `all_in()` resolves a stay to one figure:

| `tax_included` | Result | Mark |
| --- | --- | --- |
| true | the price as quoted | none |
| false, page stated its rates | rates applied in order, compounding, over the price less any included fee | `=` |
| false, page stated no charges (`taxes: []`) | the price as quoted — it is the whole bill | `=` |
| false, nothing read (`taxes: null`) | `price × (1 + vat_rate)` | `+` |
| unset | price shown untouched | `?` |

The two `false` middle rows are one distinction doing a lot of work. An empty
`taxes` means the page was read and listed nothing excluded, which plenty of
small properties genuinely do — they are under the VAT threshold. A missing
`taxes` means nobody could tell, and only then is 20% assumed. Reading them the
same way put 20% onto stays that get charged none.

A `~` marks a price flagged `indicative` — a "from" headline rather than a
quote for the dates — unless the total was settled from what the page stated,
in which case it is exact and the mark is dropped. A total you typed off a
checkout page outranks everything: it is the bill. Below that, a `native_price`
(the property's own currency, before an OTA's conversion) is preferred over the
display price.

From the all-in figure: `per_share_total` = all-in ÷ shares, and `share_nt` =
that ÷ nights. Shares are billing units, not people — a couple and a
singleton are two shares. Set the trip default in `[split]`, override per stay
with `set <id> --shares`.

`--rate` overrides the rate for one run.

## The exchange rate

Prices are held in `[currency] base` — what the property itself bills in — and
read in `quote`. The rate between them is not configured; it is read off the
stays, as the median of every one that holds the same money in both currencies:

- a capture holding the listing price off the link **and** the same listing
  price as the page rendered it, which is what the bookmarklet records; or
- a checkout total you typed **and** the total the listing price comes to once
  the page's own stated charges are applied.

The second only counts where the page stated its charges (including stating
none). Dividing a checkout total by a *guessed* all-in gives a rate that is 20%
out and perfectly steady, which is the kind of wrong nobody notices — so a stay
that can't supply both halves supplies no rate.

Everything lands on the base scale before `value` or the sort touches it, so a
stay filed in dollars is not ranked against stays filed in pounds by dividing
one by the other. `[currency] default_rate` stands in for a database holding no
such pair, and `list --rate` overrides both.

## Scoring

`points` measures desirability and excludes price entirely. `value` is
`points ÷ (share_nt ÷ price_unit)`. Both are recomputed on every read, so
editing weights re-ranks everything already captured.

Two rule shapes, both in `config.toml`:

- **tiers** — a quantity falls into the highest band it qualifies for and
  scores that band. Ratings are normalised to 0–100 first using the source's
  `score_scale`, so 4/5 and 8.6/10 compare. Factors: `walk_minutes`,
  `guest_score`, `reviews`, `cleanliness`, `look`, `spare_beds`,
  `shares_without_a_door`. Each is read by a function in `scoring.FACTORS`;
  adding a tier means adding a reader there too.
- **bonuses** — flat points, positive or negative, for a named fact being
  true. The name may be an amenity slug, a trait, a `*_nearby` slug, or a
  property kind.

A factor with no data scores zero and is named in the breakdown, so an
unreviewed stay is distinguishable from a badly reviewed one. `show` prints
the whole sum.

`[filters]` holds gates rather than preferences: `room_per_share`,
`max_walk_minutes`, and a `require` list of amenity slugs. A stay that fails
one is kept and marked `✗`, never dropped; one that can't be judged is marked
`?`. `list --viable` hides the failures.

At startup `scoring.complaints()` reports weights that can never fire — a tier
naming a factor that doesn't exist, a bonus naming a slug nothing produces, a
destination missing a label or a location.

## Walking distances

`walk` measures minutes on foot from each stay to each `[[destination]]`, one
call per stay. Providers:

- `osrm` (default) — `routing.openstreetmap.de`, `routed-foot` profile, no key
  or account. Addresses are geocoded through Nominatim first and cached for the
  run; stays with coordinates skip that. Both services are rate-limited to one
  call per second. Not the `router.project-osrm.org` demo server, which answers
  foot requests with car timings.
- `google` — Distance Matrix, key read from `$GOOGLE_MAPS_API_KEY`.

Destinations carry a `weight`; the walk tier scores the weighted mean over the
destinations actually measured. A destination may name a `db`, in which case it
applies only to that database — one config can hold two trips.

This is the only feature that sends a stay's location to a third party.
`[maps] enabled = false` disables it completely; every other command still
works. `on_capture` measures each new stay as it arrives (only when it has an
address or coordinates, and never twice). `trust_claimed_walk` fills remaining
gaps with walking times the listing itself claimed, marked `≈`; a measured
figure is never replaced.

## Reading the write-up

`summary.py` parses the listing's prose, which is present far more often than
the structured fields are. It extracts property kind, bedroom and bathroom
counts, amenity slugs, traits (`soundproofed`, `free_parking`, `visitor_levy`,
`ground_floor`, …), neighbourhood facts (`food_nearby`, `transport_nearby`, …),
and claimed walking times to the landmarks the active database's config names.

It runs on every capture and on `set --summary`; `glean` re-runs it over
stored records. It only fills holes — a scraped field always wins — and
records what it filled in `gleaned`. Where the prose contradicts the record,
nothing is overwritten and the disagreement is printed.

Claimed distances are checked against geometry. A landmark given
`latitude`/`longitude` in the config is checked against that. For the rest,
`locate()` trilaterates a position from every stay that quotes a distance to
the landmark, fitting the pavement-to-straight-line ratio from the corpus
itself. Either way, claims impossible by an order of magnitude are rejected —
in the bundled Edinburgh set, an aparthotel putting the castle a 1-minute walk
from a building 1.25 km away. Upper bounds ("less than 0.6 mi"), drives, and
anything over an hour are excluded from the walk figures.

Give coordinates only for landmarks that are a point. A kilometre of street
pinned at its midpoint makes an honest claim from one end of it measure wrong;
fitting handles those better, because what it fits is the stretch people mean.

## Storage

Stays are a JSON list, one file per database, in the folder holding
`storage.file`. `stays.db.json` is the database named `stays`; any other
`*.db.json` beside it is another. `db <name>` switches and remembers the choice
in `.lodgingbuddy-db`; `LODGINGBUDDY_DB` overrides it for one run without moving
the pointer. A database also keeps `<name>.dbconf.toml` beside its
`<name>.db.json`, naming the city it is in — see Cities and trips below.

Each of the three kinds of file says which kind it is in its own name, so a
database named after the city it is in is still three files you can tell apart:
`edinburgh.db.json`, `edinburgh.dbconf.toml`, `cities/edinburgh.cityconf.toml`.
Files left under the older plain `.json` and `.toml` names are renamed on the
next run, and the tool says what it moved.

The record schema is `sources.blank_record()` — identity, location, dates and
party, price and tax, property shape and beds, amenities and traits, ratings
and sub-scores, measured and claimed walk times, your own marks, and the
verbatim summary. Adapters fill what they can and leave the rest `None`;
nothing guesses. Re-capturing a stay merges over the existing record: a
confirmed price is never replaced by a "from" price, and no field is cleared.

`LODGINGBUDDY_CONFIG` points at a config file other than `config.toml` beside
the script.

## Configuration

Four layers, each merged over the one before: `config.DEFAULTS` in `config.py`,
then `config.toml`, then the city the active database is in
(`cities/<city>.cityconf.toml`), then the database's own `<db>.dbconf.toml`.
The tool runs with
no config file at all, and a file setting three keys overrides three keys.
Tables merge key by key; lists (sources, destinations, landmarks) replace
wholesale. `[storage]` is read only from `config.toml`, since it says where the
other files live.

| Section | Holds |
| --- | --- |
| `[storage]` | `file` (names the default database and its folder) and `cities` (where city configs live) |
| `[http]` | User agent, timeout, accept-language for listing fetches |
| `[tax]` | `vat_rate`, used when a source quotes ex-tax and the page states nothing |
| `[split]` | Default number of shares and what to call one |
| `[currency]` | Base and quote codes, default conversion rate, default native currency |
| `[display]` | Columns, widths, sort, glyphs, whether the per-stay facts lines print, and how many links follow the table |
| `[booking]` | Sanity bounds on prices read out of URLs |
| `[scoring]` | `price_unit`, `[scoring.tiers.*]`, `[scoring.bonuses]` |
| `[filters]` | The gates |
| `[maps]` | Provider, hosts, whether any of it is enabled |
| `[[destination]]` | `label`, plus `address` or `latitude`/`longitude`; optional `weight` and `db` |
| `[[landmark]]` | `name`; optional `match` (regex for how write-ups spell it) and `latitude`/`longitude` |
| `[[source]]` | `name`, `domain`, `parser`, `currency`, `tax_included`, `score_scale` |
| `[bookmarklet]` | Build inputs and outputs |

Columns available to `display.columns`: `name`, `source`, `where`, `checkin`,
`nts`, `slp`, `space`, `all_in`, `share_nt`, `score`, `reviews`, `clean`,
`look`, `walk`, `kind`, `traits`, `points`, `value`.

Settings that can never take effect — a tier naming a factor that doesn't
exist, a bonus naming a slug nothing produces, a landmark whose `match` won't
compile, `[storage]` in a city file — are reported on stderr and otherwise
ignored.

## Cities and trips

A database is a trip; a trip is in a city. The city is the reusable half.

`cities/edinburgh.cityconf.toml` holds what is true of Edinburgh however often
you go:
which landmarks its write-ups name and how they spell them, the destinations
worth measuring to, what tax is charged and in what currency, and any weights
that read differently there. These are committed — a city you have worked out
once is worth keeping and worth someone else having.

`<db>.dbconf.toml` beside `<db>.db.json` names the city and holds whatever is
true of
this trip alone (how many ways the bill splits, a must-have that only matters
this time). It is one line most of the time, and is not committed.

```
db autumn-edinburgh --new       # asks which city; existing names are offered
db autumn-edinburgh --city edinburgh
```

Naming a city that has no config starts one from a commented template. Both
commands print what came with it — how many destinations and landmarks — and
`db` with no argument lists each database against its city.

Settings are rebound whenever `database.current()` resolves a different
database, so switching at the prompt moves the landmarks, destinations and
rates together. The older arrangement still works and needs no migrating:
destinations in `config.toml` may carry `db = "<name>"`, and one that names
none counts everywhere.

## Using it in another country

A new city is a file in `cities/`. Nothing in it needs code: currency codes,
tax rate, destinations, landmarks, scoring weights, gates, columns. OSRM and
Nominatim cover the world.

The landmark table is the part worth building deliberately. Capture a dozen
listings first, read the write-ups (`show <id>` prints them in full), and write
down the places they keep naming — that is what turns "8 minutes from the
station" into a figure in the walk column. `cities/edinburgh.cityconf.toml` is
a worked example; `cities/oban.cityconf.toml` is one with destinations and no
landmarks yet.

Needs code:

- **A new site.** Write an adapter in `sources.py` that returns a
  `blank_record()`, register it in `sources.ADAPTERS`, and add a `[[source]]`
  entry naming its domain and parser. Another brand on an existing platform
  needs only the config entry. For bookmarklet support, add a host branch to
  `extract()` in `bookmarklet.js`.
- **Prose parsing.** `summary.py` matches English, and its distance patterns
  cover the miles/feet/minutes forms Booking.com writes. The trait, amenity and
  neighbourhood vocabularies are hardcoded there; slugs it emits must exist in
  `sources.AMENITY_ALIASES`, which `scoring.complaints()` checks.

`display.tax_marks`, `[tax] vat_rate` and `tax_included` describe any
inclusive/exclusive split, not VAT specifically; per-stay stated rates
(`taxes`, `fees_included`) already carry their own labels and are applied in
the order given.

## Files

```
lodgingbuddy.py       CLI, prompt, table, per-stay maths
sources.py            record schema, adapters, amenity aliases
summary.py            prose reader, landmark trilateration
scoring.py            tiers, bonuses, value, gates
proximity.py          walking times (OSRM / Google), geocoding
database.py           which set of stays is active
config.py             defaults, and the three overlays over them
config.toml           settings everything shares
cities/<city>.cityconf.toml   one place: its landmarks, destinations, rates
<db>.dbconf.toml              one trip: which city it's in, and its overrides
<db>.db.json                  one trip's captured stays
bookmarklet.js        in-page extractor
build_bookmarklet.py  builds the three installable forms
test_bookmarklet.js   unit checks for the extractor
```
