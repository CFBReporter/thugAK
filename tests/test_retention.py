"""Retention is the dangerous part of the agent layer.

A wrong prune is unrecoverable from inside the app, and the 7-game rule has a
precise scope: it applies to per-player and box-score DETAIL, not to team-level
results. The fitted blend weights put 0.80 on the full-season window and the
ratings solve reads every result this season, so a trim that touched `games`
or `team_game_stats` rows would silently replace the dominant term of a
calibrated model. These tests pin that boundary from both sides.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402

LEAGUE = "nba"
A, B = f"{LEAGUE}:A", f"{LEAGUE}:B"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    return path


def _game(gid, day, *, home=A, away=B, detail=None):
    row = {
        "game_id": gid,
        "league": LEAGUE,
        "season": 2027,
        "season_type": "regular",
        "start_time": f"2026-11-{day:02d}T01:00:00+00:00",
        "home_team": home,
        "away_team": away,
        "home_score": 100,
        "away_score": 90,
        "completed": 1,
        "neutral_site": 0,
        "source": "test",
    }
    if detail is not None:
        row["detail"] = detail
    return row


def _seed(conn, n_games=12):
    """n completed games between A and B, one per day, with box-score detail
    on every row and a player log line for one player per side."""
    for i in range(1, n_games + 1):
        store.upsert_game(
            conn,
            _game(f"g{i}", i, detail={"home": {"box": i}, "away": {"box": i}}),
        )
        for team, aid in ((A, "p1"), (B, "p2")):
            store.upsert_player(conn, LEAGUE, aid, team, name=aid, position="G")
            store.upsert_player_games(
                conn, LEAGUE, aid, team,
                [{"event_id": f"g{i}", "date": f"2026-11-{i:02d}",
                  "opponent": "X", "home_away": "vs", "result": "W",
                  "stats": {"pts": i}}],
            )
        store.replace_depth_chart(
            conn, LEAGUE, A, {"G": ["p1", "p9"]}, as_of=f"2026-11-{i:02d}T00:00:00"
        )
    conn.commit()


# --- the boundary: detail is trimmed, results are not ----------------------


def test_trim_never_deletes_games_or_team_rows(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        before_games = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
        before_tgs = conn.execute("SELECT COUNT(*) c FROM team_game_stats").fetchone()["c"]
        store.trim_player_detail(conn, LEAGUE, keep=7)
        conn.commit()
        after_games = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
        after_tgs = conn.execute("SELECT COUNT(*) c FROM team_game_stats").fetchone()["c"]
    assert before_games == after_games == 12
    assert before_tgs == after_tgs == 24


def test_trim_keeps_exactly_seven_player_game_dates_per_team(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        removed = store.trim_player_detail(conn, LEAGUE, keep=7)
        conn.commit()
        for team in (A, B):
            dates = {
                r["date"] for r in conn.execute(
                    "SELECT date FROM player_games WHERE team_id=?", (team,)
                )
            }
            assert len(dates) == 7, team
            # And they are the most recent seven.
            assert min(dates) == "2026-11-06"
    assert removed["player_games"] == 10  # 5 dates x 2 players


def test_trim_nulls_detail_beyond_window_but_keeps_the_row(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        store.trim_player_detail(conn, LEAGUE, keep=7)
        conn.commit()
        rows = conn.execute(
            """SELECT g.game_id, t.detail FROM team_game_stats t
               JOIN games g ON g.game_id=t.game_id
               WHERE t.team_id=? ORDER BY t.start_time""",
            (A,),
        ).fetchall()
    assert len(rows) == 12
    nulled = [r["game_id"] for r in rows if r["detail"] is None]
    kept = [r["game_id"] for r in rows if r["detail"] is not None]
    assert nulled == [f"g{i}" for i in range(1, 6)]
    assert kept == [f"g{i}" for i in range(6, 13)]
    # The kept detail is intact, not blanked.
    assert json.loads(rows[-1]["detail"]) == {"box": 12}


def test_trim_keeps_seven_depth_chart_snapshots(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        store.trim_player_detail(conn, LEAGUE, keep=7)
        conn.commit()
        stamps = {
            r["as_of"] for r in conn.execute(
                "SELECT as_of FROM depth_chart WHERE team_id=?", (A,)
            )
        }
    assert len(stamps) == 7
    assert min(stamps).startswith("2026-11-06")


def test_trim_is_idempotent(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        first = store.trim_player_detail(conn, LEAGUE, keep=7)
        conn.commit()
        second = store.trim_player_detail(conn, LEAGUE, keep=7)
    assert sum(first.values()) > 0
    assert second == {"player_games": 0, "depth_chart": 0, "detail_nulled": 0}


def test_trim_is_scoped_to_one_league(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        # A different league's detail must be untouched by an nba trim.
        store.upsert_player_games(
            conn, "nfl", "q1", "nfl:Q",
            [{"event_id": f"e{i}", "date": f"2026-10-{i:02d}", "stats": {}} for i in range(1, 11)],
        )
        conn.commit()
        store.trim_player_detail(conn, LEAGUE, keep=7)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) c FROM player_games WHERE league='nfl'").fetchone()["c"]
    assert n == 10


# --- things that must survive everything -----------------------------------


def test_injury_snapshots_are_never_pruned(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        for day in range(1, 20):
            store.snapshot_injuries(
                conn, LEAGUE,
                [(A, {"athlete_id": "p1", "name": "p1", "position": "G",
                      "status": "Questionable", "detail": "knee"})],
                as_of=f"2026-11-{day:02d}T12:00:00",
            )
        conn.commit()
        store.trim_player_detail(conn, LEAGUE, keep=7)
        store.prune_seasons(conn, LEAGUE, keep_seasons=[2027])
        conn.commit()
        n = conn.execute("SELECT COUNT(*) c FROM injury_snapshots").fetchone()["c"]
    assert n == 19


def test_calibration_is_byte_identical_after_trim_and_prune(db):
    with store.connect(db) as conn:
        _seed(conn, 12)
        conn.execute(
            "INSERT INTO calibration (league,key,value,n_games,seasons,fitted_at) "
            "VALUES (?,?,?,?,?,?)",
            (LEAGUE, "sigma_margin", 11.73, 3670, "2025,2026,2027", "2026-11-01T00:00:00"),
        )
        conn.commit()
        before = conn.execute("SELECT * FROM calibration").fetchall()
        store.trim_player_detail(conn, LEAGUE, keep=7)
        store.prune_seasons(conn, LEAGUE, keep_seasons=[2027])
        conn.commit()
        after = conn.execute("SELECT * FROM calibration").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]


def test_latest_injury_status_diffs_between_runs(db):
    # What the change gate reads: the most recent snapshot's statuses.
    with store.connect(db) as conn:
        store.snapshot_injuries(
            conn, LEAGUE, [(A, {"athlete_id": "p1", "status": "Questionable"})],
            as_of="2026-11-12T10:00:00",
        )
        store.snapshot_injuries(
            conn, LEAGUE, [(A, {"athlete_id": "p1", "status": "Out"})],
            as_of="2026-11-12T11:00:00",
        )
        conn.commit()
        now = store.latest_injury_status(conn, LEAGUE)
        earlier = store.latest_injury_status(conn, LEAGUE, as_of="2026-11-12T10:30:00")
    assert now == {"p1": "Out"}
    assert earlier == {"p1": "Questionable"}


def test_player_games_lookback_respects_as_of(db):
    # Agent-ingested data must obey the same anti-lookahead rule as everything
    # else: a line logged on the game's own date is not visible before kickoff.
    with store.connect(db) as conn:
        store.upsert_player_games(
            conn, LEAGUE, "p1", A,
            [{"event_id": "e1", "date": "2026-11-10", "stats": {"pts": 10}},
             {"event_id": "e2", "date": "2026-11-12", "stats": {"pts": 30}}],
        )
        conn.commit()
        rows = store.player_games_for_team(conn, LEAGUE, A, before="2026-11-12T01:00:00")
    assert [r["event_id"] for r in rows] == ["e1"]


# --- agent_runs -------------------------------------------------------------


def test_agent_run_lifecycle(db):
    with store.connect(db) as conn:
        rid = store.start_agent_run(conn, "ingest", LEAGUE)
        store.finish_agent_run(
            conn, rid, "ok", signature="abc", summary={"games": 3}, writes={"player_games": 40}
        )
        conn.commit()
        last = store.last_agent_run(conn, "ingest")
        none = store.last_agent_run(conn, "market")
    assert last["status"] == "ok"
    assert last["signature"] == "abc"
    assert json.loads(last["summary"]) == {"games": 3}
    assert last["finished_at"] is not None
    assert none is None


def test_last_agent_run_can_filter_by_status(db):
    # The gate compares against the last run that actually computed a
    # signature, so a 'failed' row in between must not shadow it.
    with store.connect(db) as conn:
        r1 = store.start_agent_run(conn, "auto")
        store.finish_agent_run(conn, r1, "ok", signature="sig1")
        r2 = store.start_agent_run(conn, "auto")
        store.finish_agent_run(conn, r2, "failed", error="boom")
        conn.commit()
        row = store.last_agent_run(conn, "auto", statuses=("ok", "no-op"))
    assert row["run_id"] == r1


# --- schema ----------------------------------------------------------------


@pytest.mark.sqlite_only
def test_init_db_is_idempotent_with_new_tables(db):
    store.init_db(db)
    store.init_db(db)
    with store.connect(db, readonly=True) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    for t in ("players", "player_games", "depth_chart", "injury_snapshots", "agent_runs"):
        assert t in names


@pytest.mark.sqlite_only
def test_init_db_upgrades_a_pre_agent_database(tmp_path):
    # A database built before these tables existed must gain them on the next
    # init without losing anything.
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE games (game_id TEXT PRIMARY KEY, league TEXT NOT NULL, "
        "season INTEGER NOT NULL, start_time TEXT NOT NULL, home_team TEXT NOT NULL, "
        "away_team TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, "
        "neutral_site INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO games VALUES ('g','nba',2027,'2026-11-01T00:00:00+00:00','nba:A','nba:B',1,0);"
    )
    raw.commit()
    raw.close()
    store.init_db(path)
    with store.connect(path, readonly=True) as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        kept = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
    assert "player_games" in names and "agent_runs" in names
    assert kept == 1
