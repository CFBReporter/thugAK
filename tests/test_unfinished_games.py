"""Unfinished games must never count as results.

ESPN's scoreboard reports 0-0 for a game that has not started. Those
placeholders were written into `team_game_stats` and read back by the form
windows as real games, because the query checked "has a score" rather than
"is final". On the Nov 14, 2026 NBA slate that put every predicted total 20-30
points under the book -- one phantom zero in a team's last three is enough.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.model import fit  # noqa: E402

L, A, B = "nba", "nba:A", "nba:B"


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    return p


def _g(gid, day, hs, aws, completed):
    return {
        "game_id": gid, "league": L, "season": 2027, "season_type": "regular",
        "start_time": f"2026-11-{day:02d}T01:00:00+00:00",
        "home_team": A, "away_team": B, "home_score": hs, "away_score": aws,
        "completed": completed, "neutral_site": 0, "source": "test",
    }


def test_placeholder_zero_is_not_written_to_team_rows(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 10, 0, 0, 0))
        conn.commit()
        rows = conn.execute("SELECT points_for, points_against FROM team_game_stats").fetchall()
    assert all(r["points_for"] is None and r["points_against"] is None for r in rows)


def test_form_windows_ignore_unfinished_games(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 5, 110, 100, 1))
        store.upsert_game(conn, _g("g2", 7, 120, 112, 1))
        store.upsert_game(conn, _g("g3", 9, 0, 0, 0))      # yesterday, never refreshed
        conn.commit()
        rows = store.team_games_before(conn, A, "2026-11-12T00:00:00+00:00")
    assert [r["game_id"] for r in rows] == ["g2", "g1"]
    assert all(r["points_for"] > 0 for r in rows)


def test_a_game_that_later_finishes_gets_its_real_score(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 9, 0, 0, 0))
        store.upsert_game(conn, _g("g1", 9, 118, 104, 1))
        conn.commit()
        rows = store.team_games_before(conn, A, "2026-11-12T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["points_for"] == 118


def test_init_db_cleans_legacy_placeholder_rows(db):
    # Simulate a database written before the fix: a 0-0 unfinished game with
    # points on its team rows.
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 9, 0, 0, 0))
        conn.execute("UPDATE team_game_stats SET points_for=0, points_against=0")
        conn.commit()
    applied = store.init_db(db)
    assert any("placeholder" in a for a in applied)
    with store.connect(db, readonly=True) as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM team_game_stats WHERE points_for IS NOT NULL"
        ).fetchone()["c"]
    assert n == 0
    assert not any("placeholder" in a for a in store.init_db(db))   # idempotent


def test_fit_history_excludes_unfinished_games(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 5, 110, 100, 1))
        store.upsert_game(conn, _g("g2", 9, 0, 0, 0))
        conn.execute("UPDATE team_game_stats SET points_for=0 WHERE game_id='g2'")
        conn.commit()
        hist = fit._team_history(conn, L)
    rows = [r for rs in hist.values() for r in rs]
    assert len(rows) == 2                      # g1, one row per team
    assert all(r["points_for"] > 0 for r in rows)
