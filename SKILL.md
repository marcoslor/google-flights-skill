---
name: flights
description: Fast Google Flights scraper (fast-flights / AWeirdDev/flights). Search flights via Google Flights protobuf API — no browser, no anti-bot wall. Use when the user wants to find flights, compare prices, or check routes/dates ("find flight GRU to JFK", "cheapest MYJ to TPE", "round-trip SFO to NRT").
---

# Flights (Google Flights via fast-flights)

## What the agent does
Search Google Flights with **minimum tokens** using the `fast-flights` Python library (AWeirdDev/flights, 1.9k stars). It encodes queries as Base64 protobuf (`?tfs=`), fetches via `primp` (Chrome 145 impersonation), and parses the embedded JS data — no Playwright, no browser needed.

The ONLY context the invoking agent needs:
- **origin / destination** (IATA 3-letter codes, e.g. `GRU`, `JFK`, `MYJ`)
- **date(s)** (`YYYY-MM-DD`)
- **what to return** (e.g. "top 5 cheapest", "nonstop only", "round-trip")

Filters and sorting are OPTIONAL refinements.

## The single entry point
```
bin/flights-search.py [flags]
# or via python:
python3 bin/flights-search.py [flags]
```
It builds the protobuf query, fetches, parses, and prints **one JSON line**. Shapes:
- `{"ok":true,"query":{"from":"GRU","to":"JFK","date":"2026-09-01",...},"count":N,"flights":[{price,airlines,flights:[{from,to,departure,arrival,duration,plane_type}],carbon}],"metadata":{...},"url":"https://www.google.com/travel/flights/search?tfs=..."}`
- `{"ok":true,"count":0,"flights":[]}` — no flights found (not an error).
- `{"ok":false,"reason":"error","detail":...}` — fetch/parse failure.

No login, no `/verify` wall. If `primp` is blocked (rare), the error detail will say so — retry once or suggest a proxy.

## Flags
**Required (no defaults, exit with `hint` if missing):**
- `--from CODE` — origin IATA (e.g. `GRU`, `SSA`, `JFK`) — required
- `--date YYYY-MM-DD` — departure date — required (unless `--legs`)
- `--to CODE` — destination IATA — required unless exploring (omit `--to` to explore anywhere from `--from`)
- `--legs JSON` — alternative to `--from/--to/--date` for multi-city

**Optional (sensible defaults, agent may omit):**
- `--return-date YYYY-MM-DD` — if set, implies `--trip round-trip`
- `--trip one-way|round-trip|multi-city` — default `one-way` (auto-set)
- `--seat economy|premium-economy|business|first` — default `economy`
- `--airlines` / `--flex-*` / `--currency` etc. — default none/`BRL` auto

**Passengers:**
- `--adults N` — default 1 (max total 9)
- `--children N` — default 0
- `--infants-seat N` — infants with seat
- `--infants-lap N` — infants on lap (must be ≤ adults)

**Global filters (whole search, passed to `create_query`):**
- `--currency CODE` — e.g. `USD`, `BRL`, `JPY` (default: Google decides)
- `--language LANG` — e.g. `en`, `pt-BR`, `zh-TW` (default: Google decides)
- `--max-price N` — max price in selected currency
- `--carry-on N` — carry-on bags to include fees for
- `--checked-bags N` — checked bags to include fees for
- `--hide-separate` — hide separate-ticket / self-transfer
- `--exclude-basic` — exclude basic economy

