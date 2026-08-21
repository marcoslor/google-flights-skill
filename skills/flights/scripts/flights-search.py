#!/opt/homebrew/bin/python3.14
"""flights-search.py — deterministic Google Flights search facade for opencode.

Wraps AWeirdDev/flights (fast-flights) — Base64 protobuf + primp + selectolax.
One call, one JSON line. No browser, no login.

Interface (only what an agent needs):
  flights-search.py --from GRU --to JFK --date 2026-09-01 [flags]
  flights-search.py --from GRU --to JFK --date 2026-09-01 --return-date 2026-09-10 [flags]
  flights-search.py --legs '[{"from":"MYJ","to":"TPE","date":"2026-08-25"},...]' [flags]
  flights-search.py --help

Output -> one JSON line:
  { ok:true, query:{...}, count, flights:[{price,airlines,type,flights:[...],carbon}], metadata, url }
  { ok:true, count:0, flights:[] }  # no flights (not error)
  { ok:false, reason:"error", detail }

Client-side flags --limit/--sort/--top/--min-price/--max-price-client filter the
already-fetched list, mirroring shopee-search.mjs ergonomics.
"""
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


def emit_error(detail: str, hint: str | None = None, extra: dict[str, Any] | None = None, exit_code: int = 1):
    payload: dict[str, Any] = {"ok": False, "reason": "error", "detail": detail}
    if hint:
        payload["hint"] = hint
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code)


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


def flight_to_dict(f) -> dict[str, Any]:
    return {
        "price": f.price,
        "airlines": f.airlines,
        "type": f.type,
        "flights": [
            {
                "from": {"code": s.from_airport.code, "name": s.from_airport.name},
                "to": {"code": s.to_airport.code, "name": s.to_airport.name},
                "departure": {
                    "date": list(s.departure.date),
                    "time": list(s.departure.time),
                    "iso": f"{s.departure.date[0]:04d}-{s.departure.date[1]:02d}-{s.departure.date[2]:02d} {s.departure.time[0]:02d}:{s.departure.time[1]:02d}",
                },
                "arrival": {
                    "date": list(s.arrival.date),
                    "time": list(s.arrival.time),
                    "iso": f"{s.arrival.date[0]:04d}-{s.arrival.date[1]:02d}-{s.arrival.date[2]:02d} {s.arrival.time[0]:02d}:{s.arrival.time[1]:02d}",
                },
                "duration": s.duration,
                "plane_type": s.plane_type,
            }
            for s in f.flights
        ],
        "carbon": {"emission": f.carbon.emission, "typical": f.carbon.typical_on_route},
    }


def _payload_from_html(html: str) -> list[Any] | None:
    """Parse the embedded JS data blob from a fetched search page."""
    try:
        from selectolax.lexbor import LexborHTMLParser
    except ImportError:
        return None
    try:
        parser = LexborHTMLParser(html)
        script = parser.css_first(r"script.ds\:1")
        if not script:
            return None
        js = script.text()
        data = js.split("data:", 1)[1].rsplit(",", 1)[0]
        return json.loads(data)
    except Exception:
        return None


def extract_price_insights(payload: list[Any] | None) -> dict[str, Any] | None:
    """Price insights panel — free, same request (payload[5][1..5]).

    Slot semantics verified against the UI ("Prices are currently high"):
      [5][1]=current cheapest for these dates, [5][2]=typical price,
      [5][4]/[5][5]=usual low/high band. UI flags "high" when current > high.
    """
    if not isinstance(payload, list) or len(payload) <= 5 or not isinstance(payload[5], list) or len(payload[5]) <= 5:
        return None

    def _price(slot: int) -> int | float | None:
        try:
            v = payload[5][slot][1]
            return v if isinstance(v, (int, float)) else None
        except Exception:
            return None

    current, typical, low, high = _price(1), _price(2), _price(4), _price(5)
    if current is None or low is None or high is None:
        return None
    out: dict[str, Any] = {
        "current_cheapest": current,
        "typical": typical,
        "usual_low": low,
        "usual_high": high,
        "verdict": "high" if current > high else ("low" if current < low else "normal"),
    }
    if typical is not None:
        out["current_vs_typical"] = current - typical
    return out


