"""Tests for the ratings solve, injuries, flow weighting and retention.

These use synthetic data with known answers, because the point is to catch a
model that is confidently wrong rather than one that crashes.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.analysis import flow  # noqa: E402
from thugsak.model import asof, injuries, ratings  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def tid(league: str, name: str) -> str:
    """Canonical team id, matching what the real importer writes."""
    return f"{league}:{name}"


def _game(gid, league, home, away, hs, as_, day, season=2026, neutral=0):
    """A game row shaped exactly as the importer produces one.

    Team ids are league-qualified here because that is what `store` keys on;
    using bare names in tests would pass against a schema the app never uses.
    """
    return {
        "game_id": gid,
        "league": league,
        "season": season,
        "season_type": "regular",
        # The year follows the season, as real data does. Sharing one year
        # across seasons would place different-season games on the same date,
        # where the store's fixture de-duplication correctly merges them.
        "start_time": f"{season}-10-{day:02d}T00:00:00+00:00",
        "home_team": tid(league, home),
        "away_team": tid(league, away),
        "home_score": hs,
        "away_score": as_,
        "completed": 1,
        "neutral_site": neutral,
        "source": "test",
    }


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    return path


# --------------------------------------------------------------------------
# ratings
# --------------------------------------------------------------------------

def test_ratings_recover_known_strength_order():
    """A round robin where A > B > C by construction must solve in that order."""
    true = {"A": 10.0, "B": 0.0, "C": -10.0}
    games, day, n = [], 1, 0
    for home in true:
        for away in true:
            if home == away:
                continue
            margin = true[home] - true[away]
            games.append(
                _game(f"g{n}", "x", home, away, 100 + margin, 100, day, neutral=1)
            )
            n += 1
            day = day % 28 + 1
    rs = ratings.solve(games, "x", "2026-12-01T00:00:00+00:00")
    assert rs.rating(tid("x", "A")) > rs.rating(tid("x", "B")) > rs.rating(tid("x", "C"))


def test_ratings_are_centred_on_zero():
    """Ratings read as 'points better than average', so they must sum to ~0."""
    games = [
        _game("g1", "x", "A", "B", 30, 20, 1, neutral=1),
        _game("g2", "x", "B", "C", 25, 24, 2, neutral=1),
        _game("g3", "x", "C", "A", 10, 40, 3, neutral=1),
    ]
    rs = ratings.solve(games, "x", "2026-12-01T00:00:00+00:00")
    assert sum(rs.net.values()) == pytest.approx(0.0, abs=1e-6)


def test_ratings_estimate_home_advantage():
    """With every home team winning by 6 between evenly matched sides, the
    solve should attribute that to the venue, not to team strength."""
    games, day, n = [], 1, 0
    teams = ["A", "B", "C", "D"]
    for home in teams:
        for away in teams:
            if home == away:
                continue
            games.append(_game(f"g{n}", "x", home, away, 26, 20, day))
            n += 1
            day = day % 28 + 1
    rs = ratings.solve(games, "x", "2026-12-01T00:00:00+00:00")
    assert rs.hfa == pytest.approx(6.0, abs=1.0)
    for t in teams:
        assert abs(rs.rating(tid("x", t))) < 1.0


def test_shrinkage_pulls_small_samples_toward_average():
    """One blowout must not produce an extreme rating."""
    games = [_game("g1", "x", "A", "B", 60, 0, 1, neutral=1)]
    rs = ratings.solve(games, "x", "2026-12-01T00:00:00+00:00")
    assert abs(rs.rating(tid("x", "A"))) < 60.0


def test_solve_rejects_games_at_or_after_cutoff():
    """An ungated query must fail loudly rather than leak the result."""
    games = [_game("g1", "x", "A", "B", 30, 20, 20)]
    with pytest.raises(asof.LookaheadError):
        ratings.solve(games, "x", "2026-10-20T00:00:00+00:00")


def test_empty_history_returns_empty_ratings():
    rs = ratings.solve([], "x", "2026-12-01T00:00:00+00:00")
    assert rs.net == {}


# --------------------------------------------------------------------------
# as-of isolation (the anti-lookahead test from the plan)
# --------------------------------------------------------------------------

def test_prediction_inputs_identical_whether_later_games_exist(db):
    """The core guarantee: adding games *after* the cutoff must not change
    anything the model sees. Without this, a lookback silently grades itself
    against data it should not have."""
    league = "nfl"
    early = [
        _game("e1", league, "A", "B", 24, 17, 1),
        _game("e2", league, "B", "C", 20, 14, 5),
        _game("e3", league, "C", "A", 10, 31, 9),
        _game("e4", league, "A", "C", 27, 20, 12),
    ]
    with store.connect(db) as conn:
        for g in early:
            store.upsert_game(conn, g)
        conn.commit()
        cutoff = "2026-10-15T00:00:00+00:00"
        before = [dict(r) for r in store.games_before(conn, league, cutoff)]
        rs_before = ratings.solve(
            store.games_before(conn, league, cutoff), league, cutoff
        )

        # Now add games the model must not be able to see.
        for g in [
            _game("l1", league, "A", "B", 45, 3, 20),
            _game("l2", league, "C", "B", 50, 0, 25),
        ]:
            store.upsert_game(conn, g)
        conn.commit()

        after = [dict(r) for r in store.games_before(conn, league, cutoff)]
        rs_after = ratings.solve(
            store.games_before(conn, league, cutoff), league, cutoff
        )

    assert before == after
    assert rs_before.net == rs_after.net
    assert rs_before.hfa == rs_after.hfa


def test_team_windows_respect_the_cutoff(db):
    with store.connect(db) as conn:
        for g in [
            _game("a", "nfl", "A", "B", 10, 7, 1),
            _game("b", "nfl", "A", "C", 20, 7, 10),
            _game("c", "nfl", "A", "D", 30, 7, 20),
        ]:
            store.upsert_game(conn, g)
        conn.commit()
        rows = store.team_games_before(conn, "nfl:A", "2026-10-15T00:00:00+00:00")
    assert len(rows) == 2
    assert all(r["start_time"] < "2026-10-15" for r in rows)


def test_limit_returns_the_most_recent_games(db):
    """`limit=7` must mean the last 7, not an arbitrary 7."""
    with store.connect(db) as conn:
        for i in range(1, 11):
            store.upsert_game(conn, _game(f"g{i}", "nfl", "A", "B", 20 + i, 10, i))
        conn.commit()
        rows = store.team_games_before(
            conn, "nfl:A", "2026-11-01T00:00:00+00:00", limit=3
        )
    assert [r["points_for"] for r in rows] == [30, 29, 28]


# --------------------------------------------------------------------------
# injuries
# --------------------------------------------------------------------------

def test_with_without_split_identifies_missed_games(db):
    """A player's log lists only games they played, so differencing it against
    the team's schedule reveals the absences -- the basis of the signal."""
    with store.connect(db) as conn:
        for i in range(1, 9):
            store.upsert_game(
                conn,
                _game(f"g{i}", "nfl", "A", "B", 30 if i <= 4 else 13, 20, i),
            )
        conn.commit()
        played = [f"2026-10-0{i}" for i in range(1, 5)]
        ww = injuries.with_without_split(
            conn, "nfl:A", "2026-11-01T00:00:00+00:00", played
        )
    assert ww.games_with == 4
    assert ww.games_without == 4
    assert ww.pts_for_with == pytest.approx(30.0)
    assert ww.pts_for_without == pytest.approx(13.0)
    assert ww.raw_delta == pytest.approx(17.0)
    assert ww.reportable


