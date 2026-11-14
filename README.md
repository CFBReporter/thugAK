# thugsak-web

A React front-end over the thugsak prediction engine, with a Kalshi-style
interface. Forked from [`thugsak`](../thugsak) at tag `v1.0-streamlit`, which
stays frozen as the Streamlit version.

**Read-only.** No Kalshi API key, no order placement — the app prices markets,
it does not touch them. Leagues: NFL, NBA, NCAAF, NCAAMB.

The engine's own documentation, carried over unchanged, is in
[`docs/engine-README.md`](docs/engine-README.md).

## What changed from v1, and what did not

**Changed — the front-end.** Streamlit is replaced by React + Vite + TypeScript
talking to a thin FastAPI bridge.

**Changed — the data source is now a seam.** v1 named ESPN directly at every
call site. `src/thugsak/providers/` makes the game feed an interface, selected
by `THUGSAK_PROVIDER`.

**Unchanged — everything that produces a number.** The ratings solve, the
as-of guard, the fitted calibration, the injury model, the fee maths, the money
tiers, the five-bet verdict, the name matching, the Central-Time clock and the
prediction record are the v1 code, untouched. `api/main.py` forwards to
`thugsak.mcp.tools`, which is what the MCP server and the Streamlit app both
called. The browser and Claude cannot disagree about what a game is worth
because they are reading the same function.

## Running it

Two processes. The engine:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt -r requirements-api.txt
.venv/Scripts/python.exe -m uvicorn api.main:app --reload --port 8000
```

The front-end:

```bash
cd web
npm install
npm run dev          # http://localhost:5173
```

Vite proxies `/api` to port 8000, so the browser uses the same relative paths
in dev that it will in production. For a single-process deploy, `npm run build`
writes `web/dist`, which `api/main.py` mounts automatically if present —
then uvicorn alone serves the whole app.

## Layout

```
api/main.py                   HTTP bridge -- no logic, forwards to mcp.tools
scripts/run_agents.py         the hourly cycle; what Task Scheduler calls
scripts/register_task.ps1     installs the 07:00-01:00 Central schedule
scripts/backfill_ncaamb.py    recovers games the history import dropped
src/thugsak/agents/
  ingest_tools.py             Agent 1's deterministic tool belt (tested, no LLM)
  gate.py                     the change signature and the router
  runner.py                   window, lock, the three SDK loops, agent_runs log
  prompts.py                  the three system prompts
src/thugsak/providers/        the game-data seam
  base.py                     GameDataProvider protocol + registry
  espn.py                     default, working
  real.py                     blocked; see docs/data-sources.md
src/thugsak/                  the v1 engine, unchanged
web/src/
  App.tsx                     league tabs, date nav, list/detail routing
  api.ts                      typed fetch + useAsync with request aborting
  format.ts                   display rules, incl. why precision is capped
  styles.css                  design tokens
  components/
    GameList.tsx              the slate
    GameDetail.tsx            predicted score, the call, the five bets
    BetRow.tsx                one bet slot
    bits.tsx                  badges, money bar, states
docs/data-sources.md          provider status and what a swap costs
docs/engine-README.md         v1's documentation of the engine
```

## The data source

Default is `espn`. Override:

```bash
THUGSAK_PROVIDER=espn .venv/Scripts/python.exe -m uvicorn api.main:app
```

**Real Sports (realapp.com) is not usable.** It has no public API, and Real
Vault, Inc's Terms of Use prohibit automated access and systematic data
retrieval without their written permission. `providers/real.py` carries the
full survey; [`docs/data-sources.md`](docs/data-sources.md) has the reasoning
and lists alternatives whose terms do permit programmatic use. A drafted
permission request is in
[`docs/real-data-access-request.md`](docs/real-data-access-request.md).

Requesting a blocked provider returns HTTP 503 with the reason, and the UI
shows it rather than a blank panel.

A provider swap is more than an adapter: the fitted constants in
`calibration` were fitted on the current provider's numbers, and the
prediction record would span two different models. The costs are enumerated at
the end of `docs/data-sources.md`.

## Dependencies

Pinned under the 7-day freshness rule (cutoff 2026-11-05 for a 2026-11-12
build). Three substitutions, all because the latest release was days old:

| Package | Latest | Age | Pinned |
|---|---|---|---|
| `vite` | 8.3.8 | 1 day | **8.3.5** (Oct 21) |
| `@vitejs/plugin-react` | 6.3.0 | 3 days | **6.2.1** (Nov 2) |
| `fastapi` | 0.162.5 | 1 day | **0.162.0** (Oct 23) |

`react`, `react-dom`, `typescript` and the `@types` packages were already
clear of the cutoff and are pinned at latest.

Charts are hand-rolled inline SVG rather than a charting library — one fewer
dependency on a project where dependency freshness is a stated concern.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q      # 375
cd web && npm run typecheck
```

