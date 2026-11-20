"""SharpAPI lines are combined across books into the home team's spread and the total."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.clients import sharpapi  # noqa: E402


def _row(book, mt, sel, side, line, **kw):
    return {"event_id": "e1", "event_uuid": "u1", "sportsbook": book, "market_type": mt,
            "home_team": "Houston Texans", "away_team": "Indianapolis Colts",
            "event_start_time": "2026-11-20T01:15Z", "selection": sel, "selection_type": side,
            "line": line, "is_live": False, "is_active": True, **kw}


def test_median_home_spread_and_total_across_books():
    rows = [
        _row("draftkings", "point_spread", "Houston Texans", "home", -12.5),
        _row("draftkings", "point_spread", "Indianapolis Colts", "away", 12.5),
        _row("fanduel", "point_spread", "Houston Texans", "home", -11.5),
        _row("draftkings", "total_points", "Over", "over", 44.5),
        _row("fanduel", "total_points", "Over", "over", 45.5),
        _row("fanduel", "total_points", "Under", "under", 45.5),
    ]
    [ev] = sharpapi.combine(rows)
    assert ev["spread"] == -12.0 and ev["total"] == 45.0
    assert ev["books"] == ["draftkings", "fanduel"]


def test_live_and_suspended_rows_are_ignored():
    rows = [
        _row("draftkings", "point_spread", "Houston Texans", "home", -3.0, is_live=True),
        _row("fanduel", "point_spread", "Houston Texans", "home", -4.0, is_active=False),
    ]
    assert sharpapi.combine(rows) == []


def test_no_key_means_not_available(monkeypatch):
    monkeypatch.delenv(sharpapi.ENV_VAR, raising=False)
    assert not sharpapi.available()
