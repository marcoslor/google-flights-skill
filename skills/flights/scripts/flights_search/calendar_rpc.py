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

from .util import _parse_date
from .tfs_urls import _calendar_pair_url, _pair_query

def _grid_chunks_for_window(window: int) -> int:
    """How many ≤200-cell GetCalendarGrid rectangles a ±window request needs."""
    dep_days = 2 * window + 1
    dep_chunks = max(1, -(-dep_days // 13))
    ret_chunk_days = max(1, _CALENDAR_MAX_CELLS // min(dep_days, 13))
    ret_chunks = max(1, -(-dep_days // ret_chunk_days))
    return dep_chunks * ret_chunks


def _explore_request_estimate(n_dests: int, window: int | None, months: int = 1) -> int:
    """Estimated Google RPC count for an explore run."""
    return n_dests * (_grid_chunks_for_window(window) if window is not None else 1) * max(1, months)


_CALENDAR_GRID_ENDPOINT = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetCalendarGrid"
)


_CALENDAR_FRONTEND_FALLBACK = "boq_travel-frontend-flights-ui_20260819.01_p0"


_CALENDAR_MAX_CELLS = 200


def _install_cookie_patch(cookie: str) -> None:
    """Force every primp.Client (ours and fast-flights internals) to send `cookie`.

    fast-flights binds `from primp import Client` at module import, so patching
    primp.Client alone is not enough — rebind already-imported references too.
    """
    import sys as _sys
    import primp

    orig_client = primp.Client

    def _patched_client(*args, **kwargs):
        client = orig_client(*args, **kwargs)
        try:
            current = ""
            try:
                current = (client.headers.get("Cookie") or "") if hasattr(client.headers, "get") else ""
            except Exception:
                pass
            merged = f"{current}; {cookie}" if current and cookie not in current else (current or cookie)
            client.headers_update({"Cookie": merged})
        except Exception:
            pass
        return client

    primp.Client = _patched_client
    for name in ("fast_flights.fetcher", "fast_flights.integrations.base", "fast_flights.integrations.searchapi"):
        mod = _sys.modules.get(name)
        if mod is not None and getattr(mod, "Client", None) is orig_client:
            mod.Client = _patched_client


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


_CALENDAR_CALL_TIMEOUT = 15


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
        timeout=_CALENDAR_CALL_TIMEOUT,
    )
    response.raise_for_status()
    # A captcha/consent wall answers HTTP 200 with HTML — surface it instead of
    # parsing to a silent zero-cell result.
    if "wrb.fr" not in response.text[:8000]:
        snippet = response.text[:150].replace("\n", " ")
        raise RuntimeError(
            f"GetCalendarGrid returned a non-payload response (captcha/consent wall?) head={snippet}"
        )
    entries = _parse_calendar_grid(response.text)
    if not entries:
        # Soft-throttled responses sometimes carry a valid envelope without
        # cells; one quick retry usually recovers them.
        time.sleep(1.5)
        response = session["client"].post(
            _CALENDAR_GRID_ENDPOINT + params,
            headers=headers,
            data=body,
            timeout=_CALENDAR_CALL_TIMEOUT,
        )
        response.raise_for_status()
        entries = _parse_calendar_grid(response.text)
    return entries


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
    # Stagger chunk submissions: dense sweeps (window 15) burst 9+ requests at
    # once and instantly trip Google's throttle; pacing keeps I/O overlapped
    # while staying under the burst threshold.
    _CHUNK_PACING_S = 0.8
    with ThreadPoolExecutor(max_workers=min(max(1, concurrency), 5)) as ex:
        futures = []
        for i, r in enumerate(ranges):
            if i:
                time.sleep(_CHUNK_PACING_S)
            futures.append(ex.submit(fetch_range, r))
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
