#!/usr/bin/env python3
"""Generate flight-search destination candidates from Jonty's route graph.

This is a stateless producer: it fetches the public route JSON, emits one JSON
object (or JSONL destination records), and never stores a local database.
It discovers candidate airports; a live flight search must still verify dates,
fares, and availability.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from collections import deque
from typing import Any


DEFAULT_URL = (
    "https://raw.githubusercontent.com/Jonty/airline-route-data/main/"
    "airline_routes.json"
)
AIRPORT_RE = re.compile(r"^[A-Z0-9]{3}$")
AIRLINE_RE = re.compile(r"^[A-Z0-9]{2,3}$")


def _error(detail: str, exit_code: int = 1) -> None:
    print(json.dumps({"ok": False, "reason": "error", "detail": detail}, ensure_ascii=False))
    raise SystemExit(exit_code)


def _codes(value: str, flag: str) -> set[str]:
    codes = {part.strip().upper() for part in value.split(",") if part.strip()}
    bad = sorted(code for code in codes if not AIRLINE_RE.fullmatch(code))
    if bad:
        _error(f"{flag} contains invalid IATA airline code(s): {', '.join(bad)}")
    return codes


def _load_routes(url: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "google-flights-skill/airline-destinations"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:
        _error(f"could not fetch route dataset: {exc}")
    if not isinstance(payload, dict):
        _error("route dataset is not a JSON object indexed by airport")
    return payload


def _route_carriers(route: dict[str, Any]) -> set[str]:
    return {
        str(carrier.get("iata", "")).upper()
        for carrier in route.get("carriers", [])
        if carrier.get("iata")
    }


def _route_record(origin: str, route: dict[str, Any], path: list[str], carrier_path: list[list[str]]) -> dict[str, Any]:
    return {
        "from": origin,
        "to": str(route.get("iata", "")).upper(),
        "carriers": sorted(_route_carriers(route)),
        "path": path,
        "carrier_path": carrier_path,
        "km": route.get("km"),
        "min": route.get("min"),
    }


def discover(args: argparse.Namespace) -> dict[str, Any]:
    routes = _load_routes(args.url, args.timeout)
    origin = args.origin.upper()
    if origin not in routes:
        _error(f"origin {origin} is not present in the route dataset")

    allowed = _codes(args.airlines, "--airlines")
    anchor = args.anchor.upper() if args.anchor else None
    if anchor and anchor not in allowed:
        _error("--anchor must also appear in --airlines")
    if args.mode == "anchor" and not anchor:
        _error("--mode anchor requires --anchor, e.g. --anchor G3")
    if args.max_hops < 1:
        _error("--max-hops must be at least 1")

    origin_country = routes[origin].get("country_code")
    queue = deque([(origin, 0, [origin], [], False)])
    visited: set[tuple[str, int, bool]] = {(origin, 0, False)}
    found: dict[str, dict[str, Any]] = {}

    while queue:
        airport, hops, path, carrier_path, anchor_seen = queue.popleft()
        if hops >= args.max_hops:
            continue
        for route in routes.get(airport, {}).get("routes", []):
            destination = str(route.get("iata", "")).upper()
            if not AIRPORT_RE.fullmatch(destination) or destination == origin:
                continue
            carriers = _route_carriers(route)
            matching = sorted(carriers & allowed)
            if not matching:
                continue
            if args.mode == "anchor" and hops == 0 and anchor not in carriers:
                continue

            next_anchor_seen = anchor_seen or (anchor in carriers if anchor else False)
            if args.mode == "anchor" and not next_anchor_seen:
                continue
            next_path = path + [destination]
            next_carrier_path = carrier_path + [matching]
            destination_entry = routes.get(destination, {})
            is_international = (
                origin_country is None
                or destination_entry.get("country_code") != origin_country
            )
            if args.international and not is_international:
                # Still traverse domestic GOL/partner gateways, but do not emit
                # them as final international candidates.
                pass
            else:
                candidate = _route_record(airport, route, next_path, next_carrier_path)
                candidate.update(
                    {
                        "city": destination_entry.get("city_name"),
                        "display_name": destination_entry.get("display_name", destination),
                        "country_code": destination_entry.get("country_code"),
                        "hops": hops + 1,
                        "international": is_international,
                    }
                )
                previous = found.get(destination)
                if previous is None or (candidate["hops"], candidate["path"]) < (
                    previous["hops"],
                    previous["path"],
                ):
                    found[destination] = candidate

            state = (destination, hops + 1, next_anchor_seen)
            if state not in visited:
                visited.add(state)
                queue.append((destination, hops + 1, next_path, next_carrier_path, next_anchor_seen))

    destinations = sorted(
        found.values(),
        key=lambda item: (item.get("hops", 99), item.get("display_name") or item["to"]),
    )
    return {
        "ok": True,
        "source": {
            "url": args.url,
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "name": "Jonty/airline-route-data",
        },
        "query": {
            "origin": origin,
            "airlines": sorted(allowed),
            "mode": args.mode,
            "anchor": anchor,
            "max_hops": args.max_hops,
            "international": args.international,
        },
        "count": len(destinations),
        "destinations": destinations,
        "notes": [
            "Candidates come from route data; verify dates, fares, and ticketing with live flight search.",
            "In anchor mode, the first edge must contain the anchor carrier; later edges may contain any --airlines carrier.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate airline destination candidates from Jonty's public route graph."
    )
    parser.add_argument("--from", dest="origin", required=True, help="origin airport IATA code")
    parser.add_argument("--airlines", required=True, help="comma-separated allowed carrier IATA codes")
    parser.add_argument("--mode", choices=("strict", "anchor"), default="strict")
    parser.add_argument("--anchor", help="required anchor carrier for --mode anchor, e.g. G3")
    parser.add_argument("--max-hops", type=int, default=2, help="maximum route-graph hops (default: 2)")
    parser.add_argument("--international", action="store_true", help="emit only destinations outside the origin country")
    parser.add_argument("--url", default=DEFAULT_URL, help="route JSON URL")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--format", choices=("json", "jsonl", "codes"), default="json")
    args = parser.parse_args()
    if not AIRPORT_RE.fullmatch(args.origin.upper()):
        _error("--from must be a three-character airport IATA code")
    result = discover(args)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False))
    elif args.format == "codes":
        for destination in result["destinations"]:
            print(destination["to"])
    else:
        for destination in result["destinations"]:
            print(json.dumps(destination, ensure_ascii=False))


if __name__ == "__main__":
    main()
