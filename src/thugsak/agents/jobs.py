"""The hourly jobs, with no LLM: everything the app actually reads.

The three Claude agents produced two things: data (scoreboards, injuries,
box scores, player logs, depth charts, the prediction record) and prose
reports. The data came entirely from deterministic tools the agents called
in a fixed order -- the ingest prompt even lists the order -- and the prose
was written to `agent_runs`, which no page displays. Running those tools
directly gives the app the same data for zero tokens, and runs anywhere
(GitHub Actions) without Claude credentials.

Per league, each run:
  1. scoreboards for today and yesterday -> new/changed games, settle bets
  2. injury snapshot (the dataset that cannot be reconstructed later)
  3. box scores for finished games in the slate scope not yet stored
  4. Kalshi prices -> the saved slate snapshot pages open on
  5. the prediction record for games starting within RECORD_WINDOW_HOURS
  6. sportsbook lines for today's and tomorrow's upcoming games: fetched
     once when missing, and refreshed (overwriting) inside the record window
     so the stored line is close to the closing line
Once a day (first run after DAILY_EVERY_SECONDS): player logs and depth
charts for the slate's teams, retention, the integrity audit, cache trim.
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import store
from ..config import DB_PATH, LEAGUES, league as get_league
from ..localtime import today_local
from . import gate, ingest_tools

# A game's recorded prediction (and the bets priced against it) is refreshed
# every hour from this long before kickoff, so the record holds the last
# read before the game -- what someone opening the page would have seen.
RECORD_WINDOW_HOURS = 3
DAILY_EVERY_SECONDS = 20 * 3600
JOB_NAME = "jobs"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _daily_due(conn) -> bool:
    row = conn.execute(
        "SELECT started_at FROM agent_runs WHERE agent=? AND status='ok' "
        "AND summary LIKE ? ORDER BY started_at DESC LIMIT 1",
        (JOB_NAME, '%"daily_ran": true%'),
    ).fetchone()
    last = _parse(row["started_at"]) if row else None
    return last is None or (datetime.now(timezone.utc) - last).total_seconds() > DAILY_EVERY_SECONDS


def games_needing_box_scores(conn, league: str, game_ids: list[str]) -> list[str]:
    """Finished games in scope whose team box score is not stored yet."""
    if not game_ids:
        return []
    marks = ",".join("?" * len(game_ids))
    rows = conn.execute(
        f"""SELECT DISTINCT g.game_id FROM games g
            JOIN team_game_stats t ON t.game_id = g.game_id
            WHERE g.completed = 1 AND t.detail IS NULL AND g.game_id IN ({marks})""",
        game_ids,
    ).fetchall()
    return [r["game_id"] for r in rows]


def games_needing_lines(conn, league: str, dates: list[str], *,
                        now: datetime | None = None) -> list[tuple[str, bool]]:
    """(game_id, overwrite) for upcoming games: those with no line yet, and --
    overwriting -- those kicking off within RECORD_WINDOW_HOURS."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=RECORD_WINDOW_HOURS)
    out: list[tuple[str, bool]] = []
    for d in dates:
        for r in store.games_on_date(conn, league, d):
            start = _parse(r["start_time"])
            if r["completed"] or start is None or start < now:
                continue
            soon = start <= horizon
            missing = r["close_spread"] is None or r["close_total"] is None
            if missing or soon:
                out.append((r["game_id"], soon))
    return out


