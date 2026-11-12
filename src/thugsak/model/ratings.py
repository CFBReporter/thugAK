"""Point-in-time team strength ratings, solved from game results.

A ridge-regularised least-squares (Massey-style) fit on margins:

    home_score - away_score  ~=  rating[home] - rating[away] + hfa

solved over every completed game before the as-of cutoff. Ratings are on a
net-points scale, so `rating[a] - rating[b] + hfa` is a predicted margin
directly, and they are shrunk toward zero so an early-season team with two
games does not get an extreme rating off a tiny sample.

This replaces published ratings (ESPN FPI, Barttorvik) as a *model input*
precisely because those are end-of-season snapshots and would leak the result
being predicted. See `model.asof` for the reasoning.

Separate offensive and defensive ratings are solved the same way against
points-for and points-against, which is what the totals model needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from . import asof

# Ridge strength. Chosen so that a team with a single game is pulled most of
# the way back to average, while a team with a full season is barely touched.
# Overridden per league by the fitted value in `calibration` when present.
DEFAULT_LAMBDA = 8.0


@dataclass
class RatingSet:
    """Solved ratings for one league at one as-of instant."""

    as_of: str
    league: str
    net: dict[str, float] = field(default_factory=dict)
    offense: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    n_games: dict[str, int] = field(default_factory=dict)
    hfa: float = 0.0
    mean_points: float = 0.0
    n_total: int = 0

    def rating(self, team_id: str) -> float:
        return self.net.get(team_id, 0.0)

    def games_seen(self, team_id: str) -> int:
        return self.n_games.get(team_id, 0)

    def predicted_margin(self, home: str, away: str, *, neutral: bool = False) -> float:
        """Pure power-rating margin: the anchor shown beside the full model."""
        return self.rating(home) - self.rating(away) + (0.0 if neutral else self.hfa)

    def expected_points(self, team: str, opponent: str, *, is_home: bool,
                        neutral: bool = False) -> float:
        """Opponent-adjusted expected points for one team.

        Offensive strength plus the opponent's defensive concession, re-based
        on the league mean, with home advantage split evenly between sides.
        """
        off = self.offense.get(team, 0.0)
        opp_def = self.defense.get(opponent, 0.0)
        edge = 0.0 if neutral else (self.hfa / 2.0 if is_home else -self.hfa / 2.0)
        return self.mean_points + off + opp_def + edge

    def is_sparse(self, team_id: str, threshold: int = 3) -> bool:
        """True when this team's rating rests on too few games to lean on."""
        return self.games_seen(team_id) < threshold


def _solve_ridge(
    rows: Sequence[tuple[str, str, float, bool]],
    teams: list[str],
    lam: float,
    *,
    fit_hfa: bool,
    hfa: float = 0.0,
) -> tuple[np.ndarray, float]:
    """Solve `target ~ beta[a] - beta[b] (+ hfa)` with an L2 penalty on beta.

    `rows` are (team_a, team_b, target, counts_hfa) tuples. Returns the
    coefficient vector aligned to `teams`, plus the HFA estimate.
    """
    idx = {t: i for i, t in enumerate(teams)}
    n_t = len(teams)
    n_cols = n_t + (1 if fit_hfa else 0)
    X = np.zeros((len(rows), n_cols), dtype=float)
    y = np.zeros(len(rows), dtype=float)

    for r, (a, b, target, counts) in enumerate(rows):
        X[r, idx[a]] += 1.0
        X[r, idx[b]] -= 1.0
        if fit_hfa and counts:
            X[r, n_t] = 1.0
        y[r] = target if fit_hfa or not counts else target - hfa

    # Penalise team coefficients only; the HFA column is left unpenalised so
    # it is estimated from the data rather than shrunk toward zero.
    penalty = np.eye(n_cols) * lam
    if fit_hfa:
        penalty[n_t, n_t] = 0.0

    beta, *_ = np.linalg.lstsq(X.T @ X + penalty, X.T @ y, rcond=None)
    est_hfa = float(beta[n_t]) if fit_hfa else hfa
    coefs = beta[:n_t]
    # Centre so ratings read as "points better than an average team".
    coefs = coefs - coefs.mean()
    return coefs, est_hfa


