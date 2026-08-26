"""flights-search.py — deterministic Google Flights search facade.

Split across flights_search/*: util, dataset, tfs_urls, calendar_rpc,
queries, cli. This module holds the CLI entry (argparse + dispatch).
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

from .util import _classify_fetch_error, _date_query_fields, _keep_flights_with_any_airline, _parse_date, _payload_from_html, _per_dest_top, _split_codes, emit_error, extract_price_graph_from_payload, extract_price_insights, flex_month_anchors, flight_to_dict
from .dataset import expand_nearby, get_public_destinations
from .tfs_urls import _calendar_pair_url, _city_browser_required, _is_city, _pair_query, multi_airports_tfs_url
from .calendar_rpc import _explore_request_estimate, _grid_chunks_for_window, _install_cookie_patch, _open_calendar_session, fetch_search_html, native_grid_for_route
from .queries import build_flight_queries
from .explore import run_explore
from .flex_engine import run_flex_range

def main():
    p = argparse.ArgumentParser(description="Google Flights search via fast-flights (AWeirdDev/flights)", add_help=True)
    # routing
    p.add_argument("--from", dest="from_", help="origin IATA code (e.g. GRU)")
    p.add_argument("--to", dest="to", help="destination IATA code (e.g. JFK)")
    p.add_argument("--date", help="departure date YYYY-MM-DD")
    p.add_argument("--return-date", help="return date YYYY-MM-DD (implies round-trip)")
    p.add_argument("--legs", help="multi-city as JSON array: '[{\"from\":\"MYJ\",\"to\":\"TPE\",\"date\":\"2026-08-25\"},...]'")
    p.add_argument("--flex-starting-date", help="flexible search: earliest departure date YYYY-MM-DD")
    p.add_argument("--flex-ending-date", help="flexible search: latest departure date YYYY-MM-DD")
    p.add_argument("--flex-days", type=int, help="flexible search: exact trip length in days")
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
    p.add_argument("--include-airlines", default=None, help="client-side: require these carriers in the itinerary (any segment) — partnership recipe: --airlines G3,AF --include-airlines G3 = Gol required, AF fills the gaps")
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
    p.add_argument("--cookie", default=None, help="raw Cookie header applied to every HTTP request, e.g. \"GOOGLE_ABUSE_EXEMPTION=ID=...:S=...; NID=...\" — clears /sorry captcha walls after solving once in a browser")
    p.add_argument("--url-only", action="store_true", help="only print URL, don't fetch")
    # flexible / price graph / insights
    p.add_argument("--price-graph", action="store_true", help="include native 61-day price graph (fixed stay) at payload[5][10][0] — parsed from the same fetch, no extra request")
    p.add_argument("--price-insights", action="store_true", help="include Google's price insights panel (current/typical/usual band/verdict), free from payload[5]")
    p.add_argument("--keep-tokens", action="store_true", help="keep per-cell booking_token in grid output (for preselected browser flows)")
    p.add_argument("--flex-concurrency", type=int, default=3, help="concurrency for flexible requests/chunks (default 3, max 5)")
    p.add_argument("--flex-limit", type=int, default=None, help="limit grid results after sorting (default all)")
    p.add_argument("--flex-chunk-days", type=int, default=None, help="override automatic chunk planning (auto = largest safe requests: ~185-day sweeps inside the fare horizon, cheap probes beyond)")
    p.add_argument("--min-stay", type=int, default=None, help="flexible search: minimum trip length in days (variable-stay band; replaces --flex-days)")
    p.add_argument("--max-stay", type=int, default=None, help="flexible search: maximum trip length in days (variable-stay band; replaces --flex-days)")
    p.add_argument("--flex-window", type=int, default=None, help="COMPAT: +/- days around --date; translated to a flex-starting/ending-date range")
    p.add_argument("--flex-months", type=int, default=None, help="COMPAT: repeat the --date window every 28 days N times; translated to one wide range")
    # public dataset explore (out-of-box, no API key)
    p.add_argument("--explore", action="store_true", help="auto-discover destinations from --from via public Jonty/airline-route-data (no key). Filters by --airlines + --explore-intl. Replaces --to")
    p.add_argument("--explore-intl", action="store_true", help="with --explore, only international destinations")
    p.add_argument("--explore-scope", choices=["direct", "network"], default="network", help="direct=only routes from --from, network=direct+1-stop via hub (anywhere, general, e.g. SSA->CDG->MAD). Default network for anywhere.")
    p.add_argument("--explore-limit", type=int, default=None, help="cap number of explored destinations (cheapest km first)")
    p.add_argument("--explore-cache-ttl", type=int, default=24, help="cache TTL hours for airline_routes.json (default 24)")
    p.add_argument("--explore-dests", default=None, help="comma-separated destination codes overriding the dataset-derived list (batch tail coverage: --explore-dests MAD,LIS,FCO)")
    p.add_argument("--per-dest-top", type=int, default=0, help="include top-N cheapest periods per destination in output as per_dest_top")
    p.add_argument("--explore-max-requests", type=int, default=15, help="auto-cap explore destination list to fit this many Google requests (default 15; anonymous bursts get throttled beyond that)")
    p.add_argument("--explore-time-budget", type=int, default=120, help="stop launching new destinations after this many seconds (default 120) and return partial results with coverage notes")
    # multi-airport / nearby
    p.add_argument("--nearby", action="store_true", help="expand --from/--to with airports within --nearby-km (offline dataset, OR-search via repeated tfs airports)")
    p.add_argument("--nearby-km", type=int, default=120, help="radius for --nearby expansion (default 120 km)")

    args = p.parse_args()
    if args.cookie:
        try:
            _install_cookie_patch(args.cookie)
        except Exception:
            pass

    # ── early date validation (workable errors) ──
    args.flex_range = None
    user_flex_window = args.flex_window
    user_flex_months = args.flex_months
    args.flex_window = None
    args.flex_months = 1
    compat_note = None

    def _translate_compat(anchor_date: str, window: int | None, months: int | None):
        """Old vocabulary (--flex-window/--flex-months around a --date anchor)
        translated into the explicit departure-range engine."""
        w = window if window is not None else 15
        m = months if months is not None else 1
        try:
            a = _parse_date(anchor_date)
        except ValueError as e:
            emit_error(str(e), hint="workable: --date must be YYYY-MM-DD")
        start = a - _dt.timedelta(days=w)
        end = a + _dt.timedelta(days=(m - 1) * 28 + w)
        note = f"--flex-window/--flex-months accepted for compatibility and translated to departure range {start.isoformat()}..{end.isoformat()}"
        return start, end, note

    compat_used = user_flex_window is not None or user_flex_months is not None
    flex_args = (args.flex_starting_date, args.flex_ending_date, args.flex_days)
    stay_band = (args.min_stay is not None) != (args.max_stay is not None)
    if stay_band:
        emit_error(
            "--min-stay and --max-stay must be given together",
            hint="workable: e.g. --min-stay 9 --max-stay 14 for trips of 9 to 14 days",
        )
    if args.min_stay is not None and args.flex_days is not None:
        emit_error(
            "--flex-days cannot combine with --min-stay/--max-stay",
            hint="workable: use --flex-days N for one exact length, or --min-stay A --max-stay B for a band",
        )
    if compat_used:
        if not args.date:
            emit_error(
                "--flex-window/--flex-months need an anchor --date",
                hint="workable: prefer explicit ranges: --flex-starting-date D1 --flex-ending-date D2 [--flex-days N | --min-stay A --max-stay B]",
            )
        if any(value is not None for value in flex_args):
            emit_error(
                "--flex-window/--flex-months cannot combine with --flex-starting-date/--flex-ending-date",
                hint="workable: use one vocabulary — either the anchor pair (--date + --flex-window) or the explicit range flags",
            )
        flex_start, flex_end, compat_note = _translate_compat(args.date, user_flex_window, user_flex_months)
        base_stay = None
        if args.return_date:
            try:
                base_stay = (_parse_date(args.return_date) - _parse_date(args.date)).days
            except ValueError as e:
                emit_error(str(e), hint="workable: dates must be YYYY-MM-DD")
        if args.min_stay is None and base_stay and base_stay > 0:
            w = user_flex_window if user_flex_window is not None else 15
            args.min_stay = max(1, base_stay - w)
            args.max_stay = base_stay + w
        elif args.min_stay is None and args.flex_days:
            args.min_stay = args.max_stay = args.flex_days
        args.flex_starting_date, args.flex_ending_date = flex_start.isoformat(), flex_end.isoformat()
        args.flex_range = (flex_start, flex_end)
        args.flex_window = 15
        args.flex_months = max(1, ((flex_end - flex_start).days + 1 + 27) // 28)
        if not args.return_date:
            args.return_date = (flex_start + _dt.timedelta(days=args.min_stay or 7)).isoformat()
    elif any(value is not None for value in flex_args[:2]) or args.flex_days is not None:
        if args.flex_starting_date is None or args.flex_ending_date is None:
            emit_error(
                "flexible searches require --flex-starting-date, --flex-ending-date, and (--flex-days or --min-stay/--max-stay)",
                hint="workable: e.g. --flex-starting-date 2027-01-01 --flex-ending-date 2027-12-31 --flex-days 12",
            )
        try:
            flex_start = _parse_date(args.flex_starting_date)
            flex_end = _parse_date(args.flex_ending_date)
        except ValueError as e:
            emit_error(str(e), hint="workable: flexible dates must be YYYY-MM-DD")
        if flex_end < flex_start:
            emit_error(
                "--flex-ending-date must be on or after --flex-starting-date",
                hint="workable: set the earliest and latest departure dates in chronological order",
            )
        if args.flex_days is not None:
            if args.flex_days <= 0:
                emit_error("--flex-days must be a positive integer", hint="workable: use --flex-days 12 for a 12-day trip")
            args.min_stay = args.max_stay = args.flex_days
        else:
            if args.min_stay <= 0 or args.max_stay < args.min_stay:
                emit_error(
                    "--min-stay/--max-stay must be positive with max >= min",
                    hint="workable: e.g. --min-stay 9 --max-stay 14",
                )
        args.flex_range = (flex_start, flex_end)
        # The native calendar RPC needs a reference pair. These are internal
        # anchors; output exposes the requested range instead.
        args.date = flex_start.isoformat()
        args.return_date = (flex_start + _dt.timedelta(days=args.min_stay)).isoformat()
        args.flex_window = 15
        span_days = (flex_end - flex_start).days + 1
        args.flex_months = max(1, (span_days + 27) // 28)
        if args.legs:
            emit_error(
                "flexible date ranges cannot be combined with --legs",
                hint="workable: use --flex-starting-date --flex-ending-date --flex-days for one route, or --legs for explicit fixed-date segments",
            )
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
    # ── Explore mode — engine moved to flights_search/explore.py ──
    # infer explore if --from without --to (redundant flag, keep compat)
    is_explore = bool(args.explore or (args.from_ and args.date and not args.to and not args.legs))
    if is_explore:
        run_explore(args, trip=trip)
        return


    # ── Multi-airport / nearby-airports normalization ──
    from_codes_all = _split_codes(args.from_)
    to_codes_all = _split_codes(args.to)
    wants_multi = len(from_codes_all) > 1 or len(to_codes_all) > 1
    if wants_multi or args.nearby:
        if args.explore or args.flex_range is not None:
            emit_error("multi-airport (--from a,b) and --nearby can't combine with --explore or a flexible date range", hint="workable: run one flexible/explore pass per airport pair instead")
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
        print(json.dumps({"ok": True, "url": url, "query": {"from": args.from_, "to": args.to, **_date_query_fields(args), "trip": trip, "seat": args.seat}}, ensure_ascii=False))
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

    # ── Flexible search — engine moved to flights_search/flex_engine.py ──
    if args.flex_window is not None:
        run_flex_range(args, trip=trip, url=url, compat_note=compat_note)
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

    # partnership semantics: server filter is an OR across the ecosystem
    # (--airlines G3,AF); this client pass requires the anchor carrier.
    if args.include_airlines:
        flights_list = _keep_flights_with_any_airline(result, flights_list, args.include_airlines)
        out_note = f"filtered to itineraries including {args.include_airlines.upper()}"

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
        "query": {"from": args.from_, "to": args.to, "date": args.date, "return_date": args.return_date, "legs": json.loads(args.legs) if args.legs else None, "trip": trip, "seat": args.seat, "currency": args.currency, "language": args.language, "max_price": args.max_price, "include_airlines": args.include_airlines},
        "count": len(flights_list),
        "flights": flights_list,
        "metadata": meta,
        "url": url,
    }
    if custom_url:
        out["query"]["origins"] = from_codes_all
        out["query"]["destinations"] = to_codes_all
        out["query"]["nearby"] = bool(args.nearby)
    if args.include_airlines and out_note:
        out["notes"] = [out_note]
    if len(flights_list) == 0:
        # workable: client filters removed all, or no flights for query - distinction
        out["hint"] = "workable: no flights after filters - try removing --min-price/--max-price-client, broadening dates, or removing --airlines/--max-stops"
        if args.include_airlines:
            out["hint"] = "workable: no itineraries include the required carrier — drop --include-airlines to see the raw ecosystem, or widen --airlines"
    if native_graph is not None:
        out["price_graph"] = native_graph
        if native_graph:
            out["price_graph_cheapest"] = min(native_graph, key=lambda x: x["price"])
    if args.price_insights:
        out["price_insights"] = price_insights
    print(json.dumps(out, ensure_ascii=False))
