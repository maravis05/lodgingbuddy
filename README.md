# lodgingbuddy

Collate lodging options you pick while browsing, so comparing them doesn't mean
copy-pasting into a spreadsheet.

Paste a link and it captures what it can — name, place, dates, beds, amenities,
review scores, and a price where the site will give one up. Fill the gaps with
`set`; `list` puts everything side by side.

Cost is the easy part, and on its own it isn't a reason to need a tool: if price
were the only thing that mattered you'd book the cheapest and be done. So the
table also carries the things that actually decide it — how long the walk into
town is, whether everyone gets their own bedroom, what previous guests said and
how many of them said it, and how the place looks to you. `points` scores those
and leaves price out entirely; `value` puts price back as the denominator, so
the question becomes how much of what you want each pound is buying.

## Prerequisites

Python 3.11 or newer — that's where `tomllib` arrived. Nothing else: this is
standard library only, so there's no `pip install` step and no virtual
environment to set up. Windows, macOS and Linux all work the same way.

If you haven't got Python, [python.org/downloads](https://www.python.org/downloads/)
covers every platform. On Windows, tick **Add python.exe to PATH** in the
installer — it's off by default, and skipping it is the usual reason a fresh
install seems to vanish.

To check, open a terminal — Terminal on macOS, PowerShell on Windows, whichever
one you use on Linux — move into the folder holding these files, and run:

```console
$ python3 --version
Python 3.13.1
```

**Two notes on reading the examples.** The `$` and `stays>` at the start of a
line are the prompt: they're already on screen, so you type what comes after.
And the command is spelled `python3` throughout, which is right on macOS and
most Linux systems; on Windows it's `python`, or `py` if that isn't found.
Substitute the one that works and nothing else changes between platforms.

**Always name the interpreter.** Type `python lodgingbuddy.py`, not
`.\lodgingbuddy.py` — and on macOS and Linux, `python3 lodgingbuddy.py` rather
than `./lodgingbuddy.py`. The bare-path forms depend on things that aren't
portable: an executable bit and a `#!` line on Unix, and on Windows a file
association, which is worse than it sounds. `.\build_bookmarklet.py` in
PowerShell doesn't run the script, it *opens* it in whatever app owns `.py` —
frequently an editor. You get that program's startup logs in your terminal, no
error, and nothing built. Naming the interpreter works identically everywhere.

## What reaches the internet

Worth saying plainly, because a tool that quietly phones out is a tool you can't
reason about:

- `add` and `refresh` fetch the listing page from the site whose link you
  pasted — that site and no other, and one you were already visiting.
- The **bookmarklet** sends nothing anywhere. It reads the page already open in
  front of you and writes to your clipboard.
- `walk` sends each stay's location and your destination list to a routing
  service — OpenStreetMap's by default, Google's if you switch `provider`.
  **This one is on out of the box**, and it's the only command that tells a
  third party which properties you're weighing up. Set `enabled = false` under
  `[maps]` in `config.toml` and no lookup ever leaves the machine.
- Everything else — `list`, `show`, `set`, `points`, `value`, `db`, `rm` — is
  arithmetic on the file on your disk, and works with no connection at all.

Your stays live in a plain JSON file next to the script. Nothing is uploaded,
and there's no account, telemetry or sync.

## Use

Run it with no arguments and it holds a prompt open, so a browsing session is
one process rather than one per link. Paste a link — or the bookmarklet's
output — and press enter. Add a space and the total you're paying to record it:

```console
$ python3 lodgingbuddy.py
lodgingbuddy — 4 stays in stays. `help` for what this takes.
stays> https://www.booking.com/Share-LjP6kp 480
  Strathisla Oban [booking.com] · 3 nts · 480 GBP all-in · 80/share/nt
stays> {"source":"sykes","name":"Heather Island View",...} 582
  Heather Island View [sykes] · 3 nts · 582 GBP all-in · 97/share/nt
stays> list
```

