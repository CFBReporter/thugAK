"""Regression tests for two bugs that made the predicted score nonsense.

Both were found by reading one live NBA game where the model projected
Oklahoma City by 23 on a game the sportsbook had at Portland by 2.5. Neither
raised an error, and neither showed up in the measured skill figures, because
both do their damage early in a season while the fit is dominated by
mid-season rows.

**1. Ratings solved across every stored season.** `solve_from_store` read all
completed games regardless of season, so ten games into a season a team's
rating was a three-year average. Worse, it made a prediction depend on
whether the bootstrap seasons had been pruned yet -- the same prediction
changed as a side effect of housekeeping.

**2. With/without injury deltas used raw.** A team's game-to-game scoring
swings by around a dozen points, so a three-game-each-side split has a
standard error near ten. Four absent players summed to the -14 point cap for
one team and, with the sign the other way, a *+6.5 point bonus* for the other
-- a team credited for having five players out.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.model import asof, injuries as inj, ratings as R  # noqa: E402

LEAGUE = "nba"
A, B, C, D = (f"{LEAGUE}:AAA", f"{LEAGUE}:BBB", f"{LEAGUE}:CCC", f"{LEAGUE}:DDD")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    return path


def _add(conn, gid, season, day, home, away, hs, aws):
    store.upsert_game(conn, {
        "game_id": gid, "league": LEAGUE, "season": season,
        "season_type": "regular",
        "start_time": f"{day}T19:00:00+00:00",
        "home_team": home, "away_team": away,
        "home_score": hs, "away_score": aws,
        "completed": 1, "neutral_site": 0, "source": "test",
    })


# --------------------------------------------------------------------------
# 1. the season bound on ratings
# --------------------------------------------------------------------------

def _pair(conn, gid, season, day, strong, weak, margin, home_is_strong):
    """One game where `strong` beats `weak` by `margin`.

    Venues alternate across the fixture. With a fixed home side the ridge
    cannot separate team strength from home advantage and attributes the
    whole gap to the venue, which would make the fixture test nothing.
    """
    if home_is_strong:
        _add(conn, gid, season, day, strong, weak, 100 + margin, 100)
    else:
        _add(conn, gid, season, day, weak, strong, 100, 100 + margin)


def _two_season_league(conn):
    """AAA dominated last season; BBB dominates this one.

    Written so the two seasons disagree sharply, because a rating that
    quietly pools them lands between the two and looks perfectly plausible.
    """
    n = 0
    for i in range(12):                     # last season: AAA by 30
        n += 1
        _pair(conn, f"old{n}", 2026, f"2026-01-{i + 1:02d}",
              A, B, 30, i % 2 == 0)
        n += 1
        _pair(conn, f"old{n}", 2026, f"2026-02-{i + 1:02d}",
              C, D, 2, i % 2 == 0)
    for i in range(12):                     # this season: BBB by 30
        n += 1
        _pair(conn, f"new{n}", 2027, f"2026-11-{i + 1:02d}",
              B, A, 30, i % 2 == 0)
        n += 1
        _pair(conn, f"new{n}", 2027, f"2026-12-{i + 1:02d}",
              D, C, 2, i % 2 == 0)
    conn.commit()


def test_unbounded_solve_reads_every_stored_season(db):
    """Pins the behaviour being fixed, so the bound is demonstrably doing
    something rather than being decoration."""
    with store.connect(db) as conn:
        _two_season_league(conn)
        as_of = "2027-06-01T00:00:00+00:00"
        unbounded = R.solve_from_store(conn, LEAGUE, as_of, use_cache=False)
        bounded = R.solve_from_store(
            conn, LEAGUE, as_of, season=2027, use_cache=False
        )
    # The unbounded solve has seen twice as many games for A.
    assert unbounded.games_seen(A) == 2 * bounded.games_seen(A)
    # And the two seasons cancel, so A comes out near average despite being
    # 30 points worse than B all of this season.
    assert abs(unbounded.rating(A) - unbounded.rating(B)) < 5.0
    assert bounded.rating(B) - bounded.rating(A) > 20.0


def test_bounded_solve_ignores_a_prior_season_entirely(db):
    """The point of the fix: a prediction must not change because last
    season's games are still sitting in the database."""
    as_of = "2027-06-01T00:00:00+00:00"
    with store.connect(db) as conn:
        for i in range(12):
            _pair(conn, f"n{i}", 2027, f"2026-11-{i + 1:02d}",
                  B, A, 30, i % 2 == 0)
        conn.commit()
        only_current = R.solve_from_store(
            conn, LEAGUE, as_of, season=2027, use_cache=False
        )
        # Now add a prior season and re-solve with the same bound.
        for i in range(12):
            _pair(conn, f"o{i}", 2026, f"2026-01-{i + 1:02d}",
                  A, B, 30, i % 2 == 0)
        conn.commit()
        with_history = R.solve_from_store(
            conn, LEAGUE, as_of, season=2027, use_cache=False
        )
    assert only_current.rating(A) == pytest.approx(with_history.rating(A))
    assert only_current.games_seen(A) == with_history.games_seen(A)


