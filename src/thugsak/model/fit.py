"""Fit the model's constants from the bootstrap seasons.

This is the step that turns guessed parameters into measured ones, and its
output -- a few dozen rows in the `calibration` table -- is the only thing that
survives pruning. Everything fitted here:

    hfa            mean home-field advantage, in points
    mean_total     league average combined score
    mean_points    league average points by one team
    w_last3        blend weight on the last-3-game window
    w_last7        blend weight on the last-7-game window
    w_season       blend weight on the season-to-date window
    sigma_margin   residual SD of margin -- converts a spread to a probability
    sigma_total    residual SD of total  -- converts a total to a probability
    ridge_lambda   shrinkage used by the ratings solve
    form_r2        how much recent form actually explains (diagnostic)

sigma matters most: it is what maps a predicted total onto a probability at
every strike of a Kalshi ladder, so an inaccurate sigma corrupts every price
comparison even when the point estimate is good.

Fitting is walk-forward throughout -- each training row is built only from
games earlier than the game it describes, with opponent strength taken from a
weekly rating snapshot that predates it. Without that, fitted weights would be
tuned against leaked results and the live model would underperform them.
"""

from __future__ import annotations

import bisect
import itertools
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable, Sequence

import numpy as np

from .. import store
from . import asof, ratings

# Resolution of the blend-weight grid search over the 3-simplex.
WEIGHT_STEP = 0.05
# How often to re-solve ratings while building training rows. Weekly keeps the
# point-in-time property while costing ~20 solves per season instead of one
# per game.
SNAPSHOT_DAYS = 7
# A training row needs at least this many prior games for both teams.
MIN_PRIOR_GAMES = 3


@dataclass
class TrainingRow:
    game_id: str
    start_time: str
    home: str
    away: str
    neutral: bool
    actual_home: float
    actual_away: float
    # Opponent-adjusted window estimates. Both scoring and conceding are kept
    # for each side, because the predictor combines a team's offence with its
    # opponent's defence -- if the fit used only the offensive half, it would
    # be tuning weights for a different model than the one that ships.
    home_windows: dict[str, float] = field(default_factory=dict)
    away_windows: dict[str, float] = field(default_factory=dict)
    home_def_windows: dict[str, float] = field(default_factory=dict)
    away_def_windows: dict[str, float] = field(default_factory=dict)

    @property
    def actual_margin(self) -> float:
        return self.actual_home - self.actual_away

    @property
    def actual_total(self) -> float:
        return self.actual_home + self.actual_away


def _team_history(conn, league: str) -> dict[str, list[dict[str, Any]]]:
    """All completed team-game rows for a league, grouped by team, time-ordered.

    Loaded once into memory because the walk-forward build touches each team's
    prior games for every game they played; doing that in SQL would be tens of
    thousands of queries.
    """
    hist: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        # Completed games only: an unplayed fixture carries ESPN's 0-0
        # placeholder, and fitting on it teaches the model teams score zero.
        """SELECT t.team_id, t.opponent_id, t.start_time, t.is_home,
                  t.neutral_site, t.season, t.points_for, t.points_against
           FROM team_game_stats t
           JOIN games g ON g.game_id = t.game_id
           WHERE t.league=? AND t.points_for IS NOT NULL AND g.completed = 1
           ORDER BY t.team_id, t.start_time""",
        (league,),
    )
    for r in rows:
        hist.setdefault(r["team_id"], []).append(dict(r))
    return hist


def _prior(
    hist: dict[str, list[dict[str, Any]]],
    team: str,
    before: str,
    k: int | None,
    season: int | None = None,
) -> list[dict[str, Any]]:
    """A team's games strictly before `before`, most recent first, capped at k.

    `season` confines the window exactly as `gamelog.team_form` does at
    predict time. Fitting on multi-season windows and then predicting from
    single-season ones would tune the weights for a different model -- the
    same fit/predict mismatch that previously made the totals model worse
    than a constant.
    """
    games = hist.get(team, [])
    if season is not None:
        games = [g for g in games if g.get("season") == season]
    times = [g["start_time"] for g in games]
    cut = bisect.bisect_left(times, before)
    window = games[:cut][::-1]
    return window[:k] if k else window


def _adjusted_points(
    games: Sequence[dict[str, Any]],
    rs: ratings.RatingSet | None,
    *,
    conceded: bool = False,
) -> float | None:
    """Mean points scored (or conceded), adjusted for opponent quality.

    Raw scoring averages mislead: 30 points against the league's worst defence
    is not the same signal as 30 against its best. Each game is re-based by the
    opponent's rating on the relevant side before averaging.
    """
    if not games:
        return None
    col = "points_against" if conceded else "points_for"
    vals: list[float] = []
    for g in games:
        pts = float(g[col])
        if rs is not None:
            opp = g["opponent_id"]
            # Scoring is judged against the opponent's defence; conceding
            # against their offence.
            pts -= rs.offense.get(opp, 0.0) if conceded else rs.defense.get(opp, 0.0)
        vals.append(pts)
    return float(np.mean(vals))