def test_split_is_suppressed_below_minimum_sample(db):
    """Two games either side is the floor; one is noise wearing a costume."""
    with store.connect(db) as conn:
        for i in range(1, 4):
            store.upsert_game(conn, _game(f"g{i}", "nfl", "A", "B", 24, 20, i))
        conn.commit()
        ww = injuries.with_without_split(
            conn, "nfl:A", "2026-11-01T00:00:00+00:00", ["2026-10-01"]
        )
    assert ww.games_with == 1
    assert not ww.reportable
    assert "Not enough sample" in ww.describe()


def test_availability_weighting_by_status():
    class I:
        pass

    from thugsak.clients.espn import Injury

    out = Injury("1", "X", "QB", "Out")
    quest = Injury("2", "Y", "QB", "Questionable")
    active = Injury("3", "Z", "QB", "Active")
    assert out.availability == 0.0 and out.is_definitely_out
    assert 0 < quest.availability < 1
    assert active.availability == 1.0
    # Healthy players appear in these feeds; they are not news.
    assert out.matters and quest.matters and not active.matters


def test_team_adjustment_is_capped():
    """A long injury list must not drive a team's projection absurdly
    negative; injuries do not stack linearly."""
    many = [
        injuries.InjuryImpact("i", "P", "QB", "Out", 0.0, points_adjustment=-8.0)
        for _ in range(6)
    ]
    total, conf = injuries.team_adjustment(many)
    assert total == -14.0
    assert conf == "low"