def extract_price_graph_from_payload(payload: list[Any] | None) -> list[dict[str, Any]] | None:
    """Native Google price graph (61 days, fixed stay) at payload[5][10][0]."""
    try:
        p5 = payload[5] if isinstance(payload, list) else None
        if not p5 or len(p5) <= 10 or not p5[10]:
            return None
        graph_raw = p5[10][0] if isinstance(p5[10][0], list) and p5[10][0] and isinstance(p5[10][0][0], list) else None
        if not graph_raw:
            return None
        out = []
        for ts, price in graph_raw:
            try:
                d = _dt.datetime.fromtimestamp(ts / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                d = str(ts)
            out.append({"date": d, "price": price})
        return out
    except Exception:
        return None


def _parse_date(s: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"invalid date '{s}' - expected YYYY-MM-DD") from None


# ── Public dataset: airline routes (out-of-box, no API key) ──
# Primary: Jonty/airline-route-data weekly dump (flightsfrom.com scrape, 5MB)
# Fallback: mvanlaar fork — both public raw.githubusercontent.com, no Cloudflare.
# flightsfrom.com live HTML is Cloudflare managed (primp chrome_145 still 403),
# so raw JSON is the only reliable out-of-box source. Stale ~months, but
# live validation via fast-flights probe compensates.
_AIRLINE_ROUTES_URLS = [
    "https://raw.githubusercontent.com/Jonty/airline-route-data/main/airline_routes.json",
    "https://raw.githubusercontent.com/mvanlaar/airline-route-data/main/airline_routes.json",
]
_CACHE_DIR = Path.home() / ".cache" / "opencode"
_CACHE_FILE = _CACHE_DIR / "airline_routes.json"


def _fetch_airline_routes(cache_ttl_h: int = 24) -> dict[str, Any] | None:
    """Fetch airline_routes.json with TTL cache (public, no key)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if _CACHE_FILE.exists():
            age_h = (time.time() - _CACHE_FILE.stat().st_mtime) / 3600
            if age_h < cache_ttl_h:
                try:
                    return json.loads(_CACHE_FILE.read_text())
                except Exception:
                    pass  # refetch
        # fetch
        last_err = None
        for url in _AIRLINE_ROUTES_URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "flights-search/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                    # cache
                    try:
                        _CACHE_FILE.write_text(json.dumps(data))
                    except Exception:
                        pass
                    return data
            except Exception as e:
                last_err = e
                continue
        # fallback to stale cache if fetch failed
        if _CACHE_FILE.exists():
            try:
                return json.loads(_CACHE_FILE.read_text())
            except Exception:
                pass
        return None
    except Exception:
        return None


def _grid_chunks_for_window(window: int) -> int:
    """How many ≤200-cell GetCalendarGrid rectangles a ±window request needs."""
    dep_days = 2 * window + 1
    dep_chunks = max(1, -(-dep_days // 13))
    ret_chunk_days = max(1, _CALENDAR_MAX_CELLS // min(dep_days, 13))
    ret_chunks = max(1, -(-dep_days // ret_chunk_days))
    return dep_chunks * ret_chunks


def _explore_request_estimate(n_dests: int, window: int | None) -> int:
    """Estimated Google RPC count for an explore run."""
    return n_dests * (_grid_chunks_for_window(window) if window is not None else 1)


def get_public_destinations(
    origin: str,
    airlines_filter: list[str] | None = None,
    intl_only: bool = False,
    scope: str = "direct",
    cache_ttl_h: int = 24,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return destinations from public dataset (no API key).

    scope=direct: only routes from origin on filtered airline(s)
    scope=network: all intl destinations served by airline(s) from any BR airport
                  (reachable from origin via 1-stop on same airline - e.g. SSA→GRU→MIA)
    Returns (destinations, meta) where destinations=[{iata,country_code,display_name}]
    """
    data = _fetch_airline_routes(cache_ttl_h)
    if not data:
        return [], {"error": "failed to fetch airline_routes.json", "ttl_h": cache_ttl_h}
    origin = origin.upper().strip()
    airlines_filter = [a.upper() for a in airlines_filter] if airlines_filter else None

    def match_carrier(carriers):
        if not airlines_filter:
            return True
        return any(c.get("iata", "").upper() in airlines_filter for c in carriers)

    meta: dict[str, Any] = {"source": "Jonty/airline-route-data", "origin": origin, "scope": scope}
    dests: list[dict[str, Any]] = []

    if scope == "direct":
        entry = data.get(origin)
        if not entry:
            return [], {**meta, "error": f"origin {origin} not in dataset"}
        origin_cc = entry.get("country_code")
        for r in entry.get("routes", []):
            if not match_carrier(r.get("carriers", [])):
                continue
            dest_iata = r.get("iata")
            dest_entry = data.get(dest_iata, {})
            dest_cc = dest_entry.get("country_code")
            if intl_only and dest_cc == origin_cc:
                continue
            if intl_only and not dest_cc:
                # unknown country - keep but mark
                pass
            dests.append(
                {
                    "iata": dest_iata,
                    "country_code": dest_cc,
                    "display_name": dest_entry.get("display_name", dest_iata),
                    "km": r.get("km"),
                    "min": r.get("min"),
                }
            )
            # dedupe already unique per origin
        # sort intl first then km asc
        dests.sort(key=lambda x: (x.get("country_code") == origin_cc, x.get("km") or 99999))
        meta["count"] = len(dests)
        # staleness warning
        try:
            age_h = (time.time() - _CACHE_FILE.stat().st_mtime) / 3600 if _CACHE_FILE.exists() else 999
            meta["cache_age_h"] = round(age_h, 1)
            if age_h > 24 * 30:
                meta["stale_warning"] = f"cache {round(age_h/24)}d old - run with live probe to validate"
        except Exception:
            pass
        return dests, meta
    else:  # network - generic 1-stop: direct + via hub (any origin, any airline, no BR hard-code)
        origin_cc = data.get(origin, {}).get("country_code")
        seen: dict[str, dict] = {}
        direct_hubs: list[str] = []
        # direct
        origin_entry = data.get(origin)
        if not origin_entry:
            return [], {**meta, "error": f"origin {origin} not in dataset"}
        for r in origin_entry.get("routes", []):
            if not match_carrier(r.get("carriers", [])):
                continue
            dest_iata = r.get("iata")
            dest_entry = data.get(dest_iata, {})
            dest_cc = dest_entry.get("country_code")
            if intl_only and dest_cc == origin_cc:
                continue
            if dest_iata not in seen:
                seen[dest_iata] = {
                    "iata": dest_iata,
                    "country_code": dest_cc,
                    "display_name": dest_entry.get("display_name", dest_iata),
                    "km": r.get("km"),
                    "min": r.get("min"),
                    "via": None,
                }
                direct_hubs.append(dest_iata)
        # 1-stop via hubs on same airlines
        for hub in direct_hubs:
            hub_entry = data.get(hub)
            if not hub_entry:
                continue
            for r in hub_entry.get("routes", []):
                if not match_carrier(r.get("carriers", [])):
                    continue
                dest_iata = r.get("iata")
                if dest_iata == origin or dest_iata in seen:
                    continue
                dest_entry = data.get(dest_iata, {})
                dest_cc = dest_entry.get("country_code")
                if not dest_cc:
                    continue
                if intl_only and dest_cc == origin_cc:
                    continue
                seen[dest_iata] = {
                    "iata": dest_iata,
                    "country_code": dest_cc,
                    "display_name": dest_entry.get("display_name", dest_iata),
                    "km": r.get("km"),
                    "min": r.get("min"),
                    "via": hub,
                }
        dests = sorted(seen.values(), key=lambda x: (x["via"] is not None, x["iata"]))
        meta["count"] = len(dests)
        meta["direct"] = len(direct_hubs)
        return dests, meta




# ── Native Google Flights date grid RPC ──
# The web UI uses this endpoint instead of searching every date pair.  The
# request is a batchexecute-style positional JSON envelope and the response is
# a length-prefixed stream of wrb.fr JSON chunks.
_CALENDAR_GRID_ENDPOINT = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetCalendarGrid"
)
_CALENDAR_FRONTEND_FALLBACK = "boq_travel-frontend-flights-ui_20260819.01_p0"
_CALENDAR_MAX_CELLS = 200

# ── City entities (/m/...) ──
# Google's airline filter is strict for airport codes (SSA/MAD + G3 => zero
# results) but includes partner-operated itineraries when the query uses
# Freebase city entities. The Airport proto message has an undocumented
# `type` field (1) that fast-flights does not model: origin city = 3,
# destination city = 2 (verified against a live capture).
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


def _split_codes(value: str | None) -> list[str]:
    """Split comma-separated IATA/entity codes: 'SSA,GRU' -> ['SSA','GRU']."""
    if not value:
        return []
    return [c.strip() for c in value.split(",") if c.strip()]


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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def expand_nearby(
    codes: list[str],
    radius_km: int = 120,
    limit: int = 8,
    cache_ttl_h: int = 24,
) -> tuple[dict[str, list[str]] | None, str | None]:
    """Expand each airport with neighbors within radius_km (offline dataset).

    Returns ({code: [nearby codes incl. itself]}, error_message|None).
    """
    data = _fetch_airline_routes(cache_ttl_h)
    if not data:
        return None, "airline_routes.json unavailable (dataset fetch failed and no cache)"
    out: dict[str, list[str]] = {}
    for code in codes:
        entry = data.get(code.upper(), {})
        try:
            lat0, lon0 = float(entry.get("latitude")), float(entry.get("longitude"))
        except (TypeError, ValueError):
            out[code.upper()] = [code.upper()]
            continue
        scored = []
        for iata, e in data.items():
            try:
                d = _haversine_km(lat0, lon0, float(e.get("latitude")), float(e.get("longitude")))
            except (TypeError, ValueError):
                continue
            if d <= radius_km:
                scored.append((d, iata))
        scored.sort()
        out[code.upper()] = [iata for _, iata in scored[:limit]]
    return out, None


