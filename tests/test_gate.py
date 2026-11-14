"""The change gate, the router, and the hourly cycle around them.

Two failure modes look identical from outside: a gate that always fires (an
expensive loop restating itself) and a gate that never fires (a slate nobody
looked at). So the gate is tested both ways, and the cycle is driven end to
end with the model stubbed, so "two quiet runs produce a no-op with zero LLM
calls" is a fact rather than a hope.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.agents import gate, runner  # noqa: E402
from thugsak.localtime import LOCAL_TZ  # noqa: E402

LEAGUE = "nba"


def _sig(**over):
    base = {
        "league": LEAGUE, "date": "2026-11-13",
        "finals": ["g1"], "scheduled": ["g2", "g3"],
        "injuries": {"p1": "Questionable", "p2": "Out"},
        "prices": {"g2": {"home": 0.60, "away": 0.41}, "g3": {"home": 0.30, "away": 0.71}},
        "markets": {"g2": 30, "g3": 28},
        "volume": {"g2": 12000.0, "g3": 800.0},
        "oi": {"g2": 5000.0, "g3": 400.0},
        "audit": ["thin_season_history:nba:A"],
    }
    base.update(over)
    return base


# --- the diff, both ways ------------------------------------------------------


def test_identical_signatures_are_not_material():
    d = gate.diff_signatures(_sig(), _sig())
    assert d["material"] is False
    assert d["agents"] == []
    assert d["reasons"] == []


def test_no_previous_signature_runs_everything():
    d = gate.diff_signatures(None, _sig())
    assert d["first_run"] is True
    assert d["agents"] == ["ingest", "market", "analyst"]


def test_new_final_routes_to_ingest_then_analyst():
    d = gate.diff_signatures(_sig(), _sig(finals=["g1", "g2"], scheduled=["g3"]))
    assert d["new_finals"] == ["g2"]
    assert d["agents"] == ["ingest", "analyst"]


def test_injury_flip_routes_to_ingest_then_analyst():
    d = gate.diff_signatures(_sig(), _sig(injuries={"p1": "Out", "p2": "Out"}))
    assert d["injury_deltas"] == {"p1": {"from": "Questionable", "to": "Out"}}
    assert d["agents"] == ["ingest", "analyst"]


def test_injury_cleared_is_material():
    d = gate.diff_signatures(_sig(), _sig(injuries={"p1": "Questionable"}))
    assert d["injuries_cleared"] == ["p2"]
    assert d["material"] is True


def test_price_move_at_threshold_routes_to_market_then_analyst():
    d = gate.diff_signatures(_sig(), _sig(prices={
        "g2": {"home": 0.63, "away": 0.41}, "g3": {"home": 0.30, "away": 0.71},
    }))
    assert "g2" in d["price_moves"] and "home" in d["price_moves"]["g2"]
    assert d["agents"] == ["market", "analyst"]


def test_price_move_below_threshold_is_quiet():
    d = gate.diff_signatures(_sig(), _sig(prices={
        "g2": {"home": 0.61, "away": 0.41}, "g3": {"home": 0.30, "away": 0.71},
    }))
    assert d["material"] is False


def test_price_appearing_from_none_is_material():
    prev = _sig(prices={"g2": {"home": None, "away": 0.41}, "g3": {"home": 0.30, "away": 0.71}})
    d = gate.diff_signatures(prev, _sig())
    assert d["price_moves"]["g2"]["home"] == {"from": None, "to": 0.60}


def test_new_market_listing_routes_to_market():
    d = gate.diff_signatures(_sig(), _sig(markets={"g2": 30, "g3": 31}))
    assert d["new_markets"] == ["g3"]
    assert "market" in d["agents"]


def test_volume_move_only_counts_above_floor():
    # g3 at 800 -> 1000 is +25% but tiny; g2 at 12000 -> 15000 is +25% and real.
    quiet = gate.diff_signatures(_sig(), _sig(volume={"g2": 12000.0, "g3": 1000.0}))
    loud = gate.diff_signatures(_sig(), _sig(volume={"g2": 15000.0, "g3": 800.0}))
    # g3's 1000 is above the 500 floor, so that one does count -- the floor is
    # about absolute size, and 1000 is not nothing. Make the quiet case truly
    # below the floor.
    assert loud["volume_moves"] == ["volume:g2"]
    tiny = gate.diff_signatures(
        _sig(volume={"g2": 12000.0, "g3": 100.0}),
        _sig(volume={"g2": 12000.0, "g3": 300.0}),
    )
    assert tiny["material"] is False
    assert quiet["volume_moves"] == ["volume:g3"]


def test_audit_change_routes_to_ingest_only():
    d = gate.diff_signatures(_sig(), _sig(audit=["no_history:nba:ZZZ"]))
    assert d["audit_changed"] is True
    assert d["agents"] == ["ingest"]


def test_agents_come_out_in_dependency_order():
    d = gate.diff_signatures(_sig(), _sig(
        prices={"g2": {"home": 0.70, "away": 0.41}, "g3": {"home": 0.30, "away": 0.71}},
        finals=["g1", "g3"], scheduled=["g2"],
    ))
    assert d["agents"] == ["ingest", "market", "analyst"]


def test_signature_digest_is_stable_and_sensitive():
    part = {"finals": ["g1"], "scheduled": ["g2"], "injuries": {"p1": "Out"}}
    mkt = {"prices": {"g2": {"home": 0.5, "away": 0.5}}, "markets": {"g2": 2}, "volume": {}, "oi": {}}
    a = gate.build_signature(LEAGUE, "2026-11-13", part, mkt, [])
    b = gate.build_signature(LEAGUE, "2026-11-13", part, mkt, [])
    c = gate.build_signature(LEAGUE, "2026-11-13", part, mkt, [{"kind": "no_history", "teams": ["nba:Z"]}])
    assert a["digest"] == b["digest"]
    assert a["digest"] != c["digest"]
    assert c["audit"] == ["no_history:nba:Z"]


# --- window -----------------------------------------------------------------


@pytest.mark.parametrize("hour,expected", [
    (7, True), (12, True), (23, True), (0, True), (1, True),
    (2, False), (3, False), (4, False), (5, False), (6, False),
])
def test_window_is_0700_to_0100_central(hour, expected):
    now = datetime(2026, 11, 13, hour, 5, tzinfo=LOCAL_TZ)
    assert runner.in_window(now) is expected


# --- lock -------------------------------------------------------------------


def test_lock_is_exclusive_and_released(tmp_path):
    p = tmp_path / "l.lock"
    with runner.RunLock(p):
        with pytest.raises(runner.Locked):
            with runner.RunLock(p):
                pass
        assert p.exists()
    assert not p.exists()


def test_stale_lock_is_broken(tmp_path):
    p = tmp_path / "l.lock"
    p.write_text("{}")
    import os, time
    old = time.time() - 4 * 3600
    os.utime(p, (old, old))
    with runner.RunLock(p, stale_after=3 * 3600):
        pass
    assert not p.exists()


# --- the cycle, end to end with the model stubbed -----------------------------


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    return str(path)


class StubWorld:
    """Replaces the network-facing pieces: the signature source and the LLM."""

    def __init__(self, monkeypatch):
        self.sig = _sig()
        self.llm_calls: list[str] = []

        async def compute_signature(league, date, *, db):
            return dict(self.sig, league=league, date=date, digest="d-" + json.dumps(self.sig, sort_keys=True)[:8])

        async def llm(agent, prompt, *, system, max_turns, budget_usd, subagents=None):
            self.llm_calls.append(agent)
            return runner.LLMResult(text=f"{agent} report", cost_usd=0.01, turns=2)

        async def daily(leagues, *, db):
            return {lg: {"retention": {"removed": {"player_games": 0}}, "audit": {}} for lg in leagues}

        monkeypatch.setattr(gate, "compute_signature", compute_signature)
        monkeypatch.setattr(runner, "LLM", llm)
        monkeypatch.setattr(runner, "run_daily", daily)


@pytest.fixture()
def world(monkeypatch):
    return StubWorld(monkeypatch)


def _auto(db, tmp_path, **kw):
    kw.setdefault("now", datetime(2026, 11, 13, 10, 0, tzinfo=LOCAL_TZ))
    kw.setdefault("lock_path", tmp_path / "agents.lock")
    return asyncio.run(runner.run_auto([LEAGUE], "2026-11-13", db=db, **kw))


def test_first_run_runs_all_three_and_second_is_a_noop(db, tmp_path, world):
    first = _auto(db, tmp_path)
    assert first["status"] == "ok"
    assert world.llm_calls == ["ingest", "market", "analyst"]

    world.llm_calls.clear()
    second = _auto(db, tmp_path)
    assert second["status"] == "no-op"
    assert world.llm_calls == []            # zero LLM calls, the whole point

    with store.connect(db, readonly=True) as conn:
        rows = conn.execute(
            "SELECT agent, status FROM agent_runs ORDER BY run_id"
        ).fetchall()
    statuses = [(r["agent"], r["status"]) for r in rows]
    assert ("auto", "no-op") in statuses
    assert ("daily", "ok") in statuses       # daily tier ran once, on the first cycle


def test_injected_change_routes_to_the_right_agents_only(db, tmp_path, world):
    _auto(db, tmp_path)
    world.llm_calls.clear()
    world.sig = _sig(prices={"g2": {"home": 0.70, "away": 0.41}, "g3": {"home": 0.30, "away": 0.71}})
    out = _auto(db, tmp_path)
    assert out["status"] == "ok"
    assert world.llm_calls == ["market", "analyst"]
    assert "ingest" not in world.llm_calls


def test_injury_flip_reruns_ingest_and_analyst_not_market(db, tmp_path, world):
    _auto(db, tmp_path)
    world.llm_calls.clear()
    world.sig = _sig(injuries={"p1": "Out", "p2": "Out"})
    _auto(db, tmp_path)
    assert world.llm_calls == ["ingest", "analyst"]


def test_outside_window_is_skipped_without_llm(db, tmp_path, world):
    out = _auto(db, tmp_path, now=datetime(2026, 11, 13, 3, 0, tzinfo=LOCAL_TZ))
    assert out["status"] == "skipped-window"
    assert world.llm_calls == []


def test_force_overrides_window_and_gate(db, tmp_path, world):
    _auto(db, tmp_path)
    world.llm_calls.clear()
    out = _auto(db, tmp_path, force=True, now=datetime(2026, 11, 13, 3, 0, tzinfo=LOCAL_TZ))
    assert out["status"] == "ok"
    assert out.get("forced") is True
    assert world.llm_calls == ["ingest", "market", "analyst"]


def test_dry_run_computes_routing_without_llm(db, tmp_path, world):
    out = _auto(db, tmp_path, dry_run=True)
    assert out["status"] == "dry-run"
    assert out["would_run"] == ["ingest", "market", "analyst"]
    assert world.llm_calls == []


def test_held_lock_skips_rather_than_queues(db, tmp_path, world):
    lock = tmp_path / "agents.lock"
    with runner.RunLock(lock):
        out = _auto(db, tmp_path)
    assert out["status"] == "skipped-locked"
    assert world.llm_calls == []


def test_failed_ingest_stops_downstream_agents(db, tmp_path, world, monkeypatch):
    async def llm(agent, prompt, *, system, max_turns, budget_usd, subagents=None):
        world.llm_calls.append(agent)
        if agent == "ingest":
            return runner.LLMResult(is_error=True, error="CLINotFoundError: no claude")
        return runner.LLMResult(text="x")
    monkeypatch.setattr(runner, "LLM", llm)
    out = _auto(db, tmp_path)
    assert out["status"] == "failed"
    assert world.llm_calls == ["ingest"]


def test_failed_run_does_not_shadow_last_good_signature(db, tmp_path, world, monkeypatch):
    _auto(db, tmp_path)                       # good run, signature stored
    async def boom(league, date, *, db):
        raise RuntimeError("kalshi down")
    monkeypatch.setattr(gate, "compute_signature", boom)
    out = _auto(db, tmp_path)
    assert out["status"] == "failed"
    with store.connect(db, readonly=True) as conn:
        assert gate.last_signature(conn, LEAGUE) is not None


def test_run_one_bypasses_gate(db, world):
    out = asyncio.run(runner.run_one("market", [LEAGUE], "2026-11-13", db=db))
    assert out["status"] == "ok"
    assert world.llm_calls == ["market"]
    with pytest.raises(KeyError):
        asyncio.run(runner.run_one("nope", [LEAGUE], "2026-11-13", db=db))


# --- quota cooldown -----------------------------------------------------------
#
# The first NBA cycle died on "You've hit your 5-hour limit". That is the
# account's quota, not the run's budget: every hourly retry until the window
# resets burns more of it and fails identically. The cycle must notice, set a
# cooldown, and skip the LLM agents until it passes -- while the gate and the
# daily tier keep running, since they cost nothing.


def _rate_limit_llm(world):
    async def llm(agent, prompt, *, system, max_turns, budget_usd, subagents=None):
        world.llm_calls.append(agent)
        if agent == "market":
            return runner.LLMResult(
                is_error=True,
                error="ResultError: You've hit your 5-hour limit · resets 8:50pm",
                rate_limited=True,
            )
        return runner.LLMResult(text=f"{agent} ok")
    return llm


def test_quota_hit_sets_cooldown_and_stops_the_cycle(db, tmp_path, world, monkeypatch):
    monkeypatch.setattr(runner, "LLM", _rate_limit_llm(world))
    out = _auto(db, tmp_path)
    assert out["status"] == "rate-limited"
    assert world.llm_calls == ["ingest", "market"]      # analyst never attempted
    assert "cooldown_until" in out
    with store.connect(db, readonly=True) as conn:
        assert store.get_meta(conn, runner.COOLDOWN_KEY) == out["cooldown_until"]


def test_cycles_during_cooldown_skip_llm_but_still_gate(db, tmp_path, world, monkeypatch):
    monkeypatch.setattr(runner, "LLM", _rate_limit_llm(world))
    _auto(db, tmp_path)
    world.llm_calls.clear()
    world.sig = _sig(finals=["g1", "g2"], scheduled=["g3"])   # something DID move
    out = _auto(db, tmp_path)
    assert out["status"] == "skipped-cooldown"
    assert world.llm_calls == []
    assert out["gate"][LEAGUE]["reasons"]                   # the gate still computed


def test_force_ignores_cooldown(db, tmp_path, world, monkeypatch):
    monkeypatch.setattr(runner, "LLM", _rate_limit_llm(world))
    _auto(db, tmp_path)
    world.llm_calls.clear()
    monkeypatch.setattr(runner, "LLM", _ok_llm(world))
    out = _auto(db, tmp_path, force=True)
    assert out["status"] == "ok"
    assert world.llm_calls == ["ingest", "market", "analyst"]


def _ok_llm(world):
    async def llm(agent, prompt, *, system, max_turns, budget_usd, subagents=None):
        world.llm_calls.append(agent)
        return runner.LLMResult(text=f"{agent} ok", cost_usd=0.01, turns=1)
    return llm


def test_expired_cooldown_lets_agents_run_again(db, tmp_path, world):
    with store.connect(db) as conn:
        store.put_meta(conn, runner.COOLDOWN_KEY, "2020-01-01T00:00:00+00:00")
        conn.commit()
    out = _auto(db, tmp_path)
    assert out["status"] == "ok"
    assert world.llm_calls == ["ingest", "market", "analyst"]


def test_rate_limit_detection_reads_the_sdk_message():
    assert runner._looks_rate_limited("Claude Code returned an error result: You've hit your 5-hour limit")
    assert runner._looks_rate_limited("HTTP 429 Too Many Requests")
    assert not runner._looks_rate_limited("Reached maximum budget ($1)")   # that is OUR cap, retry is fine
    assert not runner._looks_rate_limited(None)


def test_models_default_to_sonnet_with_env_override(monkeypatch):
    monkeypatch.delenv("THUGSAK_MODEL_INGEST", raising=False)
    assert runner.model_for("ingest") == runner.SONNET
    assert runner.model_for("market") == runner.SONNET
    monkeypatch.setenv("THUGSAK_MODEL_ANALYST", "claude-opus-5-5")
    assert runner.model_for("analyst") == "claude-opus-5-5"