def test_the_rating_cache_is_keyed_by_season(db):
    """A bounded and an unbounded solve for the same instant must not serve
    each other's results out of the cache."""
    as_of = "2027-06-01T00:00:00+00:00"
    with store.connect(db) as conn:
        _two_season_league(conn)
        bounded = R.solve_from_store(conn, LEAGUE, as_of, season=2027)
        unbounded = R.solve_from_store(conn, LEAGUE, as_of)
        conn.commit()
        # Served from cache the second time around.
        again = R.solve_from_store(conn, LEAGUE, as_of, season=2027)
    assert bounded.games_seen(A) != unbounded.games_seen(A)
    assert again.games_seen(A) == bounded.games_seen(A)


def test_early_season_ratings_shrink_toward_average(db):
    """With few games the ridge should pull everyone toward zero rather than
    extrapolate confidently -- that is the honest early-season answer, and
    the reason bounding by season is safe."""
    with store.connect(db) as conn:
        for i in range(2):
            _pair(conn, f"g{i}", 2027, f"2026-11-{i + 1:02d}",
                  B, A, 30, i % 2 == 0)
        conn.commit()
        rs = R.solve_from_store(
            conn, LEAGUE, "2027-01-01T00:00:00+00:00", season=2027,
            use_cache=False,
        )
    # Two games at +30 would imply ±15 apiece unshrunk.
    assert abs(rs.rating(A)) < 12.0
    assert abs(rs.rating(B)) < 12.0


# --------------------------------------------------------------------------
# 2. shrinking the with/without split
# --------------------------------------------------------------------------

def _ww(delta, n_with, n_without, sd=12.0):
    return inj.WithWithout(
        games_with=n_with, games_without=n_without,
        pts_for_with=100.0 + delta, pts_for_without=100.0,
        scoring_sd=sd,
    )


def test_standard_error_reflects_both_sample_sizes():
    ww = _ww(10.0, 3, 3, sd=12.0)
    assert ww.standard_error == pytest.approx(12.0 * math.sqrt(1 / 3 + 1 / 3))


def test_a_three_game_split_is_almost_entirely_discarded():
    """The core of the fix. A ten-point difference over three games either
    side carries a standard error near ten, so almost none of it survives."""
    ww = _ww(10.0, 3, 3, sd=12.0)
    assert ww.reliability < 0.10
    assert ww.shrunk_delta < 1.0


def test_a_large_sample_is_mostly_believed():
    ww = _ww(10.0, 40, 40, sd=12.0)
    assert ww.reliability > 0.40
    assert ww.shrunk_delta > 4.0


def test_reliability_rises_with_sample_size():
    rels = [_ww(10.0, n, n).reliability for n in (2, 5, 10, 30, 80)]
    assert rels == sorted(rels)


def test_a_quieter_team_yields_a_more_trustworthy_split():
    """Scoring volatility is measured per team because it varies: a split on
    a low-variance side carries more information for the same sample."""
    volatile = _ww(10.0, 5, 5, sd=18.0)
    steady = _ww(10.0, 5, 5, sd=5.0)
    assert steady.reliability > volatile.reliability


