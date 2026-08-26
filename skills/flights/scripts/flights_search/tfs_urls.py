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


_CITY_TYPE_ORIGIN = 3


_CITY_TYPE_DEST = 2


_CITY_PROTO_CACHE: dict[str, Any] = {}


def _is_city(code: str | None) -> bool:
    return bool(code) and str(code).startswith("/m/")


_EXTENDED_PROTO_CACHE: dict[Any, dict[str, Any]] = {}


def _extended_proto_classes(repeat_airports: bool = False) -> dict[str, Any]:
    """Airport/FlightData/Info classes extended with hidden wire fields.

    - Airport.type (field 1): city entity marker (origin=3, dest=2) that
      unlocks partner itineraries.
    - repeated from_airport/to_airport (13/14): multi-airport OR searches
      (verified live: GRU+SSA -> MAD returns the union of both baselines).
    """
    key = bool(repeat_airports)
    if key not in _EXTENDED_PROTO_CACHE:
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

        import fast_flights.pb.flights_pb2 as fpb

        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.ParseFromString(fpb.DESCRIPTOR.serialized_pb)
        airport = next(m for m in fdp.message_type if m.name == "Airport")
        fld = airport.field.add()
        fld.name = "type"
        fld.number = 1
        fld.label = 1
        fld.type = 5  # int32
        if repeat_airports:
            fd_msg = next(m for m in fdp.message_type if m.name == "FlightData")
            for f in fd_msg.field:
                if f.name in ("from_airport", "to_airport"):
                    f.label = 3  # LABEL_REPEATED
        pool = descriptor_pool.DescriptorPool()
        pool.Add(fdp)
        get = lambda n: message_factory.GetMessageClass(pool.FindMessageTypeByName(n))
        _EXTENDED_PROTO_CACHE[key] = {n: get(n) for n in ("Airport", "FlightData", "Info")}
    return _EXTENDED_PROTO_CACHE[key]


def _city_proto_classes():
    """Backwards-compatible tuple view of the extended classes."""
    c = _extended_proto_classes(repeat_airports=False)
    return c["Info"], c["FlightData"], c["Airport"]


def city_typed_url(query, origin: str, dest: str) -> str:
    """Rebuild a query's tfs with city-type airports so partners appear."""
    from base64 import b64encode

    InfoT, FlightDataT, AirportT = _city_proto_classes()
    info = InfoT()
    info.ParseFromString(query.to_bytes())
    typed_legs = []
    for leg, fq in zip(info.data, query.flight_data):
        new_leg = FlightDataT()
        new_leg.CopyFrom(leg)
        frm = AirportT()
        frm.CopyFrom(new_leg.from_airport)
        frm.type = _CITY_TYPE_ORIGIN if fq.from_airport == origin else _CITY_TYPE_DEST
        new_leg.from_airport.CopyFrom(frm)
        to = AirportT()
        to.CopyFrom(new_leg.to_airport)
        to.type = _CITY_TYPE_ORIGIN if fq.to_airport == origin else _CITY_TYPE_DEST
        new_leg.to_airport.CopyFrom(to)
        typed_legs.append(new_leg)
    del info.data[:]
    info.data.extend(typed_legs)
    tfs = b64encode(info.SerializeToString()).decode()
    hl = f"&hl={query.language}" if query.language else "&hl="
    curr = f"&curr={query.currency}" if query.currency else ""
    return f"https://www.google.com/travel/flights/search?tfs={tfs}{hl}{curr}"


