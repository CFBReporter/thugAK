"""The remote-read caches must never serve a read that a write has outdated.

On Postgres, a team's history and the calibration are cached in-process
(store.READ_CACHE_TTL) because one game view asked for them 13 and 5 times.
`db.on_write` drops them whenever this process writes the tables they mirror
-- including raw SQL that bypasses the store's own writers. On SQLite the
caches are bypassed, so these tests pin the same behaviour on both backends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import db as dbmod  # noqa: E402
from thugsak import store  # noqa: E402

L, A, B = "nba", "nba:A", "nba:B"
AS_OF = "2026-11-12T00:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    return p


def _g(gid, day, hs, aws, completed=1):
    return {
        "game_id": gid, "league": L, "season": 2027, "season_type": "regular",
        "start_time": f"2026-11-{day:02d}T01:00:00+00:00",
        "home_team": A, "away_team": B, "home_score": hs, "away_score": aws,
        "completed": completed, "neutral_site": 0, "source": "test",
    }


def test_new_final_is_seen_after_a_cached_read(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 5, 110, 100))
        conn.commit()
        assert [r["game_id"] for r in store.team_games_before(conn, A, AS_OF)] == ["g1"]
        store.upsert_game(conn, _g("g2", 7, 120, 112))
        conn.commit()
        assert [r["game_id"] for r in store.team_games_before(conn, A, AS_OF)] == ["g2", "g1"]


def test_raw_sql_write_also_clears_history(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 5, 110, 100))
        conn.commit()
        assert store.team_games_before(conn, A, AS_OF)[0]["points_for"] == 110
        conn.execute("UPDATE team_game_stats SET points_for=99 WHERE game_id='g1' AND team_id=?", (A,))
        conn.commit()
        assert store.team_games_before(conn, A, AS_OF)[0]["points_for"] == 99


def test_cached_rows_are_a_copy(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("g1", 5, 110, 100))
        conn.commit()
        store.team_games_before(conn, A, AS_OF).clear()
        assert len(store.team_games_before(conn, A, AS_OF)) == 1


def test_refit_calibration_is_seen(db):
    with store.connect(db) as conn:
        store.put_calibration(conn, L, {"sigma": 12.0})
        conn.commit()
        assert store.get_calibration(conn, L)["sigma"] == 12.0
        store.put_calibration(conn, L, {"sigma": 13.5})
        conn.commit()
        assert store.get_calibration(conn, L)["sigma"] == 13.5


def test_espn_id_comes_from_the_teams_table(db):
    with store.connect(db) as conn:
        store.upsert_team(conn, L, "A", "Team A", "77", is_d1=None)
        conn.commit()
        assert store.espn_id_for(conn, A) == "77"
        assert store.espn_id_for(conn, "nba:NOPE") == ""


@pytest.mark.parametrize("sql,is_write", [
    ("INSERT INTO games VALUES (1)", True),
    ("  update team_game_stats set x=1", True),
    ("DELETE FROM calibration", True),
    ("SELECT * FROM games", False),
    ("WITH x AS (SELECT 1) SELECT * FROM x", False),
    ("INSERTED_AT", False),
])
def test_write_detection(sql, is_write):
    assert bool(dbmod._WRITE_RE.match(sql)) is is_write