def _snapshot_cutoffs(games: Sequence[Any]) -> list[str]:
    if not games:
        return []
    times = sorted(asof.parse_ts(g["start_time"]) for g in games)
    out, cur, end = [], times[0], times[-1]
    while cur <= end + timedelta(days=SNAPSHOT_DAYS):
        out.append(asof.iso(cur))
        cur += timedelta(days=SNAPSHOT_DAYS)
    return out


def build_training_rows(
    conn, league: str, *, seasons: Iterable[int] | None = None, verbose: bool = False
) -> list[TrainingRow]:
    """Walk forward through history, building one row per predictable game."""
    q = "SELECT * FROM games WHERE league=? AND completed=1"
    args: list[Any] = [league]
    seasons = list(seasons) if seasons else None
    if seasons:
        q += f" AND season IN ({','.join('?' * len(seasons))})"
        args += seasons
    q += " ORDER BY start_time"
    games = list(conn.execute(q, args))
    if not games:
        return []

    hist = _team_history(conn, league)

    # Pre-solve ratings at weekly cutoffs, then attach the latest snapshot
    # strictly earlier than each game.
    #
    # Snapshots are solved **within a season**, matching what `solve_from_store`
    # does at predict time. Solving them cumulatively across the bootstrap
    # seasons would tune every weight against ratings the live model never
    # sees, and the resulting weights would be fitted for a different model
    # than the one that runs -- the same class of mismatch that already cost
    # a measurable chunk of spread skill once.
    by_season: dict[Any, list[Any]] = {}
    for g in games:
        by_season.setdefault(g["season"], []).append(g)

    snapshots: dict[Any, dict[str, ratings.RatingSet]] = {}
    snap_keys: dict[Any, list[str]] = {}
    for season, sgames in by_season.items():
        snaps: dict[str, ratings.RatingSet] = {}
        for c in _snapshot_cutoffs(sgames):
            prior = [g for g in sgames if g["start_time"] < c]
            if len(prior) >= 20:
                snaps[c] = ratings.solve(prior, league, c)
        snapshots[season] = snaps
        snap_keys[season] = sorted(snaps)

    if verbose:
        n_snaps = sum(len(v) for v in snapshots.values())
        print(
            f"  {league}: {len(games)} games, {n_snaps} rating snapshots "
            f"across {len(by_season)} season(s)"
        )

    def snapshot_for(ts: str, season: Any) -> ratings.RatingSet | None:
        keys = snap_keys.get(season) or []
        i = bisect.bisect_left(keys, ts)
        return snapshots[season][keys[i - 1]] if i > 0 else None

    rows: list[TrainingRow] = []
    for g in games:
        if g["home_score"] is None or g["away_score"] is None:
            continue
        ts = g["start_time"]
        season = g["season"]
        rs = snapshot_for(ts, season)
        h, a = g["home_team"], g["away_team"]
        h_prior_all = _prior(hist, h, ts, None, season)
        a_prior_all = _prior(hist, a, ts, None, season)
        if len(h_prior_all) < MIN_PRIOR_GAMES or len(a_prior_all) < MIN_PRIOR_GAMES:
            continue

        def windows(prior: list[dict[str, Any]], conceded: bool) -> dict[str, float]:
            return {
                "last3": _adjusted_points(prior[:3], rs, conceded=conceded),
                "last7": _adjusted_points(prior[:7], rs, conceded=conceded),
                "season": _adjusted_points(prior, rs, conceded=conceded),
            }  # type: ignore[return-value]

        hw = windows(h_prior_all, False)
        aw = windows(a_prior_all, False)
        hd = windows(h_prior_all, True)
        ad = windows(a_prior_all, True)
        if any(
            v is None
            for d in (hw, aw, hd, ad)
            for v in d.values()
        ):
            continue

        rows.append(
            TrainingRow(
                game_id=g["game_id"],
                start_time=ts,
                home=h,
                away=a,
                neutral=bool(g["neutral_site"]),
                actual_home=float(g["home_score"]),
                actual_away=float(g["away_score"]),
                home_windows=hw,
                away_windows=aw,
                home_def_windows=hd,
                away_def_windows=ad,
            )
        )
    return rows


def _simplex_grid(step: float = WEIGHT_STEP) -> list[tuple[float, float, float]]:
    n = int(round(1.0 / step))
    out = []
    for i, j in itertools.product(range(n + 1), repeat=2):
        if i + j <= n:
            out.append((i * step, j * step, (n - i - j) * step))
    return out


