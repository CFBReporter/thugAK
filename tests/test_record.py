"""Tests for the permanent record of predictions and bets.

Everything else in the database is a cache that `import_history.py` can
rebuild. This table cannot be rebuilt: it is what the model said at the time,
and no source will reissue that. Three properties therefore matter, and all
three were absent before:

1. **It survives pruning.** `prune_seasons` deletes a season's games once its
   calibration has been distilled -- including, at rollover, the season you
   have spent a year collecting predictions for. A record that joins to
   `games` for the final score becomes a drawer of forecasts with nothing to
   check them against, exactly when it finally got long enough to mean
   something.

2. **It does not double-count.** Opening a game used to append a row every
   time; one fixture reached twenty. Any accuracy figure over the table would
   have weighted it twenty times -- a silent bias toward whichever games got
   looked at most.

3. **It holds the bets, not just the forecast.** A recommendation depends on
   the Kalshi ask and the money standing at the cutoff. Kalshi still serves a
   settled market's candles today, but that is their retention policy, not a
   guarantee; a price nobody kept is a bet nobody can grade.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402

LEAGUE = "nba"
HOME, AWAY = "nba:MEM", "nba:DEN"
AS_OF = "2026-11-12T00:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    return path


def _game(conn, gid="g1", *, season=2027, hs=None, aws=None, completed=0):
    store.upsert_game(conn, {
        "game_id": gid, "league": LEAGUE, "season": season,
        "season_type": "regular", "start_time": "2026-11-12T01:00:00+00:00",
        "home_team": HOME, "away_team": AWAY,
        "home_score": hs, "away_score": aws,
        "completed": completed, "neutral_site": 0, "source": "test",
        "slate_date": "2026-11-11",
    })


def _pred(conn, gid="g1", *, as_of=AS_OF, spread=2.4, total=227.1, version="v1"):
    return store.save_prediction(conn, {
        "game_id": gid, "league": LEAGUE, "as_of": as_of,
        "pred_home_points": (total + spread) / 2,
        "pred_away_points": (total - spread) / 2,
        "pred_spread": spread, "pred_total": total,
        "model_version": version,
        "matchup": "DEN @ MEM", "slate_date": "2026-11-11",
        "home_team": HOME, "away_team": AWAY,
    })


# --------------------------------------------------------------------------
# no double counting
# --------------------------------------------------------------------------

def test_reopening_a_game_updates_rather_than_appends(db):
    with store.connect(db) as conn:
        _game(conn)
        first = _pred(conn)
        for _ in range(5):
            _pred(conn)
        conn.commit()
        rows = list(conn.execute("SELECT * FROM predictions"))
    assert len(rows) == 1
    assert rows[0]["pred_id"] == first


def test_a_different_cutoff_is_a_different_prediction(db):
    with store.connect(db) as conn:
        _game(conn)
        _pred(conn, as_of=AS_OF)
        _pred(conn, as_of="2026-11-11T20:00:00+00:00")
        conn.commit()
        assert conn.execute("SELECT count(*) c FROM predictions").fetchone()["c"] == 2


def test_a_new_model_version_keeps_its_own_row(db):
    """Otherwise a model change would overwrite the record of what the old
    one said, and there would be nothing to compare against."""
    with store.connect(db) as conn:
        _game(conn)
        _pred(conn, version="v1")
        _pred(conn, version="v2")
        conn.commit()
        assert conn.execute("SELECT count(*) c FROM predictions").fetchone()["c"] == 2


def test_the_first_timestamp_is_kept_on_update(db):
    """Re-opening a page is not a new forecast."""
    with store.connect(db) as conn:
        _game(conn)
        _pred(conn)
        conn.commit()
        first = conn.execute("SELECT created_at FROM predictions").fetchone()[0]
        _pred(conn, spread=9.9)
        conn.commit()
        row = conn.execute("SELECT * FROM predictions").fetchone()
    assert row["created_at"] == first
    assert row["pred_spread"] == pytest.approx(9.9)


def test_legacy_duplicates_are_collapsed_on_open(db):
    """A database written by the appending version must not carry its bias
    forward once the code is fixed."""
    with store.connect(db) as conn:
        _game(conn)
        for _ in range(4):
            conn.execute(
                """INSERT INTO predictions
                     (game_id, league, as_of, created_at, pred_spread,
                      model_version)
                   VALUES (?,?,?,?,?,?)""",
                ("g1", LEAGUE, AS_OF, store.utcnow(), 2.4, "v1"),
            )
        conn.commit()
        assert conn.execute("SELECT count(*) c FROM predictions").fetchone()["c"] == 4

    applied = store.init_db(db)
    with store.connect(db) as conn:
        assert conn.execute("SELECT count(*) c FROM predictions").fetchone()["c"] == 1
    assert any("deduped" in a for a in applied)


# --------------------------------------------------------------------------
# settlement
# --------------------------------------------------------------------------

def test_settling_copies_the_score_onto_the_prediction(db):
    with store.connect(db) as conn:
        _game(conn, hs=118, aws=123, completed=1)
        _pred(conn)
        conn.commit()
        assert store.settle_predictions(conn, LEAGUE) == 1
        conn.commit()
        row = conn.execute("SELECT * FROM predictions").fetchone()
    assert (row["actual_home"], row["actual_away"]) == (118, 123)
    assert row["settled_at"]


def test_an_unfinished_game_is_not_settled(db):
    with store.connect(db) as conn:
        _game(conn)
        _pred(conn)
        conn.commit()
        assert store.settle_predictions(conn, LEAGUE) == 0


def test_settling_is_idempotent(db):
    with store.connect(db) as conn:
        _game(conn, hs=118, aws=123, completed=1)
        _pred(conn)
        conn.commit()
        assert store.settle_predictions(conn, LEAGUE) == 1
        conn.commit()
        assert store.settle_predictions(conn, LEAGUE) == 0


# --------------------------------------------------------------------------
# the property that matters most: surviving a prune
# --------------------------------------------------------------------------

def test_the_record_outlives_the_games_it_came_from(db):
    """Season rollover deletes the games. The record has to stand alone, or
    a year of predictions becomes unreadable the moment it is worth reading.
    """
    with store.connect(db) as conn:
        _game(conn, hs=118, aws=123, completed=1)
        _pred(conn)
        store.record_bets(
            conn, "g1", AS_OF,
            bets=[{"slot": "moneyline_home", "recommendation": "BET",
                   "bet": "Memphis wins", "all_in_cost": 0.427,
                   "kind": "game", "side": "yes", "subject_team": HOME}],
            the_call="Bet Memphis wins at 0.41",
            money_usd=170_218.47,
        )
        store.settle_predictions(conn, LEAGUE)
        conn.commit()

        # Rollover: the season's games are pruned after calibration.
        store.prune_seasons(conn, LEAGUE, [9999])
        conn.commit()
        assert conn.execute("SELECT count(*) c FROM games").fetchone()["c"] == 0

        rows = store.settled_predictions(conn, LEAGUE)

    assert len(rows) == 1
    r = rows[0]
    # Everything needed to read and grade it is still here.
    assert r["matchup"] == "DEN @ MEM"
    assert (r["actual_home"], r["actual_away"]) == (118, 123)
    assert (r["home_team"], r["away_team"]) == (HOME, AWAY)
    assert r["pred_spread"] == pytest.approx(2.4)
    assert json.loads(r["bets"])[0]["bet"] == "Memphis wins"
    assert r["the_call"].startswith("Bet Memphis")
    assert r["kalshi_money_usd"] == pytest.approx(170_218.47)


def test_bets_are_stored_with_what_is_needed_to_settle_them(db):
    """kind, side, threshold and subject team: without all four a stored bet
    cannot be graded later, however much else was kept."""
    with store.connect(db) as conn:
        _game(conn)
        _pred(conn)
        store.record_bets(
            conn, "g1", AS_OF,
            bets=[{"slot": "total", "recommendation": "SMALL",
                   "bet": "Under 224.5 points", "kind": "total",
                   "side": "no", "threshold": 224.5, "subject_team": None,
                   "all_in_cost": 0.39}],
        )
        conn.commit()
        stored = json.loads(
            conn.execute("SELECT bets FROM predictions").fetchone()["bets"]
        )[0]
    for key in ("kind", "side", "threshold", "all_in_cost"):
        assert key in stored


def test_recording_bets_does_not_clobber_the_call_with_nothing(db):
    with store.connect(db) as conn:
        _game(conn)
        _pred(conn)
        store.record_bets(conn, "g1", AS_OF, bets=[], the_call="Bet X",
                          money_usd=100.0)
        store.record_bets(conn, "g1", AS_OF, bets=[])
        conn.commit()
        row = conn.execute("SELECT * FROM predictions").fetchone()
    assert row["the_call"] == "Bet X"
    assert row["kalshi_money_usd"] == pytest.approx(100.0)


def test_settled_predictions_can_be_filtered_by_date(db):
    with store.connect(db) as conn:
        _game(conn, "g1", hs=118, aws=123, completed=1)
        _pred(conn, "g1")
        store.settle_predictions(conn, LEAGUE)
        conn.commit()
        assert store.settled_predictions(conn, LEAGUE, since="2026-11-01")
        assert not store.settled_predictions(conn, LEAGUE, since="2026-12-01")