def test_no_injuries_means_no_adjustment():
    total, conf = injuries.team_adjustment([])
    assert total == 0.0 and conf == "none"


# --------------------------------------------------------------------------
# flow
# --------------------------------------------------------------------------

class FakeTrade:
    def __init__(self, count, price, side, hours_ago=1.0, block=False):
        self.trade_id = f"t{count}{side}{hours_ago}"
        self.count = count
        self.yes_price = price
        self.no_price = 1 - price
        self.taker_side = side
        self.is_block_trade = block
        self.created_time = (
            datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        ).isoformat()

    @property
    def notional(self):
        p = self.yes_price if self.taker_side == "yes" else self.no_price
        return float(p) * self.count


def test_one_whale_outweighs_many_minnows():
    """The explicit requirement: bigger bets get more attention. One
    3,000-contract YES must beat 100 ten-contract NOs."""
    trades = [FakeTrade(3000, 0.5, "yes")] + [
        FakeTrade(10, 0.5, "no") for _ in range(100)
    ]
    fa = flow.analyze_trades("T", trades)
    assert fa.imbalance > 0
    assert fa.direction == "yes"
    # By headcount the NOs dominate, which is exactly the distinction.
    assert fa.count_imbalance < 0


def test_notional_weighting_accounts_for_price():
    """1,000 contracts at 3c is a $30 punt; 100 at 90c is a $90 position."""
    cheap = FakeTrade(1000, 0.03, "yes")
    dear = FakeTrade(100, 0.90, "yes")
    assert dear.notional > cheap.notional


def test_time_decay_favours_recent_money():
    fresh = flow.analyze_trades(
        "T", [FakeTrade(100, 0.5, "yes", 0.5), FakeTrade(100, 0.5, "no", 72)]
    )
    stale = flow.analyze_trades(
        "T", [FakeTrade(100, 0.5, "yes", 72), FakeTrade(100, 0.5, "no", 0.5)]
    )
    assert fresh.imbalance > 0 > stale.imbalance


def test_block_trades_reported_separately():
    trades = [FakeTrade(500, 0.5, "yes", block=True), FakeTrade(10, 0.5, "no")]
    fa = flow.analyze_trades("T", trades)
    assert len(fa.blocks) == 1
    assert fa.block_notional > 0


def test_thin_market_reports_insufficient_rather_than_a_signal():
    fa = flow.analyze_trades("T", [FakeTrade(1, 0.5, "yes")])
    assert fa.conviction == "insufficient"
    assert "Too little flow" in fa.summary()


def test_no_trades_is_handled():
    fa = flow.analyze_trades("T", [])
    assert fa.n_trades == 0
    assert fa.conviction == "insufficient"


