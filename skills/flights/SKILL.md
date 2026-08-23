---
name: flights
description: Fast Google Flights search via the protobuf API (fast-flights + reverse-engineered RPCs) — no browser, no anti-bot wall. Use when the user wants to find flights, compare prices, check routes or flexible dates, explore destinations from an origin, search multiple airports at once, or find partner-airline itineraries ("find flight GRU to JFK", "cheapest MYJ to TPE", "round-trip SFO to NRT", "flexible dates GRU JFK", "anywhere from SSA on Gol"). When the user restricts airlines, clarify strict operating-carrier versus partner/codeshare intent before searching.
license: MIT
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

## Airline intent — clarify before filtering

Whenever the user says **"only [airline]"**, asks to exclude airlines, or otherwise requests an airline restriction, ask which meaning they intend before searching if partner semantics are not explicit:

1. **Strict operating carrier** — every segment must be operated by the named airline (e.g. `G3` only).
2. **Anchor carrier plus partners** — the itinerary must contain at least one segment operated by the named airline, while partner airlines may operate the remaining segments; normally require one ticket and no self-transfer.
3. **Sold/bookable through the airline or loyalty program** — partner-only flights may be acceptable if the user explicitly allows them.

Do not assume that “find me GOL” means `--airlines G3`. That strict Google filter can hide valid GOL + Air France/KLM/other-partner itineraries. If the user does not answer, state the assumption before proceeding; for destination-discovery requests, prefer asking the clarification.

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

## Airline filtering & partnerships — READ BEFORE USING `--airlines`

Google's airline filter means **every segment** of an itinerary must match. Mixed
itineraries (Gol + partner metal) vanish under a single-airline filter:

```
--airlines G3            SSA→MAD  →  ZERO results (the AF leg kills the itinerary)
--airlines G3,AF         SSA→MAD  →  mixed itineraries AND pure-AF trips to cities GOL never serves (Algiers, Abuja...)
```

### Default for GOL + partner discovery: fetch broadly, then post-filter

For an exact route/date search where the user wants itineraries containing GOL but has not selected a single partner ecosystem, omit `--airlines` and post-filter the fetched results:

```
scripts/flights-search.py --from SSA --to BCN --date 2027-03-03 \
  --return-date 2027-03-13 --include-airlines G3 \
  --hide-separate --limit 50 --sort asc
```

This lets Google return GOL + Air France, GOL + KLM, GOL + other partners, and GOL-only options before the client keeps itineraries containing GOL. Use `--hide-separate` when “bookable” means one ticket/no self-transfer.

This is not by itself exhaustive: Google returns a ranked, finite result set. For an exhaustive partner sweep, also run explicit searches for the relevant official partner ecosystems, deduplicate, and verify the ticketing/carrier combination.

For flexible date grids and `--explore`, calendar cells do not carry carrier names and `--include-airlines` is ignored. Use an unfiltered grid only to discover candidate destination/date pairs, then run an exact unfiltered search with `--include-airlines G3` for every candidate before presenting it as a GOL itinerary. Never present a grid-only price as verified GOL availability.

## Destination inventory before searching all destinations

For requests such as **"cheapest destinations served by airline X"** or **"anywhere from SSA on GOL"**, do not use Google's capped `--explore` destination list as the universe. Run the bundled stateless route-inventory producer first:

```
python3 scripts/airline-destinations.py \
  --from SSA --airlines G3 --mode strict \
  --max-hops 2 --international --format json
```

It fetches Jonty's public weekly route JSON and returns candidate destination airports with route paths and carrier provenance. It does not store a database and does not verify fares; the caller/LLM owns piping or fanning out the returned `destinations` into exact `flights-search.py` calls.

Strict airline network:

```
python3 scripts/airline-destinations.py \
  --from SSA --airlines G3 --mode strict \
  --max-hops 2 --international --format jsonl
```

Anchor airline plus explicitly allowed partners (for example, GOL + Air France):

```
python3 scripts/airline-destinations.py \
  --from SSA --airlines G3,AF --anchor G3 --mode anchor \
  --max-hops 2 --international --format json
```

In anchor mode, the first route-graph edge must contain the anchor airline; later edges may contain any code in `--airlines`. Supply the partner codes appropriate to the user's stated partnership scope. Do not infer that every airline in the route dataset is a commercial partner. The route output is a candidate list; verify each destination/date with an unfiltered exact search plus `--include-airlines G3` and `--hide-separate`.

### Decision table — pick by intent

| User says | Command | Result |
|---|---|---|
| "só Gol" (strict, accept gaps) | `--airlines G3` | only all-GOL itineraries; zero on partner-only routes |
| **"Gol com parceria"** (Gol required, partner fills the rest) | no `--airlines` + `--include-airlines G3` | JSON: only itineraries containing ≥1 Gol segment (e.g. `SSA>GIG` Gol + `GIG>CDG>MAD` AF) |
| "Gol + a named partner ecosystem" | `--airlines G3,AF --include-airlines G3` | server-narrowed probe for the explicitly named ecosystem |
| "Gol ou AF, não importa quem opera" | `--airlines G3,AF` | whole ecosystem incl. pure-AF trips — usually NOT what people mean |
| Google's native city-level partnership view | `/m/...` entities + `--airlines G3` | most faithful; browser-gated → tool returns URL only |

### Rules

0. If the user requests an airline filter without clearly stating strict operating-carrier versus partner/codeshare intent, ask the clarification above before searching.
1. If the user mentions partnership ("com parceria", "via parceira"), NEVER use bare `--airlines G3` with airport codes — it silently returns zero.
2. Use the unfiltered exact-search recipe (`--include-airlines G3`, no `--airlines`) for broad GOL-partner discovery. Use the ecosystem server-filter (`--airlines G3,AF`) + anchor client-filter (`--include-airlines G3`) only when the user explicitly selected the Air France ecosystem or when a partner-specific probe is needed. Output carries `"notes": ["filtered to itineraries including G3"]`.
3. `--include-airlines` accepts codes or names (`G3`, `GOL`, `Gol`) and applies ONLY to itinerary results. Grid/flex/explore cells carry no carrier names — the output adds a note saying so; verify carriers via the cell `url`.
4. City entities (`/m/...`) are Google's own partnership mechanism but need a real browser session: the CLI rewrites the tfs with the hidden `Airport.type` field (origin city=3, dest city=2) and returns `{"ok":false,"reason":"browser-session-required","url":...}` — open via chrome-devtools/safari MCP and read result cards.

## Explore — any destination (no API key, from anywhere)

**Agent rule for "cheapest across all destinations" questions:** if no airline/company constraint is present, ONE capped explore run IS the answer. Present the results plus the `explore_meta.request_budget` coverage note (e.g. "searched top 15 of 181 destinations") immediately. If an airline/company constraint is present, run `scripts/airline-destinations.py` first and search the returned candidate list; do not treat capped `--explore` as exhaustive. Explore runs are request- and time-budgeted so they always finish quickly.

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

**Agent rule for "top-N per destination" questions:** pass `--per-dest-top 3 --min-stay X --max-stay Y` on the FIRST capped sweep AND on every `--explore-dests` batch (so you never stitch outputs by hand), then merge `per_dest_top` maps. For destinations the budget didn't cover, follow up with ONE batched command per ~15 destinations using `--explore-dests MAD,LIS,FCO,...` (same flags), not one invocation per destination.

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
