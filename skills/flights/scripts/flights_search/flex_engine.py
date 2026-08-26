"""Flexible date-range engine: chunked oneway-sum sweeps with split-retry."""

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

from .calendar_rpc import native_grid_for_route
from .tfs_urls import _calendar_pair_url, _pair_query
from .util import _classify_fetch_error, _date_query_fields, _parse_date, _payload_from_html, emit_error, extract_price_graph_from_payload, extract_price_insights, fetch_error_is_transient

def run_flex_range(args, *, trip: str, url: str, compat_note: str | None = None) -> None:
    if not args.from_ or not args.to or not args.flex_range:
        emit_error("flexible searches require --from, --to, --flex-starting-date, --flex-ending-date, and --flex-days", hint="workable: e.g. --from SSA --to MAD --flex-starting-date 2027-01-01 --flex-ending-date 2027-12-31 --flex-days 12")
    window = args.flex_window

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
    # Strategy: oneway-sum over small chunks. The departure range is swept
    # in --flex-chunk-days windows; each chunk fetches outbound fares
    # (from→to) and inbound fares (to→from) as native one-way calendars,
    # then cells are summed per (departure, stay). A failed chunk splits
    # in halves and retries so one poisoned far edge cannot blank a whole
    # sweep, and uncovered stretches are reported in coverage.gaps.
    # Empirical Google limit: the fare calendar publishes roughly
    # today..today+10½ months; beyond that, responses come back empty
    # even though exact fixed-date searches may still book them.
    grid: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, str]] = []
    flex_start, flex_end = args.flex_range
    min_stay = args.min_stay
    max_stay = args.max_stay

    _BACKOFFS = [15, 60, 300, 900, 1800]  # rate-limited → escalate to ~30 min, never abort

    def _sweep(frm: str, to: str, start: _dt.date, end: _dt.date, depth: int = 0, leaf_floor: int = 10, max_backoffs: int | None = None) -> dict[str, dict[str, Any]]:
        span = (end - start).days
        base = start + _dt.timedelta(days=span // 2)
        window = max(span - (span // 2), span // 2) + 1
        backoffs = _BACKOFFS if max_backoffs is None else [10] * min(1, max(0, max_backoffs))
        attempt = 0
        while True:
            detail = hint = None
            try:
                entries, _ = native_grid_for_route(
                    frm,
                    to,
                    base.isoformat(),
                    None,
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
                got = {e["departure"]: e for e in entries if e.get("departure")}
                if got or span <= leaf_floor:
                    return got
                detail, hint = "empty calendar response", "no entries for this window"
                break  # genuine no-data: may split below
            except Exception as exc:
                detail, hint = _classify_fetch_error(exc)
                if fetch_error_is_transient(detail) and attempt < len(backoffs):
                    wait = backoffs[attempt]
                    attempt += 1
                    time.sleep(wait)
                    continue
                break  # non-transient or patience exhausted: record once, no split storm
        if detail == "empty calendar response" and span > leaf_floor:
            mid = start + _dt.timedelta(days=span // 2)
            merged = {
                **_sweep(frm, to, start, mid, depth + 1, leaf_floor, len(backoffs)),
                **_sweep(frm, to, mid + _dt.timedelta(days=1), end, depth + 1, leaf_floor, len(backoffs)),
            }
            if merged or depth >= 3:
                return merged
        fetch_errors.append({"from": start.isoformat(), "to": end.isoformat(), "error": f"{detail} — {hint}"})
        return {}

    def _merge(dst: dict[str, dict[str, Any]], src: dict[str, dict[str, Any]]) -> None:
        for k, v in src.items():
            old = dst.get(k)
            if old is None or (v.get("price") is not None and old.get("price") is None):
                dst[k] = v

    # Adaptive chunk planner (default): sweep each direction with the FEWEST
    # requests that stay reliable. Empirically Google's calendar (a) accepts
    # ~185-day one-way requests, (b) blanks requests whose far edge crosses
    # the fare-publication wall (~today+315±8 days, rolling), so:
    #   - inside the wall  → chunks of min(span, ~185 days) — a ≤6-month
    #     range costs exactly ONE request per direction;
    #   - beyond the wall  → ONE coarse probe chunk (leaf_floor 60) that
    #     confirms emptiness cheaply instead of burning split-retries;
    #   - explicit --flex-chunk-days overrides slicing entirely.
    _FARE_HORIZON_DAYS = 302  # stay ~10d below the observed death band (+315..+323)
    today = _dt.date.today()
    wall = today + _dt.timedelta(days=_FARE_HORIZON_DAYS)

    def _plan_chunks() -> tuple[list[tuple[_dt.date, _dt.date, int]], tuple[_dt.date, _dt.date] | None]:
        import os as _os
        if _os.environ.get("FLEX_DEBUG"):
            print(f"[plan] today={today} wall={wall} explicit={args.flex_chunk_days} start={flex_start} end={flex_end}", file=sys.stderr)
        """Returns (chunks, unfetched_tail). Emptiness beyond the wall is
        monotonic, so only the first 30 days past it are probed; anything
        farther is reported as an offline gap."""
        plans: list[tuple[_dt.date, _dt.date, int]] = []
        if args.flex_chunk_days:  # explicit override: uniform slicing, full range
            size = max(7, min(200, args.flex_chunk_days))
            c = flex_start
            while c <= flex_end:
                plans.append((c, min(c + _dt.timedelta(days=size - 1), flex_end), 10))
                c += _dt.timedelta(days=size)
            return plans, None
        z_end = min(flex_end, wall)
        if z_end >= flex_start:
            span = (z_end - flex_start).days + 1
            size = min(185, span)
            c = flex_start
            while c <= z_end:
                plans.append((c, min(c + _dt.timedelta(days=size - 1), z_end), 10))
                c += _dt.timedelta(days=size)
        tail = None
        if flex_end > z_end:
            start_b = z_end + _dt.timedelta(days=1)
            probe_end = min(z_end + _dt.timedelta(days=30), flex_end)
            plans.append((start_b, probe_end, 60))  # cheap emptiness confirmation
            if probe_end < flex_end:
                tail = (probe_end + _dt.timedelta(days=1), flex_end)
        if _os.environ.get("FLEX_DEBUG"):
            for p in plans:
                print(f"[plan] chunk {p[0]} → {p[1]} leaf={p[2]}", file=sys.stderr)
            print(f"[plan] tail={tail}", file=sys.stderr)
        return plans, tail
    outbound: dict[str, dict[str, Any]] = {}
    inbound: dict[str, dict[str, Any]] = {}
    probe_zones: list[tuple[_dt.date, _dt.date]] = []
    plans, unfetched_tail = _plan_chunks()
    for c_start, c_end, leaf_floor in plans:
        is_probe = leaf_floor > 10
        if is_probe:
            probe_zones.append((c_start, c_end))
        mb = 1 if is_probe else None
        ob = _sweep(args.from_, args.to, c_start, c_end, 0, leaf_floor, mb)
        ib = _sweep(args.to, args.from_, c_start + _dt.timedelta(days=min_stay), c_end + _dt.timedelta(days=max_stay), 0, leaf_floor, mb)
        _merge(outbound, ob)
        _merge(inbound, ib)

    # If a beyond-wall probe unexpectedly found prices (Google loaded more
    # schedule since our last calibration), re-sweep that zone at normal
    # granularity once so those dates get full coverage.
    def _priced_in(d: dict[str, dict[str, Any]], s: _dt.date, e: _dt.date) -> bool:
        return any(
            v.get("price") is not None and s <= _parse_date(k[:10]) <= e
            for k, v in d.items()
        )

    for z_start, z_end in probe_zones:
        if _priced_in(outbound, z_start, z_end) or _priced_in(inbound, z_start - _dt.timedelta(days=min_stay), z_end):
            _merge(outbound, _sweep(args.from_, args.to, z_start, z_end))
            _merge(inbound, _sweep(args.to, args.from_, z_start + _dt.timedelta(days=min_stay), z_end + _dt.timedelta(days=max_stay)))

    rt_query = _pair_query(
        args.from_,
        args.to,
        flex_start.isoformat(),
        (flex_start + _dt.timedelta(days=min_stay)).isoformat(),
        args.currency,
        args.language,
        args.seat,
        passengers_dict,
        baggage_args,
        filters,
    )
    d = flex_start
    while d <= flex_end:
        oe = outbound.get(d.isoformat())
        for stay in range(min_stay, max_stay + 1):
            r = d + _dt.timedelta(days=stay)
            ie = inbound.get(r.isoformat())
            pair_prices = [x.get("price") for x in (oe, ie) if x is not None]
            price = (
                sum(pair_prices)
                if len(pair_prices) == 2 and all(isinstance(p, (int, float)) for p in pair_prices)
                else None
            )
            cell: dict[str, Any] = {
                "departure": d.isoformat(),
                "return": r.isoformat(),
                "price": price,
                "url": _calendar_pair_url(rt_query, d.isoformat(), r.isoformat()),
                "to": args.to,
            }
            if max_stay > min_stay:
                cell["stay"] = stay
            if price is None:
                if unfetched_tail and d >= unfetched_tail[0]:
                    cell["error"] = "beyond fare-publication horizon (not fetched)"
                else:
                    err = next((x.get("error") for x in (oe, ie) if x and x.get("error")), None)
                    cell["error"] = err or ("no fare published for one of the dates" if (oe or ie) else "sweep returned nothing for these dates")
            grid.append(cell)
        d += _dt.timedelta(days=1)
    total_pairs = len(grid)

    mode = "flex-date-range"
    strategy = "oneway-sum-chunked"

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
    priced_deps = sorted({g["departure"] for g in grid_sorted if g.get("price") is not None})
    gaps: list[dict[str, str]] = []
    if priced_deps:
        priced_set = set(priced_deps)
        run_start: _dt.date | None = None
        gd = flex_start
        while gd <= flex_end:
            if gd.isoformat() not in priced_set:
                if run_start is None:
                    run_start = gd
            elif run_start is not None:
                gaps.append({"from": run_start.isoformat(), "to": (gd - _dt.timedelta(days=1)).isoformat()})
                run_start = None
            gd += _dt.timedelta(days=1)
        if run_start is not None:
            gaps.append({"from": run_start.isoformat(), "to": flex_end.isoformat()})
    out = {
        "ok": True,
        "mode": mode,
        "query": {"from": args.from_, "to": args.to, **_date_query_fields(args), "trip": trip, "seat": args.seat, "currency": args.currency},
        "count": count_ok,
        "total_pairs": total_pairs,
        "grid": grid_sorted,
        "cheapest": cheapest,
        "url": url,
        "strategy": strategy,
        "coverage": {
            "requested_from": flex_start.isoformat(),
            "requested_to": flex_end.isoformat(),
            "priced_from": priced_deps[0] if priced_deps else None,
            "priced_to": priced_deps[-1] if priced_deps else None,
            "stays": [min_stay, max_stay],
            "gaps": gaps,
        },
    }
    if compat_note:
        out["compat"] = compat_note
    if fetch_errors:
        out["chunk_errors"] = fetch_errors
    if count_ok == 0:
        out["hint"] = "workable: no flights in the requested range - broaden the flexible departure range, remove --airlines/--max-stops, or retry later"
        if fetch_errors:
            last = fetch_errors[-1]
            out["hint"] += f" | last fetch error: {last['error'][:140]}"
        else:
            out["hint"] += " | calendar returned no fares: dates may sit past Google's fare-publication horizon (~today+10-11 months); confirm interesting dates with exact --date/--return-date searches"
    notes = [
        (
            "prices are outbound+return one-way fare sums (estimate); verify candidates with an exact round-trip search before booking"
            if max_stay == min_stay
            else "prices are outbound+return one-way fare sums (estimate) per stay length in days; verify candidates with an exact round-trip search before booking"
        )
    ]
    if 0 < count_ok < total_pairs and gaps and gaps[-1]["to"] == flex_end.isoformat():
        notes.append(f"no fares from {gaps[-1]['from']} onward — likely beyond Google's fare-publication horizon; use exact --date/--return-date searches for those dates")
    if args.include_airlines:
        notes.append("--include-airlines ignored in grid modes: cells carry no airline names; verify carriers via the cell url")
    out["notes"] = notes
    if native_graph is not None:
        out["price_graph"] = native_graph
        # also show cheapest in graph for fixed stay
        if native_graph:
            out["price_graph_cheapest"] = min(native_graph, key=lambda x: x["price"])
    if args.price_insights:
        out["price_insights"] = price_insights
    print(json.dumps(out, ensure_ascii=False))
    return
