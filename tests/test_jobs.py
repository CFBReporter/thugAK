"""The no-LLM hourly jobs: what they pick to do, and that one failure does not stop the rest."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.agents import gate, ingest_tools, jobs  # noqa: E402
from thugsak.mcp import tools  # noqa: E402

L = "nba"


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    return str(p)


def _g(gid, start, completed, hs=None, aws=None):
    return {
        "game_id": gid, "league": L, "season": 2027, "season_type": "regular",
        # Distinct teams per game: the store merges one matchup on nearby
        # dates into a single game, which is right for real data.
        "start_time": start, "home_team": f"nba:{gid}H", "away_team": f"nba:{gid}A",
        "home_score": hs, "away_score": aws, "completed": completed,
        "neutral_site": 0, "source": "test", "slate_date": "2026-11-19",
    }


def test_record_window_picks_only_games_starting_soon(db):
    now = datetime(2026, 11, 19, 22, 0, tzinfo=timezone.utc)
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("soon", "2026-11-20T00:30:00+00:00", 0))
        store.upsert_game(conn, _g("later", "2026-11-20T03:30:00+00:00", 0))
        store.upsert_game(conn, _g("done", "2026-11-19T20:00:00+00:00", 1, 100, 90))
        conn.commit()
        assert jobs.games_to_record(conn, L, "2026-11-19", now=now) == ["soon"]


def test_box_scores_only_for_finished_games_without_one(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _g("final", "2026-11-19T01:00:00+00:00", 1, 100, 90))
        store.upsert_game(conn, _g("stored", "2026-11-19T02:00:00+00:00", 1, 99, 98))
        store.upsert_game(conn, _g("future", "2026-11-20T01:00:00+00:00", 0))
        conn.execute("UPDATE team_game_stats SET detail='{}' WHERE game_id='stored'")
        conn.commit()
        assert jobs.games_needing_box_scores(conn, L, ["final", "stored", "future"]) == ["final"]


def _stub(monkeypatch, *, fail_injuries=False):
    async def scores(league, date, *, db):
        return {date: 0}

    async def injuries(league, *, db):
        if fail_injuries:
            raise RuntimeError("ESPN down")
        return {"snapshot_rows": 3, "changed": [], "material": False}

    async def market(league, date, *, db):
        return {"markets": {}}

    async def daily_noop(*a, **k):
        return {"written": 0, "failed": [], "removed": {}, "findings": []}

    monkeypatch.setattr(gate, "refresh_scores", scores)
    monkeypatch.setattr(ingest_tools, "ingest_injuries", injuries)
    monkeypatch.setattr(gate, "fetch_market_state", market)
    for name in ("ingest_player_logs", "ingest_depth_charts", "run_retention", "audit_data_integrity"):
        monkeypatch.setattr(ingest_tools, name, daily_noop)

    async def no_rec(*a, **k):
        raise AssertionError("nothing is in the record window")

    monkeypatch.setattr(tools, "get_recommendation", no_rec)


def test_a_failed_step_is_partial_and_the_rest_still_run(db, monkeypatch):
    _stub(monkeypatch, fail_injuries=True)
    out = asyncio.run(jobs.run_jobs([L], "2026-11-19", db=db))
    steps = out["leagues"][L]
    assert out["status"] == "partial" and "nba.injuries" in out["error"]
    assert "error" in steps["injuries"] and "secs" in steps["prices"]


def test_daily_tier_runs_once_then_waits(db, monkeypatch):
    _stub(monkeypatch)
    first = asyncio.run(jobs.run_jobs([L], "2026-11-19", db=db))
    second = asyncio.run(jobs.run_jobs([L], "2026-11-19", db=db))
    assert first["status"] == "ok" and first["daily_ran"] is True
    assert second["daily_ran"] is False
    with store.connect(db, readonly=True) as conn:
        rows = conn.execute("SELECT status, summary FROM agent_runs WHERE agent='jobs' ORDER BY run_id").fetchall()
    assert [r["status"] for r in rows] == ["ok", "ok"]
    assert json.loads(rows[0]["summary"])["daily_ran"] is True
