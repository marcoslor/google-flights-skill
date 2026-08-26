---
name: flights
description: Fast Google Flights search via the protobuf API (fast-flights + reverse-engineered RPCs) — no browser, no anti-bot wall. Use when the user wants to find flights, compare prices, check routes or flexible dates, explore destinations from an origin, search multiple airports at once, or find partner-airline itineraries ("find flight GRU to JFK", "cheapest MYJ to TPE", "round-trip SFO to NRT", "flexible dates GRU JFK", "anywhere from SSA on Gol"). When the user restricts airlines, clarify strict operating-carrier versus partner/codeshare intent before searching.
license: MIT
metadata:
  author: marcoslor
  repository: https://github.com/marcoslor/google-flights-skill
  version: "1.2"
---

# Flights (Google Flights via fast-flights)

## Default workflow (follow for ANY flights request — no user prompting needed)

1. Load context: origin/destination codes, dates or date-range, stay length, airline intent (ask only if the Airline intent rule below requires it).
2. **Airline-constrained "anywhere/region" asks** (e.g. "Europe via Gol"): build the universe with `scripts/airline-destinations.py` first (anchor mode), never `--explore`.
3. **Cheapest-date or open-ended date asks** ("cheapest week/month", "flexible", "sometime in 2027", a month range): sweep with `--flex-starting-date/--flex-ending-date` + `--flex-days` or `--min-stay/--max-stay`. Never anchor a fixed `--date`. Batch multiple destinations into ONE `--explore-dests` sweep with `--per-dest-top N`.
4. **Verify** grid/explore candidates with exact fixed-date searches (`--include-airlines G3 --hide-separate`) before presenting them as bookable.
5. Run every sweep with a generous bash timeout (≥300000 ms; typical runtimes in "Fare-publication horizon" below). Report `coverage.gaps` honestly; present compact rows + url.

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
The launcher is thin; implementation lives in the `scripts/flights_search/` package (`util`, `dataset`, `tfs_urls`, `calendar_rpc`, `queries`, `explore`, `flex_engine`, `cli`). All paths in this document are relative to this skill's base directory.
It builds the protobuf query, fetches, parses, and prints **one JSON line**. Shapes:
- `{"ok":true,"query":{"from":"GRU","to":"JFK","date":"2026-09-01",...},"count":N,"flights":[{price,airlines,flights:[{from,to,departure,arrival,duration,plane_type}],carbon}],"metadata":{...},"url":"https://www.google.com/travel/flights/search?tfs=..."}`
- `{"ok":true,"count":0,"flights":[]}` — no flights found (not an error).
- `{"ok":false,"reason":"error","detail":...}` — fetch/parse failure.

No login, no `/verify` wall. If `primp` is blocked (rare), the error detail will say so — retry once or suggest a proxy.

## Flags
**Required (no defaults, exit with `hint` if missing):**
- `--from CODE` — origin IATA (e.g. `GRU`, `SSA`, `JFK`) — required
- `--to CODE` — destination IATA — required unless exploring (omit `--to` to explore anywhere from `--from`)
- fixed-date search: `--date YYYY-MM-DD` — departure date
- flexible-date search: `--flex-starting-date YYYY-MM-DD --flex-ending-date YYYY-MM-DD` plus either `--flex-days N` (exact length) or `--min-stay A --max-stay B` (band). The engine sweeps the range in `--flex-chunk-days` chunks (default 30), sums outbound+reversed one-way calendars per (departure, stay), auto-splits failed chunks, and reports uncovered stretches in `coverage.gaps`. Cell prices are estimates — always verify candidates with an exact fixed-date round-trip search before quoting them as bookable fares
- `--legs JSON` — alternative to `--from/--to/--date` for fixed-date multi-city

**Optional (sensible defaults, agent may omit):**
- `--return-date YYYY-MM-DD` — fixed-date return date; if set, implies `--trip round-trip`
- `--trip one-way|round-trip|multi-city` — default `one-way` (auto-set)
- `--seat economy|premium-economy|business|first` — default `economy`
- `--airlines` / `--currency` etc. — default none/`BRL` auto

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
- `--cookie "COOKIE_STRING"` — raw Cookie header (e.g. after solving a `/sorry` captcha once in a browser: `GOOGLE_ABUSE_EXEMPTION=...; NID=...`); applied to every request and clears the wall
- `--flex-concurrency N` — parallel chunk fetches (default 3, max 5); lower it if throttled
- `--flex-limit N` — trim grid output to cheapest N cells
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

