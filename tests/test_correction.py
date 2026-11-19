"""The learned correction layer and the model training log."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.model import correction, train  # noqa: E402


def _rows(n=1200, *, total_bias=0.0, b2b_effect=0.0, market_noise=None, seed=7):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        bm, bt = rnd.gauss(2, 6), rnd.gauss(225, 6)
        b2b = 1.0 if rnd.random() < 0.2 else 0.0
        am = bm + b2b * b2b_effect + rnd.gauss(0, 10)
        at = bt + total_bias + rnd.gauss(0, 12)
        f = correction.features(base_margin=bm, base_total=bt, rating_margin=bm,
                                games_home=20, games_away=20,
                                rest_home=1.0 if b2b else 2.0, rest_away=2.0, neutral=False)
        rows.append({
            "start_time": f"2026-{1 + i * 10 // n:02d}-{1 + i % 27:02d}T01:00:00+00:00",
            "features": f, "base_margin": bm, "base_total": bt,
            "actual_margin": am, "actual_total": at,
            "market_margin": (am + rnd.gauss(0, market_noise)) if market_noise else None,
            "market_total": (at + rnd.gauss(0, market_noise)) if market_noise else None,
        })
    rows.sort(key=lambda r: r["start_time"])
    return rows


def test_learns_a_league_wide_scoring_bias():
    rows = _rows(total_bias=6.0)
    c = correction.fit(rows[:1000], league="nba", version="t")
    assert c.total.intercept == pytest.approx(6.0, abs=1.5)
    before = correction.evaluate(None, rows[1000:])["mae_total"]
    after = correction.evaluate(c, rows[1000:])["mae_total"]
    assert after < before


def test_learns_a_back_to_back_effect_on_the_margin_only():
    rows = _rows(b2b_effect=-4.0)
    c = correction.fit(rows, league="nba", version="t")
    i = correction.FEATURES.index("b2b_home")
    per_game = c.margin.coef[i] / c.std[i]
    assert per_game < -1.5
    # The total may not use margin-only inputs.
    for f in correction.FEATURES:
        if f not in correction.TOTAL_FEATURES:
            assert c.total.coef[correction.FEATURES.index(f)] == 0.0


def test_market_weight_is_capped_even_when_the_market_is_perfect():
    rows = _rows(market_noise=0.01)
    c = correction.fit(rows, league="nba", version="t")
    assert c.margin.market_weight == pytest.approx(correction.MARKET_WEIGHT_CAP)
    assert c.total.market_weight == pytest.approx(correction.MARKET_WEIGHT_CAP)


def test_breakdown_adds_up_to_the_adjustment_and_round_trips():
    rows = _rows(total_bias=3.0, b2b_effect=-3.0)
    c = correction.Correction.from_dict(correction.fit(rows, league="nba", version="t").to_dict())
    dm, dt, steps = c.adjust(rows[5]["features"])
    home = sum(s["home"] for s in steps)
    away = sum(s["away"] for s in steps)
    # Steps under 0.05 pts are dropped from the display, so allow a little slack.
    assert home - away == pytest.approx(dm, abs=0.5)
    assert home + away == pytest.approx(dt, abs=0.5)


def test_change_log_is_plain_language():
    rows = _rows(total_bias=4.0, b2b_effect=-4.0)
    c = correction.fit(rows, league="nba", version="t")
    ch = train.describe_changes({"hfa": 2.9, "w_season": 0.8}, {"hfa": 2.4, "w_season": 0.72}, c)
    assert "Home advantage 2.9 -> 2.4 pts" in ch["settings"]
    assert any("Totals were running" in s and "low" in s for s in ch["learned"])
    assert any("back-to-back" in s for s in ch["learned"])


# --- the training log ---------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    return p


def test_active_model_is_the_latest_promoted_run(db):
    with store.connect(db) as conn:
        assert store.active_model_run(conn, "nba") is None
        store.save_model_run(conn, "nba", "v2", promoted=True, model={"correction": None})
        store.save_model_run(conn, "nba", "v3", promoted=False, note="kept out")
        conn.commit()
        assert store.active_model_run(conn, "nba")["version"] == "v2"
        store.save_model_run(conn, "nba", "v4", promoted=True, model={"correction": None})
        conn.commit()
        assert store.active_model_run(conn, "nba")["version"] == "v4"
        log = store.model_runs(conn, "nba")
    assert [r["version"] for r in log] == ["v4", "v3", "v2"]
    assert log[1]["promoted"] is False and log[1]["note"] == "kept out"


def test_back_to_back_only_shows_when_a_team_is_on_one():
    rows = _rows(b2b_effect=-4.0)
    c = correction.fit(rows, league="nba", version="t")
    rested = next(r for r in rows if r["features"]["b2b_home"] == 0)
    tired = next(r for r in rows if r["features"]["b2b_home"] == 1)
    _, _, steps_rested = c.adjust(rested["features"])
    _, _, steps_tired = c.adjust(tired["features"])
    assert not any(s["label"] == "Back-to-back" for s in steps_rested)
    b2b = next(s for s in steps_tired if s["label"] == "Back-to-back")
    assert b2b["home"] - b2b["away"] < -1.5