def fetch_search_html(url: str, proxy: str | None = None) -> str:
    """Fetch any Google Flights URL with the same impersonation as fast-flights.

    Needed because fetch_flights_html only accepts Query objects (it mangles
    plain URLs into ?q=...)."""
    from primp import Client

    client = Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        proxy=proxy,
        cookie_store=True,
        timeout=30,
    )
    response = client.get(url)
    response.raise_for_status()
    return response.text


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


def _calendar_leg(
    from_arg: str,
    to_arg: str,
    date: str,
    filters: dict[str, Any],
) -> list[Any]:
    """Serialize one flight leg in the shape used by GetCalendarGrid."""
    times = [
        filters.get("earliest_departure"),
        filters.get("latest_departure"),
        filters.get("earliest_arrival"),
        filters.get("latest_arrival"),
    ]
    time_filter = times if any(v is not None for v in times) else None
    max_duration = (
        [filters["max_duration"]] if filters.get("max_duration") is not None else None
    )
    max_stops = filters.get("max_stops")
    # CLI values are a maximum stop count (0 = nonstop). The calendar RPC
    # uses Google's enum (0 = any, 1 = nonstop, 2 = <=1, ...).
    stops = 0 if max_stops is None else max_stops + 1
    airlines = filters.get("airlines") or None
    connecting = filters.get("connecting") or None
    min_layover = filters.get("min_layover")
    max_layover = filters.get("max_layover")
    emissions = [1] if filters.get("less_emissions") else None
    return [
        [[[from_arg, 0]]],
        [[[to_arg, 0]]],
        time_filter,
        stops,
        airlines,
        None,
        date,
        max_duration,
        None,
        connecting,
        None,
        min_layover,
        max_layover,
        emissions,
        3,
    ]


def _calendar_request_body(
    from_arg: str,
    to_arg: str,
    departure: str,
    return_date: str | None,
    dep_start: str,
    dep_end: str,
    ret_start: str | None,
    ret_end: str | None,
    seat: str,
    passengers: dict[str, int],
    filters: dict[str, Any],
    max_price: int | None,
    baggage: dict[str, Any],
) -> str:
    one_way = return_date is None
    seat_number = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}[seat]
    passenger_list = [
        passengers.get("adults", 0),
        passengers.get("children", 0),
        passengers.get("infants_in_seat", 0),
        passengers.get("infants_on_lap", 0),
    ]
    if sum(passenger_list) == 0:
        passenger_list[0] = 1
    baggage_value = None
    if baggage.get("carry_on") or baggage.get("checked_bags"):
        baggage_value = [baggage.get("carry_on", 0), baggage.get("checked_bags", 0)]
    legs = [_calendar_leg(from_arg, to_arg, departure, filters)]
    if not one_way:
        legs.append(_calendar_leg(to_arg, from_arg, return_date, filters))
    itinerary = [
        None,
        None,
        # Google trip enum: 1 = round-trip, 2 = one-way
        2 if one_way else 1,
        None,
        [],
        seat_number,
        passenger_list,
        [None, max_price] if max_price is not None else None,
        None,
        None,
        baggage_value,
        None,
        None,
        legs,
        None,
        None,
        None,
        1,
    ]
    inner = [None, itinerary, [dep_start, dep_end], None if one_way else [ret_start, ret_end]]
    # The service expects the second f.req item to be a JSON string, not an
    # object. urlencode supplies the same form encoding as the browser.
    # No CSRF token needed: `at` is optional on this endpoint (verified).
    return urlencode(
        {
            "f.req": json.dumps(
                [None, json.dumps(inner, separators=(",", ":"))],
                separators=(",", ":"),
            ),
        }
    )


