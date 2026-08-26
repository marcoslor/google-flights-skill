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

from .util import _haversine_km

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
        # Budget caps keep the head of this list — order by proximity (km) so
        # capped sweeps probe plausibly-cheap nearby destinations first instead
        # of an alphabetical census of the letter A.
        dests = sorted(
            seen.values(),
            key=lambda x: (x["via"] is not None, x.get("km") if x.get("km") is not None else 99999),
        )
        meta["count"] = len(dests)
        meta["direct"] = len(direct_hubs)
        return dests, meta


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