async def save_sharpapi_lines(league: str, dates: list[str], *, db: str) -> dict[str, Any]:
    """Store SharpAPI's consensus line for upcoming games (when a key is set).

    Overwrites the stored line every run until kickoff, so what remains is
    the last pre-game consensus. Returns counts only -- the Actions logs are
    public, and the data is not ours to republish.
    """
    from .. import cache as cache_mod, matching  # noqa: PLC0415
    from ..clients import sharpapi  # noqa: PLC0415

    if not sharpapi.available():
        return {"skipped": "no SHARPAPI_KEY"}
    if league not in sharpapi.LEAGUES:
        return {"skipped": "league not covered"}
    now = datetime.now(timezone.utc)
    got = matched = written = 0
    unmatched: list[str] = []
    books: set[str] = set()
    async with cache_mod.make_client() as client:
        with store.connect(db) as conn:
            index: dict[frozenset[str], Any] = {}
            for d in dates:
                for g in store.games_on_date(conn, league, d):
                    index[frozenset((g["home_team"], g["away_team"]))] = g
            for d in dates:
                found, _meta = await sharpapi.lines(client, league, d)
                got += len(found)
                for ln in found:
                    h = matching.resolve_team(conn, league, ln["home_team"])
                    a = matching.resolve_team(conn, league, ln["away_team"])
                    g = index.get(frozenset((h, a))) if h and a else None
                    if g is None:
                        unmatched.append(f"{ln['away_team']} @ {ln['home_team']}")
                        continue
                    matched += 1
                    start = _parse(g["start_time"])
                    if g["completed"] or (start is not None and start < now):
                        continue
                    # Their home team should be ours; if the pair matched the
                    # other way round, flip the spread to our home side.
                    spread = ln["spread"]
                    if spread is not None and h != g["home_team"]:
                        spread = -spread
                    store.update_market_line(conn, g["game_id"], spread=spread, total=ln["total"],
                                             overwrite=True)
                    written += 1
                    books.update(ln["books"])
            conn.commit()
    return {"source": "sharpapi", "lines": got, "matched": matched, "written": written,
            "unmatched": len(unmatched), "unmatched_sample": unmatched[:3], "books": sorted(books)}


async def save_lines(league: str, dates: list[str], *, db: str) -> dict[str, Any]:
    """Store ESPN's per-game sportsbook line (pickcenter) for upcoming games.

    The scoreboard carries odds for featured games only (NBA: 49 of 3,670
    stored games had a line), which left the model's market input and the
    sportsbook yardstick almost empty outside the NFL.
    """
    from .. import cache as cache_mod  # noqa: PLC0415
    from ..mcp import tools as t  # noqa: PLC0415

    with store.connect(db, readonly=True) as conn:
        todo = games_needing_lines(conn, league, dates)
    saved, none, failed = 0, 0, 0
    if not todo:
        return {"checked": 0, "saved": 0}
    async with cache_mod.make_client() as client:
        async with t._provider().session(client) as ec:
            for gid, overwrite in todo:
                espn_id = gid.split(":")[-1]
                if not espn_id.isdigit():
                    continue
                try:
                    lines = ec.parse_pickcenter(await ec.summary(league, espn_id))
                except Exception:
                    failed += 1
                    continue
                best = next((ln for ln in lines if ln.home_ml is not None), None) or (
                    lines[0] if lines else None)
                if best is None or (best.spread is None and best.total is None):
                    none += 1
                    continue
                with store.connect(db) as conn:
                    store.update_market_line(
                        conn, gid, spread=best.spread, total=best.total,
                        home_ml=best.home_ml, away_ml=best.away_ml, overwrite=overwrite,
                    )
                    conn.commit()
                saved += 1
    return {"checked": len(todo), "saved": saved, "no_line_published": none, "failed": failed}


