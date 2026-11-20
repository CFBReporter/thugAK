"""Line movement: first-seen vs latest, per book; only big moves are flagged."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.analysis import line_moves  # noqa: E402
from thugsak.clients import sharpapi  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    return p


def test_first_seen_is_kept_and_latest_moves(db):
    with store.connect(db) as conn:
        store.record_lines(conn, [("g", "draftkings", "moneyline", "home", None, -110.0)], seen_at="2026-11-19T12:00:00+00:00")
        store.record_lines(conn, [("g", "draftkings", "moneyline", "home", None, 150.0)], seen_at="2026-11-19T18:00:00+00:00")
        store.record_lines(conn, [("g", "draftkings", "moneyline", "home", None, 210.0)], seen_at="2026-11-20T12:00:00+00:00")
        conn.commit()
        [r] = store.line_history(conn, ["g"])
    assert r["open_odds"] == -110 and r["last_odds"] == 210
    assert r["open_at"].startswith("2026-11-19T12") and r["last_at"].startswith("2026-11-20")


def test_the_lions_example_is_flagged_with_book_start_end_and_size(db):
    with store.connect(db) as conn:
        store.record_lines(conn, [("g", "draftkings", "moneyline", "home", None, -110.0),
                                  ("g", "fanduel", "moneyline", "home", None, -110.0),
                                  ("g", "draftkings", "spread", "home", -3.0, -110.0),
                                  ("g", "draftkings", "total", "over", 47.5, -110.0)])
        store.record_lines(conn, [("g", "draftkings", "moneyline", "home", None, 210.0),
                                  ("g", "fanduel", "moneyline", "home", None, -120.0),   # small: not flagged
                                  ("g", "draftkings", "spread", "home", 5.5, -110.0),
                                  ("g", "draftkings", "total", "over", 46.5, -110.0)])  # 1 pt: not flagged
        conn.commit()
        moves = line_moves.significant(store.line_history(conn, ["g"]), "Detroit Lions", "Chicago Bears")
    assert [(m["market"], m["sportsbook"]) for m in moves] == [("spread", "DraftKings"), ("moneyline", "DraftKings")]
    ml = moves[1]
    assert ml["start"] == "-110" and ml["end"] == "+210" and ml["selection"] == "Detroit Lions"
    assert "52% -> 32% implied" in ml["note"]
    assert moves[0]["start"] == "-3.0" and moves[0]["end"] == "+5.5" and moves[0]["change"] == "+8.5 pts"


def test_sharpapi_rows_yield_per_book_quotes():
    rows = [
        {"event_id": "e", "sportsbook": "draftkings", "market_type": "moneyline", "home_team": "H",
         "away_team": "A", "selection": "H", "selection_type": "home", "odds_american": -150, "line": None},
        {"event_id": "e", "sportsbook": "draftkings", "market_type": "point_spread", "home_team": "H",
         "away_team": "A", "selection": "H", "selection_type": "home", "odds_american": -110, "line": -3.5},
    ]
    [ev] = sharpapi.combine(rows)
    assert ("draftkings", "moneyline", "home", None, -150.0) in ev["quotes"]
    assert ("draftkings", "spread", "home", -3.5, -110.0) in ev["quotes"]
    assert ev["spread"] == -3.5