WINDOW_KEYS = ("last3", "last7", "season")


def _project(
    rows: Sequence[TrainingRow], weights: Sequence[float], hfa: float
) -> tuple[Any, Any]:
    """Predicted (home_points, away_points) under a weight vector.

    Mirrors `predict.predict_game` exactly: a side's expectation is the mean
    of its own adjusted scoring and its opponent's adjusted conceding, with
    home advantage split so the venue shifts the spread but not the total.
    Keeping the two in step is what makes the fitted weights and sigma
    actually describe the shipped model.
    """
    wv = np.asarray(weights, dtype=float)
    h_off = np.array([[r.home_windows[k] for k in WINDOW_KEYS] for r in rows]) @ wv
    a_off = np.array([[r.away_windows[k] for k in WINDOW_KEYS] for r in rows]) @ wv
    h_def = np.array([[r.home_def_windows[k] for k in WINDOW_KEYS] for r in rows]) @ wv
    a_def = np.array([[r.away_def_windows[k] for k in WINDOW_KEYS] for r in rows]) @ wv
    edge = np.array([0.0 if r.neutral else hfa / 2.0 for r in rows])
    ph = (h_off + a_def) / 2.0 + edge
    pa = (a_off + h_def) / 2.0 - edge
    return ph, pa


def fit_weights(
    rows: Sequence[TrainingRow], hfa: float
) -> tuple[dict[str, float], float]:
    """Grid-search the window blend that minimises total+margin error.

    Both objectives matter -- the app predicts a total and a spread, and each
    maps onto its own Kalshi ladder -- so the search minimises their combined
    RMSE rather than favouring one.
    """
    if not rows:
        return {"w_last3": 0.5, "w_last7": 0.3, "w_season": 0.2}, float("inf")

    act_total = np.array([r.actual_total for r in rows])
    act_margin = np.array([r.actual_margin for r in rows])

    best, best_err = None, float("inf")
    for w in _simplex_grid():
        ph, pa = _project(rows, w, hfa)
        err_t = np.sqrt(np.mean((ph + pa - act_total) ** 2))
        err_m = np.sqrt(np.mean((ph - pa - act_margin) ** 2))
        err = err_t + err_m
        if err < best_err:
            best, best_err = w, err
    assert best is not None
    return {"w_last3": best[0], "w_last7": best[1], "w_season": best[2]}, best_err


def fit_sigma(
    rows: Sequence[TrainingRow], weights: dict[str, float], hfa: float
) -> dict[str, float]:
    """Residual SDs under the fitted weights.

    Deliberately the SD of *residuals*, not of raw outcomes: it must describe
    how wrong this model is, which is what a probability at a ladder strike
    depends on. A bias term is reported too -- a systematic offset in totals
    would otherwise hide inside a healthy-looking SD.
    """
    if not rows:
        return {"sigma_margin": 13.5, "sigma_total": 10.0}
    ph, pa = _project(rows, [weights[f"w_{k}"] for k in WINDOW_KEYS], hfa)
    res_m = np.array([r.actual_margin for r in rows]) - (ph - pa)
    res_t = np.array([r.actual_total for r in rows]) - (ph + pa)
    return {
        "sigma_margin": float(np.std(res_m, ddof=1)),
        "sigma_total": float(np.std(res_t, ddof=1)),
        "mae_margin": float(np.mean(np.abs(res_m))),
        "mae_total": float(np.mean(np.abs(res_t))),
        "bias_margin": float(np.mean(res_m)),
        "bias_total": float(np.mean(res_t)),
    }


def fit_basics(conn, league: str, seasons: Iterable[int] | None = None) -> dict[str, float]:
    """League scoring levels and home-field advantage.

    HFA is taken from the **ridge solve**, not from the mean of home margins.
    The naive mean is unbiased only when the schedule is balanced, which it is
    in the NFL (everyone plays eight at home) but emphatically is not in
    college sports: major programmes buy home games against weaker opponents,
    so a raw home-margin average credits the venue for what is really a
    mismatch. Measured on this data the two differ by several points for
    NCAAMB, which would feed straight into every predicted spread.

    Both figures are stored so the difference stays visible.
    """
    q = """SELECT * FROM games
           WHERE league=? AND completed=1 AND home_score IS NOT NULL"""
    args: list[Any] = [league]
    seasons = list(seasons) if seasons else None
    if seasons:
        q += f" AND season IN ({','.join('?' * len(seasons))})"
        args += seasons
    rows = list(conn.execute(q, args))
    if not rows:
        return {}

    naive = [r["home_score"] - r["away_score"] for r in rows if not r["neutral_site"]]
    totals = [r["home_score"] + r["away_score"] for r in rows]
    pts = [r["home_score"] for r in rows] + [r["away_score"] for r in rows]

    # Solve once over the whole window, letting the ridge fit separate team
    # strength from home advantage. as_of is set past the last game so the
    # guard permits the full set.
    hfa_ridge = None
    try:
        last = max(r["start_time"] for r in rows)
        cutoff = asof.iso(asof.parse_ts(last) + timedelta(days=1))
        rs = ratings.solve(rows, league, cutoff)
        hfa_ridge = rs.hfa
    except Exception:
        pass

    naive_mean = float(np.mean(naive)) if naive else 0.0
    return {
        "hfa": float(hfa_ridge) if hfa_ridge is not None else naive_mean,
        "hfa_naive_mean": naive_mean,
        "mean_total": float(np.mean(totals)),
        "mean_points": float(np.mean(pts)),
        "sd_total_raw": float(np.std(totals, ddof=1)),
        "sd_margin_raw": float(np.std(naive, ddof=1)) if len(naive) > 1 else 0.0,
        "n_basis": float(len(rows)),
    }