The total is optional — leave it off and the stay is captured with whatever the
site gave up. A number typed here is taken as the **final** price: quoted for
your dates, tax included, and authoritative over anything scraped. `quit`
leaves, and so does Ctrl-D — or Ctrl-Z then enter on Windows, which is what
end-of-file is there.

Type the currency with it — `£582`, `$789`, `582 GBP` — whenever the page is
showing you two of them. Booking.com quotes a US-signed-in booker in dollars
and then names the pounds the property will actually charge, so both numbers
are on the screen you're copying from and only you know which one you took. A
bare number is filed under whatever the *site* was quoting in, which is right
until the moment you copy the other one.

A listing can still arrive as two pastes, where you don't have all of it at
once. On Booking.com you usually will: the room block states its tax rates and
any fee, so the checkout total is worked out for you at capture — see
[the numbers](#the-numbers). Elsewhere the real total only shows up once you
click through to book, so the total can be its own line, and the lines after a
capture know what they're about:

```console
stays> {"source":"booking.com","name":"Strathisla Oban",...}
  Strathisla Oban [booking.com] · 3 nts
    no price — type the total on the next line
stays> 582
  Strathisla Oban [booking.com] · 3 nts · 582 GBP all-in · 97/share/nt
```

Anything that isn't a link, a total or a command is filed as that stay's
**summary** — the listing's own prose, kept verbatim. The bookmarklet already
brings it along, so this is for topping up: a site whose markup moved, a
description that ran past the cap, the paragraph the page keeps behind a "read
more".

```console
stays> Set in three acres of walled grounds a short walk from the harbour, …
stays>
  + summary on Strathisla Oban, 49 words
```

It appends, so a paragraph arriving as six lines lands as one summary; a blank
line says you've finished and it reports the word count. `set <id> --summary "…"`
replaces it instead.

A single word is never a summary — nothing describing a cottage is one word, and
a mistyped command answered by silently filing it as a description would be
worse than being told the command doesn't exist.

Every command also works as a one-shot:

```console
$ python3 lodgingbuddy.py add https://www.sykescottages.co.uk/cottage/Argyll-and-Bute-Kilbowie/...
$ python3 lodgingbuddy.py set heather --price 582 --incl-tax --look 4
$ python3 lodgingbuddy.py walk
$ python3 lodgingbuddy.py list --sort value
   Property             Source       Space    Slp  Walk  All-in   Share/nt  Guest  Pts  Value
─────────────────────────────────────────────────────────────────────────────────────────────
   Heather Island View  sykes        2br 1ba  4    12m   582 GBP  97        —      17   4.4
?  Harbour View         cottages.co           3    —     671 GBP  112       80%    12   2.7
   The Distillers Den   booking.com  2br      3    6m    364 GBP  91        —      0    0
   Strathisla Oban      booking.com  2rm      3    9m    380 GBP  63        —      0    0

?  can't tell whether it clears a must-have — held back rather than ruled out:
     Harbour View — unknown: 2 private bedrooms
```

| command | |
|---|---|
| *(none)* | hold a prompt open for pasted links |
| `add <url>` | capture a stay from a URL |
| `set <id> --price N …` | fill in or correct a field |
| `list [--sort K] [--viable]` | everything side by side |
| `show <id> [--json]` | one stay in full, with the arithmetic shown |
| `walk [id] [--again]` | measure the walk to your destinations *(makes a network call — see [What reaches the internet](#what-reaches-the-internet))* |
| `refresh [id]` | re-fetch prices |
| `paste [json]` | take a record from the bookmarklet |
| `rm <id>…` | drop stays you've ruled out |
| `db [name] [--new]` | which set of stays to work in |

`<id>` is a property code or part of a name — `1198632`, `heather`.

`--sort` takes `share`, `price`, `score`, `sleeps`, `walk`, `points`, `value`,
`checkin` or `name`. `--viable` hides anything that fails a must-have.

## More than one set of stays

Two trips, or a real shortlist and a pile of examples you're only feeding in to
sharpen the scoring — either way they shouldn't be in the same table. A database
is a name and a file: `examples` is `examples.json` next to `stays.json`, and
every `.json` in that folder is one.

```console
$ python3 lodgingbuddy.py db examples --new
Started and now in examples — 0 stays. `db stays` goes back to 4 stays.
$ python3 lodgingbuddy.py db
→ examples  0 stays
  stays     4 stays
```

The choice sticks, so it's said once rather than typed on every capture — which
is also why the prompt is named after the database you're in, and why `list`
says so when it isn't the usual one. A mode you can't see is one you'll
eventually paste into by mistake.

`db <name>` switches; without `--new` it refuses a name that doesn't exist, so a
typo can't quietly open an empty database instead of the one you meant. Delete a
database by deleting its file.

To read a different one for a single command without moving the pointer, set
`LODGINGBUDDY_DB`. How you do that for one command is the one thing that really
does differ by platform:

```console
macOS, Linux   $ LODGINGBUDDY_DB=examples python3 lodgingbuddy.py list
PowerShell     > $env:LODGINGBUDDY_DB="examples"; python lodgingbuddy.py list
Command Prompt > set LODGINGBUDDY_DB=examples && python lodgingbuddy.py list
```

The PowerShell and Command Prompt forms outlive the command and stay set for the
rest of that window; `Remove-Item Env:\LODGINGBUDDY_DB` and `set
LODGINGBUDDY_DB=` respectively undo them. If that's a nuisance, `db <name>` and
back is the simpler route.

## The numbers

Two adjustments make prices from different sites comparable.

**VAT.** UK consumer sites quote inclusive totals; Booking.com shows some
bookers the ex-VAT figure and adds tax at checkout — a 20% gap that would put
Booking flatteringly under everything else. Records carry `tax_included`, and
`list` finishes the sum on the ones flagged pre-tax — two ways, marked
differently. Where the page stated its own rates, and any fee already inside
the price, that is the arithmetic done and the figure is marked `=`; `show <id>`
names the rates that went into it. Where it didn't, or stated something that
doesn't add up, a flat VAT gross-up stands in and is marked `+`. Where no site
said either way the number is shown untouched and marked `?`, which is not the
same claim as either.

**Nights and shares.** `Share/nt` is all-in ÷ nights ÷ shares, so a 2-night stay
compares with a 4-night one. Shares are not heads: three people splitting a bill
down the middle — a couple and a singleton — are two shares, and dividing by
three would describe a payment nobody makes. Set `shares` in `config.toml`, or
per stay with `set <id> --shares N`.

Where a property's own price is known it beats the OTA's converted one, which
has FX markup baked in. A "from" price is marked `~` and is not a quote for your
dates — click through and `set` the real total. A price marked `=` never carries
`~`: the sum was finished from the page's own rates, so it is what the checkout
charges for the dates in the link, and hedging it would be a claim about it that
isn't true.

**One currency, or the table says so.** Every comparison assumes prices are in
`currency.base`: `Share/nt` divides by nights and shares, `Value` divides by
that, and the sort ranks what comes out. A single price filed under a different
currency doesn't fail anywhere — it sorts as though a dollar were a pound, which
buries the stay at the bottom of the table looking a third too expensive. So
`list` says when it's holding more than one, names them, and points at `set <id>
--currency`. `--rate` doesn't paper over it: it converts the column for display
and deliberately leaves `Value` alone, so a mixed table stays flagged either
way.

## Everything that isn't cost

`Pts` is how much you want to stay somewhere. Price is deliberately not in it.
Cost comes back as `Value` — points per £25 a share a night — so the column
you sort on answers "which of these gives me the most of what I want per
pound", and a cheap disappointment stops looking like a bargain.

The weights are all in `config.toml` and are meant to be edited per trip. Two
shapes of rule:

**Tiers** band a quantity. They aren't additive: a six-minute walk scores the
top band, not every band it passes. List them generous-first.

```toml
[scoring.tiers.walk_minutes]
direction = "lower"
steps = [
  { max = 10, points = 25 },   # out the door and you're there
  { max = 20, points = 15 },   # a walk, but nobody complains
  { max = 35, points = 6 },    # you'd do it once, in good weather
]
```

**Bonuses** are flat points for a fact being true — `parking = 6`, `wifi = 4`.

Everything a tier reads is normalised to 0–100 first, which is what lets one
table score a five-star site, a ten-point site and your own mark out of 5. It
also fixes a real trap: sorting raw review scores ranked a 4.8-out-of-5 below a
9.0-out-of-10.

Two factors are yours alone, because nothing can scrape them:

```console
$ python3 lodgingbuddy.py set heather --look 4 --clean 5
```

`--look` is aesthetic appeal out of 5. `--clean` is how clean it looks, and
overrides the site's cleanliness sub-score where there is one — you looked at
the photos with your own standards in mind, and an aggregate of strangers
didn't.

**Who ends up on the sofa** is its own tier, `beds_outside_bedrooms`, scored
apart from `spare_beds` because they answer different questions: spare beds are
elbow room, this is privacy, and no quantity of the first buys the second.
Booking.com breaks its room block out room by room, which is the only place
this is legible at all:

```
Two-Bedroom Apartment          One-Bedroom Apartment
Max. people: 3                 Max. people: 3
Bedroom 1: 1 queen bed         Bedroom 1: 1 full bed
Bedroom 2: 1 bunk bed          Living room: 1 sofa bed
Bathrooms: 2
```

Both sleep three. Only one of them puts everybody behind a door, and a bed list
that says "1 queen, 1 bunk" against "1 full, 1 sofa" barely hints at it. The
room labels come over with the beds, so `show` prints `Bedroom 1: 1 queen ·
Bedroom 2: 1 bunk`, and the count of beds outside a bedroom scores: 0 is worth
10 points, 1 is worth 3.

A site that never broke the beds out room by room leaves this **unscored**
rather than scoring zero — an unread layout must not read as a place where
everyone gets a door. It shows up in `show`'s `no data:` tail, as everything
unmeasured does.

An apart-hotel lists every apartment type in the one table — four suites, two
rate plans each — and read whole that table describes no apartment that exists.
The first suite's bedroom and living room, the last one's second bedroom and
its `Bathrooms: 2`, fused into a one-bedroom flat recorded as having two of
each. So the table is split at each unit's capacity line, which is the site's
own marker for where one description starts, and only the priced unit is read.
The priced one is the cheapest — the same rule the captured price already
follows — so the layout on the record is the layout that price buys.

The **summary** is captured but not scored, and deliberately so: nothing here
reads prose, and a number quietly derived from one would be a judgement you
couldn't check. It's kept because it's the evidence — the sofa bed, the steep
track, the sea view that turns out to be from the car park — and because a
scoring rule worth having is one fitted to stays you've actually judged rather
than one guessed in advance.

`show` never prints a bare total. It names every contribution, because a
composite you can't take apart is one you end up either trusting or ignoring
wholesale:

```
  Points    17   spare_beds=5 | +wifi=4 | +kitchen=5 | +fireplace=3 | no data: walk_minutes, guest_score, reviews, cleanliness, look
  Value     4.4  = 17 points / (97 GBP per share a night / 25)
```

That `no data:` tail matters more than it looks. A place nobody has reviewed
scores the same zero as a place everybody disliked, and it is the only thing
telling them apart. `list` repeats it under the table.

## Must-haves

Gates, not preferences — no amount of hot tub buys back a bedroom that isn't
there. They live in `[filters]`:

```toml
private_bedroom_per_share = true
max_walk_minutes = 0     # 0 = don't gate on it
require = []             # e.g. ["parking", "wifi"]
```

`private_bedroom_per_share` is measured in shares, not heads, reusing the same
idea as the split: a couple is one share and one bedroom, a singleton is one
share and one bedroom. Three of you sharing two ways needs two bedrooms. A
hotel booking satisfies it with rooms instead.

A bedroom count the site actually stated wins outright here. It used to be
taken as the larger of that and the room count — but on Booking.com the room
count comes off the URL's `no_rooms`, which says how many rooms your *search*
asked for. A one-bedroom apartment turned up by a two-room search cleared a
two-bedroom must-have on the strength of the question rather than the answer.
Where no bedroom count is known, the room count is still the fallback, which
is what keeps hotels working.

Giving the stated count the last word is only worth anything if it is a count
of one apartment, which is the other half of splitting the table above: a fused
two is a stated count, and it cleared the same gate by the same route it was
closed against. The room count gets narrowed to one on the same evidence —
where the URL asked for two rooms, one block was priced, and the unit's own
`Sleeps:` holds the whole party, you are being quoted one apartment, and
recording two makes the record disagree with the price printed beside it.

A stay that fails is marked `✗` and kept — you captured it, so it stays
captured. `list --viable` hides them. A stay we simply lack the data on is
marked `?` and held back rather than ruled out, which is not the same claim:

```
✗  fails a must-have, so no score can buy it back:
     Heather Island View — needs hot_tub

?  can't tell whether it clears a must-have — held back rather than ruled out:
     Harbour View — unknown: 2 private bedrooms
```

## How far is it, really

Coordinates tell a human nothing. The only location question anyone actually
asks is whether you have to drive, so `walk` stores minutes on foot and the
table never shows a distance.

Name the places you want to be near in `config.toml`. This is the part you
rewrite per trip — it's what makes the tool work for Edinburgh as well as Oban:

```toml
[[destination]]
label = "Oban town centre"
address = "George Street, Oban PA34 5NX, UK"
weight = 0.6
```

`weight` is that destination's share of the average the walk tier scores; they
need not sum to anything.

**This one makes a network call, and it's on out of the box.** `walk` is the
only command that tells anyone where the places you're considering are: it sends
each stay's coordinates or address, along with your destination list, to a
routing service. Nothing else here does that. It ships on because the default
provider costs nothing and needs no key, and because how long the walk into town
is decides more stays than the price does.

```console
$ python3 lodgingbuddy.py walk
  Heather Island View: Oban town centre 12m, Ferry terminal 18m
```

If you'd rather it didn't, one line in `config.toml` stops it dead:

```toml
[maps]
enabled = false
```

Then nothing leaves the machine, `walk` says so and stops rather than failing at
something further in, and everything else carries on — the walk column just
stays empty, and `points` counts `walk_minutes` among its `no data:` tail rather
than scoring a guess.

One call per stay, all destinations batched. Stays already measured are skipped
unless you pass `--again`, which is what you want after editing the destination
list.

**Which service.** `provider` takes `osrm` or `google`.

`osrm` is the default and needs no key, no account and no billing profile. It
routes over OpenStreetMap via FOSSGIS's pedestrian profile, and addresses are
turned into coordinates by Nominatim first, since OSRM speaks only coordinates.
Both are volunteer-run and free, so the tool identifies itself honestly in
`[maps] user_agent` — deliberately not the browser string in `[http]` — and
holds to one call a second. Don't lower `min_interval_seconds`.

Not `router.project-osrm.org`, though. That demo server accepts a request for
the foot profile and answers it with **car** timings: same distance, same
duration as `/driving/`, about 26 km/h. It doesn't error, so the numbers look
plausible and are roughly five times too small — which lands a stay in the wrong
scoring band rather than merely mismeasuring it.

`google` is Distance Matrix, and wants a key with that API enabled, read from
the environment and never from `config.toml`, which is committed:

```console
macOS, Linux   $ export GOOGLE_MAPS_API_KEY=…
PowerShell     > $env:GOOGLE_MAPS_API_KEY="…"
Command Prompt > set GOOGLE_MAPS_API_KEY=…
```

Each of those lasts as long as the terminal window is open, so it's once per
session rather than once per command. To stop setting it by hand, put it in your
shell profile on macOS and Linux, or under **Environment Variables** in the
Windows system settings.

**Where OSM routing gets thin.** Pedestrian routing is only as good as the
footway tagging underneath it, and that is excellent in cities and patchy in
small towns. In Oban, walks from the harbour measure fine, while the north end
of Corran Esplanade comes back `no walking route` to every destination — the
seafront is a severed island in the foot graph, most likely because the road
joining it to the centre is trunk-classified and the pedestrian profile won't
use trunk roads. London and Edinburgh test clean over the same code.

When that happens the destination is left out and said out loud, never guessed
at, and `points` counts `walk_minutes` in its `no data:` tail. So a thin graph
costs you a measurement, not a wrong one. If it costs you too many, `provider =
"google"` is the fallback — Google's pedestrian network doesn't have the gap.

Geocoding is thin in the same places. Nominatim returns nothing at all for
`Oban Ferry Terminal, Railway Pier` — so that destination carries `latitude`
and `longitude` instead, which any destination may do, and which skips the
geocoder entirely. Worth doing for anywhere a postal address describes vaguely:
a terminal, a trailhead, a beach.

Straight-line distance would be free and, here, wrong: a sea loch turns six
kilometres into a forty-minute drive, and Argyll is mostly sea lochs.

Measuring needs somewhere to measure *from*. Sykes publishes coordinates and
Booking.com's map pin gives them up to the bookmarklet; failing both, an
address works, since the routing service geocodes it:

```console
$ python3 lodgingbuddy.py set harbour --address "Corran Esplanade, Oban PA34 5AQ"
```

## Config

Everything tunable lives in `config.toml`: store path — which also names the
default database and the folder the others live in — VAT rate, currency pair,
the split, scoring weights, must-haves, destinations, table shape, and which
domains route to which adapter. Every key has a built-in default, so a partial
file — or none — still runs. Set `LODGINGBUDDY_CONFIG` to use a different file,
the same way as `LODGINGBUDDY_DB` above.

The table got wide enough that `columns` picks which of them `list` prints:

```toml
columns = ["name", "source", "space", "slp", "walk", "all_in", "share_nt",
           "score", "points", "value"]
```

Choose from `name`, `source`, `where`, `checkin`, `nts`, `slp`, `space`,
`all_in`, `share_nt`, `score`, `reviews`, `clean`, `look`, `walk`, `points`,
`value`. Which ones earn their place changes with the trip — `where` and
`checkin` say nothing when every stay is the same town on the same dates.

## Sources

Verified against live pages on 2026-08-15. What each site gives up varies a
lot, so this is what actually came back rather than what the adapters hope for:

| site | how it's read | what came back |
|---|---|---|
| **sykescottages.co.uk** | schema.org JSON-LD, no bot wall | name, location, region, sleeps, bedrooms, bathrooms, total rooms, **bed layout**, **amenities**, **coordinates**, **the write-up**, check-in/out times, and a "from" price |
| **booking.com** — `/Share-` link | the first 301 carries the query string | property, dates, party size |
| **booking.com** — property URL | URL parameters | name, dates, nights, party, rooms |
| **booking.com** — bookmarklet | the rendered page | **score and sub-scores, review count, amenities, beds, address, map pin, the write-up** |
| **cottages.com** | Next.js `__NEXT_DATA__` | name, location, review score, nights, **the write-up** |
| **hoseasons.co.uk** | same platform as cottages.com | name and property code |

**The write-up** comes over too, verbatim and unparsed — from JSON-LD on Sykes,
from `__NEXT_DATA__` on the Awaze pair, and off the rendered page on
Booking.com, with the page's `meta` description as a last resort for a site
whose markup has moved. It's the part of a listing no schema has a column for:
which bed is the sofa bed, how steep the track is, whether the sea view is from
the kitchen or the car park. Boilerplate that lives inside the description block
— Booking.com's Genius banner, "show more" — is dropped a line at a time.

Sykes turned out to be publishing far more than was being read: the bed list
(1 double + 2 singles), an amenity list, a room count and coordinates were all
sitting in its JSON-LD, discarded. That bed list is the honest answer to "how
much space" — "sleeps 4" counts a sofa bed in the lounge the same as a double
behind a door, and only one of those settles who sleeps where.

**Mostly no site hands over a usable total for your dates.** Sykes publishes a
"from" figure (shown `~`, and a 3-night stay advertised "from £1090" billed at
£582). The Awaze pair load pricing in a client-side widget that isn't in the
page source. So the workflow is: capture the stay, then type the total you see
at checkout — that number is taken as final.

**Booking.com is the exception, because it shows its working.** Under each room
block it states what the price leaves out:

```
Included: £78 Cleaning fee per stay
Excluded: 20 % VAT, 5 % City tax
```

Which is enough to finish the sum without going to the checkout. Taxes compound
rather than summing, and an included fee is not taxed:

```
Princes St   602.14 × 1.20 × 1.05              = 758.70   checkout: £758.70
Royal Mile  (673.24 − 78) × 1.20 × 1.05 + 78   = 828.00   checkout: £828.00
Calton Hill  480.21 × 1.20 × 1.05              = 605.06   checkout: £605.07
```

To the penny on the first two, and a rounding penny out on the third: the
figure in the URL is the pre-tax price already rounded to pence, so a base of
£480.2142 arrives as £480.21 and the last decimal has nowhere to come from.
No other arrangement of the same numbers lands anywhere near any of the three.
The rates are read off the page rather than hardcoded, so a city with a
different levy needs no change here.

Calton Hill also carried a 10% Genius discount, which turns out not to matter:
the price in the URL is already the discounted one, and a discount that
multiplies commutes with the tax rates that multiply after it. "Applied before
taxes and charges" and taking it off the gross total are the same arithmetic.

A price that got there this way is marked `=` in the table, and `show` names the
rates that went into it. Where the page doesn't state them — or states something
that doesn't add up, like a fee larger than the price or a fee in a different
currency from it — the old flat-VAT estimate stands and is marked `+` as before.
It falls back rather than computing something plausible.

Two checkouts is two checkouts, though. Spot-check the first few against the
book page; a typed total still wins over anything worked out here.

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

It works in any browser on any platform, since the code runs inside the page
rather than against anything on your machine. And because the clipboard is all
that passes between the two halves, the browser needn't even be on the same
computer as the script — handy if you keep this on a server and browse from a
laptop, but nothing depends on that arrangement. (A browser extension couldn't
do it either way round without an install and a permissions dialog.)

**Build and install.**

Neither output file is committed, so build them first. Any machine with Python 3
will do:

```console
$ python3 build_bookmarklet.py
source    26,346 bytes
stripped  18,269 bytes
encoded   34,399 bytes

Wrote bookmarklet.txt, bookmarklet.html, install-bookmarklet.html
```

Three files, because installing a bookmarklet goes wrong in three different
ways. If you built them somewhere other than the machine you browse on, copy
them across first — they're a set, and the install page refers to the other two.

**Open `install-bookmarklet.html` in your browser and drag the button onto your
bookmarks bar.** That's the whole install.

The one trap, and it's an easy one: drag *the button out of the opened page*,
not the file out of your file manager. Dragging the file bookmarks the file —
you get a link to `install-bookmarklet.html` on your bar, which does nothing on
a listing. The page says so on itself, in case you meet this before you meet
this paragraph.

If dragging won't do — a locked-down bookmarks bar, a browser that won't accept
the drop — there are two more routes, both also written on the page:

- **Import `bookmarklet.html`.** It's the Netscape bookmark format every
  browser's importer speaks, deliberately kept to nothing but what a parser
  expects. Chrome and Edge: bookmark manager → ⋮ → *Import bookmarks*. Firefox:
  *Manage bookmarks* → *Import and Backup*. Safari: *File* → *Import From*.
  Choose the HTML-file option, not the import-from-another-browser one.
- **Paste `bookmarklet.txt` by hand.** Same thing as a bare URL: make a new
  bookmark and paste the file's contents into its **URL** field, not its name.
  It's 34 KB, so it's a fiddly thing to select out of a terminal — which is why
  the other two exist.

Whichever route, it has to go in through the bookmark editor: browsers strip
`javascript:` pasted straight into the address bar. Drag is the most reliable,
since it's an ordinary link — some browsers have been known to drop
`javascript:` bookmarklets on import.

Rebuild after editing `bookmarklet.js`, and reinstall.

**Use.** Open a listing, click the bookmark. It reads the page's schema.org
JSON-LD, its `__NEXT_DATA__` blob, and failing those the visible text, then
copies the record to your clipboard as JSON and tells you what it found. Paste
that at the `stays>` prompt, with the total after a space if you have it — or on
the next line, once you've clicked through to book and know what it is.

What it copies is the bare `{...}` and nothing else. It used to wrap that in a
`./lodgingbuddy.py paste '…'` command line, which read as a shell command and
was one on no version of Windows — `cmd.exe` doesn't take single quotes, and the
`./` is wrong off Unix. The prompt reads raw JSON directly, so the wrapper
bought nothing and cost a platform. The old form still parses, so anything
already sitting on a clipboard keeps working.

For Booking.com it is the *only* route to anything but the price, since the
property pages are WAF-locked to everything that isn't a browser. It reads the
review score and its sub-scores, the review count, the facilities list, **the
property write-up**, the address and the map pin's coordinates — off
`data-testid` hooks and the map's `data-atlas-latlng`, which have outlasted
several rounds of class-name obfuscation. Amenities travel in the site's own
words and are normalised into slugs on the Python side, so the alias table
lives in one language rather than being kept in step across two.

It also reads **the room block** — bedroom count, bathroom count, max
occupancy, and which bed sits in which room. That last one is read out of the
rendered text rather than through a selector, because the labels are the site's
own words ("Bedroom 1", "Living room") whatever the markup around them is doing
that week. Parsing it by finding the label positions and taking each room's
beds as the text up to the next label handles both shapes the text arrives in:
with line breaks in a browser, and collapsed onto one line under the test
harness. Matching to the end of a line handles only the first, and quietly
hands every bed in the block to whichever room is named first.

The alert now names what it captured, not just the price:

```
Heather Island View — price 582

Also captured:
  score 8.6 (1731 reviews)
  4 sub-scores
  beds: 1 double, 2 single
  9 amenities
  located
  summary: 210 words
```

A missing line there is how you find out a site changed its markup — before the
stay lands in the table scoring zero for no visible reason.

It only reads the DOM — no network calls, no cookies, no storage — and the
extractor is split from the browser plumbing so it can be tested outside a
browser:

```console
$ node test_bookmarklet.js                          # unit checks, no page needed
ok    one bedroom, sofa bed in the living room
ok    two bedrooms, everyone behind a door
ok    same block with the whitespace collapsed
...
7 passed

$ node test_bookmarklet.js saved.html <original-url>   # against a real page
```

With no arguments it runs the room-block cases, which need no fixture and so
cost nothing to run. Given a page saved out of your browser with Ctrl-S and the
URL you saved it from, it runs the whole extractor and prints the record — the
only way to check the Booking.com path, which has no JSON-LD and no
`__NEXT_DATA__` to read.

Where a page shows several plausible amounts and none is clearly the total, it
reports them as candidates rather than guessing. A discounted price is printed
four times over — struck-through original, new figure, then both again in words
for a screen reader — so where the page labels one `Current price`, that label
picks the list: half of what's on screen is a price nobody is charged. The list
is capped, and cut from the expensive end, since the cheapest is the one this
tool would take. Sorted the other way round, a table of eight plans offered
every pre-discount original and not one figure you could pay. On Booking.com
the total is
worked out from the room block's stated rates instead — see
[the numbers](#the-numbers) — but a figure you've read off the checkout still
beats it, and typing one in is taken as final.

`refresh` never trades good data for bad: a site that answers with a bot wall
keeps what it already had.
