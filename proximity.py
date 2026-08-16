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

Google's Distance Matrix, because a key for it already exists next door in
realtor2.0 and the response shape is known-good. The key is read from the
environment — never from config.toml, which is committed.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.parse

import config

PATH = "/maps/api/distancematrix/json"


class NoKey(RuntimeError):
    """No API key in the environment. Raised with the fix in the message."""


class MapsError(RuntimeError):
    """The service refused the whole request — a bad key, a disabled API, a quota.

    Separate from a destination that merely couldn't be routed to, because this
    one will fail identically for every remaining stay. Making it an exception
    is what stops `walk` working through twenty properties to collect twenty
    copies of the same complaint.
    """


def api_key() -> str:
    key = os.environ.get(config.MAPS_KEY_ENV)
    if not key:
        raise NoKey(
            f"No maps key. `walk` needs one to ask how far anything is:\n"
            f"    export {config.MAPS_KEY_ENV}=<your Google Maps API key>\n"
            f"Enable Distance Matrix on the key at console.cloud.google.com."
        )
    return key


def origin_of(rec: dict) -> str | None:
    """Where to measure from, in whatever form we have it.

    Coordinates are exact and free of ambiguity, so they win. Failing those an
    address string is fine — the routing service geocodes it, which is one less
    thing for us to get wrong. A bare property name is the last resort and is
    only as good as the name being unusual.
    """
    lat, lon = rec.get("latitude"), rec.get("longitude")
    if lat is not None and lon is not None:
        return f"{lat},{lon}"
    if rec.get("address"):
        return rec["address"]
    where = ", ".join(x for x in (rec.get("name"), rec.get("location") or
                                  rec.get("region")) if x)
    return where or None


def walk_times(origin: str, destinations: list[dict]) -> tuple[dict[str, int], list[str]]:
    """Minutes on foot from one origin to every destination, in a single call.

    Returns (minutes by label, complaints). Destinations that fail are left out
    rather than defaulted, so a lookup that didn't happen never reads as a walk
    that happens to be zero minutes long.
    """
    labels = [d["label"] for d in destinations]
    query = urllib.parse.urlencode({
        "origins": origin,
        "destinations": "|".join(d["address"] for d in destinations),
        "mode": config.MAPS_MODE,
        "key": api_key(),
    })

    conn = http.client.HTTPSConnection(config.MAPS_HOST, timeout=config.TIMEOUT)
    try:
        conn.request("GET", f"{PATH}?{query}", headers={"User-Agent": config.USER_AGENT})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise MapsError(f"maps returned {resp.status}, not JSON")

    if data.get("status") != "OK":
        # error_message is where Google explains a key that isn't enabled for
        # this API, which is the failure worth reading in full.
        raise MapsError("maps: " + (data.get("error_message") or data.get("status")))

    out: dict[str, int] = {}
    problems: list[str] = []
    elements = (data.get("rows") or [{}])[0].get("elements", [])
    for label, element in zip(labels, elements):
        status = element.get("status")
        if status == "OK":
            out[label] = round(element["duration"]["value"] / 60)
        elif status == "ZERO_RESULTS":
            # No footpath at all — which is an answer, but not one we can put a
            # number on, so it stays absent and gets said out loud instead.
            problems.append(f"{label}: no walking route")
        else:
            problems.append(f"{label}: {status}")
    return out, problems


def describe(rec: dict) -> str:
    """The walk to each destination, shortest first."""
    measured = rec.get("walk_minutes") or {}
    if not measured:
        return ""
    return ", ".join(f"{label} {mins}m"
                     for label, mins in sorted(measured.items(), key=lambda kv: kv[1]))
