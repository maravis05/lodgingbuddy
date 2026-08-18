"""
Source adapters — turn a lodging URL into a common record.

Each adapter takes a URL and returns a dict using the shared schema in
`blank_record()`. Adapters fill what they can and leave the rest None; nothing
here guesses. A record that comes back with price=None simply needs a number
typed in, which is the normal case for WAF-protected sites.

Three tiers of what a source will give us, learned by testing rather than
assuming:

  sykescottages.co.uk   full scrape — schema.org JSON-LD, no bot wall
  cottages.com          Next.js __NEXT_DATA__, but AWS WAF trips intermittently
  hoseasons.co.uk       same platform as cottages.com (both are Awaze brands)
  booking.com           URL parameters only; the page itself is WAF-locked

Which domain routes to which adapter, and what each site quotes in, is in
config.toml — an adapter here only knows how to read a page.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import re
import urllib.parse

import config

# Status values a record can carry.
OK = "ok"              # everything we wanted, including a price
NEEDS_PRICE = "needs_price"  # identified the property, but no price
BLOCKED = "blocked"    # the site refused to serve the page


def blank_record() -> dict:
    """The common schema. Every source produces one of these."""
    return {
        "source": None, "code": None, "name": None, "url": None,
        # where
        "location": None, "region": None, "country": None,
        "latitude": None, "longitude": None,
        # when / who
        "checkin": None, "checkout": None, "nights": None,
        "adults": None, "children": None, "rooms": None,
        # cost
        # `price` is whatever the site quoted. `tax_included` says whether VAT
        # is already in it — without that flag two prices from different sites
        # are not comparable, since UK consumer sites quote inclusive totals
        # while Booking.com shows a US booker the ex-VAT figure.
        "price": None, "was_price": None, "currency": None,
        "tax_included": None, "vat_rate": config.VAT_RATE,
        # What the property itself charges, before the OTA's currency
        # conversion. Immune to FX markup, so it's the truest number we hold.
        "native_price": None, "native_currency": None,
        # The same money as `native_price`, in the currency the page was
        # rendering it in. Not a second price and never used as one — its whole
        # job is to be a pair with the native figure, so the exchange rate the
        # table converts at is read off the pages rather than typed into
        # config.toml and left to go stale. Only recorded where the two
        # currencies actually differ and where both figures are the same one
        # room on the same tax basis; see `rate_from`.
        "display_price": None, "display_currency": None,
        # "quoted"  = the site's price for these actual dates
        # "indicative" = a "from" / headline price that may not apply
        "price_basis": None,
        # What the quoted price leaves out, where the page said so plainly:
        # [{"label": "VAT", "rate": 0.20}, {"label": "City tax", "rate": 0.05}]
        # and [{"label": "Cleaning fee", "amount": 78, "currency": "GBP"}].
        #
        # `taxes` has three states, not two. A list is what the page said, []
        # is the page saying it adds nothing — a real answer, and common, since
        # a property under the VAT threshold charges none — and None is nobody
        # having read it, which is the only one the flat VAT estimate stands in
        # for. Where Booking's page state was readable the list is a single
        # aggregate entry carrying the site's own wording, because its itemised
        # amounts compound and dividing them back out one at a time doesn't
        # reconstruct the rates that produced them.
        # Rates rather than a total, because the rates are the durable fact —
        # and together they are what makes the checkout number derivable
        # without going to the checkout. Taxes compound; an included fee is
        # not taxed. See with_stated_charges.
        "taxes": None, "fees_included": None,
        # Which of a property's several rates this is (occupancy, cancellation).
        "offer": None,
        # Ways this stay's bill divides, when it isn't the trip's usual split.
        # Left None so config.toml stays the single answer for the common case.
        "shares": None,
        # property shape
        "sleeps": None, "bedrooms": None, "bathrooms": None,
        # The bed list, which is the honest answer to "how much space". "Sleeps
        # 4" is a capacity claim: it counts a sofa bed in the lounge the same as
        # a double behind a door. 1 double + 2 singles across 2 bedrooms says
        # who actually gets to shut a door, and that is the thing being decided.
        "beds": None, "rooms_total": None,
        # Canonical slugs — see AMENITY_ALIASES. Sites name the same fact a
        # dozen ways, and a config that gates on "parking" shouldn't have to
        # know which of them this site picked.
        "amenities": None,
        # What the prose says the place is — apartment, aparthotel, hotel,
        # guesthouse — and the facts it holds that no other field does:
        # soundproofed, ground_floor, adults_only, visitor_levy. Read out of
        # `summary` by summary.py at capture, and weighted in config.toml
        # alongside the amenities, because a fact's worth doesn't depend on
        # which part of the page it was written on.
        "kind": None, "traits": None,
        # What's around it rather than in it: food_nearby, nightlife_nearby,
        # shops_nearby, green_nearby. The neighbourhood has no coordinates and
        # no landmark to measure to, and it is most of what makes a street worth
        # staying on — "bars, restaurants, shops and pubs within 5 minutes' walk"
        # is a fact about a stay that no other field could hold.
        "nearby": None,
        # Which fields above are currently holding a value that came from the
        # prose rather than from the page's own markup. Rebuilt on every read,
        # so a field a later scrape fills properly drops off it.
        "gleaned": None,
        # The listing's own words, kept verbatim rather than parsed. Filled by
        # the bookmarklet, which reads the rendered page, and topped up by hand
        # at the prompt; the adapters below never set it, so a `refresh` can't
        # trample what you pasted. The prose is where
        # everything a schema has no column for lives: which bed is the sofa
        # bed, how steep the track up to it is, whether the sea view is from
        # the kitchen or from the car park.
        "summary": None,
        # quality
        # `score` is meaningless without `score_scale`: 4 out of 5 and 8.6 out
        # of 10 are the same property, and the site never says which it means.
        "score": None, "reviews": None, "score_scale": None,
        # Per-category ratings where a site breaks them out — cleanliness,
        # location, value. On the same scale as `score`.
        "subscores": None,
        # practical
        "checkin_time": None, "checkout_time": None,
        "address": None,
        # Minutes on foot to each destination in config.toml. Filled by `walk`,
        # never by a capture — no listing page knows where you want to go.
        "walk_minutes": None,
        # What the router said when it couldn't answer. Kept because "we never
        # asked" and "we asked and there is no route from here" are different
        # facts with different fixes, and an empty walk column looks identical
        # either way. Five of the twenty Oban stays are the second kind: their
        # coordinates land somewhere the pedestrian graph doesn't reach, and no
        # amount of re-running `walk` will change that — only a better address,
        # or somebody mapping the path.
        "walk_problems": None,
        # Minutes on foot to whatever landmarks the write-up chose to mention,
        # keyed by its names for them rather than yours. Kept apart from the
        # measured figures on purpose: these are the property selling itself,
        # they name places you may not care about, and averaging the two
        # together would mix a routing engine's answer with a marketer's. What
        # they are good for is a stay the router never reached — see
        # scoring.walk_minutes.
        "walk_claimed": None,
        # Your own marks out of 5, for the things no scrape reaches.
        "look": None, "clean": None,
        # ours
        "note": None, "status": NEEDS_PRICE, "captured_at": None,
    }


# Sites name the same amenity a dozen ways. Matched as substrings against a
# lowercased, punctuation-stripped version of whatever the page called it, most
# specific first — "hottub" has to be tried before "tub" would be, and
# "freeparking" and "onsiteparking" are both just parking.
AMENITY_ALIASES = {
    "wifi": ("wifi", "internet", "broadband"),
    "parking": ("parking", "garage", "driveway"),
    "kitchen": ("kitchen", "kitchenette", "ovenstove", "oven", "hob", "cooker"),
    "washing_machine": ("washingmachine", "washer", "laundry"),
    "dishwasher": ("dishwasher",),
    # Before `pool`, which "whirlpool" would otherwise also answer to.
    "hot_tub": ("hottub", "jacuzzi", "whirlpool"),
    "fireplace": ("fireplace", "woodburner", "logburner", "openfire"),
    "pet_friendly": ("petfriendly", "petsallowed", "dogfriendly", "dogsallowed",
                     "petswelcome", "dogswelcome"),
    "air_conditioning": ("airconditioning", "aircon"),
    "heating": ("heating", "centralheating", "radiator"),
    "tv": ("tv", "television", "smarttv"),
    "balcony": ("balcony", "terrace", "patio"),
    "garden": ("garden", "yard", "lawn"),
    "sea_view": ("seaview", "oceanview", "waterview", "lochview"),
    "pool": ("swimmingpool", "pool"),
    "lift": ("lift", "elevator"),
    "ev_charger": ("evcharg", "electricvehiclecharg", "carcharg"),
    "breakfast": ("breakfast",),
}


def normalise_amenities(raw: list) -> list[str]:
    """Turn a site's amenity names into canonical slugs, deduplicated and sorted.

    Anything we don't recognise is dropped rather than passed through. A slug
    only earns its place by being something config.toml can gate or score on,
    and a list padded with one site's private vocabulary would suggest
    otherwise.
    """
    found = set()
    for item in raw or []:
        if isinstance(item, dict):
            # schema.org LocationFeatureSpecification: {name, value}. A feature
            # explicitly marked false is an absence, not a presence.
            if item.get("value") is False:
                continue
            item = item.get("name") or item.get("value")
        if not isinstance(item, str):
            continue
        flat = "".join(ch for ch in item.lower() if ch.isalnum())
        for slug, aliases in AMENITY_ALIASES.items():
            if any(alias in flat for alias in aliases):
                found.add(slug)
                # One line on a page names one amenity, so stop at the first
                # match. It's also what makes the ordering above load-bearing.
                break
    return sorted(found)


def beds_from_schema(raw) -> list[dict]:
    """schema.org BedDetails into [{"type": "double", "count": 2}, …].

    "Double Bed" and "Double" are the same bed, so the noun is dropped — it
    carries no information and doubles the vocabulary the display has to know.
    """
    out = []
    for item in raw if isinstance(raw, list) else [raw]:
        if not isinstance(item, dict):
            continue
        kind = (item.get("typeOfBed") or "").strip()
        if isinstance(kind, dict):  # schema.org allows a BedType object
            kind = kind.get("name") or ""
        if not kind:
            continue
        kind = kind.lower().removesuffix(" bed").strip()
        count = item.get("numberOfBeds") or 1
        out.append({"type": kind, "count": int(count)})
    return out


def describe_beds(beds: list | None) -> str:
    """The bed list as one readable phrase.

    Grouped by room where the site said which room — "Bedroom 1: 1 queen ·
    Living room: 1 sofa" — because that is the whole difference between a bed
    behind a door and a bed in the lounge. Sources that only give a flat list,
    and stays captured before rooms were read at all, still read as before:
    "1 double, 2 single".
    """
    if not beds:
        return ""
    if not any(b.get("room") for b in beds):
        return ", ".join(f"{b.get('count', 1)} {b.get('type', 'bed')}" for b in beds)

    by_room: dict[str, list[str]] = {}
    for b in beds:
        room = b.get("room") or "Elsewhere"
        by_room.setdefault(room, []).append(f"{b.get('count', 1)} {b.get('type', 'bed')}")
    return " · ".join(f"{room}: {', '.join(items)}" for room, items in by_room.items())


def sleeping_rooms(beds: list | None) -> tuple[int, int] | None:
    """(rooms with a bed in them, how many of those shut). None if nobody said.

    Rooms rather than beds, because rooms are what a party divides into. Two
    people who didn't come together can't take one room between them however
    many beds are in it, and one of them can take the living room sofa however
    few — so a bed count answers neither question and a room count answers
    both. `count` is deliberately ignored here for that reason.
    """
    if not beds or not any(b.get("room") for b in beds):
        return None
    shut = {}
    for bed in beds:
        room = bed.get("room")
        if room:
            shut[room] = shut.get(room, False) or bool(bed.get("private"))
    return len(shut), sum(1 for private in shut.values() if private)


def apply_site(rec: dict, site: dict) -> None:
    """Stamp a fresh record with what config.toml says about the site.

    The brand name and what it quotes in are settings, not findings, so they go
    on before the adapter reads anything. An adapter is free to overwrite them
    from the page — Sykes' own priceCurrency beats our assumption.
    """
    rec["source"] = site.get("name") or site.get("domain")
    rec["currency"] = site.get("currency")
    if site.get("tax_included") is not None:
        rec["tax_included"] = site["tax_included"]
    # What this site's ratings are out of. A number without its scale can't be
    # compared with another site's, and no page ever restates it.
    rec["score_scale"] = site.get("score_scale")


def fetch(url: str) -> tuple[int, str]:
    """GET a URL, returning (status, body). Never raises on HTTP errors."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(parts.netloc, timeout=config.TIMEOUT)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request("GET", path or "/", headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": config.ACCEPT_LANGUAGE,
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body
    finally:
        conn.close()


