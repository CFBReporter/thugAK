"""Kalshi-to-game lookup, run on whichever backend the suite targets.

`matching._find_game` is reached only through live Kalshi matching, so no
other test exercised it -- which is how its SQLite-only `julianday` survived
the Postgres port until the app itself hit it. This pins it on both backends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import matching, store  # noqa: E402

L = "nba"


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    with store.connect(p) as conn:
        for gid, day in (("g10", 10), ("g12", 12)):
            store.upsert_game(conn, {
                "game_id": gid, "league": L, "season": 2027, "season_type": "regular",
                "start_time": f"2026-11-{day:02d}T01:00:00+00:00",
                "slate_date": f"2026-11-{day - 1:02d}",
                "home_team": "nba:A", "away_team": "nba:B",
                "completed": 0, "neutral_site": 0, "source": "espn",
            })
        conn.commit()
    return p


def test_exact_slate_date_wins(db):
    with store.connect(db, readonly=True) as conn:
        row = matching._find_game(conn, L, "2026-11-11", ("nba:A", "nba:B"), 1)
    assert row["game_id"] == "g12"


def test_team_order_does_not_matter(db):
    with store.connect(db, readonly=True) as conn:
        row = matching._find_game(conn, L, "2026-11-09", ("nba:B", "nba:A"), 1)
    assert row["game_id"] == "g10"


def test_nearest_within_tolerance(db):
    # 2026-11-10 is one day from g10's slate (11-09) and one from g12's (11-11);
    # either is acceptable, but a match must come back.
    with store.connect(db, readonly=True) as conn:
        row = matching._find_game(conn, L, "2026-11-10", ("nba:A", "nba:B"), 1)
    assert row is not None


def test_outside_tolerance_is_none(db):
    with store.connect(db, readonly=True) as conn:
        assert matching._find_game(conn, L, "2026-11-20", ("nba:A", "nba:B"), 1) is None


def test_find_existing_game_cross_source_tolerance(db):
    # The other day-arithmetic statement (store.find_existing_game).
    with store.connect(db, readonly=True) as conn:
        hit = store.find_existing_game(conn, {
            "league": L, "home_team": "nba:A", "away_team": "nba:B",
            "start_time": "2026-11-13T00:00:00+00:00", "source": "nflverse",
        })
    assert hit == "g12"