**Per-leg filters (applied to every leg):**
- `--max-stops N` — 0=nonstop, 1, 2...
- `--airlines CODE,CODE` — e.g. `JL,NH` or `ONEWORLD` (Google applies first leg's filter to whole search)
- `--connecting CODE,CODE` — allowed connecting airports
- `--earliest-departure H` / `--latest-departure H` — 0–23 local time
- `--earliest-arrival H` / `--latest-arrival H` — 0–23 local time
- `--max-duration N` — minutes
- `--min-layover N` / `--max-layover N` — minutes
- `--less-emissions` — only less-emissions flights

**Client-side post-processing:**
- `--limit N` — max flights to return (default 20, cap 50)
- `--sort asc|desc` — sort by price
- `--top N` — return only first N after sorting
- `--min-price N` / `--max-price-client N` — client-side price filter (different from server --max-price)
- `--proxy URL` — e.g. `http://user:pass@host:port` (passed to primp)
- `--url-only` — only print the Google Flights URL, don't fetch

**Price context & multi-airport:**
- `--price-insights` — include Google's price-insights panel (free, same request): `{current_cheapest, typical, usual_low, usual_high, verdict: high|low|normal}`
- `--price-graph` — include the native 61-day bar graph (parsed from the same fetch, no extra request)
- `--keep-tokens` — keep per-cell `booking_token` in grid output (for preselected browser flows)
- `--nearby` / `--nearby-km R` — expand --from/--to with airports within R km (default 120) into one OR-search
- `--from A,B` / `--to X,Y` — comma-separated airports = one combined OR-search (e.g. `--from SSA,GRU --to MAD`)

## Multi-airport searches (OR semantics)

`--from SSA,GRU --to MAD` returns itineraries departing **either** airport in one sorted result set — the same encoding Google's UI uses for "select multiple airports" (repeated Airport entries in the tfs protobuf; verified live: union of both single-airport baselines).

```
bin/flights-search.py --from SSA,GRU --to MAD --date 2026-11-05 --return-date 2026-11-12 --currency USD --sort asc --top 4
# → flights from both SSA and GRU, cheapest first; query.origins/query.destinations echo the sets
```

Combine with `--nearby` to auto-expand each side with airports within `--nearby-km` (offline geo dataset, haversine):

```
bin/flights-search.py --from GRU --to MAD --date 2026-11-05 --return-date 2026-11-12 --nearby
# → GRU + CGH + VCP departures in one search
```

Not combinable with `--explore`, `--flex-window`, `--legs`, or `/m/` city entities (each emits a workable error).

## Flexible dates — Date Grid (2-axis table) & Price Graph (bar)

Google Flights' UI has two flexible views, both replicable:

**1. Native price graph (bar, fixed stay)**
`--price-graph` extracts the 61-day graph Google already returns at `payload[5][10][0]` for the *same stay length*. Parsed from the same fetch as the flights — zero extra cost, covers ~today±30d (near-term). Use to find cheapest departure for a fixed duration in the next 2 months.

```
bin/flights-search.py --from GRU --to JFK --date 2026-08-25 --return-date 2026-08-30 --price-graph
# → {"price_graph":[{"date":"2026-07-24","price":331},...],"price_graph_cheapest":{...},"price_insights":{...}}
```

**2. Native date grid (2-axis table) — the only flex engine**
`--flex-window N` uses Google's internal `GetCalendarGrid` RPC. A normal 7×7 picker is one request per destination; wider windows are automatically split into rectangles of at most 200 cells. It returns the cheapest fare for every departure × return pair, each with a normal Google Flights URL. Stay filters are applied client-side on the returned matrix.

*Fixed stay* (default; same trip length as base dates):
```
bin/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 2
# → {"mode":"flex-fixed-stay","grid":[{"departure":"2026-09-17","return":"2026-09-22","price":773},...],"cheapest":{...}}
```

*Variable stay range* (e.g. stay 7–12 nights flexible):
```
bin/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 2 --min-stay 7 --max-stay 12
# → {"mode":"flex-variable-stay","grid":[...]}
```

*Full 2-axis matrix* (`--flex-grid` disables the fixed-stay filter):
```
bin/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 1 --flex-grid
# → {"mode":"native-calendar-grid","grid":[...9 cells...],"cheapest":{...}}
```

*One-way flexible dates* (omit `--return-date`; same RPC, single leg):
```
bin/flights-search.py --from GRU --to JFK --date 2026-09-15 --flex-window 3
# → {"mode":"flex-one-way","grid":[{"departure":"2026-09-17","price":437},...],"cheapest":{...}}
```

Flexible search works for one-way and round-trip. The native grid is an undocumented Google frontend RPC, not a stable public API — if it changes, the command returns a workable hint.

**Which to use?**
- Near-term (≤60d) fixed stay → `--price-graph` (parsed from the search fetch, instant).
- Flexible one-way → `--flex-window N` (no `--return-date`).
- Any flexible round-trip → `--flex-window N --return-date ...` (+ `--min-stay/--max-stay`, or `--flex-grid` for the full matrix).

## City entities — partnership flights (/m/...)

Google's airline filter is **strict on airport codes**: `--from SSA --to MAD --airlines G3` returns zero even when Gol+Air France partnership itineraries exist. With **Freebase city entities** the partners appear:

```
bin/flights-search.py --from /m/09wwlj --to /m/056_y --date 2027-05-30 --return-date 2027-06-09 --airlines G3
# → {"ok":false,"reason":"browser-session-required","url":"https://www.google.com/travel/flights/search?tfs=...","hint":"workable: open url via chrome-devtools/safari MCP ..."}
```

The script rewrites the tfs with the hidden Airport.type field (origin city = 3, destination city = 2) so the URL shows partner flights. City results are served client-side and gated to real browser sessions, so the script returns the ready-made URL instead of fetching: open it via chrome-devtools/safari MCP and read the result cards.

## Explore — any destination (no API key, from anywhere)

Omit `--to` to explore anywhere from `--from` (any origin worldwide, any `--airlines`, direct + 1-stop via hub, derived from public dataset). Filter with `--explore-intl` (international only).

```
# anywhere from SSA on GOL — direct + 1-stop (AEP + MAD via CDG etc.)
bin/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3,AF --explore-intl
# same as omit --to (inferred explore):
bin/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3,AF

# exhaustive 7-12d dest × date grid (anywhere)
bin/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3,AF --explore-intl --flex-window 1 --min-stay 7 --max-stay 12 --currency BRL
# → {"destinations":[...],"grid":[{"to":"AEP","price":2280},...],"cheapest":{...},"per_dest_cheapest":{...}}
```

Optional explore flags:
- `--explore` — explicit alias for omit-`--to` (redundant)
- `--explore-intl` — only international
- `--explore-limit N` / `--explore-validate` — cap / prune stale
- `--flex-window` / `--min-stay` / `--max-stay` work with explore

## Invoking from an agent (minimal context)
Pass the skill + query + output intent:
```
opencode run "Use the flights skill. Search GRU->JFK on 2026-09-01, one-way, economy. Output top 3 cheapest as table."
```
For round-trip:
```
bin/flights-search.py --from GRU --to JFK --date 2026-09-01 --return-date 2026-09-10 --currency USD --limit 5 --sort asc
```
For multi-city:
```
bin/flights-search.py --legs '[{"from":"MYJ","to":"TPE","date":"2026-08-25"},{"from":"TPE","to":"MYJ","date":"2026-08-30"}]' --seat economy
```
For filtered nonstop under $800:
```
bin/flights-search.py --from SFO --to NRT --date 2026-10-01 --max-stops 0 --max-price 800 --currency USD --seat economy
```
For flexible cheapest (GFlights grid):
```
bin/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 2 --price-graph
# or 2-axis: add --flex-grid ; variable stay: add --min-stay 3 --max-stay 7
```

## Presenting results
Show compact rows: `price · airlines · segments (FROM→TO dep-arr duration) · carbon (optional)`. Always include the Google Flights `url` for verification. Apply `--limit/--sort/--top` yourself if you prefer, but the binary already does it. Never dump raw HTML or the full JSON — compact rows + url only.

Token rule: output ONLY compact structured rows + url. No HTML, no verbose logs.

## Notes
- Dates must be `YYYY-MM-DD` future dates; past dates return 0 results.
- `price` is integer in the selected currency's minor unit as returned by Google JS (no division needed — display as `$price`).
- `flights[].flights` are the segments (1 = nonstop, 2+ = connections). `flights[].type` is the Google internal type.
- Install: `pip install fast-flights` (requires `primp`, `protobuf`, `selectolax`). Already installed via `pip --break-system-packages` on this host at `/opt/homebrew/bin/python3.14`.
- Source: https://github.com/AWeirdDev/flights • Pip: `fast-flights` • Docs: https://aweirddev.github.io/flights
