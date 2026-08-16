"""
Desirability, with price deliberately left out of it.

`points` answers one question — how much do you want to stay here? — and cost
never enters it. Cost comes back as the denominator of `value`, which is points
per unit of what one share pays a night. Keeping the two apart is the whole
point: a composite that mixes price into the score can't tell a cheap
disappointment from an expensive one, and re-weighting it silently re-prices
everything.

Two shapes of rule, both read from config.toml so a trip can be re-weighted
without touching this file:

  tiers    a quantity falls into the highest band it qualifies for, and that
           band's points are awarded. Not additive — a 5-minute walk scores the
           25-point band, not 25 + 15 + 6.
  bonuses  flat points for a fact being true, e.g. there is parking.

Everything a tier reads is normalised to 0-100 first. A 4.0 from a five-star
site, an 8.6 from a ten-point one and your own 4-out-of-5 are the same claim
expressed three ways, and one tier table should read them all. It also stops
`list --sort score` ranking a 4.8/5 below a 9.0/10.

Nothing here guesses. A factor we have no number for scores nothing *and says
so* — `Breakdown.unknown` names it — because a stay nobody has reviewed must
not be presented as a stay everybody disliked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config


@dataclass
class Breakdown:
    """Points, and the arithmetic that produced them."""

    tiers: dict[str, float] = field(default_factory=dict)
    bonuses: dict[str, float] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    points: float = 0.0
    value: float | None = None

    def summary(self) -> str:
        """One line naming every contribution, so the total can be checked."""
        parts = [f"{name}={pts:.0f}" for name, pts in self.tiers.items() if pts]
        parts += [f"+{name}={pts:.0f}" for name, pts in self.bonuses.items() if pts]
        if self.unknown:
            parts.append("no data: " + ", ".join(self.unknown))
        return " | ".join(parts) or "nothing scored yet"


# ─────────────────────────── normalising to 0-100 ──────────────────────────

def as_pct(value: float | None, scale: float | None) -> float | None:
    """A rating on any scale, as a percentage of its maximum."""
    if value is None or not scale:
        return None
    return 100.0 * value / scale


def scale_of(rec: dict) -> float | None:
    """What this stay's ratings are out of.

    Stamped on at capture, but fetched fresh from config.toml when it isn't —
    so records captured before the field existed become comparable the moment
    the setting appears, rather than needing to be captured again.
    """
    if rec.get("score_scale"):
        return rec["score_scale"]
    for site in config.SOURCES:
        if site.get("name") == rec.get("source"):
            return site.get("score_scale")
    return None


def guest_score(rec: dict) -> float | None:
    """What previous guests said, as a percentage.

    The number alone doesn't say what it's out of and the site never repeats
    itself, so the scale has to come from what we know about the source.
    """
    return as_pct(rec.get("score"), scale_of(rec))


def cleanliness(rec: dict) -> float | None:
    """How clean the place is, as a percentage.

    Your own mark wins over the site's sub-score. You looked at the photos with
    your own standards in mind; an aggregate of strangers did not.
    """
    if rec.get("clean") is not None:
        return as_pct(rec["clean"], 5)
    sub = rec.get("subscores") or {}
    return as_pct(sub.get("cleanliness"), scale_of(rec))


def look(rec: dict) -> float | None:
    """Aesthetic appeal, as a percentage. Hand-marked only — no scrape reaches it."""
    return as_pct(rec.get("look"), 5)


def spare_beds(rec: dict) -> float | None:
    """Beds beyond the number of people, which is elbow room rather than capacity.

    A cottage that sleeps six for three of you means a spare room to dump bags
    in; one that sleeps exactly three means everyone is on top of each other.
    """
    sleeps, heads = rec.get("sleeps"), rec.get("adults")
    if not sleeps or not heads:
        return None
    return float(sleeps - heads)


def shares_of(rec: dict) -> int | None:
    """How many ways this stay divides — into payers, and into sleepers.

    Read fresh from config every time rather than stamped onto the record at
    capture, so editing config.toml re-costs and re-scores the whole list. A
    stay that splits differently from the rest of the trip carries its own
    `shares`.
    """
    return rec.get("shares") or config.SHARES


def rooms_to_sleep_in(rec: dict) -> tuple[int | None, int | None]:
    """(rooms with a bed in them, how many of those shut), for this stay.

    Two sources, and they are not equally good. A room-by-room bed list is the
    site telling us the layout, and where there is one it is complete — a
    living room with a sofa bed in it is in that list, so a layout without one
    hasn't got one. Failing that, a stated bedroom count says how many rooms
    shut but nothing about how many there are in total, which is why the second
    half of the answer comes back alone.

    `rooms` is last and is the search's `no_rooms`, not the property's: it says
    how many rooms were *asked* for. It stands only for a hotel booking, where
    asking for two rooms and getting two rooms is the same fact.
    """
    import sources  # local, for the same reason complaints() does it

    counted = sources.sleeping_rooms(rec.get("beds"))
    if counted:
        return counted
    stated = rec.get("bedrooms") or rec.get("rooms")
    return None, stated or None


def shares_without_a_door(rec: dict) -> float | None:
    """How many shares don't get a bedroom to themselves.

    The rooms that shut are handed out first, because that is how anyone would
    do it — the couple and the singleton take a bedroom each while there are
    bedrooms — and whoever is left is on the sofa in the living room.

    Which is a worse stay, not a disqualifying one. It is scored rather than
    gated for exactly that reason: a sofa bed is a real answer to where the
    third person sleeps, just the answer you'd rather not have, and it should
    cost points instead of the argument. What isn't an answer is two shares in
    one room — see `gates`, which is where that lives.

    None, not zero, when nothing said how many rooms shut: an unread layout
    must not read as a place where everyone gets a door.
    """
    shares = shares_of(rec)
    _, shut = rooms_to_sleep_in(rec)
    if not shares or shut is None:
        return None
    return float(max(0, shares - shut))


def walked(rec: dict) -> tuple[dict[str, float], bool]:
    """Minutes to each destination, and whether any of them came from the prose.

    The router's answer wins wherever there is one. A listing's own "8 minutes
    from the castle" is a real fact and the write-up is where most of them live,
    but it is the property describing itself, and where both exist the one that
    measured the pavement is the better number.

    Where there is no routed figure it is much better than the alternative,
    which is a hole. A stay captured with maps off, or one the router couldn't
    place, scored nothing at all for its location and sat at the bottom of the
    table looking like somewhere remote.
    """
    import database  # local, for the same reason complaints() imports sources
    import summary

    measured = dict(rec.get("walk_minutes") or {})
    if not config.TRUST_CLAIMED_WALK:
        return measured, False
    wanted = [d["label"] for d in config.destinations_for(database.current())]
    borrowed = False
    for label, minutes in summary.claimed_for(rec, wanted).items():
        if label not in measured:
            measured[label] = minutes
            borrowed = True
    return measured, borrowed


def walk_minutes(rec: dict) -> float | None:
    """Minutes on foot to the places you named, weighted by how much each matters.

    Weighted over the destinations we actually have a figure for, not all of
    them, so one lookup that failed doesn't quietly drag the average toward zero.
    """
    import database

    measured, _ = walked(rec)
    if not measured:
        return None
    weights = {d["label"]: d.get("weight", 1.0)
               for d in config.destinations_for(database.current())}
    total_w = total = 0.0
    for label, minutes in measured.items():
        w = weights.get(label, 1.0)
        total += minutes * w
        total_w += w
    return total / total_w if total_w else None


# The quantity behind each tier name in config.toml. Adding a tier means adding
# a reader here; renaming one in config alone would silently score nothing.
FACTORS = {
    "walk_minutes": walk_minutes,
    "guest_score": guest_score,
    "reviews": lambda rec: rec.get("reviews"),
    "cleanliness": cleanliness,
    "look": look,
    "spare_beds": spare_beds,
    "shares_without_a_door": shares_without_a_door,
}


# ───────────────────────────────── scoring ─────────────────────────────────

def complaints() -> list[str]:
    """Anything in [scoring] that will silently never fire.

    A tier named for a factor that doesn't exist scores nothing, and so does a
    bonus naming an amenity slug that nothing produces — in both cases with no
    error and a total that looks deliberate. Checked once at startup rather
    than per stay, so it reads as a settings problem rather than a data one.
    """
    import sources  # here, not at module scope: sources imports config, not us.
    import summary

    known_bonuses = (set(sources.AMENITY_ALIASES) | set(summary.TRAITS)
                     | set(summary.KINDS) | {"second_bathroom"})
    out = []
    # Not a settings problem but a code one, and this is the only place that
    # sees both lists. summary.py fills `amenities` using its own patterns; if
    # it learns a slug sources.py has never heard of, that slug silently can't
    # be gated or scored on, and the fact reads as missing rather than as
    # unnameable.
    if adrift := summary.CORE - set(sources.AMENITY_ALIASES):
        out.append(f"summary.py emits amenities sources.py doesn't define: "
                   f"{', '.join(sorted(adrift))}")
    for name in config.TIERS:
        if name not in FACTORS:
            out.append(f"[scoring.tiers.{name}] — no such factor. "
                       f"Have: {', '.join(FACTORS)}")
    for name in config.BONUSES:
        if name not in known_bonuses:
            out.append(f"scoring.bonuses.{name} — nothing produces that. "
                       f"Have: {', '.join(sorted(known_bonuses))}")
    for name in config.REQUIRED_AMENITIES:
        if name not in sources.AMENITY_ALIASES:
            out.append(f"filters.require = {name!r} — no such amenity, so "
                       f"nothing can ever satisfy it")
    for i, dest in enumerate(config.DESTINATIONS):
        # Either way of saying where a place is will do. Coordinates skip the
        # geocoder, which is the point of allowing them.
        located = dest.get("address") or (dest.get("latitude") is not None and
                                          dest.get("longitude") is not None)
        if not dest.get("label") or not located:
            out.append(f"[[destination]] #{i + 1} needs a label, and either an "
                       f"address or a latitude and longitude")
    return out


def tier_points(value: float | None, tier: dict) -> float:
    """Points for the highest band this value qualifies for.

    Bands are tried in the order written, so a config that lists them
    generous-first reads the way it scores.
    """
    if value is None:
        return 0.0
    lower_is_better = tier.get("direction") == "lower"
    for step in tier.get("steps", []):
        bound = step.get("max") if lower_is_better else step.get("min")
        if bound is None:
            continue
        if (value <= bound) if lower_is_better else (value >= bound):
            return float(step.get("points", 0))
    return 0.0


def has(rec: dict, amenity: str) -> bool:
    return amenity in (rec.get("amenities") or [])


def bonus_facts(rec: dict) -> dict[str, bool]:
    """Which flat bonuses this stay earns.

    A bonus config knows nothing but a name and a number of points; this is
    where a name becomes a question about the record.

    Amenities and traits answer here on the same footing, which is the point of
    having gleaned them: `soundproofed` is worth whatever config.toml says it is
    worth, and that it was read out of a paragraph rather than a feature list
    doesn't change the sentence it came from. Points may be negative — a
    `visitor_levy` is a fact you would rather know about, and the only honest
    way to weight it is downward.
    """
    baths = rec.get("bathrooms") or 0
    traits = set(rec.get("traits") or [])
    kind = rec.get("kind")
    facts = {name: has(rec, name) or name in traits or name == kind
             for name in config.BONUSES}
    # One of them isn't a flag at all but a count, and reads better named than
    # written as a tier with a single band.
    if "second_bathroom" in facts:
        facts["second_bathroom"] = baths >= 2
    return facts


def evaluate(rec: dict, share_per_night: float | None = None) -> Breakdown:
    """Score one stay, and show the working.

    `share_per_night` is passed in rather than computed here so that the cost
    side stays in one place — this module never learns what anything costs
    beyond the single division at the end.
    """
    out = Breakdown()

    for name, tier in config.TIERS.items():
        reader = FACTORS.get(name)
        if reader is None:
            continue
        value = reader(rec)
        if value is None:
            out.unknown.append(name)
        out.tiers[name] = tier_points(value, tier)

    facts = bonus_facts(rec)
    for name, points in config.BONUSES.items():
        out.bonuses[name] = float(points) if facts.get(name) else 0.0

    out.points = round(sum(out.tiers.values()) + sum(out.bonuses.values()), 1)

    # Points per unit of cost. `price_unit` only sets the scale of the number —
    # at 25 the figure reads as "points per £25 a share a night" — so changing
    # it re-labels the column without re-ordering it.
    if share_per_night and share_per_night > 0:
        out.value = round(out.points / (share_per_night / config.PRICE_UNIT), 1)
    return out


# ──────────────────────────────── hard gates ───────────────────────────────

def gates(rec: dict) -> list[tuple[str, str]]:
    """Must-haves this stay fails, as (what, "fail" | "unknown").

    Separate from scoring on purpose. A gate is not a preference you can
    out-score: no amount of hot tub buys back a room that doesn't exist.

    "unknown" is not "fail". A stay we simply lack the data on is held back for
    you to check rather than ruled out on our own missing homework.
    """
    out: list[tuple[str, str]] = []

    shares = shares_of(rec)
    if config.REQUIRE_ROOM_PER_SHARE and shares:
        # A room each, not a *bedroom* each. Shares are sleeping units — a
        # couple is one, a singleton is one — and the line that can't be
        # crossed is two of them in the same room, whatever is in it: a room
        # with two beds in it is one room. Whether the room shuts is a
        # different question, worth points rather than a veto, and asked in
        # `shares_without_a_door`.
        total, shut = rooms_to_sleep_in(rec)
        if total is not None and total < shares:
            out.append((f"a room each for {shares} shares, has {total}", "fail"))
        elif total is None and (shut or 0) < shares:
            # No layout, so nothing rules out a sofa bed we were never told
            # about. A stated bedroom count short of the party is a reason to
            # go and look, not a reason to decide.
            out.append((f"a room each for {shares} shares", "unknown"))

    if config.MAX_WALK_MINUTES:
        mins = walk_minutes(rec)
        if mins is None:
            out.append(("walking distance", "unknown"))
        elif mins > config.MAX_WALK_MINUTES:
            out.append((f"walk under {config.MAX_WALK_MINUTES} min, is {mins:.0f}", "fail"))

    for amenity in config.REQUIRED_AMENITIES:
        if rec.get("amenities") is None:
            out.append((amenity, "unknown"))
        elif not has(rec, amenity):
            out.append((amenity, "fail"))

    return out
