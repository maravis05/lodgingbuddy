"""
How long it takes to walk there.

Coordinates tell a human nothing. "56.404, -5.500" is not an answer to the only
question that actually gets asked about a property's location — do we have to
drive, or can we walk? So this stores minutes on foot, one figure per place you
named in config.toml, and never shows a distance.

Straight-line distance would be cheaper and, here, wrong. A sea loch turns six
kilometres into a forty-minute drive, and Argyll is mostly sea lochs. Asking a
routing service what the walk actually is costs one HTTP call per stay and
removes a whole class of confident nonsense.

This is the only part of the tool that hands a stay's location to anyone. It
ships on, because the default provider needs no key and nothing to sign up for,
but `[maps] enabled = false` switches it off completely — nothing here opens a
connection while that is false, and every other command still works.

Two providers. OSRM over OpenStreetMap is the default and wants no key, no
account and no billing profile. Google's Distance Matrix is still here because
the code was written against it and the response shape is known-good; it reads
its key from the environment, never from config.toml, which is committed.

A warning worth keeping in the file, because it costs nothing to get wrong: the
OSRM demo server at router.project-osrm.org accepts a request for the foot
profile and answers it with car timings — the same distance and duration you
get from /driving/, around 26 km/h. It doesn't error, so the numbers look fine
and are five times too small. FOSSGIS runs a genuine pedestrian profile, and
that is what osrm_host points at.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.parse

import config

PATH = "/maps/api/distancematrix/json"

# When the last call to an OSM service went out, so the next one can wait its
# turn. Their usage policy asks for one a second and it is not a suggestion.
_last_call = 0.0

# Geocoding the same destination list for every stay would be both slow and
# rude, and the answers don't change during a run.
_geocoded: dict[str, tuple[float, float]] = {}


class Disabled(RuntimeError):
    """`[maps] enabled` is false, so no lookup was attempted.

    Not an error so much as the configured answer. Kept separate from NoKey
    because the fix is different and because "you haven't asked for this" and
    "you asked but I can't" deserve different sentences.
    """


class NoKey(RuntimeError):
    """No API key in the environment. Raised with the fix in the message."""


class Unlocatable(RuntimeError):
    """This stay's origin couldn't be put on the map. The next one still might.

    A name or a vague address that geocodes to nothing is a fact about one
    record, not about the service, so it must not stop the run the way
    MapsError does. `walk` counts these and moves on.
    """


class MapsError(RuntimeError):
    """The service refused the whole request — a bad key, a disabled API, a quota.

    Separate from a destination that merely couldn't be routed to, because this
    one will fail identically for every remaining stay. Making it an exception
    is what stops `walk` working through twenty properties to collect twenty
    copies of the same complaint.
    """


def check_enabled() -> None:
    """Refuse before anything is sent, not after.

    Called at the top of walk_times as well as by the command, so the guard
    holds no matter who calls in.
    """
    if not config.MAPS_ENABLED:
        raise Disabled(
            "`walk` is switched off, so nothing was sent anywhere.\n"
            "    set `enabled = true` under [maps] in config.toml\n"
            "It's the one command that gives a stay's location to a routing "
            "service, which is why it has a switch at all."
        )


def api_key() -> str:
    key = os.environ.get(config.MAPS_KEY_ENV)
    if not key:
        raise NoKey(
            f"No maps key. Google's Distance Matrix needs one.\n"
            f"Put it in the environment as {config.MAPS_KEY_ENV} — with\n"
            f"`export` on macOS and Linux, `$env:` in PowerShell, `set` in\n"
            f"Command Prompt — and enable Distance Matrix on the key at\n"
            f"console.cloud.google.com.\n"
            f"Or set `provider = \"osrm\"` under [maps] and need no key at all."
        )
    return key


def _get_json(host: str, path: str) -> dict | list:
    """One GET, decoded. Shared so every call carries the same identification."""
    conn = http.client.HTTPSConnection(host, timeout=config.TIMEOUT)
    try:
        conn.request("GET", path, headers={"User-Agent": config.MAPS_USER_AGENT})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise MapsError(f"{host} returned {resp.status}, not JSON")


def _polite() -> None:
    """Wait, if the last call was recent enough to make this one rude."""
    global _last_call
    gap = time.monotonic() - _last_call
    if gap < config.MAPS_MIN_INTERVAL:
        time.sleep(config.MAPS_MIN_INTERVAL - gap)
    _last_call = time.monotonic()


def geocode(text: str) -> tuple[float, float] | None:
    """An address to a coordinate, via Nominatim. None if it can't be placed.

    Cached for the run: a destination list gets geocoded once, not once per
    stay, which is the difference between three calls and three hundred.
    """
    if text in _geocoded:
        return _geocoded[text]

    _polite()
    query = urllib.parse.urlencode({"q": text, "format": "json", "limit": 1})
    found = _get_json(config.GEOCODER_HOST, f"/search?{query}")
    if not isinstance(found, list) or not found:
        return None

    place = found[0]
    try:
        pair = (float(place["lat"]), float(place["lon"]))
    except (KeyError, TypeError, ValueError):
        return None
    _geocoded[text] = pair
    return pair


def origin_of(rec: dict) -> str | None:
    """Where to measure from, in whatever form we have it.

    Coordinates are exact and free of ambiguity, so they win. Failing those an
    address string is fine — it gets geocoded, which is one less thing for us
    to get wrong. A bare property name is the last resort and is only as good
    as the name being unusual.
    """
    lat, lon = rec.get("latitude"), rec.get("longitude")
    if lat is not None and lon is not None:
        return f"{lat},{lon}"
    if rec.get("address"):
        return rec["address"]
    where = ", ".join(x for x in (rec.get("name"), rec.get("location") or
                                  rec.get("region")) if x)
    return where or None


def _as_coords(text: str) -> tuple[float, float] | None:
    """A "lat,lon" string back into numbers, or an address geocoded into them."""
    parts = text.split(",")
    if len(parts) == 2:
        try:
            return (float(parts[0]), float(parts[1]))
        except ValueError:
            pass
    return geocode(text)


def _osrm_table(origin: str, destinations: list[dict]) -> tuple[dict[str, int], list[str]]:
    """Minutes on foot from one origin to every destination, in a single call.

    OSRM's table service is the direct analogue of a distance matrix: give it
    the origin and every destination, ask for durations from source 0, and get
    one row back. Destinations that need geocoding are resolved first, and one
    that can't be placed is left out of the request rather than silently
    shifting the row it would have occupied.
    """
    here = _as_coords(origin)
    if here is None:
        raise Unlocatable(f"couldn't place {origin!r} on the map")

    points, labels, problems = [here], [], []
    for dest in destinations:
        if dest.get("latitude") is not None and dest.get("longitude") is not None:
            there = (float(dest["latitude"]), float(dest["longitude"]))
        else:
            there = _as_coords(dest["address"])
        if there is None:
            problems.append(f"{dest['label']}: couldn't place that address")
            continue
        points.append(there)
        labels.append(dest["label"])

    if not labels:
        return {}, problems

    # OSRM wants lon,lat — the opposite order to every other thing here, and a
    # transposition it will happily route across rather than complain about.
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    query = urllib.parse.urlencode({"sources": "0", "annotations": "duration"})
    path = f"/{config.OSRM_PROFILE}/table/v1/driving/{coords}?{query}"

    _polite()
    data = _get_json(config.OSRM_HOST, path)

    if not isinstance(data, dict) or data.get("code") != "Ok":
        message = (data or {}).get("message") if isinstance(data, dict) else None
        raise MapsError("osrm: " + (message or (data or {}).get("code", "no answer")))

    # One row, because there is one source. Its first entry is the origin's
    # distance to itself, so the destinations start at index 1.
    row = (data.get("durations") or [[]])[0][1:]
    out: dict[str, int] = {}
    for label, seconds in zip(labels, row):
        if seconds is None:
            # No walking route at all — an answer, but not one we can put a
            # number on, so it stays absent and gets said out loud instead.
            problems.append(f"{label}: no walking route")
        else:
            out[label] = round(seconds / 60)
    return out, problems


def _google_place(dest: dict) -> str:
    """A destination as Distance Matrix wants it — coordinates if we have them."""
    if dest.get("latitude") is not None and dest.get("longitude") is not None:
        return f"{dest['latitude']},{dest['longitude']}"
    return dest["address"]


def _google_matrix(origin: str, destinations: list[dict]) -> tuple[dict[str, int], list[str]]:
    """The same question put to Distance Matrix, which takes addresses directly."""
    labels = [d["label"] for d in destinations]
    query = urllib.parse.urlencode({
        "origins": origin,
        # Distance Matrix takes "lat,lon" wherever it takes an address, so a
        # destination pinned by coordinates needs no special handling — but it
        # does need asking for, rather than assuming every entry has an address.
        "destinations": "|".join(_google_place(d) for d in destinations),
        "mode": config.MAPS_MODE,
        "key": api_key(),
    })

    data = _get_json(config.MAPS_HOST, f"{PATH}?{query}")
    if not isinstance(data, dict) or data.get("status") != "OK":
        # error_message is where Google explains a key that isn't enabled for
        # this API, which is the failure worth reading in full.
        data = data if isinstance(data, dict) else {}
        raise MapsError("maps: " + (data.get("error_message") or
                                    data.get("status") or "no answer"))

    out: dict[str, int] = {}
    problems: list[str] = []
    elements = (data.get("rows") or [{}])[0].get("elements", [])
    for label, element in zip(labels, elements):
        status = element.get("status")
        if status == "OK":
            out[label] = round(element["duration"]["value"] / 60)
        elif status == "ZERO_RESULTS":
            problems.append(f"{label}: no walking route")
        else:
            problems.append(f"{label}: {status}")
    return out, problems


def walk_times(origin: str, destinations: list[dict]) -> tuple[dict[str, int], list[str]]:
    """Minutes on foot from one origin to every destination, in a single call.

    Returns (minutes by label, complaints). Destinations that fail are left out
    rather than defaulted, so a lookup that didn't happen never reads as a walk
    that happens to be zero minutes long.
    """
    check_enabled()
    if config.MAPS_PROVIDER == "osrm":
        return _osrm_table(origin, destinations)
    if config.MAPS_PROVIDER == "google":
        return _google_matrix(origin, destinations)
    raise MapsError(
        f"Unknown maps provider {config.MAPS_PROVIDER!r}. "
        f"`provider` under [maps] takes \"osrm\" or \"google\"."
    )


def describe(rec: dict) -> str:
    """The walk to each destination, shortest first."""
    measured = rec.get("walk_minutes") or {}
    if not measured:
        return ""
    return ", ".join(f"{label} {mins}m"
                     for label, mins in sorted(measured.items(), key=lambda kv: kv[1]))