375 = v1's 228, unchanged, plus 17 on the provider seam, 22 on the two
failures described below, and 108 on the agent layer: retention's boundary,
the request limiter, the ingest tools against a fake session, the change gate
in both directions, the SDK type boundary, the injuries parse, and the
read-only-against-Kalshi assertion.

## The agent layer

Three Claude Agent SDK loops over this project's own MCP tools, run hourly by
Windows Task Scheduler from 07:00 to 01:00 Central. Nothing waits on a human
opening a page.

```bash
.venv/Scripts/python.exe scripts/run_agents.py                   # the scheduled cycle
.venv/Scripts/python.exe scripts/run_agents.py --dry-run         # gate + routing, no model
.venv/Scripts/python.exe scripts/run_agents.py --agent ingest --league nba
powershell -ExecutionPolicy Bypass -File scripts/register_task.ps1   # install the schedule
```

**The split that makes it trustworthy.** `src/thugsak/agents/ingest_tools.py`
is deterministic Python over the existing clients, unit-tested with a fake
session, and produces the same rows every run. The LLM loops in `runner.py`
decide what to call, notice anomalies, judge ambiguous names, and write the
report. Numbers the model trusts never depend on an LLM's mood.

**The gate is a router** (`agents/gate.py`). Each cycle computes a change
signature — new finals, injury-status deltas, winner-price moves past 3¢,
newly listed markets, volume/OI moves, integrity findings — and diffs it
against the last run's. Nothing moved: a `no-op` row, no model call. Something
moved: the diff names *which* agents think, so a price move does not trigger a
re-ingest. Verified on a live slate: two runs minutes apart produced identical
digests and a `no-op`.

**Retention has a boundary.** The rolling last-7 applies to per-player and
box-score *detail* (`player_games`, `depth_chart`, `team_game_stats.detail`).
Team-level results keep the full season: the fitted blend weights put 0.80 on
the season window and the ratings solve reads every result, so trimming those
would silently change the model. `store.trim_player_detail` enforces this and
never touches `games`.

**Injury snapshots are the quiet prize.** ESPN's report is current-only, which
is why backtests have run without the injury adjustment. Every change to the
league report (and a daily heartbeat) is now a full snapshot in
`injury_snapshots`, never pruned. It does not pay off this season; it is the
one dataset that cannot be reconstructed later.

**What the first unattended run found.** On an empty Friday NFL slate the
agents did the right thing — did not widen scope, did not manufacture a bet —
and surfaced two real bugs: the SDK tool boundary hands integers to handlers
as strings (`thin_games` arrived as `"4"`; `sdk_server.coerce_args` now casts
by schema), and the league-wide injuries endpoint carries no `athlete.id` at
all, so v1's parser had been producing empty ids — 800 injuries, 0 rows
stored. The id lives only in the player-card URL; `_athlete_id` recovers it.
The ingest agent also noted it had *considered* editing the source to fix the
first bug. It could not — write builtins are disallowed — but that it reached
for them is why they are.

**Authority.** Full autonomy short of betting: agents may repair data, seed
aliases, run retention. Never an order. `tests/test_kalshi_readonly.py`
asserts no code path reaches a write endpoint and no API-key plumbing exists.

## Name matching is where this breaks, twice over

The v1 bug worth not repeating was `import_nfl` silently dropping teams it
could not resolve, which left two NFL clubs with one stored game each and the
model declining to predict their games with nothing on screen explaining why.
Both halves of that recurred in NCAAMB, and the tests now pin both.

**The data half.** `import_ncaamb` skips a Barttorvik row when either team
fails to resolve — correct, since a game credited to the wrong team is worse
than a missing one, but it means one unresolvable name costs a school its
*entire* history. Twelve Division I spellings never resolved ("McNeese St."
against "McNeese Cowboys", "Tennessee Martin" against "UT Martin Skyhawks",
"Hawaii" against "Hawai'i"), and eight teams ended up with no stored games at
all. `NAME_OVERRIDES` in `matching.py` now declares them, and
`scripts/backfill_ncaamb.py` recovers the rows — 958 games, after which every
D1 team has history. One name is still unmapped on purpose: Barttorvik's
"Saint Francis" has no team record to point at, and the nearest match is a
different school.

**The reporting half.** The engine signals "I will not call this game" by
*returning* `{"error", "warnings"}` rather than raising, because its original
caller was Claude reading prose. Over HTTP that was a 200 carrying none of the
fields the detail view reads, so the page rendered empty with nothing saying
why. It is now a 422 with the engine's own reasons attached, and the UI shows
them as an explanation rather than a crash. A model that refuses on thin
history is working correctly; refusing *silently* is the defect.