def test_whale_threshold_is_relative_to_the_market():
    """A whale on a thin market is a minnow on a liquid one, so the threshold
    comes from the market's own distribution."""
    thin = flow.analyze_trades("T", [FakeTrade(c, 0.5, "yes") for c in range(1, 21)])
    liquid = flow.analyze_trades(
        "T", [FakeTrade(c * 100, 0.5, "yes") for c in range(1, 21)]
    )
    assert liquid.whale_threshold > thin.whale_threshold * 10


class FakeCandle:
    def __init__(self, close, oi):
        self.close = close
        self.open_interest = oi
        self.mean = close
        self.high = close
        self.low = close
        self.volume = 100.0


def test_rising_price_with_rising_oi_reads_as_new_money():
    fa = flow.analyze_trades("T", [FakeTrade(100, 0.5, "yes")])
    fa = flow.analyze_open_interest(fa, [FakeCandle(0.40, 1000), FakeCandle(0.55, 5000)])
    assert fa.oi_strength == "strong-yes"


def test_rising_price_with_falling_oi_reads_as_covering():
    """Looks identical on a price chart; the open-interest direction is what
    separates conviction from position-closing."""
    fa = flow.analyze_trades("T", [FakeTrade(100, 0.5, "yes")])
    fa = flow.analyze_open_interest(fa, [FakeCandle(0.40, 5000), FakeCandle(0.55, 1000)])
    assert fa.oi_strength == "weak-yes"


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------

def test_prune_keeps_calibration_and_the_current_season(db):
    """The whole retention design rests on this: the fitted constants survive
    deletion of the data they came from."""
    with store.connect(db) as conn:
        for season, gid in ((2024, "a"), (2025, "b"), (2026, "c")):
            store.upsert_game(
                conn, _game(gid, "nfl", "A", "B", 24, 20, 1, season=season)
            )
        store.put_calibration(
            conn, "nfl",
            {"sigma_margin": 13.4, "hfa": 1.58, "w_last7": 0.45},
            n_games=3, seasons=[2024, 2025, 2026],
        )
        conn.commit()

        before = store.get_calibration(conn, "nfl")
        removed = store.prune_seasons(conn, "nfl", [2026])
        conn.commit()

        after = store.get_calibration(conn, "nfl")
        remaining = store.count_games(conn, "nfl")

    assert removed == 2
    assert after == before          # the distilled knowledge is untouched
    assert set(remaining) == {2026}


def test_prune_cascades_to_team_rows(db):
    with store.connect(db) as conn:
        store.upsert_game(conn, _game("old", "nfl", "A", "B", 24, 20, 1, season=2024))
        store.upsert_game(conn, _game("new", "nfl", "A", "B", 24, 20, 1, season=2026))
        conn.commit()
        store.prune_seasons(conn, "nfl", [2026])
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) c FROM team_game_stats WHERE game_id='old'"
        ).fetchone()["c"]
    assert n == 0


def test_predictions_survive_pruning(db):
    """Predictions are the user's own record and must never be pruned."""
    with store.connect(db) as conn:
        store.upsert_game(conn, _game("g", "nfl", "A", "B", 24, 20, 1, season=2024))
        store.save_prediction(
            conn,
            {
                "game_id": "g", "league": "nfl",
                "as_of": "2026-10-01T00:00:00+00:00",
                "pred_spread": 3.0, "pred_total": 44.0,
            },
        )
        conn.commit()
        store.prune_seasons(conn, "nfl", [2026])
        conn.commit()
        rows = store.predictions_for(conn, "g")
    assert len(rows) == 1


def test_trim_to_rolling_keeps_exactly_n_per_team(db):
    with store.connect(db) as conn:
        for i in range(1, 13):
            store.upsert_game(conn, _game(f"g{i}", "nfl", "A", "B", 20, 10, i))
        conn.commit()
        store.trim_to_rolling(conn, "nfl", keep=7)
        conn.commit()
        rows = store.team_games_before(
            conn, "nfl:A", "2026-11-01T00:00:00+00:00"
        )
    assert len(rows) == 7
    # And they must be the most recent seven.
    assert rows[0]["start_time"].startswith("2026-10-12")