Not combinable with `--explore`, a flexible date range, `--legs`, or `/m/` city entities (each emits a workable error).

## Flexible dates — explicit departure range

Flexible round-trip searches use the explicit range flags:

- `--flex-starting-date` — earliest departure date to consider
- `--flex-ending-date` — latest departure date to consider
- trip length: either `--flex-days N` (exact) or `--min-stay A --max-stay B` (band; cells then carry a `stay` key)

**Flag vocabulary is closed.** The only flexible flags are those four plus `--flex-chunk-days`. Do NOT invent others. For compatibility, `--date D --flex-window W [--flex-months M]` is accepted and silently translated into an equivalent departure range (output carries a `compat` note); prefer the explicit range flags.

Example: cheapest 12-day trips departing anytime in 2027:

```
scripts/flights-search.py --from SSA --to MAD \
  --flex-starting-date 2027-01-01 \
  --flex-ending-date 2027-12-31 \
  --flex-days 12 --currency BRL
```

Variable-stay example — cheapest trips of 9–14 days in Q2 2027:

```
scripts/flights-search.py --from SSA --to LIS \
  --flex-starting-date 2027-04-01 --flex-ending-date 2027-06-30 \
  --min-stay 9 --max-stay 14 --currency BRL
```

Output mode is `flex-date-range`. Key fields: `grid` (cells with `departure`, `return`, `price`, optional `stay`/`error`), `cheapest`, and `coverage` = `{requested_from, requested_to, priced_from, priced_to, stays, gaps:[{from,to}]}`. Read `coverage.gaps` before concluding anything: unpriced stretches are normal beyond Google's fare-publication horizon.

### Fare-publication horizon (read before sweeping far dates)

Google's fare calendar publishes roughly **today → today+10½ months** (rolling). Departures beyond that come back as unpriced cells (`coverage.gaps` + trailing note); exact fixed-date searches return 0 there too until airlines load schedules. This is not a bug — do not retry or debug it. When a user asks for dates past the wall, answer with what IS priced and state plainly that later months are not yet bookable/published.

Typical runtimes (set tool timeouts accordingly): short sweep ≤1 month ≈ 5–20 s; one quarter ≈ 20–60 s; full year ≈ 1–3 min per route (use timeout ≥ 300000 ms).

The native price graph remains available for a fixed-date, near-term search:

**1. Native price graph (bar, fixed stay)**
`--price-graph` extracts the 61-day graph Google already returns at `payload[5][10][0]` for the *same stay length*. Parsed from the same fetch as the flights — zero extra cost, covers ~today±30d (near-term). Use to find cheapest departure for a fixed duration in the next 2 months.

```
scripts/flights-search.py --from GRU --to JFK --date 2026-08-25 --return-date 2026-08-30 --price-graph
# → {"price_graph":[{"date":"2026-07-24","price":331},...],"price_graph_cheapest":{...},"price_insights":{...}}
```

The calendar RPC is an undocumented Google frontend endpoint, not a stable public API. If it changes, the command returns a workable hint.

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

For flexible date grids and `--explore` without an airline/company constraint, calendar cells do not carry carrier names and `--include-airlines` is ignored. Use the grid only to discover candidate destination/date pairs, then run an exact unfiltered search with `--include-airlines G3` for every candidate before presenting it as a GOL itinerary. When an airline/company constraint is present, use the destination inventory below instead of `--explore` for the destination universe. Never present a grid-only price as verified GOL availability.

## Destination inventory before searching all destinations

For requests such as **"cheapest destinations served by airline X"** or **"anywhere from SSA on GOL"**, do not use Google's capped `--explore` destination list as the universe. Run the bundled stateless route-inventory producer first:

```
python3 scripts/airline-destinations.py \
  --from SSA --airlines G3 --mode strict \
  --max-hops 2 --international --format json
```

It fetches Jonty's public weekly route JSON and returns candidate destination airports with route paths and carrier provenance. The output includes a prominent `note` explaining that it is not a complete list of an airline's bookable destinations and may omit codeshares, partner-marketed, interline, seasonal, or date-specific destinations. It does not store a database and does not verify fares; the caller/LLM owns piping or fanning out the returned `destinations` into exact `flights-search.py` calls.

This supersedes `--explore` for airline-scoped “anywhere” requests: inventory chooses the candidate universe, and exact flight searches provide the live price/bookability check.

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