def first_redirect(url: str) -> str:
    """Return the Location of the first redirect, or the URL unchanged.

    Booking.com share links need GET — they answer HEAD with a bare 200 and no
    Location header. We stop after one hop because the *second* redirect strips
    every query parameter, which is exactly the data we came for.
    """
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(parts.netloc, timeout=config.TIMEOUT)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request("GET", path,
                     headers={"User-Agent": config.USER_AGENT, "Accept": "*/*"})
        resp = conn.getresponse()
        loc = resp.getheader("Location")
        if resp.status in (301, 302, 303, 307, 308) and loc:
            return urllib.parse.urljoin(url, loc)
        return url
    finally:
        conn.close()


def nights_between(checkin: str | None, checkout: str | None) -> int | None:
    if not (checkin and checkout):
        return None
    try:
        return (dt.date.fromisoformat(checkout) - dt.date.fromisoformat(checkin)).days
    except ValueError:
        return None


def _json_ld(html: str) -> list[dict]:
    """Every schema.org object on the page, flattened."""
    out = []
    for blob in re.findall(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _next_data(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ─────────────────────────────── Booking.com ───────────────────────────────

BOOKING_PATH = re.compile(r"/hotel/([a-z]{2})/([^/.]+)\.")


def booking(url: str, site: dict) -> dict:
    """Booking.com: parse the URL. The page is behind an AWS WAF challenge.

    A /Share-XXXX link 301s to a URL carrying dates, party size and city ID in
    the query string, which is everything except the price.
    """
    rec = blank_record()
    apply_site(rec, site)

    if "/Share-" in url:
        url = first_redirect(url)

    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(parts.query)

    def one(key, cast=str, default=None):
        vals = q.get(key)
        if not vals:
            return default
        try:
            return cast(vals[0])
        except (TypeError, ValueError):
            return default

    if m := BOOKING_PATH.search(parts.path):
        rec["country"] = m.group(1)
        rec["code"] = m.group(2)
        rec["name"] = m.group(2).replace("-", " ").title()

    rec["url"] = f"{parts.scheme}://{parts.netloc}{parts.path}"
    rec["checkin"] = one("checkin")
    rec["checkout"] = one("checkout")
    rec["nights"] = nights_between(rec["checkin"], rec["checkout"])
    rec["adults"] = one("group_adults", int) or one("req_adults", int)
    rec["children"] = one("group_children", int, 0)
    rec["rooms"] = one("no_rooms", int)

    amount, blocks = booking_blocks(url)
    if amount is not None:
        # Filed as the property's own price, which is what it is, and which is
        # also where the bookmarklet files it. Two readers of one URL parameter
        # putting the answer in two different fields is how a stay ends up
        # priced or not depending on which one saw it.
        rec["native_price"] = amount
        rec["native_currency"] = rec.get("currency") or config.NATIVE_CURRENCY
        # Checked against two real bookings, this sits 19-23% below the
        # checkout total — near the pre-tax room rate. Useful as a
        # ballpark, misleading if presented as the price.
        rec["price_basis"] = "indicative"
        rec["tax_included"] = False
        if blocks > 1:
            # How many units the figure buys, which beats no_rooms: that is the
            # search's question, this is what the number in hand covers.
            rec["rooms"] = blocks

    rec["status"] = OK if (rec["price"] or rec["native_price"]) else NEEDS_PRICE
    return rec


def booking_blocks(url: str) -> tuple[float | None, int]:
    """What a Booking search result priced, off sr_pri_blocks, and in how many units.

    Booking encodes each selected block's price as minor units on the end of its
    id — ..._5_0_0__29363 is 293.63 — and the parameter holds every block that
    one search card was priced on. So the total is their sum: a hotel room taken
    twice for a party of three is listed twice, and reading only the first files
    a two-room booking at one room's price.

    Which is what this did until it was made to agree with the bookmarklet,
    where the summing has always been right. The same URL got two different
    prices depending on which of the two parsed it, and the disagreement was
    silent and always in the same direction.

    Present on links copied from a search result, absent on a bare property URL
    — which is the whole reason a Booking stay can arrive with no price at all.

    The figure is in the property's own currency, not the one the page is
    displaying to you. Returns (total, block count), or (None, 0).
    """
    found = re.search(r"sr_pri_blocks=([^&]*)", url)
    if not found:
        return None, 0
    minor, blocks = 0, 0
    for block in urllib.parse.unquote(found.group(1)).split(","):
        if m := re.search(r"__(\d+)$", block):
            minor += int(m.group(1))
            blocks += 1
    if not blocks:
        return None, 0
    # Summed in minor units and divided once, so two rooms at 296.66 come to
    # 593.32 rather than the 593.3199999999999 that adding the pounds gives.
    amount = minor / 100
    if not config.BOOKING_MIN_PRICE <= amount <= config.BOOKING_MAX_PRICE:
        return None, 0
    return amount, blocks


# ────────────────────────────── Sykes Cottages ─────────────────────────────

SYKES_PATH = re.compile(r"/cottage/([^/]+)/(.+?)-(\d+)\.html")


def sykes(url: str, site: dict) -> dict:
    """Sykes: full scrape. Serves plain HTML with schema.org VacationRental."""
    rec = blank_record()
    apply_site(rec, site)
    rec["url"] = url.split("#")[0]

    if m := SYKES_PATH.search(url):
        rec["region"] = m.group(1).replace("-", " ")
        rec["name"] = m.group(2).replace("-", " ")
        rec["code"] = m.group(3)

    status, html = fetch(url)
    if status != 200 or not html:
        rec["status"] = BLOCKED
        return rec

    for obj in _json_ld(html):
        if obj.get("@type") not in ("VacationRental", "LodgingBusiness", "Accommodation"):
            continue
        rec["name"] = obj.get("name") or rec["name"]
        rec["code"] = str(obj.get("identifier") or rec["code"] or "")
        rec["latitude"] = obj.get("latitude")
        rec["longitude"] = obj.get("longitude")
        rec["checkin_time"] = obj.get("checkinTime")
        rec["checkout_time"] = obj.get("checkoutTime")

        addr = obj.get("address") or {}
        if isinstance(addr, dict):
            rec["location"] = addr.get("streetAddress") or rec["location"]
            # Kept whole and separately, because it's what a geocoder wants.
            rec["address"] = ", ".join(
                x for x in (addr.get("streetAddress"), addr.get("addressLocality"),
                            addr.get("postalCode"), addr.get("addressCountry"))
                if isinstance(x, str)) or rec["address"]

        rating = obj.get("aggregateRating") or {}
        if isinstance(rating, dict):
            rec["score"] = rating.get("ratingValue")
            rec["reviews"] = rating.get("reviewCount") or rating.get("ratingCount")

        place = obj.get("containsPlace") or {}
        if isinstance(place, dict):
            occ = place.get("occupancy") or {}
            rec["sleeps"] = occ.get("value") if isinstance(occ, dict) else None
            rec["bedrooms"] = place.get("numberOfBedrooms")
            rec["bathrooms"] = place.get("numberOfBathroomsTotal")
            # Total rooms, not bedrooms — 5 rooms behind 2 bedrooms is a
            # separate lounge and kitchen rather than a corridor of beds.
            rec["rooms_total"] = place.get("numberOfRooms")
            rec["amenities"] = normalise_amenities(place.get("amenityFeature")) or None
            rec["beds"] = beds_from_schema(place.get("bed")) or None

            # The price is a schema.org Offer nested under containsPlace.
            offer = place.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            if isinstance(offer, dict) and offer.get("price") is not None:
                try:
                    rec["price"] = float(offer["price"])
                except (TypeError, ValueError):
                    pass
                rec["currency"] = offer.get("priceCurrency") or rec["currency"]
                # Sykes publishes a headline rate tagged unitText "from". It is
                # not the quote for the dates being browsed — a 3-night stay
                # showing "from £1090" bills at £582 — so say so rather than
                # letting a marketing number pose as a price.
                spec = offer.get("priceSpecification") or {}
                unit = str(spec.get("unitText") or "").strip().lower()
                rec["price_basis"] = "indicative" if unit == "from" else "quoted"
        break

    # Sykes states no stay length on the page — its price is a "from" figure for
    # an unspecified duration. The link may carry one; otherwise leave it unset
    # rather than assuming the standard week and quietly skewing the per-night
    # maths.
    if m := re.search(r"[#&]duration=(\d+)", url):
        rec["nights"] = int(m.group(1))

    rec["status"] = OK if rec["price"] else NEEDS_PRICE
    return rec


# ───────────────────── cottages.com / Hoseasons (Awaze) ────────────────────

AWAZE_PATH = re.compile(r"/(?:cottages|accom)/(.+?)-([a-z0-9]+)/?$", re.I)


def awaze(url: str, site: dict) -> dict:
    """cottages.com and hoseasons.co.uk — one platform, one parser.

    Both are Awaze brands on the same Next.js stack, so property records live in
    __NEXT_DATA__ under sections[].content.properties. Detail pages sit behind an
    AWS WAF that answers with 202 and an empty body, so we fall back to whatever
    the URL itself tells us. Which brand we're on comes from the matched entry
    in config.toml, so a third Awaze site needs no code here.
    """
    rec = blank_record()
    apply_site(rec, site)
    rec["url"] = url.split("?")[0]

    parts = urllib.parse.urlsplit(url)
    if m := AWAZE_PATH.search(parts.path):
        rec["name"] = m.group(1).replace("-", " ").title()
        rec["code"] = m.group(2).upper()

    # Awaze search links carry the whole query in the URL: start=09-10-2026 is
    # day-month-year, plus nights, adult and regionName. Free and exact, so
    # read it before touching the network.
    q = urllib.parse.parse_qs(parts.query)

    def one(key, cast=str, default=None):
        vals = q.get(key)
        if not vals or vals[0] == "":
            return default
        try:
            return cast(vals[0])
        except (TypeError, ValueError):
            return default

    rec["adults"] = one("adult", int)
    rec["children"] = one("child", int)
    rec["nights"] = one("nights", int)
    rec["region"] = one("regionName")
    if start := one("start"):
        try:
            checkin = dt.datetime.strptime(start, "%d-%m-%Y").date()
            rec["checkin"] = checkin.isoformat()
            if rec["nights"]:
                rec["checkout"] = (checkin + dt.timedelta(days=rec["nights"])).isoformat()
        except ValueError:
            pass

    status, html = fetch(url)
    if status != 200 or not html.strip():
        # 202 + empty body is the WAF challenge. We still have a usable record.
        rec["status"] = BLOCKED
        return rec

    data = _next_data(html)
    if not data:
        rec["status"] = NEEDS_PRICE
        return rec

    page = data.get("props", {}).get("pageProps", {})

    # Detail pages describe the property in `service` but load pricing from a
    # client-side microfrontend, so no price is available here. Take the solid
    # identity fields and let the price be supplied another way.
    service = page.get("service")
    if isinstance(service, dict):
        rec["name"] = service.get("propertyName") or rec["name"]
        rec["code"] = service.get("code") or rec["code"]
        rec["location"] = service.get("location") or rec["location"]
        rec["note"] = service.get("propertyType") or rec["note"]
        if str(service.get("grade") or "").isdigit():
            rec["score"] = float(service["grade"])

    if isinstance(page.get("lengthOfStay"), (int, str)) and str(page["lengthOfStay"]).isdigit():
        rec["nights"] = int(page["lengthOfStay"])

    guests = page.get("guests")
    if isinstance(guests, dict) and str(guests.get("adults", "")).isdigit():
        rec["adults"] = int(guests["adults"])

    wanted = (rec["code"] or "").upper()
    best = None
    for section in data.get("props", {}).get("pageProps", {}).get("sections", []):
        for prop in (section.get("content") or {}).get("properties") or []:
            if not isinstance(prop, dict):
                continue
            if wanted and str(prop.get("code", "")).upper() == wanted:
                best = prop
                break
            best = best or prop
        if best and wanted and str(best.get("code", "")).upper() == wanted:
            break

    if best:
        rec["name"] = best.get("title") or rec["name"]
        rec["code"] = best.get("code") or rec["code"]
        rec["location"] = best.get("location")
        rec["region"] = best.get("rhs3")
        rec["price"] = best.get("price") or best.get("priceFrom")
        rec["price_basis"] = "quoted" if best.get("price") else "indicative"
        rec["was_price"] = best.get("wasPrice")
        rec["nights"] = best.get("nights")
        rec["sleeps"] = best.get("guests")
        rec["bedrooms"] = best.get("bedrooms")
        rec["bathrooms"] = best.get("bathrooms")
        rec["score"] = best.get("score")
        if best.get("pageURL"):
            rec["url"] = best["pageURL"]

    rec["status"] = OK if rec["price"] else NEEDS_PRICE
    return rec


# ──────────────────────────────── dispatch ─────────────────────────────────

ADAPTERS = {"booking": booking, "sykes": sykes, "awaze": awaze}


def site_for(rec: dict) -> dict:
    """What config.toml says about the site a record came from.

    By name first, because that is what a capture stamps on the record, then by
    the URL's host for anything captured before the name settled. An empty dict
    where nothing matches, so callers can `.get` without checking twice.
    """
    name = (rec.get("source") or "").lower()
    host = urllib.parse.urlsplit(rec.get("url") or "").netloc.lower()
    for site in config.SOURCES:
        if name and name == str(site.get("name") or "").lower():
            return site
    for site in config.SOURCES:
        domain = str(site.get("domain") or "").lower()
        if domain and (domain in host or domain in name):
            return site
    return {}


def capture(url: str) -> dict:
    """Route a URL to its adapter and stamp the capture time."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    for site in config.SOURCES:
        # An entry with no domain would otherwise match every URL, since "" is
        # a substring of anything.
        domain = str(site.get("domain") or "").lower()
        if not domain or domain not in host:
            continue
        fn = ADAPTERS.get(site.get("parser"))
        if fn is None:
            raise ValueError(
                f"{site.get('domain')} names parser {site.get('parser')!r}, "
                f"which doesn't exist. In {config.PATH}, pick one of: "
                + ", ".join(sorted(ADAPTERS))
            )
        rec = fn(url, site)
        rec["captured_at"] = dt.datetime.now().isoformat(timespec="seconds")
        return rec
    raise ValueError(
        f"No adapter for {host or url!r}. Supported: "
        + ", ".join(str(s.get("domain")) for s in config.SOURCES)
    )
