import argparse
import datetime as _dt
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

# fast-flights imports
try:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    from fast_flights.exceptions import FlightsNotFound
    from fast_flights.fetcher import fetch_flights_html
    from fast_flights.parser import parse as parse_flights_html

    _HAS_FETCH = True
except ImportError as e:
    print(json.dumps({"ok": False, "reason": "error", "detail": f"fast-flights not installed: {e}. Run: /opt/homebrew/bin/pip3.14 install --break-system-packages fast-flights", "hint": "install deps: primp, protobuf, selectolax"}))
    sys.exit(1)


def parse_legs_arg(legs_str: str):
    try:
        data = json.loads(legs_str)
        if not isinstance(data, list):
            raise ValueError("legs must be a JSON array")
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid --legs JSON: {e}. Hint: use '[{{\"from\":\"MYJ\",\"to\":\"TPE\",\"date\":\"2026-08-25\"}}]'") from e


def build_flight_queries(args) -> list[FlightQuery]:
    legs = []
    if args.legs:
        raw = parse_legs_arg(args.legs)
        for item in raw:
            legs.append(
                FlightQuery(
                    date=item["date"],
                    from_airport=item["from"],
                    to_airport=item["to"],
                    max_stops=args.max_stops,
                    airlines=args.airlines.split(",") if args.airlines else None,
                    earliest_departure_hour=args.earliest_departure,
                    latest_departure_hour=args.latest_departure,
                    earliest_arrival_hour=args.earliest_arrival,
                    latest_arrival_hour=args.latest_arrival,
                    max_duration_minutes=args.max_duration,
                    connecting_airports=args.connecting.split(",") if args.connecting else None,
                    min_layover_minutes=args.min_layover,
                    max_layover_minutes=args.max_layover,
                    less_emissions_only=bool(args.less_emissions),
                )
            )
    else:
        if not args.from_ or not args.to or not args.date:
            raise ValueError("--from, --to, --date are required (or use --legs or --explore). Example: --from GRU --to JFK --date 2026-09-15")

        legs.append(
            FlightQuery(
                date=args.date,
                from_airport=args.from_,
                to_airport=args.to,
                max_stops=args.max_stops,
                airlines=args.airlines.split(",") if args.airlines else None,
                earliest_departure_hour=args.earliest_departure,
                latest_departure_hour=args.latest_departure,
                earliest_arrival_hour=args.earliest_arrival,
                latest_arrival_hour=args.latest_arrival,
                max_duration_minutes=args.max_duration,
                connecting_airports=args.connecting.split(",") if args.connecting else None,
                min_layover_minutes=args.min_layover,
                max_layover_minutes=args.max_layover,
                less_emissions_only=bool(args.less_emissions),
            )
        )
        if args.return_date:
            # return leg is TO -> FROM
            legs.append(
                FlightQuery(
                    date=args.return_date,
                    from_airport=args.to,
                    to_airport=args.from_,
                    max_stops=args.max_stops,
                    airlines=args.airlines.split(",") if args.airlines else None,
                    earliest_departure_hour=args.earliest_departure,
                    latest_departure_hour=args.latest_departure,
                    earliest_arrival_hour=args.earliest_arrival,
                    latest_arrival_hour=args.latest_arrival,
                    max_duration_minutes=args.max_duration,
                    connecting_airports=args.connecting.split(",") if args.connecting else None,
                    min_layover_minutes=args.min_layover,
                    max_layover_minutes=args.max_layover,
                    less_emissions_only=bool(args.less_emissions),
                )
            )
    return legs
