"""HTTP bridge: the v1 engine, unchanged, behind JSON endpoints for the React UI.

The Streamlit app called `mcp.tools` functions directly in-process. React
cannot, so this module exposes the same callables over HTTP. It is deliberately
thin -- no analysis, no formatting, no business logic. Every number the browser
renders was computed by the same code path that v1 and the MCP server use, so
the web UI and Claude can never disagree about what a game is worth.

Three things it does add, all of them about being a server rather than a script:

1. **Error translation.** A `KeyError` from an unknown league is a 404, not a
   500; a `ProviderUnavailable` is a 503 carrying the reason, so the UI can
   display why a data source is off rather than a blank panel.
2. **A single-writer guard.** SQLite gives "database is locked" when two
   processes write at once, and `list_games` writes (it settles predictions).
   Requests are serialised through one lock, which is correct for a local
   single-user app and cheap at this request volume.
3. **CORS for the Vite dev server**, which runs on a different port.

Read-only with respect to Kalshi, exactly as v1: no key, no order endpoints.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from thugsak import config, db, localtime, providers  # noqa: E402
from thugsak.mcp import tools  # noqa: E402

app = FastAPI(
    title="thugsak",
    description="Kalshi sports edge finder -- read-only",
    version="2.0.0-react",
)

# The Vite dev server is a different origin. In production the built assets are
# served by this same app (see the static mount at the bottom), so these only
# matter while developing.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# One lock across all requests keeps SQLite to a single writer (the hourly
# agents write the same file); see the module docstring. Postgres handles
# concurrent readers itself, and pages no longer write, so there the lock is
# skipped -- otherwise a slow Kalshi price fetch would queue every game view
# behind it.
_DB_LOCK = asyncio.Lock()


def _db_guard() -> Any:
    return _DB_LOCK if db.backend() == "sqlite" else contextlib.nullcontext()


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run an engine call, translating its failures into HTTP ones."""
    try:
        async with _db_guard():
            result = await fn(*args, **kwargs)
        # The engine reports a bad reference by returning {"error": ...} with
        # no exception, because its original caller was Claude reading prose.
        # Over HTTP that arrives as a 200 and the UI renders an empty page, so
        # it is promoted to a real status the front-end already handles.
        if isinstance(result, dict) and "error" in result:
            # Two different failures wear the same shape, and they are not the
            # same thing to a reader:
            #
            #   {"error": ...}                      -- no such game
            #   {"game_id", "error", "warnings"}    -- the game is real, the
            #                                          model declined to call it
            #
            # An earlier version of this check required len(result) == 1, so
            # the second form sailed through as a 200 carrying none of the
            # fields the detail view reads. The browser rendered an empty
            # page with nothing saying why -- the same silent-emptiness
            # failure the provider seam exists to prevent, reintroduced one
            # layer up. Any dict carrying "error" is now promoted, and the
            # engine's own warnings travel with it because they are the only
            # explanation the user gets.
            reasons = result.get("warnings") or []
            detail = str(result["error"])
            if reasons:
                detail += " -- " + " ".join(str(w) for w in reasons)
            resolved = any(k in result for k in ("game_id", "warnings"))
            raise HTTPException(status_code=422 if resolved else 404, detail=detail)
        return result
    except HTTPException:
        raise
    except providers.ProviderUnavailable as exc:
        # 503: the provider exists but may not be used. The message says what
        # would have to change, so it is safe and useful to show the user.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        # The hourly agent cycle writes to the same SQLite file. A lock timeout
        # is "busy, try again", not a crash, and the UI shows a 503 as such.
        if "locked" in str(exc).lower():
            raise HTTPException(
                status_code=503,
                detail="The database is busy -- the hourly agents are writing. "
                       "Retry in a few seconds.",
            ) from exc
        raise


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Liveness plus which data provider is actually in force.

    Reports each provider's own declaration rather than probing the network,
    so a health check stays instant and never hits a third party.
    """
    return {
        "healthy": True,
        "provider": config.DATA_PROVIDER,
        "providers": [
            {
                "name": name,
                "leagues": sorted(p.leagues),
                "blocked_reason": _blocked_reason(p),
            }
            for name, p in sorted(providers.available().items())
        ],
        "today": localtime.today_local().isoformat(),
        "timezone": localtime.tz_abbrev(),
    }


def _blocked_reason(provider: Any) -> str | None:
    """The provider's own explanation of why it cannot be used, or None.

    Asks the provider by calling nothing -- a blocked provider raises
    `ProviderUnavailable` from its methods, and the message is the reason, so
    it is read from the one attribute rather than by triggering a failure.
    """
    from thugsak.providers import real as real_mod

    if getattr(provider, "source_name", None) == "real" and not real_mod.PERMISSION_ON_FILE:
        return real_mod._BLOCKED
    return None


@app.get("/api/leagues")
async def leagues() -> dict[str, Any]:
    return {
        "leagues": [
            {
                "key": key,
                "label": lg.name,
            }
            for key, lg in config.LEAGUES.items()
        ],
        "timezone": localtime.tz_abbrev(),
        "today": localtime.today_local().isoformat(),
    }


@app.get("/api/games")
async def games(
    league: str = Query(..., description="nfl | nba | ncaaf | ncaamb"),
    date: str | None = Query(None, description="YYYY-MM-DD, Central Time; default today"),
) -> Any:
    """Today's (or a past date's) slate. Past dates drive the lookback.

    Database only: prices are the agents' last saved ones (`prices_as_of`),
    so the page renders without waiting on Kalshi. `/api/prices` has live.
    """
    return await _call(tools.list_games, league, date, prices="snapshot")


@app.get("/api/prices")
async def prices(
    league: str = Query(...),
    date: str | None = Query(None),
) -> Any:
    """Live Kalshi prices for a slate, keyed by game_id (shared for 60s)."""
    return await _call(tools.slate_prices, league, date)


@app.get("/api/recommendation")
async def recommendation(
    league: str = Query(...),
    game_id: str = Query(...),
    contracts: int = Query(100, ge=1, le=10_000),
) -> Any:
    """The answer: predicted score, one definitive call, and the five bets."""
    # Pages compute; they do not write. The agents keep the prediction record.
    return await _call(
        tools.get_recommendation, league, game_id, contracts=contracts, record=False
    )


@app.get("/api/edges")
async def edges(
    league: str = Query(...),
    game_id: str = Query(...),
    contracts: int = Query(100, ge=1, le=10_000),
) -> Any:
    """Every Kalshi ladder strike for a game, scored against the model.

    The one engine tool the bridge did not forward until now. v1's plan called
    the ladder "the core of the app" and the engine calls it "the actionable
    table"; it was reachable from Claude and never from the browser. Sixty rows
    for one game is data, not an answer -- the detail view renders it below
    the five-slot verdict, not above it.
    """
    return await _call(tools.get_edges, league, game_id, contracts=contracts)


@app.get("/api/prediction")
async def prediction(
    league: str = Query(...),
    game_id: str = Query(...),
    include_injuries: bool = Query(True),
) -> Any:
    """Every model input behind the prediction, for the 'show your work' panel."""
    return await _call(
        tools.get_prediction, league, game_id,
        include_injuries=include_injuries, record=False,
    )


@app.get("/api/track-record")
async def track_record(
    league: str | None = Query(None),
    since: str | None = Query(None),
) -> Any:
    return await _call(tools.get_track_record, league, since=since)


@app.get("/api/team-stats")
async def team_stats(league: str = Query(...), team: str = Query(...)) -> Any:
    return await _call(tools.get_team_stats, league, team)


@app.get("/api/money-flow")
async def money_flow(
    ticker: str = Query(...),
    lookback_hours: int = Query(48, ge=1, le=720),
) -> Any:
    return await _call(tools.get_money_flow, ticker, lookback_hours=lookback_hours)


@app.get("/api/price-history")
async def price_history(
    ticker: str = Query(...),
    hours: int = Query(72, ge=1, le=2160),
    interval: int = Query(60),
) -> Any:
    if interval not in config.CANDLE_INTERVALS:
        # Kalshi returns an empty list rather than an error for a bad interval,
        # which reads as "no history" instead of "you asked wrongly".
        raise HTTPException(
            status_code=400,
            detail=f"interval must be one of {list(config.CANDLE_INTERVALS)}",
        )
    return await _call(tools.get_price_history, ticker, hours=hours, interval=interval)


# Serve the built front-end if it has been built, so one process runs the whole
# app in production. Absent a build, the API still works on its own and Vite
# serves the UI in dev.
_DIST = ROOT / "web" / "dist"
if _DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="web")