def multi_airports_tfs_url(query, per_leg: list[tuple[list[str], list[str]]]) -> str:
    """Rebuild a query's tfs with repeated airports per leg (OR semantics).

    per_leg matches query.flight_data order: each entry is (from_codes, to_codes).
    """
    from base64 import b64encode

    if len(per_leg) != len(query.flight_data):
        raise ValueError(f"per_leg has {len(per_leg)} entries, query has {len(query.flight_data)} legs")
    C = _extended_proto_classes(repeat_airports=True)
    info = C["Info"]()
    info.ParseFromString(query.to_bytes())
    new_legs = []
    for leg_idx, (from_codes, to_codes) in enumerate(per_leg):
        new_leg = C["FlightData"]()
        new_leg.CopyFrom(info.data[leg_idx])
        del new_leg.from_airport[:]
        del new_leg.to_airport[:]
        for code in from_codes:
            new_leg.from_airport.append(C["Airport"](airport=code))
        for code in to_codes:
            new_leg.to_airport.append(C["Airport"](airport=code))
        new_legs.append(new_leg)
    del info.data[:]
    info.data.extend(new_legs)
    tfs = b64encode(info.SerializeToString()).decode()
    hl = f"&hl={query.language}" if query.language else "&hl="
    curr = f"&curr={query.currency}" if query.currency else ""
    return f"https://www.google.com/travel/flights/search?tfs={tfs}{hl}{curr}"


def _city_browser_required(query, origin: str, dest: str, extra: dict[str, Any] | None = None) -> None:
    """City-entity queries need a real browser session; emit workable JSON."""
    out: dict[str, Any] = {
        "ok": False,
        "reason": "browser-session-required",
        "detail": (
            "city-entity queries (partnership flights) are served client-side "
            "and gated to real browser sessions; anonymous fetches return no itineraries"
        ),
        "url": city_typed_url(query, origin, dest),
        "hint": (
            "workable: open url via chrome-devtools/safari MCP (navigate_to_url), "
            "then read result cards from page content; airport codes cannot show "
            "partner flights - keep the /m/ city entities"
        ),
    }
    if extra:
        out.update(extra)
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)


def _pair_query(
    from_arg: str,
    to_arg: str,
    dep: str,
    ret: str | None,
    currency: str,
    language: str,
    seat: str,
    passengers_dict: dict[str, int],
    baggage_args: dict[str, Any],
    filters: dict[str, Any],
):
    """Build the normal fast-flights query for one date pair."""
    leg_args = {
        "max_stops": filters.get("max_stops"),
        "airlines": filters.get("airlines"),
        "earliest_departure_hour": filters.get("earliest_departure"),
        "latest_departure_hour": filters.get("latest_departure"),
        "earliest_arrival_hour": filters.get("earliest_arrival"),
        "latest_arrival_hour": filters.get("latest_arrival"),
        "max_duration_minutes": filters.get("max_duration"),
        "connecting_airports": filters.get("connecting"),
        "min_layover_minutes": filters.get("min_layover"),
        "max_layover_minutes": filters.get("max_layover"),
        "less_emissions_only": filters.get("less_emissions", False),
    }
    fq = [
        FlightQuery(date=dep, from_airport=from_arg, to_airport=to_arg, **leg_args)
    ]
    trip = "one-way"
    if ret is not None:
        fq.append(
            FlightQuery(date=ret, from_airport=to_arg, to_airport=from_arg, **leg_args)
        )
        trip = "round-trip"
    return create_query(
        flights=fq,
        seat=seat,
        trip=trip,
        passengers=Passengers(**passengers_dict),
        language=language,
        currency=currency,
        max_price=baggage_args.get("max_price"),
        carry_on_bags=baggage_args.get("carry_on", 0),
        checked_bags=baggage_args.get("checked_bags", 0),
        hide_separate_and_self_transfer=baggage_args.get("hide_separate", False),
        exclude_basic_economy=baggage_args.get("exclude_basic", False),
    )


def _calendar_pair_url(base_query, departure: str, return_date: str | None) -> str:
    """Make a normal Google Flights URL for a native-grid cell."""
    d0 = base_query.flight_data[0].date
    r0 = base_query.flight_data[1].date if len(base_query.flight_data) > 1 else None
    base_query.flight_data[0].date = departure
    if r0 is not None and return_date is not None:
        base_query.flight_data[1].date = return_date
    url = base_query.url()
    base_query.flight_data[0].date = d0
    if r0 is not None:
        base_query.flight_data[1].date = r0
    return url
