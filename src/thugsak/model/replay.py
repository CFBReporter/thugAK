"""Replay finished games as of their kickoff: the training set for learning.

Every finished game is re-predicted exactly as the live app would have
predicted it an hour before kickoff -- same engine, same as-of cutoff, so no
later result can leak in -- and the miss against the real score is recorded.
That turns the ~20,000 stored games into training rows, where the live
prediction record (a few hundred so far) could only ever evaluate.

The replay runs against a local SQLite snapshot of one league rather than the
hosted database: thousands of predictions times several queries each would be
thousands of network round trips, and a snapshot also lets a candidate's
settings be written and tested without touching the live calibration.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from .. import store
from ..localtime import parse_ts
from . import asof, predict as predict_mod, ratings as ratings_mod

SNAPSHOT_TABLES = ("teams", "games", "team_game_stats", "calibration")


def snapshot(source_conn: Any, league: str, *, seasons: Iterable[int] | None = None,
             path: str | Path | None = None) -> str:
    """Copy one league's teams, games, results and calibration into a local
    SQLite file. Returns its path."""
    path = str(path or Path(tempfile.mkdtemp(prefix="thugsak-replay-")) / "snap.db")
    store.init_db(path)
    seasons = sorted(set(seasons)) if seasons else None
    with store.connect(path) as dst:
        for table in SNAPSHOT_TABLES:
            q = f"SELECT * FROM {table} WHERE league=?"
            args: list[Any] = [league]
            if seasons and table in ("games", "team_game_stats"):
                q += f" AND season IN ({','.join('?' * len(seasons))})"
                args += seasons
            rows = source_conn.execute(q, args).fetchall()
            if not rows:
                continue
            cols = list(rows[0].keys())
            dst.executemany(
                f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [tuple(r[c] for c in cols) for r in rows],
            )
        dst.commit()
    return path


def recent_seasons(conn: Any, league: str, n: int) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT season FROM games WHERE league=? AND completed=1 ORDER BY season DESC LIMIT ?",
        (league, n),
    ).fetchall()
    return sorted(int(r["season"]) for r in rows)


def _week_cutoff(as_of: str) -> str:
    """Monday 00:00 UTC of the week holding `as_of` (never after it), so one
    ratings solve serves a week of games without seeing any of them."""
    d = parse_ts(as_of)
    monday = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return asof.iso(monday)


def replay(snap_path: str, league: str, *, start: str | None = None, end: str | None = None,
           seasons: Iterable[int] | None = None, progress: bool = False) -> list[dict[str, Any]]:
    """Re-predict every finished game in [start, end) (ISO dates) as of kickoff.

    Returns time-ordered rows: features, the engine's base margin/total, the
    actual result and, where one was stored, the sportsbook closing line.
    """
    seasons = sorted(set(seasons)) if seasons else None
    out: list[dict[str, Any]] = []
    with store.connect(snap_path) as conn:
        q = "SELECT * FROM games WHERE league=? AND completed=1 AND home_score IS NOT NULL"
        args: list[Any] = [league]
        if start:
            q += " AND start_time >= ?"
            args.append(start)
        if end:
            q += " AND start_time < ?"
            args.append(end)
        if seasons:
            q += f" AND season IN ({','.join('?' * len(seasons))})"
            args += seasons
        games = conn.execute(q + " ORDER BY start_time", args).fetchall()
        rs_cache: dict[tuple[int, str], Any] = {}
        for i, g in enumerate(games):
            as_of = asof.as_of_for_kickoff(g["start_time"])
            key = (g["season"], _week_cutoff(as_of))
            rs = rs_cache.get(key)
            if rs is None:
                rs = ratings_mod.solve_from_store(conn, league, key[1], season=g["season"])
                rs_cache[key] = rs
            try:
                p = predict_mod.predict_game(conn, league, g, as_of=as_of, rs=rs, model=None)
            except Exception:
                continue
            if not p.usable or not p.features:
                continue
            spread = g["close_spread"]
            out.append({
                "game_id": g["game_id"],
                "start_time": g["start_time"],
                "season": g["season"],
                "features": p.features,
                "base_margin": p.base_home_points - p.base_away_points,
                "base_total": p.base_home_points + p.base_away_points,
                "actual_margin": float(g["home_score"]) - float(g["away_score"]),
                "actual_total": float(g["home_score"]) + float(g["away_score"]),
                # Books quote negative for a home favourite; home-relative here.
                "market_margin": -float(spread) if spread is not None else None,
                "market_total": float(g["close_total"]) if g["close_total"] is not None else None,
            })
            if progress and i % 500 == 0:
                print(f"    replay {league}: {i}/{len(games)}", flush=True)
    return out