def fit_league(
    conn, league: str, *, seasons: Iterable[int] | None = None, verbose: bool = True
) -> dict[str, float]:
    """Fit everything for one league and persist it to `calibration`."""
    seasons = list(seasons) if seasons else None
    basics = fit_basics(conn, league, seasons)
    if not basics:
        if verbose:
            print(f"  {league}: no completed games, nothing to fit")
        return {}

    rows = build_training_rows(conn, league, seasons=seasons, verbose=verbose)
    hfa = basics["hfa"]
    weights, _ = fit_weights(rows, hfa)
    sigmas = fit_sigma(rows, weights, hfa)

    # Diagnostic: how much better is the fitted blend than a flat season
    # average? If this is near zero, recent form carries little signal for
    # this league and the windows are mostly decoration.
    flat = {"w_last3": 0.0, "w_last7": 0.0, "w_season": 1.0}
    flat_sig = fit_sigma(rows, flat, hfa) if rows else sigmas
    improvement = (
        (flat_sig["sigma_total"] - sigmas["sigma_total"]) / flat_sig["sigma_total"]
        if rows and flat_sig["sigma_total"]
        else 0.0
    )

    # How much the model beats simply predicting the league average, as a
    # variance-explained share. This is the honest measure of whether the
    # model is worth consulting at all, and it is stored so the UI can show it
    # beside every prediction rather than leaving the reader to assume skill.
    # A negative value means a constant would do better.
    raw_m = basics.get("sd_margin_raw") or 0.0
    raw_t = basics.get("sd_total_raw") or 0.0
    skill_margin = (
        1.0 - (sigmas["sigma_margin"] / raw_m) ** 2 if raw_m > 0 else 0.0
    )
    skill_total = 1.0 - (sigmas["sigma_total"] / raw_t) ** 2 if raw_t > 0 else 0.0

    values: dict[str, float] = {
        **basics,
        **weights,
        **sigmas,
        "ridge_lambda": ratings.DEFAULT_LAMBDA,
        "form_gain": float(improvement),
        "skill_margin": float(skill_margin),
        "skill_total": float(skill_total),
        "n_training_rows": float(len(rows)),
    }
    store.put_calibration(
        conn, league, values, n_games=int(basics["n_basis"]), seasons=seasons
    )

    if verbose:
        print(f"  {league}: fitted from {len(rows)} training rows "
              f"({int(basics['n_basis'])} games)")
        naive = basics.get("hfa_naive_mean")
        if naive is not None and abs(naive - hfa) > 0.5:
            print(f"    hfa          {hfa:+.2f} pts  (ridge; naive mean of home "
                  f"margins was {naive:+.2f} -- inflated by scheduling)")
        else:
            print(f"    hfa          {hfa:+.2f} pts")
        print(f"    raw SD       margin {basics.get('sd_margin_raw', 0):.2f}  "
              f"total {basics.get('sd_total_raw', 0):.2f}")
        print(f"    mean total   {basics['mean_total']:.1f}")
        print(f"    weights      last3={weights['w_last3']:.2f} "
              f"last7={weights['w_last7']:.2f} season={weights['w_season']:.2f}")
        print(f"    sigma margin {sigmas['sigma_margin']:.2f}  "
              f"total {sigmas['sigma_total']:.2f}")
        print(f"    form gain    {improvement:+.1%} vs season-average only")
        print(f"    skill        margin {skill_margin:+.1%} of variance, "
              f"total {skill_total:+.1%}")
        for label, skill in (("spread", skill_margin), ("total", skill_total)):
            if skill <= 0:
                print(
                    f"    ** the {label} model does NOT beat predicting the "
                    f"league average. Treat its {label} numbers as weak."
                )
    return values