def solve(
    games: Sequence[Any],
    league: str,
    as_of: str,
    *,
    lam: float = DEFAULT_LAMBDA,
    hfa: float | None = None,
) -> RatingSet:
    """Fit ratings from completed games strictly before `as_of`.

    `games` rows need: home_team, away_team, home_score, away_score,
    start_time, neutral_site. Rows at or after the cutoff raise rather than
    being dropped, so a caller passing an ungated query fails fast.
    """
    asof.require_as_of(as_of)
    asof.assert_before(games, as_of)

    played = [
        g for g in games
        if g["home_score"] is not None and g["away_score"] is not None
    ]
    rs = RatingSet(as_of=as_of, league=league)
    if not played:
        return rs

    teams = sorted({g["home_team"] for g in played} | {g["away_team"] for g in played})
    counts: dict[str, int] = {t: 0 for t in teams}
    all_points: list[float] = []

    margin_rows: list[tuple[str, str, float, bool]] = []
    off_rows: list[tuple[str, str, float, bool]] = []

    for g in played:
        h, a = g["home_team"], g["away_team"]
        hs, as_ = float(g["home_score"]), float(g["away_score"])
        neutral = bool(g["neutral_site"]) if "neutral_site" in g.keys() else False
        counts[h] += 1
        counts[a] += 1
        all_points += [hs, as_]
        margin_rows.append((h, a, hs - as_, not neutral))

    mean_points = float(np.mean(all_points))

    net, est_hfa = _solve_ridge(
        margin_rows, teams, lam, fit_hfa=(hfa is None), hfa=hfa or 0.0
    )
    rs.net = dict(zip(teams, (float(v) for v in net)))
    rs.hfa = est_hfa if hfa is None else float(hfa)
    rs.mean_points = mean_points
    rs.n_games = counts
    rs.n_total = len(played)

    # Offence/defence: solve points-scored as attacker strength minus the
    # defender's suppression, on the same scale and with the same shrinkage.
    for g in played:
        h, a = g["home_team"], g["away_team"]
        hs, as_ = float(g["home_score"]), float(g["away_score"])
        neutral = bool(g["neutral_site"]) if "neutral_site" in g.keys() else False
        # Attacker's deviation from league mean scoring, home edge removed.
        off_rows.append((h, a, hs - mean_points - (rs.hfa / 2 if not neutral else 0), False))
        off_rows.append((a, h, as_ - mean_points + (rs.hfa / 2 if not neutral else 0), False))

    # Here beta[attacker] is offensive strength and -beta[defender] its
    # defensive concession, so one solve yields both halves.
    off, _ = _solve_ridge(off_rows, teams, lam, fit_hfa=False)
    rs.offense = dict(zip(teams, (float(v) for v in off)))
    rs.defense = {t: -float(v) for t, v in zip(teams, off)}
    return rs


def solve_from_store(
    conn: Any,
    league: str,
    as_of: str,
    *,
    season: int | None = None,
    lam: float | None = None,
    hfa: float | None = None,
    use_cache: bool = True,
) -> RatingSet:
    """Convenience wrapper: read as-of-gated games from the store and solve.

    **`season` should always be passed when predicting a game**, set to that
    game's season. A rating is a within-season measure: rosters turn over
    completely between seasons, so a result from two years ago says little
    about tonight, and pooling seasons quietly credits a team for a squad it
    no longer has.

    Leaving it unbounded has a second, worse consequence: the solve then reads
    whatever history happens to still be in the database, so a prediction
    changes depending on whether the bootstrap seasons have been pruned yet.
    That was a live bug -- it put a three-season average behind an NBA
    prediction ten games into a season, which produced a 22-point projected
    margin on a game the market had at 2.5.

    Early in a season this leaves few games per team, and that is the honest
    position: the ridge shrinks the ratings toward average and the model
    leans on the form windows and the market anchor instead of inventing
    confidence it has not earned.

    Results are cached per (league, as_of, season) because a lookback session
    re-solves the same cutoffs repeatedly.
    """
    from .. import store

    asof.require_as_of(as_of)
    cal = store.get_calibration(conn, league)
    lam = lam if lam is not None else cal.get("ridge_lambda", DEFAULT_LAMBDA)
    if hfa is None and "hfa" in cal:
        hfa = cal["hfa"]

    # The season has to be part of the cache key, or a bounded solve and an
    # unbounded one for the same instant would serve each other's results.
    key = as_of if season is None else f"{as_of}|season={season}"

    if use_cache:
        cached = store.get_ratings(conn, league, key)
        if cached:
            rs = RatingSet(as_of=as_of, league=league, hfa=cal.get("hfa", 0.0))
            rs.net = {k: v[0] for k, v in cached.items()}
            rs.n_games = {k: v[1] for k, v in cached.items()}
            # Offence/defence are not cached; recompute when a total is needed.
            return rs

    games = store.games_before(conn, league, as_of, season=season)
    rs = solve(games, league, as_of, lam=lam, hfa=hfa)
    if use_cache and rs.net:
        store.put_ratings(
            conn, league, key,
            {t: (v, rs.n_games.get(t, 0)) for t, v in rs.net.items()},
        )
    return rs
