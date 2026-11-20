"""The tempo-free efficiency (KenPom-style) model for college basketball."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.model import correction, efficiency  # noqa: E402

L = "ncaamb"
# True per-100 offence / defence and tempo for four teams.
TRUE = {"ncaamb:A": (118, 92, 72), "ncaamb:B": (108, 100, 68), "ncaamb:C": (100, 104, 64), "ncaamb:D": (94, 110, 70)}


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    rnd = random.Random(3)
    teams = list(TRUE)
    with store.connect(p) as conn:
        n = 0
        for day in range(1, 29):
            for i, h in enumerate(teams):
                a = teams[(i + day) % 4]
                if a == h:
                    continue
                (ho, hd, ht), (ao, ad, at) = TRUE[h], TRUE[a]
                poss = ht * at / 68 + rnd.gauss(0, 2)
                hs = round(ho * ad / 100 * poss / 100 * 1.014 + rnd.gauss(0, 4))
                as_ = round(ao * hd / 100 * poss / 100 * 0.986 + rnd.gauss(0, 4))
                n += 1
                store.upsert_game(conn, {
                    "game_id": f"g{n}", "league": L, "season": 2027, "season_type": "regular",
                    "start_time": f"2026-12-{day:02d}T0{i}:00:00+00:00", "home_team": h, "away_team": a,
                    "home_score": hs, "away_score": as_, "completed": 1, "neutral_site": 0,
                    "source": "test", "slate_date": f"2026-12-{day:02d}",
                })
                conn.execute("UPDATE games SET possessions=? WHERE game_id=?", (poss, f"g{n}"))
        conn.commit()
    efficiency.clear_cache()
    return p


def test_ratings_recover_the_order_of_offence_defence_and_tempo(db):
    with store.connect(db) as conn:
        r = efficiency.ratings_as_of(conn, L, 2027, "2026-12-30T00:00:00+00:00")
    assert r.n_games > 80
    order_o = sorted(TRUE, key=lambda t: -r.adj_o[t])
    order_d = sorted(TRUE, key=lambda t: r.adj_d[t])
    order_t = sorted(TRUE, key=lambda t: -r.adj_t[t])
    assert order_o == ["ncaamb:A", "ncaamb:B", "ncaamb:C", "ncaamb:D"]
    assert order_d == ["ncaamb:A", "ncaamb:B", "ncaamb:C", "ncaamb:D"]
    assert order_t[0] == "ncaamb:A" and order_t[-1] == "ncaamb:C"
    p = r.project("ncaamb:A", "ncaamb:D")
    assert p["home_points"] - p["away_points"] > 15


def test_only_games_before_the_day_are_used(db):
    with store.connect(db) as conn:
        early = efficiency.ratings_as_of(conn, L, 2027, "2026-12-10T18:00:00+00:00")
        n_before = conn.execute(
            "SELECT count(*) n FROM games WHERE start_time < '2026-12-10T00:00:00+00:00'"
        ).fetchone()["n"]
    assert early.n_games == n_before


def test_other_leagues_get_nothing(db):
    with store.connect(db) as conn:
        assert efficiency.ratings_as_of(conn, "nba", 2027, "2026-12-30T00:00:00+00:00") is None


def test_models_saved_before_the_efficiency_inputs_still_work():
    d = {
        "league": L, "version": "v2", "mean": [0.0] * 9, "std": [1.0] * 9,
        "margin": {"intercept": 1.0, "coef": [0.5] + [0.0] * 8, "lam": 10.0, "market_weight": 0.0},
        "total": {"intercept": -2.0, "coef": [0.0] * 9, "lam": 10.0, "market_weight": 0.0},
        "n_rows": 100,
    }
    c = correction.Correction.from_dict(d)
    assert c.features == list(correction.LEGACY_FEATURES)
    x = correction.features(base_margin=2.0, base_total=140.0, rating_margin=2.0, games_home=10,
                            games_away=10, rest_home=3, rest_away=3, neutral=False,
                            efficiency_margin=6.0, efficiency_total=150.0)
    dm, dt, _ = c.adjust(x)
    assert dm == pytest.approx(1.0 + 0.5 * 2.0) and dt == pytest.approx(-2.0)