def _calendar_wrb_inners(text: str) -> list[Any]:
    """Decode Google's length-prefixed wrb.fr response stream."""
    raw = text.encode("utf-8").lstrip()
    if raw.startswith(b")]}'"):
        raw = raw[4:].lstrip()
    inners: list[Any] = []
    while raw:
        if raw[:1].isdigit():
            newline = raw.find(b"\n")
            if newline < 0:
                break
            try:
                length = int(raw[:newline])
            except ValueError:
                break
            start = newline + 1
            payload = raw[start : start + max(length - 1, 0)]
            raw = raw[start + max(length - 1, 0) :].lstrip()
        else:
            payload = raw
            raw = b""
        try:
            outer = json.loads(payload.strip().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        for row in outer if isinstance(outer, list) else []:
            if isinstance(row, list) and len(row) >= 3 and row[0] == "wrb.fr":
                try:
                    inners.append(json.loads(row[2]))
                except (TypeError, json.JSONDecodeError):
                    pass
    return inners


def _parse_calendar_grid(text: str) -> list[dict[str, Any]]:
    """Parse grid cells. Round-trip: [dep, ret, [[?, price], token], ...].
    One-way (single-leg request): [date, null, [[null, price], token], 1]."""
    entries: list[dict[str, Any]] = []
    for inner in _calendar_wrb_inners(text):
        raw_entries = inner[1] if isinstance(inner, list) and len(inner) > 1 else []
        for item in raw_entries if isinstance(raw_entries, list) else []:
            try:
                if not isinstance(item[0], str):
                    continue
                price_data = item[2]
                price = price_data[0][1]
                if not isinstance(price, (int, float)):
                    continue
                token = price_data[1] if len(price_data) > 1 else None
                if isinstance(item[1], str):
                    entries.append(
                        {"departure": item[0], "return": item[1], "price": price, "booking_token": token}
                    )
                elif item[1] is None:
                    entries.append(
                        {"departure": item[0], "return": None, "price": price, "booking_token": token}
                    )
            except (IndexError, TypeError, KeyError):
                continue
    return entries


def _open_calendar_session(query, proxy: str | None, currency: str, client: Any = None) -> dict[str, Any]:
    """Create (or reuse) one HTTP session for native grid calls.

    No page bootstrap is needed: the RPC accepts f.sid=0 and an arbitrary
    `at` token, so we skip the ~2MB landing-page fetch entirely.
    """
    if client is None:
        from primp import Client

        client = Client(
            impersonate="chrome_145",
            impersonate_os="macos",
            referer=True,
            proxy=proxy,
            cookie_store=True,
            timeout=30,
        )
    return {
        "client": client,
        "f_sid": "0",
        "build": _CALENDAR_FRONTEND_FALLBACK,
        "language": query.language or "en-US",
        "currency": currency or "BRL",
    }


def _calendar_grid_chunk(
    session: dict[str, Any],
    from_arg: str,
    to_arg: str,
    departure: str,
    return_date: str | None,
    dep_start: str,
    dep_end: str,
    ret_start: str | None,
    ret_end: str | None,
    seat: str,
    passengers: dict[str, int],
    baggage: dict[str, Any],
    filters: dict[str, Any],
    max_price: int | None,
) -> list[dict[str, Any]]:
    body = _calendar_request_body(
        from_arg,
        to_arg,
        departure,
        return_date,
        dep_start,
        dep_end,
        ret_start,
        ret_end,
        seat,
        passengers,
        filters,
        max_price,
        baggage,
    )
    params = (
        f"?f.sid={session['f_sid']}&bl={session['build']}"
        f"&hl=en&soc-app=162&soc-platform=1&soc-device=1"
        f"&_reqid={int(time.time() * 1000) % 10000000}&rt=c"
    )
    headers = {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "x-same-domain": "1",
        "x-goog-ext-259736195-jspb": json.dumps(
            [session["language"], "BR", session["currency"], 1, None, [180], None, None, 7, []],
            separators=(",", ":"),
        ),
        "origin": "https://www.google.com",
        "accept-language": "en-US,en;q=0.9",
    }
    response = session["client"].post(
        _CALENDAR_GRID_ENDPOINT + params,
        headers=headers,
        data=body,
    )
    response.raise_for_status()
    return _parse_calendar_grid(response.text)


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


def fetch_calendar_grid(
    session: dict[str, Any],
    from_arg: str,
    to_arg: str,
    base_departure: str,
    base_return: str,
    window: int,
    seat: str,
    passengers: dict[str, int],
    baggage: dict[str, Any],
    filters: dict[str, Any],
    max_price: int | None,
    concurrency: int,
) -> list[dict[str, Any]]:
    """Fetch the native 2-D calendar, chunking wide windows automatically.

    One-way (base_return=None): only the departure axis is requested; chunks
    are single rectangles of at most 200 days.
    """
    dep0 = _parse_date(base_departure)
    ret0 = _parse_date(base_return) if base_return else None
    dep_start = dep0 - _dt.timedelta(days=window)
    dep_end = dep0 + _dt.timedelta(days=window)
    ranges: list[tuple[_dt.date, _dt.date, _dt.date | None, _dt.date | None]] = []
    if ret0 is None:
        cursor = dep_start
        while cursor <= dep_end:
            chunk_end = min(cursor + _dt.timedelta(days=_CALENDAR_MAX_CELLS - 1), dep_end)
            ranges.append((cursor, chunk_end, None, None))
            cursor = chunk_end + _dt.timedelta(days=1)
    else:
        ret_start = ret0 - _dt.timedelta(days=window)
        ret_end = ret0 + _dt.timedelta(days=window)
        # Build rectangular chunks independently for the two axes. The service
        # rejects requests with more than 200 cells.
        dep_cursor = dep_start
        while dep_cursor <= dep_end:
            dep_chunk_end = min(dep_cursor + _dt.timedelta(days=13), dep_end)
            dep_days = (dep_chunk_end - dep_cursor).days + 1
            ret_chunk_days = max(1, _CALENDAR_MAX_CELLS // dep_days)
            ret_cursor = ret_start
            while ret_cursor <= ret_end:
                ret_chunk_end = min(ret_cursor + _dt.timedelta(days=ret_chunk_days - 1), ret_end)
                ranges.append((dep_cursor, dep_chunk_end, ret_cursor, ret_chunk_end))
                ret_cursor = ret_chunk_end + _dt.timedelta(days=1)
            dep_cursor = dep_chunk_end + _dt.timedelta(days=1)

    def fetch_range(r):
        a, b, c, d = r
        ref_dep = min(max(dep0, a), b)
        ref_ret = min(max(ret0, c), d) if ret0 and c and d else None
        return _calendar_grid_chunk(
            session,
            from_arg,
            to_arg,
            ref_dep.isoformat(),
            ref_ret.isoformat() if ref_ret else None,
            a.isoformat(),
            b.isoformat(),
            c.isoformat() if c else None,
            d.isoformat() if d else None,
            seat,
            passengers,
            baggage,
            filters,
            max_price,
        )

    entries: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max(1, concurrency), 5)) as ex:
        futures = [ex.submit(fetch_range, r) for r in ranges]
        for future in as_completed(futures):
            entries.extend(future.result())
    deduped = {(e["departure"], e["return"]): e for e in entries}
    return list(deduped.values())


def native_grid_for_route(
    from_arg: str,
    to_arg: str,
    base_departure: str,
    base_return: str | None,
    window: int,
    currency: str,
    language: str,
    seat: str,
    passengers: dict[str, int],
    baggage: dict[str, Any],
    filters: dict[str, Any],
    max_price: int | None,
    proxy: str | None,
    concurrency: int,
    session: dict[str, Any] | None = None,
    keep_tokens: bool = False,
) -> tuple[list[dict[str, Any]], Any]:
    """Fetch one route's native calendar and attach normal deep links."""
    base_query = _pair_query(
        from_arg,
        to_arg,
        base_departure,
        base_return,
        currency,
        language,
        seat,
        passengers,
        baggage,
        filters,
    )
    if session is None:
        session = _open_calendar_session(base_query, proxy, currency)
    entries = fetch_calendar_grid(
        session,
        from_arg,
        to_arg,
        base_departure,
        base_return,
        window,
        seat,
        passengers,
        baggage,
        filters,
        max_price,
        concurrency,
    )
    for entry in entries:
        entry["to"] = to_arg
        entry["url"] = _calendar_pair_url(base_query, entry["departure"], entry["return"])
        if not keep_tokens:
            entry.pop("booking_token", None)
    return entries, base_query


def _classify_fetch_error(e: Exception) -> tuple[str, str]:
    msg = str(e)
    if "FlightsNotFound" in type(e).__name__ or "no flights found" in msg.lower():
        return ("no flights", "workable: route/date has no published schedule - try different dates, remove --airlines filter, or use --explore-scope network for 1-stop")
    if "'NoneType' object is not subscriptable" in msg or "payload[3]" in msg:
        return ("no flights", "workable: Google returned no schedule for this route/date - airline may not operate that day, try ±1 day or different weekday")
    if "primp" in msg.lower() or "403" in msg or "429" in msg or "timeout" in msg.lower():
        return (msg, "workable: transient fetch blocked - retry once, reduce --flex-concurrency, or set --proxy")
    return (msg, "workable: check --from/--to codes, dates are future YYYY-MM-DD, and try without restrictive filters")