def test_the_raw_difference_stays_visible_beside_the_shrunk_one():
    """Shrinking the figure used must not hide the figure observed."""
    ww = _ww(10.0, 3, 3)
    assert ww.adjusted_delta == pytest.approx(10.0)
    assert ww.shrunk_delta != pytest.approx(10.0)


def test_opponent_adjustment_still_applies_before_shrinking():
    ww = _ww(10.0, 5, 5)
    ww.opp_rating_with = 0.0
    ww.opp_rating_without = 4.0   # missed games came against weaker defences
    assert ww.adjusted_delta == pytest.approx(6.0)
    assert ww.shrunk_delta == pytest.approx(6.0 * ww.reliability)


# --------------------------------------------------------------------------
# an absence may never make a team better
# --------------------------------------------------------------------------

class FakeGame:
    """One row of a player's game log, as `injuries` expects to read it."""

    def __init__(self, date, points=12.0):
        self.date = date
        self.opponent = "BBB"
        self.home_away = "vs"
        self.result = "W"
        self.stats = {"points": points, "minutes": 30.0}


class FakeInjury:
    def __init__(self, status="Out"):
        self.athlete_id = "1"
        self.name = "Test Player"
        self.position = "G"
        self.status = status
        self.availability = 0.0
        self.detail = ""


def test_team_adjustment_never_credits_a_team_for_an_absence(db):
    """A favourable with/without split is a small-sample artefact, not a
    benefit. Five absent Oklahoma City players once produced +6.5 points."""
    with store.connect(db) as conn:
        # The team scored far more in the games this player missed.
        for i in range(4):
            _add(conn, f"with{i}", 2027, f"2026-11-{i + 1:02d}", A, B, 90, 100)
        for i in range(4):
            _add(conn, f"out{i}", 2027, f"2026-11-{i + 10:02d}", A, B, 125, 100)
        conn.commit()
        as_of = "2026-12-01T00:00:00+00:00"
        played = [f"2026-11-{i + 1:02d}" for i in range(4)]
        impact = inj.assess(
            conn, injury=FakeInjury(), team_id=A, as_of=as_of,
            player_games=[FakeGame(d) for d in played],
            team_window_points=105.0,
        )
    assert impact.points_adjustment <= 0.0
    assert any("artefact" in r for r in impact.rationale)


def test_a_genuine_loss_still_registers_as_negative(db):
    """The clamp must not neuter the signal it exists to protect."""
    with store.connect(db) as conn:
        for i in range(8):
            _add(conn, f"with{i}", 2027, f"2026-11-{i + 1:02d}", A, B, 120, 100)
        for i in range(8):
            _add(conn, f"out{i}", 2027, f"2026-12-{i + 1:02d}", A, B, 95, 100)
        conn.commit()
        played = [f"2026-11-{i + 1:02d}" for i in range(8)]
        impact = inj.assess(
            conn, injury=FakeInjury(), team_id=A,
            as_of="2027-01-05T00:00:00+00:00",
            player_games=[FakeGame(d) for d in played],
            team_window_points=107.0,
        )
    assert impact.points_adjustment < 0.0


def test_confidence_tracks_how_much_survived_shrinkage():
    """Four games either side of a twelve-point swing is still not a
    measurement, so confidence follows reliability rather than raw n."""
    assert _ww(10.0, 3, 3, sd=14.0).reliability < 0.15     # -> low
    assert _ww(10.0, 25, 25, sd=10.0).reliability > 0.35   # -> high


def test_aggregate_cap_is_a_backstop_not_a_routine_clamp(db):
    """After shrinkage a normal injury report should land far inside the cap.
    It binding is now a signal, not a Tuesday."""
    impacts = []
    for _ in range(4):
        imp = inj.InjuryImpact(
            athlete_id="x", name="n", position="G", status="Out",
            availability=0.0,
        )
        imp.points_adjustment = -1.5
        impacts.append(imp)
    total, _conf = inj.team_adjustment(impacts)
    assert total == pytest.approx(-6.0)
    assert total > -14.0
