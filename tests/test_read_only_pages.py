"""Pages read; the agents write.

A page load (`list_games` without refresh, predictions with record=False)
must leave the database exactly as it found it. And the agents' scoreboard
refresh writes a game only when it is new or changed -- on a remote database
every write is a round trip, and re-saving an unchanged 118-game NCAAF slate
on every refresh was ~600 of them.

Runs on whichever backend the suite targets.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.mcp import tools  # noqa: E402

L = "nba"


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    with store.connect(p) as conn:
        for code, name, eid in (("BOS", "Boston Celtics", "2"), ("NYK", "New York Knicks", "18")):
            store.upsert_team(conn, L, code, name, eid, is_d1=None)
            store.add_alias(conn, L, code, "espn", f"{L}:{code}")
            store.add_alias(conn, L, eid, "espn_id", f"{L}:{code}")
        conn.commit()
    return str(p)


def _snapshot(db):
    with store.connect(db, readonly=True) as conn:
        return {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("games", "team_game_stats", "predictions", "team_aliases", "teams")
        }


# --- the unchanged check ----------------------------------------------------------


def _row(**over):
    base = {
        "game_id": "nba:espn:1", "league": L, "season": 2027, "season_type": "regular",
        "start_time": "2026-11-15T00:00Z", "home_team": "nba:BOS", "away_team": "nba:NYK",
        "home_score": 0, "away_score": 0, "completed": 0, "neutral_site": 0,
        "close_spread": -3.5, "close_total": 228.5, "close_home_ml": None,
        "close_away_ml": None, "source": "espn", "slate_date": "2026-11-14",
    }
    base.update(over)
    return base


def test_no_stored_row_means_new():
    assert store.game_unchanged(None, _row()) is False


def test_identical_row_is_unchanged():
    assert store.game_unchanged(_row(), _row()) is True


@pytest.mark.parametrize("change", [
    {"completed": 1, "home_score": 110, "away_score": 101},   # it finished
    {"start_time": "2026-11-15T01:00Z"},                     # rescheduled
    {"close_total": 230.5},                                  # line moved
])
def test_real_changes_are_detected(change):
    assert store.game_unchanged(_row(), _row(**change)) is False


def test_incoming_none_never_counts_as_a_change():
    # upsert_game merges with COALESCE: a missing line keeps the stored one.
    assert store.game_unchanged(_row(), _row(close_spread=None, close_total=None)) is True


# --- store_scoreboard: write only what changed ------------------------------------


@dataclass
class _Team:
    espn_id: str
    abbrev: str
    display_name: str
    location: str = ""
    nickname: str = ""
    short_name: str = ""


@dataclass
class _Line:
    spread: float | None = -3.5
    total: float | None = 228.5
    home_ml: int | None = None
    away_ml: int | None = None


@dataclass
class _Game:
    espn_id: str = "1"
    start_time: str = "2026-11-15T00:00Z"
    home: _Team = field(default_factory=lambda: _Team("2", "BOS", "Boston Celtics"))
    away: _Team = field(default_factory=lambda: _Team("18", "NYK", "New York Knicks"))
    home_score: int | None = 0
    away_score: int | None = 0
    completed: bool = False
    neutral_site: bool = False
    season: int = 2027
    best_line: _Line | None = field(default_factory=_Line)


def test_second_identical_refresh_writes_nothing(db):
    with store.connect(db) as conn:
        first = tools.store_scoreboard(conn, L, "2026-11-14", [_Game()])
        conn.commit()
    before = _snapshot(db)
    with store.connect(db) as conn:
        second = tools.store_scoreboard(conn, L, "2026-11-14", [_Game()])
        conn.commit()
    assert first == {"written": 1, "unchanged": 0, "skipped": 0}
    assert second == {"written": 0, "unchanged": 1, "skipped": 0}
    assert _snapshot(db) == before


def test_a_final_is_written(db):
    with store.connect(db) as conn:
        tools.store_scoreboard(conn, L, "2026-11-14", [_Game()])
        res = tools.store_scoreboard(
            conn, L, "2026-11-14",
            [_Game(completed=True, home_score=112, away_score=104)],
        )
        conn.commit()
        row = store.get_game(conn, "nba:espn:1")
        tgs = conn.execute(
            "SELECT points_for FROM team_game_stats WHERE game_id='nba:espn:1' AND team_id='nba:BOS'"
        ).fetchone()
    assert res["written"] == 1
    assert row["completed"] == 1 and row["home_score"] == 112
    assert tgs["points_for"] == 112


# --- pages do not write -----------------------------------------------------------


def test_list_games_without_refresh_writes_nothing(db, monkeypatch):
    with store.connect(db) as conn:
        tools.store_scoreboard(conn, L, "2026-11-14", [_Game()])
        conn.commit()

    async def no_markets(conn, client, league):
        return {}, []

    def no_espn(client):
        raise AssertionError("a page load must not call ESPN for the scoreboard")

    monkeypatch.setattr(tools, "_league_markets", no_markets)
    monkeypatch.setattr(tools, "_provider", lambda: type("P", (), {"session": staticmethod(no_espn)})())
    before = _snapshot(db)
    out = asyncio.run(tools.list_games(L, "2026-11-14", db=db))
    assert [g["game_id"] for g in out["games"]] == ["nba:espn:1"]
    assert _snapshot(db) == before


# --- prices: snapshot first, live after -------------------------------------------


@dataclass
class _Mkt:
    ticker: str
    yes_ask: str | None
    _subject_team: str | None = None
    title: str = ""
    volume: float = 0
    open_interest: float = 0


def _fake_markets(price="0.62"):
    calls = {"n": 0}

    async def fake(conn, client, league):
        calls["n"] += 1
        return {
            "nba:espn:1": [
                _Mkt("KXNBAGAME-26NOV14NYKBOS-BOS", price, "nba:BOS"),
                _Mkt("KXNBAGAME-26NOV14NYKBOS-NYK", "0.40", "nba:NYK"),
                _Mkt("KXNBASPREAD-26NOV14NYKBOS-BOS3", "0.50"),
            ]
        }, []

    return fake, calls


def _seed(db):
    with store.connect(db) as conn:
        tools.store_scoreboard(conn, L, "2026-11-14", [_Game()])
        conn.commit()


def test_snapshot_mode_never_calls_kalshi(db, monkeypatch):
    _seed(db)

    async def boom(*a, **k):
        raise AssertionError("snapshot mode must not fetch markets")

    monkeypatch.setattr(tools, "_league_markets", boom)
    out = asyncio.run(tools.list_games(L, "2026-11-14", prices="snapshot", db=db))
    g = out["games"][0]
    assert out["prices_source"] == "none" and out["prices_as_of"] is None
    assert g["kalshi_markets"] == 0 and g["home_win_price"] is None


def test_saved_snapshot_is_served_without_kalshi(db, monkeypatch):
    _seed(db)
    fake, _ = _fake_markets()
    with store.connect(db) as conn:
        rows = store.games_on_date(conn, L, "2026-11-14")
        by_game, unmatched = asyncio.run(fake(conn, None, L))
        tools.save_price_snapshot(conn, tools._build_prices(rows, by_game, unmatched, L, "2026-11-14"))
        conn.commit()

    async def boom(*a, **k):
        raise AssertionError("snapshot mode must not fetch markets")

    monkeypatch.setattr(tools, "_league_markets", boom)
    out = asyncio.run(tools.list_games(L, "2026-11-14", prices="snapshot", db=db))
    g = out["games"][0]
    assert out["prices_source"] == "snapshot" and out["prices_as_of"]
    assert g["home_win_price"] == 0.62 and g["away_win_price"] == 0.40
    assert g["kalshi_markets"] == 3 and g["has_ladders"] is True


def test_live_prices_match_the_live_slate_and_are_shared_for_a_minute(db, monkeypatch):
    _seed(db)
    fake, calls = _fake_markets()
    monkeypatch.setattr(tools, "_league_markets", fake)
    live = asyncio.run(tools.slate_prices(L, "2026-11-14", db=db))
    full = asyncio.run(tools.list_games(L, "2026-11-14", prices="live", db=db))
    assert calls["n"] == 1, "second request within the TTL must reuse the fetch"
    p = live["prices"]["nba:espn:1"]
    g = full["games"][0]
    for k in ("kalshi_markets", "home_win_price", "away_win_price", "has_ladders"):
        assert p[k] == g[k]
    assert live["source"] == "live" and full["prices_source"] == "live"


def test_live_prices_write_nothing(db, monkeypatch):
    _seed(db)
    fake, _ = _fake_markets()
    monkeypatch.setattr(tools, "_league_markets", fake)
    before = _snapshot(db)
    asyncio.run(tools.slate_prices(L, "2026-11-14", db=db))
    assert _snapshot(db) == before


def test_old_snapshots_are_pruned(db):
    with store.connect(db) as conn:
        for day in ("2026-11-01", "2026-11-14"):
            tools.save_price_snapshot(conn, {"league": L, "date": day, "prices": {}})
        conn.commit()
        assert tools.load_price_snapshot(conn, L, "2026-11-01") is None
        assert tools.load_price_snapshot(conn, L, "2026-11-14") is not None


def test_gate_saves_the_hourly_snapshot(db, monkeypatch):
    from thugsak.agents import gate

    _seed(db)
    fake, _ = _fake_markets("0.55")
    monkeypatch.setattr(tools, "_league_markets", fake)
    asyncio.run(gate.fetch_market_state(L, "2026-11-14", db=db))
    with store.connect(db, readonly=True) as conn:
        snap = tools.load_price_snapshot(conn, L, "2026-11-14")
    assert snap["prices"]["nba:espn:1"]["home_win_price"] == 0.55
