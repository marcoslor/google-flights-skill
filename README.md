# google-flights-search skill

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://pypi.org/project/fast-flights/)
[![Tests](https://github.com/marcoslor/google-flights-skill/actions/workflows/tests.yml/badge.svg?branch=main)]()
[![Install](https://img.shields.io/badge/install-npx%20skills%20add%20marcoslor%2Fgoogle--flights--skill-000000.svg)](https://skills.sh)

Skill + CLI helpers that let AI agents search Google Flights from the command line, using reverse-engineered Google Flights internals.

It replicates the full Google Flights web experience: every search filter, flexible-date grids, price insights and graphs, multi-airport and nearby-airport searches, "explore anywhere", even partner-airline itineraries.

```
skills/flights/scripts/flights-search.py --from GRU --to JFK --date 2026-09-01 --return-date 2026-09-10 --limit 5 --sort asc
```

This repository is a fork of [AWeirdDev/flights](https://github.com/AWeirdDev/flights) (MIT) that adds the CLI and an [opencode skill](skills/flights/SKILL.md); the upstream library is unchanged and usable on its own.

## Supported features (from the UI)

| Google Flights feature | CLI | Notes |
|---|---|---|
| Search one-way / round-trip / multi-city | ✅ | |
| All search filters — stops, airlines/alliances, times, duration, layovers, connections, cabin, passengers, bags, currency, max price | ✅ | |
| Flexible dates — round-trip | ✅ | `--flex-starting-date`, `--flex-ending-date`, `--flex-days` |
| Price insights ("typical prices", cheap/high verdicts) | ✅ | `--price-insights` |
| Multiple airports per side | ✅ | `--from SSA,GRU --to MAD` |
| Nearby-airports toggle | ✅ | `--nearby [--nearby-km R]` |
| Explore "anywhere" destinations | ✅* | `--explore` |
| Price graph (bar) | ⚠️ | near-term only (~±30 days), fixed stay |
| Partner-airline itineraries (city entities) | ⚠️ | CLI emits the URL; open it in a real browser session |
| Booking options / OTA links per itinerary | ⚠️ | the emitted `url` opens the booking flow — no porting needed |
| Exact fares & airline names inside date grids | ⚠️ | grid prices are per-date estimates; exact fares appear when you open a cell's link |
| Price tracking & alerts | ❌ | login-gated |

\* destination list comes from a public route dataset (stale by weeks at worst); prices are live.

## Install

As an agent skill (self-contained: SKILL.md + script travel together), via the open [Skills CLI](https://skills.sh) — works with Claude Code, OpenCode, Cursor, Codex, Gemini CLI and 40+ agents:

```shell
npx skills add marcoslor/google-flights-skill          # project scope
npx skills add marcoslor/google-flights-skill -g       # global, all projects
```

Manual setup (library deps only):

```bash
pip install fast-flights        # primp, protobuf, selectolax
python3 skills/flights/scripts/flights-search.py --help
```

On macOS with Homebrew Python: `/opt/homebrew/bin/pip3.14 install --break-system-packages fast-flights`.

## Quick start

```bash
# cheapest nonstops under $800
skills/flights/scripts/flights-search.py --from SFO --to NRT --date 2026-10-01 --max-stops 0 --max-price 800 --currency USD

# multi-city
skills/flights/scripts/flights-search.py --legs '[{"from":"MYJ","to":"TPE","date":"2026-08-25"},{"from":"TPE","to":"MYJ","date":"2026-08-30"}]'

# flexible round-trip: departures throughout a range, exact 12-day stay
skills/flights/scripts/flights-search.py --from GRU --to JFK \
  --flex-starting-date 2026-09-01 --flex-ending-date 2026-10-31 --flex-days 12

# explore anywhere international from SSA on GOL (+partners), direct or 1-stop
skills/flights/scripts/flights-search.py --from SSA --date 2027-06-15 --return-date 2027-06-22 --airlines G3 --explore-intl

# partnership itineraries (city entities — returns a URL to open in a real browser session)
skills/flights/scripts/flights-search.py --from /m/09wwlj --to /m/056_y --date 2027-05-30 --return-date 2027-06-09 --airlines G3
```

See [`skills/flights/SKILL.md`](skills/flights/SKILL.md) for the complete agent-facing reference (all flags, modes, output shapes).

## What a search returns

One JSON line, always:

```jsonc
{"ok":true,"query":{...},"count":5,"flights":[{price,airlines,type,flights:[segments],carbon}],"metadata":{},"url":"https://www.google.com/travel/flights/search?tfs=..."}
{"ok":true,"count":0,"flights":[]}                          // no flights — not an error
{"ok":false,"reason":"browser-session-required","url":"...","hint":"workable: open url via chrome-devtools/safari MCP"} // gated mode
{"ok":false,"reason":"error","detail":"...","hint":"workable: ..."}   // failures always ship a recovery hint
```

Every result includes a `url` to the equivalent Google Flights page for verification or booking.

## How it works under the hood

Reverse-engineering notes, for maintainers:

- **Normal searches**: Base64 protobuf `tfs` param built by `fast-flights`; HTML fetched anonymously with `primp`; results parsed from the embedded JS blob. No `/verify` wall observed.
- **Flexible dates** use `GetCalendarGrid` (`/_/FlightsFrontendUi/data/travel.frontend.flights.FlightsFrontendService/GetCalendarGrid`), the undocumented RPC behind the UI's date-grid. Needs no bootstrap — `f.sid=0`, no CSRF token, one shared session. Responses are length-prefixed `wrb.fr` streams; wide windows are split into ≤200-cell rectangles fetched concurrently. Round-trip cells are `[dep, ret, [[?, price], token], …]`; single-leg requests answer `[date, null, [[null, price], token], 1]`. Stay filters run client-side on the returned matrix.
- **Multi-airport searches** reuse the UI's own encoding for "select multiple airports": repeated `Airport` submessages on `FlightData` fields 13/14. Verified live — `GRU+SSA → MAD` returns exactly the union of both single-airport baselines.
- **Price insights** come free in every response: slots `payload[5][1..5]` hold current-cheapest / typical / delta / usual-low / usual-high; the UI renders "Prices are currently high" when current > usual-high.
- **City entities**: the Airport proto has an undocumented `type` field (1). Setting origin→3 / destination→2 switches Google from strict airport matching to city-level matching, surfacing partner-operated itineraries (e.g. Gol × Air France SSA↔MAD). City-query results are served client-side and gated to real browser sessions, so the CLI emits a ready-made URL instead.
- **Explore destinations** are derived offline from the public [`airline_routes.json`](https://github.com/Jonty/airline-route-data) dump rather than Google's internal Explore RPCs — stale by weeks at worst, compensated by live price probes.
- **Assessed, not ported**: `GetShoppingResults` (booking options per selected itinerary) requires page-bootstrap session id, cookies and an anti-bot token — and doesn't need porting: the emitted flights-page link opens the whole booking flow. `GetExploreDestinations` *does* accept anonymous `f.sid=0` (90+ destinations verified) but requires a Freebase city entity as origin — plain IATA returns HTTP 400 — so it waits on an IATA→entity resolver.

## Known limitations

- Undocumented RPCs can change without notice; failures degrade gracefully to `{"ok":false,...,"hint":"workable: ..."}`.
- Google applies the first leg's airline filter to the whole search (upstream behavior).

## Attribution

Forked from [AWeirdDev/flights](https://github.com/AWeirdDev/flights) (MIT) — all credit for the protobuf scraping approach goes upstream. Upstream docs: https://aweirddev.github.io/flights. This fork's additions (the CLI script, the skill, and the RPC reverse-engineering) are documented above; sponsor banners and integrations from upstream were removed.
