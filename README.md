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
.venv/Scripts/python.exe -m pytest -q      # 240
cd web && npm run typecheck
```

240 = v1's 228, unchanged, plus 12 on the provider seam. The seam's tests
assert that a blocked provider raises with a stated reason rather than
returning empty — the v1 bug worth not repeating was `import_nfl` silently
dropping teams it could not resolve, which left two NFL clubs with one stored
game each and the model declining to predict their games with nothing on
screen explaining why.