def games_to_record(conn, league: str, date: str, *, now: datetime | None = None) -> list[str]:
    """Upcoming games on the slate kicking off within the record window."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=RECORD_WINDOW_HOURS)
    out = []
    for r in store.games_on_date(conn, league, date):
        start = _parse(r["start_time"])
        if not r["completed"] and start is not None and now <= start <= horizon:
            out.append(r["game_id"])
    return out


async def _league(league: str, date: str, *, daily: bool, db: str) -> dict[str, Any]:
    from ..mcp import tools as t  # noqa: PLC0415

    out: dict[str, Any] = {}

    def step(name: str):
        class _Step:
            def __enter__(self):
                self.t = time.perf_counter()

            def __exit__(self, et, ev, tb):
                secs = round(time.perf_counter() - self.t, 1)
                if ev is not None:
                    out[name] = {"error": f"{type(ev).__name__}: {ev}", "secs": secs}
                    return True   # one failed step must not stop the others
                out.setdefault(name, {})
                if isinstance(out[name], dict):
                    out[name]["secs"] = secs
                return False
        return _Step()

    with step("scores"):
        out["scores"] = {"dates": await gate.refresh_scores(league, date, db=db)}

    with step("injuries"):
        inj = await ingest_tools.ingest_injuries(league, db=db)
        out["injuries"] = {
            "snapshot_rows": inj.get("snapshot_rows"),
            "changed": len(inj.get("changed") or []),
            "material": bool(inj.get("material")),
        }

    with store.connect(db, readonly=True) as conn:
        scope = ingest_tools.slate_scope(conn, league, date)
        # Yesterday's finals too: a late game ends after midnight Central.
        y = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        ids = scope["games"] + [r["game_id"] for r in store.games_on_date(conn, league, y)]
        need_box = games_needing_box_scores(conn, league, ids)

    with step("box_scores"):
        res = await ingest_tools.ingest_box_scores(league, need_box, db=db) if need_box else {"written": {}}
        out["box_scores"] = {"games": len(need_box), "written": res.get("written"), "failed": len(res.get("failed") or [])}

    with step("prices"):
        m = await gate.fetch_market_state(league, date, db=db)
        out["prices"] = {"games_with_markets": len(m["markets"])}

    # Lines before the record step, so recorded predictions see them.
    with step("lines"):
        tomorrow = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            out["lines_sharpapi"] = await save_sharpapi_lines(league, [date, tomorrow], db=db)
        except Exception as e:  # never let the odds feed stop the rest
            out["lines_sharpapi"] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
        # ESPN fills whatever SharpAPI did not (games it lacks, other leagues).
        out["lines"] = await save_lines(league, [date, tomorrow], db=db)

    with step("record"):
        with store.connect(db, readonly=True) as conn:
            todo = games_to_record(conn, league, date)
        recorded, failed = 0, []
        for gid in todo:
            try:
                r = await t.get_recommendation(league, gid, record=True, db=db)
                if isinstance(r, dict) and r.get("error"):
                    failed.append(gid)
                else:
                    recorded += 1
            except Exception:
                failed.append(gid)
        out["record"] = {"recorded": recorded, "failed": failed}

    if daily and league == "ncaamb":
        # Possession counts for the efficiency model, before the next slate.
        with step("possessions"):
            out["possessions"] = await ingest_tools.ingest_possessions(league, db=db)

    if daily:
        with step("player_logs"):
            pl = await ingest_tools.ingest_player_logs(league, scope["on_slate"], db=db) if scope["on_slate"] else {}
            out["player_logs"] = {"written": pl.get("written"), "failed": len(pl.get("failed") or [])}
        if get_league(league).has_depth_chart:
            with step("depth_charts"):
                dc = await ingest_tools.ingest_depth_charts(league, scope["on_slate"], db=db) if scope["on_slate"] else {}
                out["depth_charts"] = {"written": dc.get("written"), "failed": len(dc.get("failed") or [])}
    return out


async def run_jobs(
    leagues: list[str] | None = None,
    date: str | None = None,
    *,
    force_daily: bool = False,
    db: str = str(DB_PATH),
) -> dict[str, Any]:
    """One hourly run over every league. Returns and logs what it did."""
    leagues = leagues or list(LEAGUES)
    for lg in leagues:
        get_league(lg)
    date = date or today_local().strftime("%Y-%m-%d")
    store.init_db(db)
    with store.connect(db) as conn:
        closed = store.abandon_stale_runs(conn)
        daily = force_daily or _daily_due(conn)
        rid = store.start_agent_run(conn, JOB_NAME, ",".join(leagues))
        conn.commit()

    summary: dict[str, Any] = {"date": date, "daily_ran": False, "leagues": {}}
    if closed:
        summary["abandoned_runs_closed"] = closed
    status, error = "ok", None
    try:
        for lg in leagues:
            summary["leagues"][lg] = await _league(lg, date, daily=daily, db=db)
        if daily:
            summary["daily"] = {
                lg: {
                    "retention": (await ingest_tools.run_retention(lg, trim_cache=(lg == leagues[-1]), db=db)).get("removed"),
                    "audit_findings": len((await ingest_tools.audit_data_integrity(lg, db=db)).get("findings") or []),
                }
                for lg in leagues
            }
            summary["daily_ran"] = True
        errors = [
            f"{lg}.{step}" for lg, steps in summary["leagues"].items()
            for step, v in steps.items() if isinstance(v, dict) and "error" in v
        ]
        if errors:
            status, error = "partial", "steps failed: " + ", ".join(errors)
    except Exception as e:
        status, error = "failed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
    finally:
        with store.connect(db) as conn:
            store.finish_agent_run(conn, rid, status, summary=summary, error=error)
            conn.commit()
    return {"status": status, "run_id": rid, "error": error, **summary}
