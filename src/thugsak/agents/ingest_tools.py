"""Agent 1's tool belt: fetch and store player-level data, deterministically.

Every function here is plain async Python over the existing clients. The LLM
loop in `runner.py` decides which to call and reads what they report; it never
touches HTTP or SQL itself. That split is what makes the stored numbers
reproducible, and what makes these testable with a fake session and no
network.

Scope rule, from the plan: teams on the slate plus the opponents from their
last 7 games. Bounded at a few hundred requests per run with the limiter in
`cache.py`, and it covers every game you could bet.

Each function returns JSON-safe dicts with counts of what it wrote, because
the agent-run log and the change gate both read those counts.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from datetime import datetime, timezone

from .. import cache, config, matching, providers, store
from ..clients import espn as espn_mod
from ..config import DB_PATH, LEAGUES, league as get_league
from ..localtime import parse_ts as _parse_utc, today_local
from ..model import availability as avail_mod

# A full injury snapshot is written whenever the report changed, and at least
# this often regardless, so the history has a daily heartbeat even on a quiet
# week.
INJURY_HEARTBEAT_SECONDS = 24 * 3600


def _session(client: Any) -> Any:
    return providers.get(config.DATA_PROVIDER).session(client)


def _espn_team_id(conn, team_id: str) -> str:
    row = conn.execute(
        "SELECT espn_id FROM teams WHERE team_id=?", (team_id,)
    ).fetchone()
    return str(row["espn_id"]) if row and row["espn_id"] else ""


def _today() -> str:
    return today_local().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------

def slate_scope(conn, league: str, date: str, *, keep: int = 7) -> dict[str, Any]:
    """Teams playing on `date` plus each one's last-`keep` opponents.

    The opponents matter because the with/without split and the opponent
    adjustment both need the other side's recent form; a team's own log is
    only half of each comparison.
    """
    rows = store.games_on_date(conn, league, date)
    on_slate: set[str] = set()
    for r in rows:
        on_slate.add(r["home_team"])
        on_slate.add(r["away_team"])
    opponents: set[str] = set()
    for t in on_slate:
        for g in store.team_games_before(
            conn, t, f"{date}T23:59:59+00:00", limit=keep
        ):
            opponents.add(g["opponent_id"])
    opponents -= on_slate
    return {
        "date": date,
        "games": [r["game_id"] for r in rows],
        "on_slate": sorted(on_slate),
        "opponents": sorted(opponents),
        "teams": sorted(on_slate | opponents),
    }


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

async def ingest_slate(
    league: str, date: str | None = None, *, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """Refresh the slate for a date and report what is on it.

    Delegates to `list_games`, which already does the scoreboard -> `games`
    upsert, settles finished predictions and matches Kalshi events -- the one
    place that logic lives. Imported lazily to avoid a cycle with the registry.
    """
    from ..mcp import tools as t  # noqa: PLC0415

    date = date or _today()
    out = await t.list_games(league, date, refresh=True, db=db)
    games = out.get("games", [])
    with store.connect(db, readonly=True) as conn:
        scope = slate_scope(conn, league, date)
    return {
        "league": league,
        "date": date,
        "games": len(games),
        "completed": sum(1 for g in games if g.get("completed")),
        "with_markets": sum(1 for g in games if g.get("kalshi_markets")),
        "unmatched_kalshi_events": out.get("unmatched_count", 0),
        "scope": scope,
        "note": out.get("note", ""),
    }


async def ingest_box_scores(
    league: str, game_ids: list[str], *, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """Store the box score for completed games: team `detail` plus one
    `player_games` row per athlete who appeared.

    Only completed games carry a box score; others are reported as skipped
    rather than fetched, since the summary endpoint for a future game has no
    `boxscore.players`.
    """
    get_league(league)
    store.init_db(db)
    written = {"games": 0, "players": 0, "player_games": 0}
    skipped: list[str] = []
    failed: list[str] = []

    async with cache.make_client() as client:
        with store.connect(db) as conn:
            async with _session(client) as ec:
                for gid in game_ids:
                    game = store.get_game(conn, gid)
                    if game is None or not game["completed"]:
                        skipped.append(gid)
                        continue
                    espn_id = gid.split(":")[-1]
                    try:
                        summary = await ec.summary(league, espn_id)
                    except Exception:
                        failed.append(gid)
                        continue
                    box = avail_mod.parse_boxscore(summary)
                    if not box:
                        skipped.append(gid)
                        continue
                    detail: dict[str, Any] = {}
                    for abbrev, players in box.items():
                        team_id = matching.resolve_team(conn, league, abbrev)
                        if not team_id:
                            continue
                        side = (
                            "home" if team_id == game["home_team"]
                            else "away" if team_id == game["away_team"]
                            else None
                        )
                        if side is None:
                            continue
                        opp = game["away_team"] if side == "home" else game["home_team"]
                        detail[side] = {
                            "players": len(players),
                            "appeared": sum(1 for p in players.values() if not p["dnp"]),
                        }
                        for aid, p in players.items():
                            store.upsert_player(
                                conn, league, aid, team_id,
                                name=p["name"], position=p["position"],
                            )
                            written["players"] += 1
                            written["player_games"] += store.upsert_player_games(
                                conn, league, aid, team_id,
                                [{
                                    "event_id": espn_id,
                                    "date": game["start_time"][:10],
                                    "opponent": opp,
                                    "home_away": "vs" if side == "home" else "@",
                                    "result": None,
                                    "stats": {"usage": p["usage"], "dnp": p["dnp"]},
                                }],
                                source="espn:boxscore",
                            )
                    if detail:
                        # Merge onto the existing row. The upsert INSERTs every
                        # column and SQLite checks NOT NULL on that attempt
                        # before ON CONFLICT fires, so the NOT NULL fields must
                        # be carried from the row we already hold rather than
                        # left for COALESCE -- a partial dict here raised
                        # IntegrityError on `completed`. Scores still merge
                        # via COALESCE and are not blanked.
                        store.upsert_game(conn, {
                            "game_id": gid, "league": league,
                            "season": game["season"],
                            "season_type": game["season_type"],
                            "start_time": game["start_time"],
                            "home_team": game["home_team"],
                            "away_team": game["away_team"],
                            "completed": game["completed"],
                            "neutral_site": game["neutral_site"],
                            "source": game["source"],
                            "detail": detail,
                        })
                        written["games"] += 1
                    # Commit per game: the next summary fetch is a network
                    # call, and holding the write lock across it blocks the
                    # API (and the next cycle) for the whole batch.
                    conn.commit()
                conn.commit()
    return {"league": league, "written": written, "skipped": skipped, "failed": failed}


async def ingest_player_logs(
    league: str,
    team_ids: list[str],
    *,
    max_players_per_team: int = 20,
    db: str = str(DB_PATH),
) -> dict[str, Any]:
    """Roster each team and store every rostered player's game log.

    Bounded per team: a basketball roster is ~15, a football roster 53, and
    the long tail of a football roster never touches the ball. The cap is a
    parameter so the agent can widen it for a specific team it is unsure
    about. Requests run concurrently under the limiter.
    """
    lg = get_league(league)
    store.init_db(db)
    written = {"teams": 0, "players": 0, "player_games": 0}
    failed: list[str] = []
    no_espn_id: list[str] = []

    async with cache.make_client() as client:
        with store.connect(db) as conn:
            async with _session(client) as ec:
                for team_id in team_ids:
                    espn_id = _espn_team_id(conn, team_id)
                    if not espn_id:
                        no_espn_id.append(team_id)
                        continue
                    try:
                        rr = await ec._get(
                            f"{espn_mod.ESPN_SITE}/{lg.espn_path}/teams/{espn_id}/roster",
                            ttl=43_200,
                        )
                    except Exception:
                        failed.append(team_id)
                        continue
                    groups = rr.get("athletes") or []
                    flat = groups if (groups and "id" in groups[0]) else [
                        a for g in groups for a in (g.get("items") or [])
                    ]
                    roster = [
                        (str(a.get("id") or ""), a.get("displayName") or "",
                         ((a.get("position") or {}) or {}).get("abbreviation") or "")
                        for a in flat if a.get("id")
                    ][:max_players_per_team]

                    async def one(aid: str, name: str, pos: str) -> tuple[str, str, str, list[Any]]:
                        try:
                            return aid, name, pos, await ec.player_gamelog(league, aid)
                        except Exception:
                            return aid, name, pos, []

                    results = await asyncio.gather(*(one(*p) for p in roster))
                    for aid, name, pos, pgames in results:
                        store.upsert_player(conn, league, aid, team_id, name=name, position=pos)
                        written["players"] += 1
                        written["player_games"] += store.upsert_player_games(
                            conn, league, aid, team_id, pgames, source="espn:gamelog"
                        )
                    written["teams"] += 1
                    conn.commit()
    return {
        "league": league, "written": written,
        "failed": failed, "no_espn_id": no_espn_id,
    }


async def ingest_injuries(league: str, *, db: str = str(DB_PATH)) -> dict[str, Any]:
    """Snapshot the league injury report and report what changed since last.

    The delta is the change gate's input: a status flip (Questionable -> Out)
    is the kind of thing worth re-predicting for.
    """
    get_league(league)
    store.init_db(db)
    async with cache.make_client() as client:
        with store.connect(db) as conn:
            previous = store.latest_injury_status(conn, league)
            prev_at = store.latest_injury_snapshot_time(conn, league)
            async with _session(client) as ec:
                by_abbrev = await ec.injuries(league)
            rows: list[tuple[str, Any]] = []
            unresolved: list[str] = []
            for abbrev, items in by_abbrev.items():
                team_id = matching.resolve_team(conn, league, abbrev)
                if not team_id:
                    unresolved.append(abbrev)
                    continue
                for inj in items:
                    if inj.athlete_id and inj.status:
                        rows.append((team_id, inj))

            # The current picture, from the report itself rather than read
            # back from the table, so the diff does not depend on whether this
            # pass wrote anything.
            current = {inj.athlete_id: inj.status.strip() for _, inj in rows}
            changed = {
                aid: {"from": previous.get(aid), "to": st}
                for aid, st in current.items()
                if previous.get(aid) != st
            }
            cleared = sorted(set(previous) - set(current))
            material = bool(changed or cleared)

            # Write a full snapshot only when something changed, or once a day
            # as a heartbeat. The NFL report is ~800 rows and most of them are
            # `Active`; appending all of it every hour would be ~15k rows a
            # day for one league, and the table is meant to be the one that is
            # never pruned. Change-gated, its growth tracks real news, and
            # `latest_injury_status` still reads one complete picture because
            # every write is a full snapshot.
            stale = prev_at is None or (
                (datetime.now(timezone.utc) - _parse_utc(prev_at)).total_seconds()
                > INJURY_HEARTBEAT_SECONDS
            )
            n = store.snapshot_injuries(conn, league, rows) if (material or stale) else 0
            conn.commit()

    return {
        "league": league,
        "report_rows": len(rows),
        "snapshot_rows": n,
        "wrote_snapshot": n > 0,
        "teams_unresolved": unresolved,
        "changed": changed,
        "cleared": cleared,
        "material": material,
    }


async def ingest_depth_charts(
    league: str, team_ids: list[str], *, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """Snapshot depth charts where the league publishes one.

    College leagues do not (`has_depth_chart=False`), so for those this
    reports `unsupported` rather than fetching a 400.
    """
    lg = get_league(league)
    if not lg.has_depth_chart:
        return {"league": league, "unsupported": True, "written": 0}
    store.init_db(db)
    written = 0
    failed: list[str] = []
    stamp = store.utcnow()
    async with cache.make_client() as client:
        with store.connect(db) as conn:
            async with _session(client) as ec:
                for team_id in team_ids:
                    espn_id = _espn_team_id(conn, team_id)
                    if not espn_id:
                        failed.append(team_id)
                        continue
                    try:
                        chart = await ec.depth_chart(league, espn_id)
                    except Exception:
                        failed.append(team_id)
                        continue
                    written += store.replace_depth_chart(
                        conn, league, team_id, chart, as_of=stamp
                    )
            conn.commit()
    return {"league": league, "written": written, "failed": failed, "as_of": stamp}


async def audit_data_integrity(
    league: str, *, thin_games: int = 2, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """The standing report: today's bug class as a checklist.

    Database-only, so it is cheap enough to run every hour and deterministic
    enough to diff between runs. Each finding names the fix.

    `async` although it does no I/O: both MCP adapters `await` every handler,
    so a sync function here would register cleanly and then fail on first call.
    """
    lg = get_league(league)
    store.init_db(db)
    with store.connect(db, readonly=True) as conn:
        season = store.current_season(conn, league)
        findings: list[dict[str, Any]] = []

        rows = conn.execute(
            """SELECT t.team_id, t.display_name,
                      (SELECT COUNT(*) FROM team_game_stats s
                        WHERE s.team_id=t.team_id AND s.points_for IS NOT NULL
                          AND s.season=?) n_season,
                      (SELECT COUNT(*) FROM team_game_stats s
                        WHERE s.team_id=t.team_id AND s.points_for IS NOT NULL) n_all
               FROM teams t
               WHERE t.league=? AND (t.is_d1=1 OR t.is_d1 IS NULL)
               ORDER BY n_all, n_season, t.team_id""",
            (season, league),
        ).fetchall()
        zero = [r for r in rows if r["n_all"] == 0]
        thin = [r for r in rows if 0 < r["n_season"] <= thin_games]
        if zero:
            findings.append({
                "kind": "no_history",
                "severity": "high",
                "teams": [r["team_id"] for r in zero],
                "fix": "Source name probably fails to resolve. Check "
                       "NAME_OVERRIDES / link_team_alias, then backfill.",
            })
        if thin:
            findings.append({
                "kind": "thin_season_history",
                "severity": "info",
                "teams": {r["team_id"]: r["n_season"] for r in thin},
                "fix": "Expected early season; predictions carry a caution.",
            })

        missing_espn = conn.execute(
            "SELECT team_id FROM teams WHERE league=? AND (espn_id IS NULL OR espn_id='') "
            "AND (is_d1=1 OR is_d1 IS NULL)",
            (league,),
        ).fetchall()
        if missing_espn:
            findings.append({
                "kind": "no_espn_id",
                "severity": "medium",
                "teams": [r["team_id"] for r in missing_espn],
                "fix": "Player logs and depth charts cannot be fetched for these.",
            })

        cal = conn.execute(
            "SELECT MAX(fitted_at) f, MAX(n_games) n FROM calibration WHERE league=?",
            (league,),
        ).fetchone()
        newest = conn.execute(
            "SELECT MAX(start_time) t, COUNT(*) n FROM games WHERE league=? AND completed=1",
            (league,),
        ).fetchone()
        if cal and cal["f"] and newest and newest["t"] and newest["t"] > cal["f"]:
            added = conn.execute(
                "SELECT COUNT(*) n FROM games WHERE league=? AND completed=1 AND start_time>?",
                (league, cal["f"]),
            ).fetchone()["n"]
            findings.append({
                "kind": "calibration_stale",
                "severity": "info" if added < 200 else "medium",
                "fitted_at": cal["f"],
                "completed_games_since": added,
                "fix": "Refit when convenient; weekly is the plan's cadence.",
            })

        dup = conn.execute(
            """SELECT alias, COUNT(DISTINCT team_id) n FROM team_aliases
               WHERE league=? GROUP BY alias HAVING COUNT(DISTINCT team_id) > 1""",
            (league,),
        ).fetchall()
        if dup:
            findings.append({
                "kind": "ambiguous_alias",
                "severity": "medium",
                "aliases": [r["alias"] for r in dup][:25],
                "fix": "matching.drop_ambiguous_aliases, then re-link the right one.",
            })

        pg = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT team_id) t FROM player_games WHERE league=?",
            (league,),
        ).fetchone()
        inj = conn.execute(
            "SELECT MAX(as_of) a FROM injury_snapshots WHERE league=?", (league,)
        ).fetchone()

    return {
        "league": league,
        "season": season,
        "d1_teams": len(rows),
        "findings": findings,
        "player_games_rows": pg["n"],
        "teams_with_player_data": pg["t"],
        "last_injury_snapshot": inj["a"],
        "ok": not any(f["severity"] in ("high", "medium") for f in findings),
        "has_depth_chart": lg.has_depth_chart,
    }


async def link_team_alias(
    league: str, name: str, source: str, team_id: str, *, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """Record that `name` from `source` means `team_id`. The agent's one write
    after judging an ambiguous name. Refuses an unknown team outright."""
    get_league(league)
    store.init_db(db)
    with store.connect(db) as conn:
        row = conn.execute(
            "SELECT team_id, display_name FROM teams WHERE team_id=? AND league=?",
            (team_id, league),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"unknown team_id {team_id!r} in {league}"}
        holder = matching.resolve_team(conn, league, name, source=source, fuzzy=False)
        matching.link_alias(conn, league, name, source, team_id)
        conn.commit()
    return {
        "ok": True, "league": league, "alias": name, "source": source,
        "team_id": team_id, "display_name": row["display_name"],
        "previously": holder,
    }


async def run_retention(
    league: str, *, keep: int = 7, trim_cache: bool = True, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """Apply the rolling-7 to detail, trim the HTTP cache, reclaim space.

    Never prunes seasons: that is import-time behaviour tied to a calibration
    fit, not something to do hourly.
    """
    get_league(league)
    store.init_db(db)
    with store.connect(db) as conn:
        removed = store.trim_player_detail(conn, league, keep=keep)
        conn.commit()
    out: dict[str, Any] = {"league": league, "removed": removed}
    if trim_cache:
        out["cache"] = cache.trim()
    out["vacuumed"] = store.vacuum(db)
    out["db_size_bytes"] = store.db_size_bytes(db)
    return out


# --------------------------------------------------------------------------
# registry entries, appended to tools.TOOL_SCHEMAS
# --------------------------------------------------------------------------

_LEAGUE = {"type": "string", "enum": list(LEAGUES)}
_IDS = {"type": "array", "items": {"type": "string"}}

INGEST_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "ingest_slate",
        "description": (
            "Refresh a league's slate for a date (default today, Central) and "
            "return its scope: games, teams playing, and their last-7 opponents. "
            "Call this first; the team list it returns is what the other ingest "
            "tools take."
        ),
        "handler": ingest_slate,
        "schema": {
            "type": "object",
            "properties": {"league": _LEAGUE, "date": {"type": "string"}},
            "required": ["league"],
        },
    },
    {
        "name": "ingest_box_scores",
        "description": (
            "Store box scores for COMPLETED games: team detail plus one row per "
            "athlete who appeared. Future games are skipped, not fetched."
        ),
        "handler": ingest_box_scores,
        "schema": {
            "type": "object",
            "properties": {"league": _LEAGUE, "game_ids": _IDS},
            "required": ["league", "game_ids"],
        },
    },
    {
        "name": "ingest_player_logs",
        "description": (
            "Roster each team and store every rostered player's game log "
            "(capped per team). Use the scope from ingest_slate."
        ),
        "handler": ingest_player_logs,
        "schema": {
            "type": "object",
            "properties": {
                "league": _LEAGUE,
                "team_ids": _IDS,
                "max_players_per_team": {"type": "integer", "default": 20},
            },
            "required": ["league", "team_ids"],
        },
    },
    {
        "name": "ingest_injuries",
        "description": (
            "Snapshot the league injury report and report every status change "
            "since the previous snapshot. 'material' is true when something "
            "moved."
        ),
        "handler": ingest_injuries,
        "schema": {
            "type": "object",
            "properties": {"league": _LEAGUE},
            "required": ["league"],
        },
    },
    {
        "name": "ingest_depth_charts",
        "description": (
            "Snapshot depth charts for teams (NFL and NBA only; college has "
            "none published)."
        ),
        "handler": ingest_depth_charts,
        "schema": {
            "type": "object",
            "properties": {"league": _LEAGUE, "team_ids": _IDS},
            "required": ["league", "team_ids"],
        },
    },
    {
        "name": "audit_data_integrity",
        "description": (
            "Database-only health report: teams with no or thin history, "
            "missing ESPN ids, stale calibration, ambiguous aliases. Each "
            "finding names its fix. Cheap; run every time."
        ),
        "handler": audit_data_integrity,
        "schema": {
            "type": "object",
            "properties": {
                "league": _LEAGUE,
                "thin_games": {"type": "integer", "default": 2},
            },
            "required": ["league"],
        },
    },
    {
        "name": "link_team_alias",
        "description": (
            "Record that a source's spelling of a team means a given team_id. "
            "Only after you are sure: a wrong link credits one team's games to "
            "another. Refuses unknown team_ids."
        ),
        "handler": link_team_alias,
        "schema": {
            "type": "object",
            "properties": {
                "league": _LEAGUE,
                "name": {"type": "string"},
                "source": {"type": "string"},
                "team_id": {"type": "string"},
            },
            "required": ["league", "name", "source", "team_id"],
        },
    },
    {
        "name": "run_retention",
        "description": (
            "Apply the rolling last-7 rule to player and box-score detail, trim "
            "the HTTP cache, reclaim space. Team-level season results are never "
            "touched. Once a day is enough."
        ),
        "handler": run_retention,
        "schema": {
            "type": "object",
            "properties": {
                "league": _LEAGUE,
                "keep": {"type": "integer", "default": 7},
                "trim_cache": {"type": "boolean", "default": True},
            },
            "required": ["league"],
        },
    },
]