### Mandatory disclosure for airline-destination discovery

When the user's intent is to discover or list an airline's destinations, tell them before presenting results: **"This is a non-exhaustive candidate list, not a complete list of all destinations bookable through the airline."** Do not call the output "all destinations" or imply that a missing city is unavailable. Distinguish route-data candidates from destinations verified by a live, date-specific search.

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

## Explore — unconstrained anywhere (no airline/company filter)

**Agent rule:** use `--explore` only when the user did not constrain the airline/company. ONE capped explore run is the initial answer. Present the results plus the `explore_meta.request_budget` coverage note (e.g. "searched top 15 of 181 destinations"). For an airline/company constraint, use `airline-destinations.py` instead; do not run `--explore` as a second destination-discovery mechanism. Explore runs are request- and time-budgeted so they always finish quickly.

Omit `--to` to explore anywhere from `--from` (any origin worldwide, any `--airlines`, direct + 1-stop via hub, derived from public dataset). Filter with `--explore-intl` (international only).

```
# anywhere from SSA — direct + 1-stop
scripts/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --explore-intl
# same as omit --to (inferred explore):
scripts/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22

# exact 12-day dest × date grid (anywhere in 2027)
scripts/flights-search.py --from SSA --explore-intl \
  --flex-starting-date 2027-01-01 --flex-ending-date 2027-12-31 \
  --flex-days 12 --currency BRL
# → {"destinations":[...],"grid":[{"to":"AEP","price":2280},...],"cheapest":{...},"per_dest_cheapest":{...}}
```

Optional explore flags:
- `--explore` — explicit alias for omit-`--to` (redundant)
- `--explore-intl` — only international
- `--explore-limit N` — cap number of destinations
- `--explore-max-requests N` — request budget (default 15). Explore fans out one Google RPC per destination; big fan-outs are auto-capped to fit the budget — direct routes first, then nearest destinations by distance (not alphabetical) — and the run always succeeds; `explore_meta.request_budget` reports coverage (e.g. searched 15 of 181). For wider unconstrained coverage, raise the budget or batch tail coverage with `--explore-dests`; this is still not an airline destination inventory.
- `--explore-time-budget S` — wall-clock budget (default 120s): stops launching new destinations after S seconds and returns partial results with a `time_capped` coverage note
- `--explore-dests A,B,C` — explicit destination list overriding the dataset-derived one; batch tail coverage in a single command (session, pacing and budgets still apply)
- `--per-dest-top N` — add `per_dest_top` to output: top-N cheapest in-window periods per destination
- flexible explore searches use `--flex-starting-date`, `--flex-ending-date`, and `--flex-days`; grid cells are filtered to that departure range and exact stay length

## Long-horizon sweeps

Open-ended date questions ("after January", "sometime next summer", "cheapest week") must SWEEP a range, never anchor on one fixed start day:

```
# full 2027 range, exact 12-day stay; no arbitrary anchor date is exposed
scripts/flights-search.py --from SSA --to BCN \
  --flex-starting-date 2027-01-01 --flex-ending-date 2027-12-31 \
  --flex-days 12
```

The engine chunks the range automatically (`--flex-chunk-days`, default 30), retries failed chunks by splitting them, deduplicates cells, and filters to the requested stay(s). Pacing keeps anonymous sessions off Google's captcha wall; if you hit HTTP 429 `/sorry`, wait a few minutes and retry.

**Agent rules:**
- Translate the user's date boundary into `--flex-starting-date`/`--flex-ending-date`; never invent a fixed `--date`/`--return-date` pair for "flexible/cheapest" asks. Duration → `--flex-days`, or `--min-stay/--max-stay` when the user gives a range or says "around N days".
- For "top-N per destination": pass `--per-dest-top N --flex-starting-date ... --flex-ending-date ... --flex-days X` on the FIRST capped sweep AND on every `--explore-dests` batch (never stitch outputs by hand), then merge `per_dest_top` maps. For uncovered destinations follow up with ONE batched command per ~15 destinations via `--explore-dests MAD,LIS,FCO,...`.
- Check `coverage.priced_from/priced_to/gaps` before presenting; beyond the fare-publication horizon (see above) gaps are expected — report them, don't debug.

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
For flexible cheapest dates:
```
scripts/flights-search.py --from GRU --to JFK \
  --flex-starting-date 2026-09-01 --flex-ending-date 2026-10-31 \
  --flex-days 5
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
