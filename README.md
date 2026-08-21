# flights-search — Google Flights CLI facade for agents

One-shot Google Flights searches from the command line, designed for LLM agents and scripts: **one invocation, one JSON line**. No browser, no login, no anti-bot wall.

```
bin/flights-search.py --from GRU --to JFK --date 2026-09-01 --return-date 2026-09-10 --limit 5 --sort asc
```

This repository is a **fork of [AWeirdDev/flights](https://github.com/AWeirdDev/flights)** (`fast-flights` v3.1.0, MIT) that adds `bin/flights-search.py` — a deterministic JSON-line CLI wrapping the library plus reverse-engineered Google Flights frontend RPCs. The upstream Python package is unchanged and remains usable as a library.

## Capability matrix (web UI vs CLI)

| Google Flights web-UI capability | CLI | How |
|---|---|---|
| Search one-way / round-trip / multi-city | ✅ | protobuf `tfs` + `primp`, 1 request |
| Full filter set (stops, airlines/alliances, times, duration, layovers, connections, cabin, passengers, bags, currency, max price, basic-economy) | ✅ | encoded server-side in the query |
| Flexible dates — round-trip (fixed stay, variable stay, full 2-axis matrix) | ✅ | `--flex-window` (+ `--min-stay/--max-stay`/`--flex-grid`) — native `GetCalendarGrid` RPC |
| Flexible dates — one-way | ✅ | `--flex-window N` without `--return-date` |
| Price graph (bar, near-term) | ✅ | `--price-graph` — parsed from the same fetch |
| Price insights ("typical prices", high/low verdict) | ✅ | `--price-insights` — free from `payload[5][1..5]` |
| Multiple airports per side ("select multiple airports") | ✅ | `--from SSA,GRU --to MAD` — repeated Airport on tfs fields 13/14 |
| Nearby-airports toggle | ✅ | `--nearby [--nearby-km R]` — offline geo dataset + haversine |
| Explore "anywhere" destinations | ✅* | `--explore` — public dataset + live grid probes; official `GetExploreDestinations` RPC assessed but needs an IATA→city-entity resolver |
| Partner itineraries via city entities (`/m/...`) | ⚠️ | CLI builds the typed URL; results need a real browser session → open via MCP |
| Booking options / OTA links per itinerary (`GetShoppingResults`) | ⚠️→URL | link to the flights page is enough: the emitted `url` opens the full booking flow (`--keep-tokens` adds preselected deep links) |
| Price tracking & alerts | ❌ | login-gated, not portable |

\* approximated: destination list is offline-derived (stale by weeks at worst), prices are live.

## Output contract

```jsonc
{"ok":true,"query":{...},"count":5,"flights":[{price,airlines,type,flights:[segments],carbon}],"metadata":{},"url":"https://www.google.com/travel/flights/search?tfs=..."}
{"ok":true,"count":0,"flights":[]}                          // no flights — not an error
{"ok":false,"reason":"browser-session-required","url":"...","hint":"workable: open url via chrome-devtools/safari MCP"} // gated mode
{"ok":false,"reason":"error","detail":"...","hint":"workable: ..."}   // every failure ships a recovery hint
```

Errors are always actionable: each failure carries a `hint` describing a workable next step.

## Quick start

```bash
# cheapest nonstops under $800
bin/flights-search.py --from SFO --to NRT --date 2026-10-01 --max-stops 0 --max-price 800 --currency USD

# multi-city
bin/flights-search.py --legs '[{"from":"MYJ","to":"TPE","date":"2026-08-25"},{"from":"TPE","to":"MYJ","date":"2026-08-30"}]'

# flexible round-trip: ±2d around both dates, stays of 7–12 nights
bin/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 2 --min-stay 7 --max-stay 12

# explore anywhere international from SSA on GOL (+partners), direct or 1-stop
bin/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3 --explore-intl

# partnership itineraries (city entities — returns a URL to open in a real browser session)
bin/flights-search.py --from /m/09wwlj --to /m/056_y --date 2027-05-30 --return-date 2027-06-09 --airlines G3
```

See [`SKILL.md`](SKILL.md) for the complete agent-facing reference (all flags, modes, output shapes).

## Architecture notes (reverse-engineered)

- **Normal searches**: Base64 protobuf `tfs` param built by `fast-flights`; HTML fetched anonymously with `primp`; results parsed from the embedded JS blob. No `/verify` wall observed.
- **`GetCalendarGrid`** (`/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetCalendarGrid`): undocumented batchexecute-style RPC powering the UI's flexible-dates table. Needs no bootstrap — `f.sid=0`, no CSRF token, one shared session. Response is a length-prefixed `wrb.fr` stream; wide windows are split into ≤200-cell rectangles fetched concurrently. Round-trip cells are `[dep, ret, [[?, price], token], …]`; single-leg requests answer with `[date, null, [[null, price], token], 1]`. Stay filters run client-side on the returned matrix.
- **Multi-airport OR-searches**: the UI's "select multiple airports" encodes each side as repeated `Airport` submessages on `FlightData` fields 13/14. Verified live: `GRU+SSA → MAD` returns exactly the union of the two single-airport baselines.
- **Price insights**: slots `payload[5][1..5]` hold current-cheapest / typical / delta / usual-low / usual-high; the UI renders "Prices are currently high" when current > usual-high.
- **City entities**: the Airport proto has an undocumented `type` field (1). Setting origin→3 / destination→2 switches Google from strict airport matching to city-level matching, which surfaces partner-operated itineraries (e.g. Gol × Air France SSA↔MAD). Results for city queries are served client-side and gated to real browser sessions, so the CLI emits a ready-made URL instead.
- **Explore destinations**: derived offline from the public `airline_routes.json` dump rather than Google's internal Explore RPCs — stale by weeks at worst, compensated by live price probes.
- **Assessed, not ported**: `GetShoppingResults` (booking options per selected itinerary) requires page-bootstrap session id, cookies and an anti-bot token — and doesn't need porting: the emitted flights-page link opens the whole booking flow. `GetExploreDestinations` *does* accept anonymous `f.sid=0` (90+ destinations verified) but requires a Freebase city entity as origin — plain IATA returns HTTP 400 — so it waits on an IATA→entity resolver.

## Known limitations

- Grid/calendar prices are route-level estimates, not bookable exact fares (no airline names per cell).
- Booking options and city-entity itineraries don't need porting — the emitted flights-page URL (via chrome-devtools/safari MCP when anonymous fetches are gated) is the interface.
- Undocumented RPCs can change without notice; failures degrade gracefully to `{"ok":false,...,"hint":"workable: ..."}`.
- Google applies the first leg's airline filter to the whole search (upstream behavior).

## Install

```bash
pip install fast-flights        # primp, protobuf, selectolax
python3 bin/flights-search.py --help
```

On macOS with Homebrew Python: `/opt/homebrew/bin/pip3.14 install --break-system-packages fast-flights`.

## Attribution

Forked from [AWeirdDev/flights](https://github.com/AWeirdDev/flights) (MIT) — all credit for the protobuf scraping approach goes upstream. Upstream docs: https://aweirddev.github.io/flights. This fork's additions (`bin/flights-search.py`, `SKILL.md`, the RPC reverse-engineering) are documented above; sponsor banners and integrations from upstream were removed.
