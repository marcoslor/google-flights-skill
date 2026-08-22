---
name: flights
description: Fast Google Flights search via the protobuf API (fast-flights + reverse-engineered RPCs) — no browser, no anti-bot wall. Use when the user wants to find flights, compare prices, check routes or flexible dates, explore destinations from an origin, search multiple airports at once, or find partner-airline itineraries ("find flight GRU to JFK", "cheapest MYJ to TPE", "round-trip SFO to NRT", "flexible dates GRU JFK", "anywhere from SSA on Gol").
license: MIT
compatibility: Requires Python 3.10+ with fast-flights installed (pip install fast-flights; needs primp, protobuf, selectolax), network access to google.com, and a shell. Works in opencode and any Agent-Skills-compatible agent.
metadata:
  author: marcoslor
  repository: https://github.com/marcoslor/google-flights-skill
  version: "1.1"
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
scripts/flights-search.py [flags]
# or via python:
python3 scripts/flights-search.py [flags]
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
scripts/flights-search.py --from SSA,GRU --to MAD --date 2026-11-05 --return-date 2026-11-12 --currency USD --sort asc --top 4
# → flights from both SSA and GRU, cheapest first; query.origins/query.destinations echo the sets
```

Combine with `--nearby` to auto-expand each side with airports within `--nearby-km` (offline geo dataset, haversine):

```
scripts/flights-search.py --from GRU --to MAD --date 2026-11-05 --return-date 2026-11-12 --nearby
# → GRU + CGH + VCP departures in one search
```

Not combinable with `--explore`, `--flex-window`, `--legs`, or `/m/` city entities (each emits a workable error).

## Flexible dates — Date Grid (2-axis table) & Price Graph (bar)

Google Flights' UI has two flexible views, both replicable:

**1. Native price graph (bar, fixed stay)**
`--price-graph` extracts the 61-day graph Google already returns at `payload[5][10][0]` for the *same stay length*. Parsed from the same fetch as the flights — zero extra cost, covers ~today±30d (near-term). Use to find cheapest departure for a fixed duration in the next 2 months.

```
scripts/flights-search.py --from GRU --to JFK --date 2026-08-25 --return-date 2026-08-30 --price-graph
# → {"price_graph":[{"date":"2026-07-24","price":331},...],"price_graph_cheapest":{...},"price_insights":{...}}
```

**2. Native date grid (2-axis table) — the only flex engine**
`--flex-window N` uses Google's internal `GetCalendarGrid` RPC. A normal 7×7 picker is one request per destination; wider windows are automatically split into rectangles of at most 200 cells. It returns the cheapest fare for every departure × return pair, each with a normal Google Flights URL. Stay filters are applied client-side on the returned matrix.

*Fixed stay* (default; same trip length as base dates):
```
scripts/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 2
# → {"mode":"flex-fixed-stay","grid":[{"departure":"2026-09-17","return":"2026-09-22","price":773},...],"cheapest":{...}}
```

*Variable stay range* (e.g. stay 7–12 nights flexible):
```
scripts/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 2 --min-stay 7 --max-stay 12
# → {"mode":"flex-variable-stay","grid":[...]}
```

*Full 2-axis matrix* (`--flex-grid` disables the fixed-stay filter):
```
scripts/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 1 --flex-grid
# → {"mode":"native-calendar-grid","grid":[...9 cells...],"cheapest":{...}}
```

*One-way flexible dates* (omit `--return-date`; same RPC, single leg):
```
scripts/flights-search.py --from GRU --to JFK --date 2026-09-15 --flex-window 3
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
scripts/flights-search.py --from /m/09wwlj --to /m/056_y --date 2027-05-30 --return-date 2027-06-09 --airlines G3
# → {"ok":false,"reason":"browser-session-required","url":"https://www.google.com/travel/flights/search?tfs=...","hint":"workable: open url via chrome-devtools/safari MCP ..."}
```

The script rewrites the tfs with the hidden Airport.type field (origin city = 3, destination city = 2) so the URL shows partner flights. City results are served client-side and gated to real browser sessions, so the script returns the ready-made URL instead of fetching: open it via chrome-devtools/safari MCP and read the result cards.

## Explore — any destination (no API key, from anywhere)

**Agent rule for "cheapest across all destinations" questions:** ONE capped explore run IS the answer. Present the results plus the `explore_meta.request_budget` coverage note (e.g. "searched top 15 of 181 destinations") immediately — do NOT loop remaining destinations unless the user explicitly asks for full coverage. Explore runs are request- and time-budgeted so they always finish quickly.

Omit `--to` to explore anywhere from `--from` (any origin worldwide, any `--airlines`, direct + 1-stop via hub, derived from public dataset). Filter with `--explore-intl` (international only).

```
# anywhere from SSA on GOL — direct + 1-stop (AEP + MAD via CDG etc.)
scripts/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3,AF --explore-intl
# same as omit --to (inferred explore):
scripts/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3,AF

