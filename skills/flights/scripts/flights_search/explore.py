"""Explore mode: dataset-driven destination fan-out over the native calendar grid."""

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

from .calendar_rpc import _explore_request_estimate, _grid_chunks_for_window, _open_calendar_session, native_grid_for_route
from .dataset import get_public_destinations
from .tfs_urls import _pair_query
from .util import _classify_fetch_error, _date_query_fields, _parse_date, _per_dest_top, emit_error, flex_month_anchors


def run_explore(args, *, trip: str) -> None:
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
    if args.explore_dests:
        # Explicit destination list (tail-coverage batches): overrides the
        # dataset-derived list; dataset still enriches display metadata.
        wanted = [c.strip().upper() for c in args.explore_dests.split(",") if c.strip()]
        known = {d["iata"]: d for d in dests}
        dests = [known.get(c, {"iata": c, "country_code": None, "display_name": c}) for c in wanted]
        explore_meta["source"] = "explicit --explore-dests"
    if not dests:
        hint = "workable: check --from code, try without --airlines, without --explore-intl, or use --explore-scope network for 1-stop"
        if explore_meta.get("error"):
            hint += f" ({explore_meta['error']})"
        emit_error(f"no destinations found for {args.from_}", hint=hint, extra={"explore_meta": explore_meta})
    if args.explore_limit and args.explore_limit > 0:
        dests = dests[: args.explore_limit]

    # Request-budget guard: explore fans out one RPC per destination (per
    # grid chunk). Never error out on big fan-outs — auto-cap the list to
    # fit the budget (direct routes first) and report coverage in-band so
    # zero-context agents always get results plus honest scope notes.
    # Exception: an explicit --explore-dests list IS the user's requested
    # scope, so when the budget is still at the default, auto-fit it to
    # cover every listed destination instead of silently capping to one.
    per_dest = _grid_chunks_for_window(args.flex_window) if args.flex_window is not None else 1
    est_requests = _explore_request_estimate(len(dests), args.flex_window, args.flex_months)
    max_requests = max(1, args.explore_max_requests)
    if args.explore_dests and args.explore_max_requests == 15 and est_requests > 15:
        needed = min(est_requests, len(dests) * per_dest + len(dests))
        args.explore_max_requests = max_requests = needed
        explore_meta["request_budget"] = {
            "auto_fitted": True,
            "requested_destinations": len(dests),
            "estimated_requests": needed,
            "note": "explicit --explore-dests list: request budget auto-fitted to cover every destination",
        }
    if args.explore_dests and args.explore_time_budget == 120:
        args.explore_time_budget = min(900, max(240, 100 * len(dests)))
    if est_requests > max_requests:
        cap = max_requests // per_dest
        if cap < 1:
            emit_error(
                f"--explore-max-requests {max_requests} cannot fit even one destination ({per_dest} grid chunk(s) each)",
                hint=f"workable: raise --explore-max-requests to at least {per_dest}, or narrow the flexible departure range",
            )
        requested_count = len(dests)
        dests = dests[:cap]
        explore_meta["request_budget"] = {
            "capped": True,
            "requested_destinations": requested_count,
            "searched_destinations": len(dests),
            "estimated_requests": len(dests) * per_dest,
            "note": "direct routes kept first, then nearest destinations by distance; narrow --airlines/--explore-intl or batch tail coverage with --explore-dests",
        }

    # Flexible dates are served entirely by the native calendar grid;
    # stay filters are applied client-side on the returned matrix.
    mode = "explore"
    if args.flex_range is not None:
        window = args.flex_window
    else:
        window = None

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
        started = time.monotonic()
        searched_count = 0
        grid_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for destination in dests:
            if time.monotonic() - started > args.explore_time_budget:
                budget_note = explore_meta.setdefault("request_budget", {})
                budget_note["time_capped"] = {
                    "searched_destinations": searched_count,
                    "skipped_destinations": len(dests) - searched_count,
                    "budget_seconds": args.explore_time_budget,
                    "note": "stopped early at the time budget; results above are complete for the destinations searched",
                }
                break
            dest_errored = False
            for anchor_dep, anchor_ret in flex_month_anchors(args.date, args.return_date, args.flex_months):
                if time.monotonic() - started > args.explore_time_budget:
                    budget_note = explore_meta.setdefault("request_budget", {})
                    budget_note["time_capped"] = {
                        "searched_destinations": searched_count,
                        "skipped_destinations": len(dests) - searched_count,
                        "budget_seconds": args.explore_time_budget,
                        "note": "stopped early at the time budget; results above are complete for the destinations searched",
                    }
                    break
                entries: list[dict[str, Any]] | None = None
                for attempt in range(2):  # one retry: transient body/decode errors are common under bursts
                    try:
                        got, _ = native_grid_for_route(
                            args.from_,
                            destination["iata"],
                            anchor_dep,
                            anchor_ret,
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
                        entries = got
                        break
                    except Exception as e:
                        detail, hint = _classify_fetch_error(e)
                        if attempt == 0:
                            time.sleep(1.5)
                            continue
                        grid.append({
                            "departure": anchor_dep,
                            "return": anchor_ret,
                            "price": None,
                            "url": None,
                            "error": detail,
                            "hint": hint,
                            "to": destination["iata"],
                        })
                        dest_errored = True
                if entries:
                    for entry in entries:
                        key = (entry["departure"], entry["return"], entry["to"])
                        if key not in grid_by_key:
                            grid_by_key[key] = entry
                            total_pairs += 1
            if not dest_errored:
                searched_count += 1
        grid.extend(grid_by_key.values())
        if args.flex_range:
            flex_start, flex_end = args.flex_range
            grid = [
                g for g in grid
                if g.get("return")
                and flex_start <= _parse_date(g["departure"]) <= flex_end
                and args.min_stay <= (_parse_date(g["return"]) - _parse_date(g["departure"])).days <= args.max_stay
            ]
        mode = "explore+native-calendar-grid"
    else:
        # single pair per dest: one grid cell each (window 0)
        if not args.return_date:
            emit_error("explore needs --return-date", hint="workable: add --return-date (native grid is round-trip); for one-way use --to with explicit dates")
        shared_session = _open_calendar_session(args, args.proxy, args.currency)
        started = time.monotonic()
        for searched_count, destination in enumerate(dests):
            if time.monotonic() - started > args.explore_time_budget:
                budget_note = explore_meta.setdefault("request_budget", {})
                budget_note["time_capped"] = {
                    "searched_destinations": searched_count,
                    "skipped_destinations": len(dests) - searched_count,
                    "budget_seconds": args.explore_time_budget,
                    "note": "stopped early at the time budget; results above are complete for the destinations searched",
                }
                break
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

    if args.include_airlines:
        explore_meta["note"] = "--include-airlines ignored in grid modes: cells carry no airline names; verify carriers via the cell url"
    count_ok = len([g for g in grid_sorted if g["price"] is not None])
    out: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "query": {"from": args.from_, **_date_query_fields(args), "trip": trip, "seat": args.seat, "currency": args.currency, "airlines": args.airlines, "explore": True, "explore_scope": args.explore_scope, "explore_intl": args.explore_intl},
        "explore_meta": explore_meta,
        "destinations": dests,
        "count": count_ok,
        "total_pairs": total_pairs,
        "total_destinations": len(dests),
        "grid": grid_sorted,
        "cheapest": cheapest,
        "per_dest_cheapest": per_dest,
    }
    if args.per_dest_top and args.per_dest_top > 0:
        out["per_dest_top"] = _per_dest_top(grid_sorted, args.per_dest_top)
    if count_ok == 0:
        out["hint"] = "workable: no flights match - broaden the flexible departure range, remove --airlines, try --explore-scope network, or check grid[].error/hint per entry"
    print(json.dumps(out, ensure_ascii=False))
    return
