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


def _keep_flights_with_any_airline(result, flights_list: list[dict[str, Any]], codes: str) -> list[dict[str, Any]]:
    """Client-side filter: keep itineraries whose carriers include any of `codes`.

    `airlines` in parsed results holds carrier NAMES ("Gol"), while users pass
    CODES ("G3") — resolve via the result's airline metadata table. Implements
    'Gol required, partner metal fills the gaps': search --airlines G3,AF
    (server OR-filter, surfaces mixed itineraries) then require G3 presence so
    pure-partner trips (e.g. all-AF CDG->ALG) are dropped.
    """
    want: set[str] = set()
    meta_by_code: dict[str, str] = {}
    try:
        for a in result.metadata.airlines:
            meta_by_code[a.code.upper()] = a.name.upper()
            if a.code.upper() in {c.strip().upper() for c in codes.split(",")}:
                want.add(a.name.upper())
    except Exception:
        pass
    for c in codes.split(","):
        want.add(c.strip().upper())
    return [
        f for f in flights_list
        if any(str(a).strip().upper() in want for a in (f.get("airlines") or []))
    ]


def emit_error(detail: str, hint: str | None = None, extra: dict[str, Any] | None = None, exit_code: int = 1):
    payload: dict[str, Any] = {"ok": False, "reason": "error", "detail": detail}
    if hint:
        payload["hint"] = hint
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code)


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


_FLEX_MONTH_STEP_DAYS = 28  # anchors every 4 weeks; window edges overlap so coverage stays contiguous


def flex_month_anchors(date: str, return_date: str, months: int) -> list[tuple[str, str]]:
    """Monthly (departure, return) anchor pairs for a long-horizon sweep."""
    d0, r0 = _parse_date(date), _parse_date(return_date)
    return [
        ((d0 + _dt.timedelta(days=_FLEX_MONTH_STEP_DAYS * k)).isoformat(),
         (r0 + _dt.timedelta(days=_FLEX_MONTH_STEP_DAYS * k)).isoformat())
        for k in range(max(1, months))
    ]


def _date_query_fields(args) -> dict[str, Any]:
    """Expose the user-facing date mode without leaking internal anchors."""
    if getattr(args, "flex_range", None):
        fields: dict[str, Any] = {
            "flex_starting_date": args.flex_starting_date,
            "flex_ending_date": args.flex_ending_date,
            "flex_days": args.flex_days,
        }
        if args.flex_days is None:
            fields["min_stay"] = args.min_stay
            fields["max_stay"] = args.max_stay
        return fields
    return {"date": args.date, "return_date": args.return_date}


def _per_dest_top(grid: list[dict[str, Any]], n: int) -> dict[str, list[dict[str, Any]]]:
    """Top-N cheapest priced periods per destination (client-side)."""
    tops: dict[str, list[dict[str, Any]]] = {}
    for g in sorted((g for g in grid if g.get("price") is not None), key=lambda x: x["price"]):
        dest = g.get("to")
        if not dest:
            continue
        bucket = tops.setdefault(dest, [])
        if len(bucket) < n:
            bucket.append(g)
    return tops


def _split_codes(value: str | None) -> list[str]:
    """Split comma-separated IATA/entity codes: 'SSA,GRU' -> ['SSA','GRU']."""
    if not value:
        return []
    return [c.strip() for c in value.split(",") if c.strip()]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def _classify_fetch_error(e: Exception) -> tuple[str, str]:
    msg = str(e)
    if "FlightsNotFound" in type(e).__name__ or "no flights found" in msg.lower():
        return ("no flights", "workable: route/date has no published schedule - try different dates, remove --airlines filter, or use --explore-scope network for 1-stop")
    if "'NoneType' object is not subscriptable" in msg or "payload[3]" in msg:
        return ("no flights", "workable: Google returned no schedule for this route/date - airline may not operate that day, try ±1 day or different weekday")
    if "primp" in msg.lower() or "403" in msg or "429" in msg or "timeout" in msg.lower():
        return (msg, "workable: transient fetch blocked - retry once, reduce --flex-concurrency, or set --proxy")
    return (msg, "workable: check --from/--to codes, dates are future YYYY-MM-DD, and try without restrictive filters")