def main():
    p = argparse.ArgumentParser(description="Google Flights search via fast-flights (AWeirdDev/flights)", add_help=True)
    # routing
    p.add_argument("--from", dest="from_", help="origin IATA code (e.g. GRU)")
    p.add_argument("--to", dest="to", help="destination IATA code (e.g. JFK)")
    p.add_argument("--date", help="departure date YYYY-MM-DD")
    p.add_argument("--return-date", help="return date YYYY-MM-DD (implies round-trip)")
    p.add_argument("--legs", help="multi-city as JSON array: '[{\"from\":\"MYJ\",\"to\":\"TPE\",\"date\":\"2026-08-25\"},...]'")
    p.add_argument("--trip", choices=["one-way", "round-trip", "multi-city"], default=None, help="trip type (auto-inferred if omitted)")
    p.add_argument("--seat", choices=["economy", "premium-economy", "business", "first"], default="economy")
    # passengers
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--children", type=int, default=0)
    p.add_argument("--infants-seat", type=int, default=0)
    p.add_argument("--infants-lap", type=int, default=0)
    # global filters
    p.add_argument("--currency", default="")
    p.add_argument("--language", default="")
    p.add_argument("--max-price", type=int, default=None, help="server-side max price in selected currency")
    p.add_argument("--carry-on", type=int, default=0)
    p.add_argument("--checked-bags", type=int, default=0)
    p.add_argument("--hide-separate", action="store_true")
    p.add_argument("--exclude-basic", action="store_true")
    # per-leg filters
    p.add_argument("--max-stops", type=int, default=None)
    p.add_argument("--airlines", default=None, help="comma-separated IATA or alliance, e.g. JL,ONEWORLD")
    p.add_argument("--connecting", default=None, help="comma-separated connecting airport codes")
    p.add_argument("--earliest-departure", type=int, default=None)
    p.add_argument("--latest-departure", type=int, default=None)
    p.add_argument("--earliest-arrival", type=int, default=None)
    p.add_argument("--latest-arrival", type=int, default=None)
    p.add_argument("--max-duration", type=int, default=None)
    p.add_argument("--min-layover", type=int, default=None)
    p.add_argument("--max-layover", type=int, default=None)
    p.add_argument("--less-emissions", action="store_true")
    # client-side
    p.add_argument("--limit", type=int, default=20, help="max flights to return (cap 50)")
    p.add_argument("--sort", choices=["asc", "desc"], default=None)
    p.add_argument("--top", type=int, default=0, help="return only first N after sorting")
    p.add_argument("--min-price", type=int, default=None, help="client-side min price filter")
    p.add_argument("--max-price-client", type=int, default=None, help="client-side max price filter")
    p.add_argument("--proxy", default=None)
    p.add_argument("--url-only", action="store_true", help="only print URL, don't fetch")
    # flexible / price graph / insights
    p.add_argument("--price-graph", action="store_true", help="include native 61-day price graph (fixed stay) at payload[5][10][0] — parsed from the same fetch, no extra request")
    p.add_argument("--price-insights", action="store_true", help="include Google's price insights panel (current/typical/usual band/verdict), free from payload[5]")
    p.add_argument("--keep-tokens", action="store_true", help="keep per-cell booking_token in grid output (for preselected browser flows)")
    p.add_argument("--flex-window", type=int, default=None, help="flexible search: +/- N days around --date (and --return-date if set; without it = one-way flex). 2 => 5 dates per axis")
    p.add_argument("--flex-grid", action="store_true", help="with --flex-window, use Google's native 2-axis departure×return calendar (one RPC per <=200-cell chunk). Requires --return-date")
    p.add_argument("--min-stay", type=int, default=None, help="variable stay: min days (requires --max-stay)")
    p.add_argument("--max-stay", type=int, default=None, help="variable stay: max days (requires --min-stay)")
    p.add_argument("--flex-concurrency", type=int, default=3, help="concurrency for flexible requests/chunks (default 3, max 5)")
    p.add_argument("--flex-limit", type=int, default=None, help="limit grid results after sorting (default all)")
    # public dataset explore (out-of-box, no API key)
    p.add_argument("--explore", action="store_true", help="auto-discover destinations from --from via public Jonty/airline-route-data (no key). Filters by --airlines + --explore-intl. Replaces --to")
    p.add_argument("--explore-intl", action="store_true", help="with --explore, only international destinations")
    p.add_argument("--explore-scope", choices=["direct", "network"], default="network", help="direct=only routes from --from, network=direct+1-stop via hub (anywhere, general, e.g. SSA->CDG->MAD). Default network for anywhere.")
    p.add_argument("--explore-limit", type=int, default=None, help="cap number of explored destinations (cheapest km first)")
    p.add_argument("--explore-cache-ttl", type=int, default=24, help="cache TTL hours for airline_routes.json (default 24)")
    p.add_argument("--explore-max-requests", type=int, default=40, help="refuse explore runs estimated above this many Google requests (default 40)")
    # multi-airport / nearby
    p.add_argument("--nearby", action="store_true", help="expand --from/--to with airports within --nearby-km (offline dataset, OR-search via repeated tfs airports)")
    p.add_argument("--nearby-km", type=int, default=120, help="radius for --nearby expansion (default 120 km)")

    args = p.parse_args()

    # ── early date validation (workable errors) ──
    for label, val in [("--date", args.date), ("--return-date", args.return_date)]:
        if val:
            try:
                d = _parse_date(val)
                if d < _dt.date.today():
                    # not fatal, but hint - past dates return 0 results
                    pass
            except ValueError as e:
                emit_error(str(e), hint=f"workable: {label} must be YYYY-MM-DD and a real calendar date, e.g. 2027-06-15")
    if args.legs:
        try:
            raw_legs = json.loads(args.legs) if isinstance(args.legs, str) else args.legs
            for leg in raw_legs:
                _parse_date(leg.get("date", ""))
        except ValueError as e:
            emit_error(str(e), hint="workable: each leg needs from/to/date with YYYY-MM-DD")
        except Exception:
            pass

    # infer trip if not set
    trip = args.trip
    if not trip:
        if args.legs:
            try:
                raw = json.loads(args.legs) if isinstance(args.legs, str) else args.legs
                trip = "multi-city" if len(raw) > 1 else "one-way"
            except Exception:
                trip = "one-way"
        elif args.return_date:
            trip = "round-trip"
        else:
            trip = "one-way"

    # ── Explore mode (public dataset, no API key) ──
    # infer explore if --from without --to (redundant flag, keep compat)
    is_explore = bool(args.explore or (args.from_ and args.date and not args.to and not args.legs))
    if is_explore:
        if not args.from_ or not args.date:
            emit_error("--explore requires --from and --date (and --return-date for round-trip)", hint="workable: add --from SSA --date 2027-06-15 [--return-date 2027-06-22] [--explore-intl] [--explore-scope network]")
        # --legs not compatible with explore
        if args.legs:
            emit_error("--explore not compatible with --legs", hint="workable: use --explore for auto dests or --legs for explicit multi-city, not both")
        airlines_filter = [a.strip().upper() for a in args.airlines.split(",")] if args.airlines else None
        dests, explore_meta = get_public_destinations(
            origin=args.from_,
            airlines_filter=airlines_filter,
            intl_only=bool(args.explore_intl),
            scope=args.explore_scope,
            cache_ttl_h=args.explore_cache_ttl,
        )
        if not dests:
            hint = "workable: check --from code, try without --airlines, without --explore-intl, or use --explore-scope network for 1-stop"
            if explore_meta.get("error"):
                hint += f" ({explore_meta['error']})"
            emit_error(f"no destinations found for {args.from_}", hint=hint, extra={"explore_meta": explore_meta})
        if args.explore_limit and args.explore_limit > 0:
            dests = dests[: args.explore_limit]

        # Request-budget guard: explore fans out one RPC per destination (per
        # grid chunk). Refuse combos that would run for minutes so the agent
        # can tell the user instead of hanging until the shell timeout.
        est_requests = _explore_request_estimate(len(dests), args.flex_window)
        max_requests = max(1, args.explore_max_requests)
        if est_requests > max_requests:
            per_dest = _grid_chunks_for_window(args.flex_window) if args.flex_window is not None else 1
            suggested_limit = max(0, max_requests // per_dest)
            emit_error(
                f"explore would issue ~{est_requests} Google requests ({len(dests)} destinations x {per_dest} grid chunk(s) each)",
                hint=(
                    "workable: this fan-out exceeds the request budget -- "
                    f"cap destinations with --explore-limit {suggested_limit}, drop --flex-window for single-pair pricing, "
                    "narrow --airlines/--explore-intl, or query a few destinations individually"
                ),
                extra={"estimated_requests": est_requests, "destinations": len(dests), "chunks_per_destination": per_dest},
            )

        # Flexible dates are served entirely by the native calendar grid;
        # stay filters are applied client-side on the returned matrix.
        mode = "explore"
        if args.flex_window is not None:
            window = args.flex_window
            if window < 0 or window > 15:
                emit_error("--flex-window must be 0..15", hint="workable: use 1 for ±1d (3 dates), 2 for ±2d (5 dates), max 15")
            if not args.return_date:
                emit_error("flexible search needs --return-date", hint="workable: add --return-date for round-trip grids; for one-way near-term use --price-graph")
            if (args.min_stay is None) != (args.max_stay is None):
                emit_error("--min-stay and --max-stay must be given together", hint="workable: pass both, e.g. --min-stay 7 --max-stay 12")
        else:
            if args.min_stay is not None or args.max_stay is not None:
                emit_error("--min-stay/--max-stay requires --flex-window", hint="workable: add --flex-window N (e.g. 1) to enable variable stay grid")

        conc = min(max(1, args.flex_concurrency), 5)
        passengers_dict = {"adults": args.adults, "children": args.children, "infants_in_seat": args.infants_seat, "infants_on_lap": args.infants_lap}
        baggage_args = {"max_price": args.max_price, "carry_on": args.carry_on, "checked_bags": args.checked_bags, "hide_separate": args.hide_separate, "exclude_basic": args.exclude_basic, "proxy": args.proxy}
        filters = {
            "max_stops": args.max_stops,
            "airlines": airlines_filter,
            "earliest_departure": args.earliest_departure,
            "latest_departure": args.latest_departure,
            "earliest_arrival": args.earliest_arrival,
            "latest_arrival": args.latest_arrival,
            "max_duration": args.max_duration,
            "connecting": args.connecting.split(",") if args.connecting else None,
            "min_layover": args.min_layover,
            "max_layover": args.max_layover,
            "less_emissions": bool(args.less_emissions),
        }
        grid: list[dict[str, Any]] = []
        total_pairs = 0
        if args.flex_window is not None:
            # One shared session for every destination: the RPC needs no page
            # bootstrap, so a single client serves the whole explore run.
            _probe = _pair_query(args.from_, dests[0]["iata"], args.date, args.return_date, args.currency, args.language, args.seat, passengers_dict, baggage_args, filters) if dests else None
            shared_session = _open_calendar_session(_probe or args, args.proxy, args.currency)
            for destination in dests:
                try:
                    entries, _ = native_grid_for_route(
                        args.from_,
                        destination["iata"],
                        args.date,
                        args.return_date,
                        window,
                        args.currency,
                        args.language,
                        args.seat,
                        passengers_dict,
                        baggage_args,
                        filters,
                        args.max_price,
                        args.proxy,
                        conc,
                        session=shared_session,
                        keep_tokens=args.keep_tokens,
                    )
                    grid.extend(entries)
                    total_pairs += len(entries)
                except Exception as e:
                    detail, hint = _classify_fetch_error(e)
                    grid.append({
                        "departure": args.date,
                        "return": args.return_date,
                        "price": None,
                        "url": None,
                        "error": detail,
                        "hint": hint,
                        "to": destination["iata"],
                    })
            mode = "explore+native-calendar-grid"
        else:
            # single pair per dest: one grid cell each (window 0)
            if not args.return_date:
                emit_error("explore needs --return-date", hint="workable: add --return-date (native grid is round-trip); for one-way use --to with explicit dates")
            shared_session = _open_calendar_session(args, args.proxy, args.currency)
            for destination in dests:
                try:
                    entries, _ = native_grid_for_route(
                        args.from_,
                        destination["iata"],
                        args.date,
                        args.return_date,
                        0,
                        args.currency,
                        args.language,
                        args.seat,
                        passengers_dict,
                        baggage_args,
                        filters,
                        args.max_price,
                        args.proxy,
                        conc,
                        session=shared_session,
                        keep_tokens=args.keep_tokens,
                    )
                    base_cell = next((e for e in entries if e["departure"] == args.date and e["return"] == args.return_date), entries[0] if entries else None)
                    if base_cell:
                        grid.append(base_cell)
                        total_pairs += 1
                except Exception as e:
                    detail, hint = _classify_fetch_error(e)
                    grid.append({
                        "departure": args.date,
                        "return": args.return_date,
                        "price": None,
                        "url": None,
                        "error": detail,
                        "hint": hint,
                        "to": destination["iata"],
                    })
            mode = "explore"

        # sort by price (None last)
        grid_sorted = sorted(grid, key=lambda x: (x["price"] is None, x["price"] if x["price"] is not None else 10**9))
        if args.flex_limit:
            grid_sorted = grid_sorted[: args.flex_limit]
        if args.min_price is not None:
            grid_sorted = [g for g in grid_sorted if g["price"] is not None and g["price"] >= args.min_price]
        if args.max_price_client is not None:
            grid_sorted = [g for g in grid_sorted if g["price"] is not None and g["price"] <= args.max_price_client]

        cheapest = next((g for g in grid_sorted if g["price"] is not None), None)
        # per-dest cheapest
        per_dest: dict[str, Any] = {}
        for g in grid_sorted:
            to = g.get("to")
            if to not in per_dest and g.get("price") is not None:
                per_dest[to] = g

        count_ok = len([g for g in grid_sorted if g["price"] is not None])
        # optimization note: for full 2027 time exhaustive, window is ±N around --date
        # To cover 2027-01..12, run monthly loop externally or extend to date-range flag (future).
        out: dict[str, Any] = {
            "ok": True,
            "mode": mode,
            "query": {"from": args.from_, "date": args.date, "return_date": args.return_date, "trip": trip, "seat": args.seat, "currency": args.currency, "airlines": args.airlines, "explore": True, "explore_scope": args.explore_scope, "explore_intl": args.explore_intl, "flex_window": args.flex_window, "min_stay": args.min_stay, "max_stay": args.max_stay},
            "explore_meta": explore_meta,
            "destinations": dests,
            "count": count_ok,
            "total_pairs": total_pairs,
            "total_destinations": len(dests),
            "grid": grid_sorted,
            "cheapest": cheapest,
            "per_dest_cheapest": per_dest,
        }
        if count_ok == 0:
            out["hint"] = "workable: no flights match - try different dates/weekday, remove --airlines, broaden --flex-window, try --explore-scope network, or check grid[].error/hint per entry"
        print(json.dumps(out, ensure_ascii=False))
        return

    # ── Multi-airport / nearby-airports normalization ──
    from_codes_all = _split_codes(args.from_)
    to_codes_all = _split_codes(args.to)
    wants_multi = len(from_codes_all) > 1 or len(to_codes_all) > 1
    if wants_multi or args.nearby:
        if args.explore or args.flex_window is not None:
            emit_error("multi-airport (--from a,b) and --nearby can't combine with --explore or --flex-window", hint="workable: run one flexible/explore pass per airport pair instead")
        if args.legs:
            emit_error("comma-separated codes need plain --from/--to, not --legs", hint="workable: e.g. --from SSA,GRU --to MAD --date 2026-11-05")
        if not from_codes_all or not to_codes_all:
            emit_error("multi-airpoint/--nearby searches need both --from and --to", hint="workable: --from SSA[,GRU] --to MAD[,AGP] --date ... ; 'anywhere' searches are covered by --explore")
        if any(_is_city(c) for c in from_codes_all + to_codes_all):
            emit_error("city entities (/m/...) can't combine with multi-airport lists or --nearby", hint="workable: partnership flows take a single pair of city entities")
    if len(from_codes_all) > 1:
        args.from_ = from_codes_all[0]
    if len(to_codes_all) > 1:
        args.to = to_codes_all[0]

    # validation for help when no args
    if not args.legs and not (args.from_ and args.to and args.date):
        if len(sys.argv) == 1:
            p.print_help(sys.stderr)
        emit_error("--from, --to, --date required (or --legs or --explore)", hint="workable: --from GRU --to JFK --date 2026-09-15  OR  --from SSA --date 2027-06-15 --explore --explore-intl  OR  --legs '[...]' . See --help", exit_code=2)

    try:
        flight_queries = build_flight_queries(args)
    except Exception as e:
        emit_error(str(e), hint="workable: check IATA codes 3 letters, dates YYYY-MM-DD future, --legs JSON valid")

    passengers = Passengers(
        adults=args.adults,
        children=args.children,
        infants_in_seat=args.infants_seat,
        infants_on_lap=args.infants_lap,
    )

    try:
        query = create_query(
            flights=flight_queries,
            seat=args.seat,
            trip=trip,
            passengers=passengers,
            language=args.language,
            currency=args.currency,
            max_price=args.max_price,
            carry_on_bags=args.carry_on,
            checked_bags=args.checked_bags,
            hide_separate_and_self_transfer=args.hide_separate,
            exclude_basic_economy=args.exclude_basic,
        )
    except Exception as e:
        emit_error(f"create_query failed: {e}", hint="workable: check --adults/--children totals <=9, --airlines codes valid, dates future")

    url = query.url()

    # City entities need a real browser session and a typed tfs — handle all
    # modes (plain/flex/explore) here before any anonymous fetch is attempted.
    if _is_city(args.from_) or _is_city(args.to):
        _city_browser_required(query, args.from_, args.to)

    if args.url_only:
        print(json.dumps({"ok": True, "url": url, "query": {"from": args.from_, "to": args.to, "date": args.date, "return_date": args.return_date, "trip": trip, "seat": args.seat}}, ensure_ascii=False))
        return

    # ── Multi-airport / nearby: rewrite tfs with repeated airports ──
    custom_url = None
    if wants_multi or args.nearby:
        if args.nearby:
            expanded_o, err = expand_nearby(from_codes_all, radius_km=args.nearby_km, cache_ttl_h=args.explore_cache_ttl)
            if err:
                emit_error(f"nearby lookup failed: {err}", hint="workable: retry later (dataset cached 24h at ~/.cache/opencode/airline_routes.json) or run without --nearby")
            expanded_d, err = expand_nearby(to_codes_all, radius_km=args.nearby_km, cache_ttl_h=args.explore_cache_ttl)
            if err:
                emit_error(f"nearby lookup failed: {err}", hint="workable: retry later or run without --nearby")
        else:
            expanded_o = {c: [c] for c in from_codes_all}
            expanded_d = {c: [c] for c in to_codes_all}
        origin_set = list(dict.fromkeys(from_codes_all + [x for c in from_codes_all for x in expanded_o.get(c.upper(), [])]))
        dest_set = list(dict.fromkeys(to_codes_all + [x for c in to_codes_all for x in expanded_d.get(c.upper(), [])]))
        per_leg = [(origin_set, dest_set)]
        if trip == "round-trip":
            per_leg.append((dest_set, origin_set))
        try:
            custom_url = multi_airports_tfs_url(query, per_leg)
        except Exception as e:
            emit_error(f"multi-airport tfs build failed: {e}", hint="workable: check codes are plain 3-letter IATA (or repeat the flag with single codes)")
        url = custom_url

    # ── Flexible search: native Date Grid, stay filters applied client-side ──
    if args.flex_window is not None:
        if not args.from_ or not args.to or not args.date:
            emit_error("--flex-window requires --from, --to, --date", hint="workable: add --from GRU --to JFK --date 2026-09-15 [--return-date 2026-09-20] or use --explore for auto dests")
        window = args.flex_window
        if window < 0 or window > 15:
            emit_error("--flex-window must be 0..15", hint="workable: use 1 (±1d) 2 (±2d) up to 15, higher = more requests")
        one_way_flex = args.return_date is None
        if one_way_flex and (args.min_stay is not None or args.max_stay is not None):
            emit_error("one-way flexible search has no stay length", hint="workable: drop --min-stay/--max-stay, or add --return-date for round-trip grids")
        if (args.min_stay is None) != (args.max_stay is None):
            emit_error("--min-stay and --max-stay must be given together", hint="workable: pass both, e.g. --min-stay 7 --max-stay 12")

        conc = min(max(1, args.flex_concurrency), 5)
        passengers_dict = {"adults": args.adults, "children": args.children, "infants_in_seat": args.infants_seat, "infants_on_lap": args.infants_lap}
        baggage_args = {"max_price": args.max_price, "carry_on": args.carry_on, "checked_bags": args.checked_bags, "hide_separate": args.hide_separate, "exclude_basic": args.exclude_basic, "proxy": args.proxy}
        filters = {
            "max_stops": args.max_stops,
            "airlines": args.airlines.split(",") if args.airlines else None,
            "earliest_departure": args.earliest_departure,
            "latest_departure": args.latest_departure,
            "earliest_arrival": args.earliest_arrival,
            "latest_arrival": args.latest_arrival,
            "max_duration": args.max_duration,
            "connecting": args.connecting.split(",") if args.connecting else None,
            "min_layover": args.min_layover,
            "max_layover": args.max_layover,
            "less_emissions": bool(args.less_emissions),
        }
        grid: list[dict[str, Any]] = []
        try:
            grid, _ = native_grid_for_route(
                args.from_,
                args.to,
                args.date,
                args.return_date,
                window,
                args.currency,
                args.language,
                args.seat,
                passengers_dict,
                baggage_args,
                filters,
                args.max_price,
                args.proxy,
                conc,
                keep_tokens=args.keep_tokens,
            )
        except Exception as e:
            detail, hint = _classify_fetch_error(e)
            grid = [{
                "departure": args.date,
                "return": args.return_date,
                "price": None,
                "url": None,
                "error": detail,
                "hint": hint,
                "to": args.to,
            }]
        total_pairs = len(grid)

        # Stay filters on the returned matrix. Default (no --flex-grid, no
        # min/max stay): fixed-stay diagonal — same trip length as base dates.
        def _stay(cell: dict[str, Any]) -> int | None:
            if cell.get("price") is None or cell.get("return") is None:
                return None
            return (_parse_date(cell["return"]) - _parse_date(cell["departure"])).days

        if one_way_flex:
            mode = "flex-one-way"
        elif args.min_stay is not None and args.max_stay is not None:
            mode = "flex-variable-stay"
            grid = [g for g in grid if (s := _stay(g)) is not None and args.min_stay <= s <= args.max_stay]
        elif not args.flex_grid:
            mode = "flex-fixed-stay"
            base_stay = (_parse_date(args.return_date) - _parse_date(args.date)).days
            grid = [g for g in grid if (s := _stay(g)) == base_stay]
        else:
            mode = "native-calendar-grid"

        # sort by price (None last)
        grid_sorted = sorted(grid, key=lambda x: (x["price"] is None, x["price"] if x["price"] is not None else 10**9))
        # flex-limit
        if args.flex_limit:
            grid_sorted = grid_sorted[: args.flex_limit]
        # also apply min/max price client filters if requested
        if args.min_price is not None:
            grid_sorted = [g for g in grid_sorted if g["price"] is not None and g["price"] >= args.min_price]
        if args.max_price_client is not None:
            grid_sorted = [g for g in grid_sorted if g["price"] is not None and g["price"] <= args.max_price_client]

        cheapest = next((g for g in grid_sorted if g["price"] is not None), None)
        # optionally attach native price graph / insights (1 extra request total)
        native_graph = None
        price_insights = None
        if args.price_graph or args.price_insights:
            try:
                _html = fetch_flights_html(query, proxy=args.proxy)
                _payload = _payload_from_html(_html)
                if args.price_graph:
                    native_graph = extract_price_graph_from_payload(_payload)
                if args.price_insights:
                    price_insights = extract_price_insights(_payload)
            except Exception:
                pass

        count_ok = len([g for g in grid_sorted if g["price"] is not None])
        out = {
            "ok": True,
            "mode": mode,
            "query": {"from": args.from_, "to": args.to, "date": args.date, "return_date": args.return_date, "trip": trip, "seat": args.seat, "currency": args.currency, "flex_window": window, "flex_grid": args.flex_grid, "min_stay": args.min_stay, "max_stay": args.max_stay},
            "count": count_ok,
            "total_pairs": total_pairs,
            "grid": grid_sorted,
            "cheapest": cheapest,
            "url": url,
        }
        if count_ok == 0:
            out["hint"] = "workable: no flights in grid - try different dates/weekday, remove --airlines/--max-stops, increase --flex-window, or add --price-graph for near-term"
        if native_graph is not None:
            out["price_graph"] = native_graph
            # also show cheapest in graph for fixed stay
            if native_graph:
                out["price_graph_cheapest"] = min(native_graph, key=lambda x: x["price"])
        if args.price_insights:
            out["price_insights"] = price_insights
        print(json.dumps(out, ensure_ascii=False))
        return

    # ── Unified fetch: flights + optional graph/insights from the same page ──
    native_graph = None
    price_insights = None
    want_extras = bool(args.price_graph or args.price_insights)
    try:
        if custom_url:
            html = fetch_search_html(custom_url, proxy=args.proxy)
            result = parse_flights_html(html)
            payload = _payload_from_html(html)
            if args.price_graph:
                native_graph = extract_price_graph_from_payload(payload)
            if args.price_insights:
                price_insights = extract_price_insights(payload)
        elif want_extras:
            html = fetch_flights_html(query, proxy=args.proxy)
            result = parse_flights_html(html)
            payload = _payload_from_html(html)
            if args.price_graph:
                native_graph = extract_price_graph_from_payload(payload)
            if args.price_insights:
                price_insights = extract_price_insights(payload)
        else:
            result = get_flights(query, proxy=args.proxy)
    except FlightsNotFound as e:
        # expected workable outcome: route/date has no schedule (seasonal, past, filter too strict)
        out = {"ok": True, "count": 0, "flights": [], "url": url, "query": {"from": args.from_, "to": args.to, "date": args.date, "return_date": args.return_date, "legs": json.loads(args.legs) if args.legs else None, "trip": trip, "seat": args.seat, "currency": args.currency, "language": args.language}, "metadata": {"airlines": [], "alliances": []}, "hint": "workable: no flights match - try different dates/weekday, remove --airlines/--max-stops, or use --explore for alternatives"}
        print(json.dumps(out, ensure_ascii=False))
        return
    except Exception as e:
        detail, hint = _classify_fetch_error(e)
        emit_error(detail, hint=hint, extra={"url": url})

    # result is ResultList
    flights_list = [flight_to_dict(f) for f in result]

    # client-side filters
    if args.min_price is not None:
        flights_list = [f for f in flights_list if f["price"] >= args.min_price]
    if args.max_price_client is not None:
        flights_list = [f for f in flights_list if f["price"] <= args.max_price_client]

    if args.sort == "asc":
        flights_list.sort(key=lambda x: x["price"])
    elif args.sort == "desc":
        flights_list.sort(key=lambda x: x["price"], reverse=True)

    if args.top and args.top > 0:
        flights_list = flights_list[: args.top]
    else:
        limit = min(args.limit, 50) if args.limit else 20
        flights_list = flights_list[:limit]

    # metadata
    meta = {}
    try:
        meta = {
            "airlines": [{"code": a.code, "name": a.name} for a in result.metadata.airlines],
            "alliances": [{"code": a.code, "name": a.name} for a in result.metadata.alliances],
        }
    except Exception:
        meta = {"airlines": [], "alliances": []}

    out = {
        "ok": True,
        "query": {"from": args.from_, "to": args.to, "date": args.date, "return_date": args.return_date, "legs": json.loads(args.legs) if args.legs else None, "trip": trip, "seat": args.seat, "currency": args.currency, "language": args.language, "max_price": args.max_price},
        "count": len(flights_list),
        "flights": flights_list,
        "metadata": meta,
        "url": url,
    }
    if custom_url:
        out["query"]["origins"] = from_codes_all
        out["query"]["destinations"] = to_codes_all
        out["query"]["nearby"] = bool(args.nearby)
    if len(flights_list) == 0:
        # workable: client filters removed all, or no flights for query - distinction
        out["hint"] = "workable: no flights after filters - try removing --min-price/--max-price-client, broadening dates, or removing --airlines/--max-stops"
    if native_graph is not None:
        out["price_graph"] = native_graph
        if native_graph:
            out["price_graph_cheapest"] = min(native_graph, key=lambda x: x["price"])
    if args.price_insights:
        out["price_insights"] = price_insights
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