# exhaustive 7-12d dest × date grid (anywhere)
scripts/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3,AF --explore-intl --flex-window 1 --min-stay 7 --max-stay 12 --currency BRL
# → {"destinations":[...],"grid":[{"to":"AEP","price":2280},...],"cheapest":{...},"per_dest_cheapest":{...}}
```

Optional explore flags:
- `--explore` — explicit alias for omit-`--to` (redundant)
- `--explore-intl` — only international
- `--explore-limit N` / `--explore-validate` — cap / prune stale
- `--explore-max-requests N` — request budget (default 15). Explore fans out one Google RPC per destination; big fan-outs are auto-capped to fit the budget — direct routes first, then nearest destinations by distance (not alphabetical) — and the run always succeeds; `explore_meta.request_budget` reports coverage (e.g. searched 15 of 181). For full coverage narrow `--airlines`/`--explore-intl`, raise the budget, or batch tail coverage with `--explore-dests`
- `--explore-time-budget S` — wall-clock budget (default 120s): stops launching new destinations after S seconds and returns partial results with a `time_capped` coverage note
- `--explore-dests A,B,C` — explicit destination list overriding the dataset-derived one; batch tail coverage in a single command (session, pacing and budgets still apply)
- `--per-dest-top N` — add `per_dest_top` to output: top-N cheapest in-window periods per destination
- `--flex-window` / `--min-stay` / `--max-stay` work with explore (stay range must overlap what the window can reach, or the command says so upfront)

## Long-horizon sweeps (--flex-months)

Open-ended date questions ("after January", "sometime next summer") should SWEEP months, not anchor on one week:

```
# contiguous 6-month sweep of one route (window 15 covers each month densely; ~9 RPCs/month)
# ALWAYS carry --min-stay/--max-stay when the user gave a duration — the tool filters cells, agents must not hand-filter
scripts/flights-search.py --from SSA --to BCN --date 2027-02-09 --return-date 2027-02-17 --flex-window 15 --flex-months 6 --min-stay 9 --max-stay 14

# light sampling across 6 months (3 anchors' worth of requests — good first pass), stay flags included
scripts/flights-search.py --from SSA --date 2027-02-10 --return-date 2027-02-19 --airlines G3,AF --explore-intl --flex-window 2 --flex-months 6 --min-stay 9 --max-stay 14
```

`--flex-months N` (1..12) repeats the window at anchors every 28 days and dedupes cells. Request cost multiplies by N; the explore request/time budgets still auto-cap destinations. Pacing (1s between monthly anchors) keeps anonymous sessions off Google's captcha wall; if you still hit HTTP 429 `/sorry`, wait a few minutes and retry — the error is surfaced in the output hint.

**Agent rule:** for open-ended periods, default to `--flex-months` matching the asked horizon (e.g. "after January" → sweep Feb–Jul: `--flex-months 5..6`). Start with a small window + wide month span; drill down with `--flex-window 15` on the cheapest month only.

**Agent rule for "top-N per destination" questions:** pass `--per-dest-top 3 --min-stay X --max-stay Y` on the FIRST capped sweep and read `per_dest_top` — never hand-filter cells. For destinations the budget didn't cover, follow up with ONE batched command per ~15 destinations using `--explore-dests MAD,LIS,FCO,...` (same flags), not one invocation per destination.

## Invoking from an agent (minimal context)
Pass the skill + query + output intent:
```
opencode run "Use the flights skill. Search GRU->JFK on 2026-09-01, one-way, economy. Output top 3 cheapest as table."
```
For round-trip:
```
scripts/flights-search.py --from GRU --to JFK --date 2026-09-01 --return-date 2026-09-10 --currency USD --limit 5 --sort asc
```
For multi-city:
```
scripts/flights-search.py --legs '[{"from":"MYJ","to":"TPE","date":"2026-08-25"},{"from":"TPE","to":"MYJ","date":"2026-08-30"}]' --seat economy
```
For filtered nonstop under $800:
```
scripts/flights-search.py --from SFO --to NRT --date 2026-10-01 --max-stops 0 --max-price 800 --currency USD --seat economy
```
For flexible cheapest (GFlights grid):
```
scripts/flights-search.py --from GRU --to JFK --date 2026-09-15 --return-date 2026-09-20 --flex-window 2 --price-graph
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
