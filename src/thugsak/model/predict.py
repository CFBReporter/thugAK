"""Turn team form into a predicted spread, total, and ladder probabilities.

The chain:

    game logs -> opponent-adjusted windows -> fitted blend -> expected points
              -> predicted spread and total
              -> a probability for every strike on a Kalshi ladder

That last step is what makes a prediction tradeable. Kalshi lists spread and
total markets as ladders of independent binaries ("over 59.5?", "wins by over
7.5?"), so a single predicted total of 44.3 implies a probability at every
strike, and each can be compared against its own price.

The conversion needs a standard deviation, and sigma is taken from
`calibration` where it was fitted on three seasons of residuals. It is
deliberately the SD of *this model's errors*, not of raw outcomes: the
question a strike asks is "how wrong might this prediction be", and using
outcome variance instead would overstate the spread of plausible results and
flatten every probability toward 50%.

Three anchors are always produced -- this model, the pure power rating, and the
market -- because agreement between them is uninformative and disagreement is
the entire signal. When our number is the lone outlier, that is more often a
bug than an opportunity, and `Prediction.outlier_warning` says so.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import store
from ..config import league as get_league
from . import asof, correction as correction_mod, efficiency as efficiency_mod, gamelog, injuries as injuries_mod, ratings as ratings_mod

MODEL_VERSION = "v1"

# Fallbacks used only when `calibration` is empty, i.e. before the history
# import has run. They are rough league norms, and `Prediction.calibrated`
# reports False so nothing presents them as fitted.
FALLBACK = {
    "nfl": {"sigma_margin": 13.5, "sigma_total": 10.5, "hfa": 2.0, "mean_total": 44.0},
    "nba": {"sigma_margin": 12.0, "sigma_total": 17.0, "hfa": 2.5, "mean_total": 228.0},
    "ncaaf": {"sigma_margin": 16.0, "sigma_total": 14.0, "hfa": 2.5, "mean_total": 52.0},
    "ncaamb": {"sigma_margin": 11.0, "sigma_total": 14.0, "hfa": 3.0, "mean_total": 145.0},
}
DEFAULT_WEIGHTS = {"w_last3": 0.5, "w_last7": 0.3, "w_season": 0.2}


def phi(x: float) -> float:
    """Standard normal CDF, via erf -- avoids a scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_over(predicted_total: float, strike: float, sigma: float) -> float:
    """P(actual total > strike)."""
    if sigma <= 0:
        return 1.0 if predicted_total > strike else 0.0
    return 1.0 - phi((strike - predicted_total) / sigma)


def prob_margin_over(predicted_margin: float, strike: float, sigma: float) -> float:
    """P(home margin > strike). Negative strike means the home team may lose."""
    if sigma <= 0:
        return 1.0 if predicted_margin > strike else 0.0
    return 1.0 - phi((strike - predicted_margin) / sigma)


def win_probability(predicted_margin: float, sigma: float) -> float:
    """P(home team wins) -- the margin clearing zero.

    This is what converts a predicted spread into something comparable with a
    Kalshi winner market, whose price is already a probability.
    """
    return prob_margin_over(predicted_margin, 0.0, sigma)


@dataclass
class Anchor:
    name: str
    spread: float | None = None      # home-relative, positive = home favoured
    total: float | None = None
    home_win_prob: float | None = None
    source: str = ""


@dataclass
class Prediction:
    game_id: str
    league: str
    as_of: str
    home_team: str
    away_team: str

    home_points: float | None = None
    away_points: float | None = None

    sigma_margin: float = 0.0
    sigma_total: float = 0.0
    calibrated: bool = False

    injury_adj_home: float = 0.0
    injury_adj_away: float = 0.0
    injury_confidence: str = "none"
    injury_detail: list[dict[str, Any]] = field(default_factory=list)

    anchors: list[Anchor] = field(default_factory=list)
    home_form: dict[str, Any] = field(default_factory=dict)
    away_form: dict[str, Any] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # The learned layer (model/correction.py): the engine's own score before
    # it, the readable steps it applied, and the inputs it saw. `model_version`
    # is the league's active trained model, "v1" when none has been promoted.
    model_version: str = "v1"
    base_home_points: float | None = None
    base_away_points: float | None = None
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    features: dict[str, float] = field(default_factory=dict)
    # College basketball: the tempo-free efficiency (KenPom-style) projection.
    efficiency: dict[str, Any] | None = None

    @property
    def spread(self) -> float | None:
        """Predicted margin, home-relative. Positive means home favoured."""
        if self.home_points is None or self.away_points is None:
            return None
        return self.home_points - self.away_points

    @property
    def total(self) -> float | None:
        if self.home_points is None or self.away_points is None:
            return None
        return self.home_points + self.away_points

    @property
    def home_win_prob(self) -> float | None:
        s = self.spread
        if s is None or self.sigma_margin <= 0:
            return None
        return win_probability(s, self.sigma_margin)

    @property
    def usable(self) -> bool:
        return self.home_points is not None and self.away_points is not None

    def anchor(self, name: str) -> Anchor | None:
        return next((a for a in self.anchors if a.name == name), None)

    # How far the model may stray from every other anchor before the
    # disagreement is treated as suspect, as a fraction of sigma. Scaled by
    # sigma rather than fixed in points because a 3-point gap means something
    # very different in the NFL (sigma ~13) than in college football
    # (sigma ~20).
    OUTLIER_SIGMA_FRACTION = 0.25

    @property
    def outlier_warning(self) -> str | None:
        """Flag when this model disagrees with every other anchor.

        A model that disagrees with the market *and* with a simple power
        rating is usually broken rather than insightful, and saying so is more
        useful than presenting the number confidently.
        """
        mine = self.spread
        if mine is None:
            return None
        others = [
            (a.name, a.spread) for a in self.anchors
            if a.name != "model" and a.spread is not None
        ]
        if len(others) < 2:
            return None
        threshold = max(self.sigma_margin * self.OUTLIER_SIGMA_FRACTION, 2.0)
        if all(abs(mine - o) > threshold for _, o in others):
            listed = ", ".join(f"{n} {o:+.1f}" for n, o in others)
            return (
                f"The model's spread ({mine:+.1f}) disagrees with every other "
                f"anchor ({listed}) by more than {threshold:.1f} points "
                f"({self.OUTLIER_SIGMA_FRACTION:.0%} of sigma). When the model "
                f"is the lone outlier, a data or matching fault is more likely "
                f"than an edge."
            )
        return None

    @property
    def total_outlier_warning(self) -> str | None:
        """The same check for totals, which drive a whole ladder at once.

        A totals ladder is all OVERs, so a model total above the market's
        makes every single rung look cheap. Without this the arithmetic is
        reported as a dozen separate opportunities.
        """
        mine = self.total
        if mine is None:
            return None
        mkt = self.anchor("market")
        if mkt is None or mkt.total is None:
            return None
        threshold = max(self.sigma_total * self.OUTLIER_SIGMA_FRACTION, 2.0)
        gap = mine - mkt.total
        if abs(gap) > threshold:
            return (
                f"The model's total ({mine:.1f}) is {gap:+.1f} against the "
                f"market's ({mkt.total:.1f}), beyond the {threshold:.1f}-point "
                f"tolerance. This single disagreement prices every strike on "
                f"the totals ladder, so those rungs are one position rather "
                f"than many."
            )
        return None

    def prob_total_over(self, strike: float) -> float | None:
        t = self.total
        if t is None or self.sigma_total <= 0:
            return None
        return prob_over(t, strike, self.sigma_total)

    def prob_spread_over(self, strike: float, *, team: str | None = None) -> float | None:
        """P(`team` wins by more than `strike`). Defaults to the home team."""
        s = self.spread
        if s is None or self.sigma_margin <= 0:
            return None
        margin = s if (team is None or team == self.home_team) else -s
        return prob_margin_over(margin, strike, self.sigma_margin)

    def summary(self) -> str:
        if not self.usable:
            return f"No prediction: {'; '.join(self.warnings) or 'insufficient data'}"
        return (
            f"{self.away_team} {self.away_points:.1f} @ "
            f"{self.home_team} {self.home_points:.1f} | "
            f"spread {self.spread:+.1f} | total {self.total:.1f} | "
            f"home win {self.home_win_prob:.1%}"
        )


def _calibration(conn: sqlite3.Connection, league: str) -> tuple[dict[str, float], bool]:
    cal = store.get_calibration(conn, league)
    if cal.get("sigma_margin"):
        return cal, True
    fb = dict(FALLBACK.get(league, FALLBACK["nfl"]))
    fb.update(DEFAULT_WEIGHTS)
    return fb, False


def predict_game(
    conn: sqlite3.Connection,
    league: str,
    game: Any,
    *,
    as_of: str | None = None,
    rs: ratings_mod.RatingSet | None = None,
    injury_impacts: dict[str, Sequence[injuries_mod.InjuryImpact]] | None = None,
    market_spread: float | None = None,
    market_total: float | None = None,
    market_home_prob: float | None = None,
    espn_predictor_prob: float | None = None,
    model: Any = "active",
) -> Prediction:
    """Predict one game as of kickoff minus the standard lead time.

    `market_spread` follows the sportsbook convention (negative = home
    favoured) and is flipped internally to the home-relative convention used
    throughout this module.

    `model`: "active" applies the league's promoted correction layer (if
    any); None gives the engine alone -- what the replay trains on; a model
    run dict applies that specific version.
    """
    home, away = game["home_team"], game["away_team"]
    as_of = as_of or asof.as_of_for_kickoff(game["start_time"])
    asof.require_as_of(as_of)

    pred = Prediction(
        game_id=game["game_id"],
        league=league,
        as_of=as_of,
        home_team=home,
        away_team=away,
    )
    cal, calibrated = _calibration(conn, league)
    pred.calibrated = calibrated
    pred.sigma_margin = cal.get("sigma_margin", 13.5)
    pred.sigma_total = cal.get("sigma_total", 10.5)
    hfa = cal.get("hfa", 2.0)
    weights = {k: cal.get(k, DEFAULT_WEIGHTS[k]) for k in DEFAULT_WEIGHTS}
    pred.weights = weights
    if not calibrated:
        pred.warnings.append(
            "Using fallback constants -- run scripts/import_history.py to fit "
            "sigma, home-field advantage and blend weights from real data."
        )

    neutral = bool(game["neutral_site"]) if "neutral_site" in game.keys() else False

    season = game["season"] if "season" in game.keys() else None
    # Ratings are bounded to this game's season, exactly as the form windows
    # below are. Unbounded, the solve would read whatever prior seasons are
    # still stored and the prediction would change with retention state.
    rs = rs or ratings_mod.solve_from_store(
        conn, league, as_of, season=season
    )
    hf = gamelog.team_form(conn, home, as_of, rs=rs, season=season)
    af = gamelog.team_form(conn, away, as_of, rs=rs, season=season)
    pred.home_form = gamelog.describe(hf)
    pred.away_form = gamelog.describe(af)

    if hf.sparse or af.sparse:
        pred.warnings.append(
            f"Thin history ({home}: {hf.games_played} games, "
            f"{away}: {af.games_played}); the estimate leans on shrunk ratings."
        )

    # Expected points: this team's adjusted offence against that opponent's
    # adjusted defence, averaged so neither side's sample dominates.
    h_off = gamelog.blended_points(hf, weights)
    a_def = gamelog.blended_points_against(af, weights)
    a_off = gamelog.blended_points(af, weights)
    h_def = gamelog.blended_points_against(hf, weights)

    if h_off is None or a_off is None:
        pred.warnings.append("No usable scoring history for one or both teams.")
        return pred

    home_pts = (h_off + (a_def if a_def is not None else h_off)) / 2
    away_pts = (a_off + (h_def if h_def is not None else a_off)) / 2

    # Home advantage, split so the total is unaffected by venue.
    if not neutral:
        home_pts += hfa / 2.0
        away_pts -= hfa / 2.0

        # Prefer a team-specific home/away delta when both samples support it.
        hd = hf.home_away_delta
        if hd is not None:
            home_pts += (hd - hfa) / 4.0

    # --- learned correction -------------------------------------------------
    pred.base_home_points = round(home_pts, 2)
    pred.base_away_points = round(away_pts, 2)
    pr_margin_for_features = rs.predicted_margin(home, away, neutral=neutral)

    def _rest(team: str) -> float | None:
        last = store.team_games_before(conn, team, as_of, limit=1)
        if not last:
            return None
        return (asof.parse_ts(as_of) - asof.parse_ts(last[0]["start_time"])).total_seconds() / 86400.0

    eff = None
    if league in efficiency_mod.LEAGUES and season is not None:
        er = efficiency_mod.ratings_as_of(conn, league, int(season), as_of)
        if er is not None and er.has(home) and er.has(away):
            eff = er.project(home, away, neutral=neutral)
            if eff:
                eff["ratings_games"] = er.n_games
    pred.efficiency = eff

    pred.features = correction_mod.features(
        base_margin=home_pts - away_pts,
        base_total=home_pts + away_pts,
        rating_margin=pr_margin_for_features,
        games_home=hf.games_played,
        games_away=af.games_played,
        rest_home=_rest(home),
        rest_away=_rest(away),
        neutral=neutral,
        efficiency_margin=(eff["home_points"] - eff["away_points"]) if eff else None,
        efficiency_total=(eff["home_points"] + eff["away_points"]) if eff else None,
        pace=(eff["possessions"] - eff["avg_tempo"]) if eff else None,
        sos_diff=(eff["home"]["sos"] - eff["away"]["sos"]) if eff else None,
    )
    run = store.active_model_run(conn, league) if model == "active" else model
    corr = None
    if run:
        # A promoted version may carry new settings only (no correction).
        pred.model_version = run["version"]
    if run and run.get("model") and run["model"].get("correction"):
        corr = correction_mod.Correction.from_dict(run["model"]["correction"])
        dm, dt, steps = corr.adjust(pred.features)
        margin, total = home_pts - away_pts + dm, home_pts + away_pts + dt
        home_pts, away_pts = (total + margin) / 2, (total - margin) / 2
        pred.adjustments.extend(steps)

    # Injuries.
    if injury_impacts:
        h_adj, h_conf = injuries_mod.team_adjustment(injury_impacts.get(home, []))
        a_adj, a_conf = injuries_mod.team_adjustment(injury_impacts.get(away, []))
        pred.injury_adj_home, pred.injury_adj_away = h_adj, a_adj
        home_pts += h_adj
        away_pts += a_adj
        pred.injury_confidence = (
            h_conf if h_conf == a_conf else f"home={h_conf}, away={a_conf}"
        )
        pred.injury_detail = [
            {
                "team": team,
                "player": i.name,
                "position": i.position,
                "status": i.status,
                "adjustment": i.points_adjustment,
                "confidence": i.confidence,
                "rationale": i.rationale,
                "last3": i.last3.stats,
                "last7": i.last7.stats,
                "with_without": i.with_without.describe(),
                "backup": i.backup_note,
            }
            for team, lst in injury_impacts.items()
            for i in lst
        ]

    if injury_impacts and (pred.injury_adj_home or pred.injury_adj_away):
        pred.adjustments.append({
            "label": "Injuries", "home": round(pred.injury_adj_home, 2),
            "away": round(pred.injury_adj_away, 2),
        })

    # Market input, capped: the line nudges the score by a learned weight
    # (at most correction.MARKET_WEIGHT_CAP); it never sets it.
    if corr is not None and (market_spread is not None or market_total is not None):
        m0, t0 = home_pts - away_pts, home_pts + away_pts
        m1 = correction_mod.blend_market(
            m0, -market_spread if market_spread is not None else None, corr.margin.market_weight)
        t1 = correction_mod.blend_market(t0, market_total, corr.total.market_weight)
        nh, na = (t1 + m1) / 2, (t1 - m1) / 2
        if abs(nh - home_pts) >= 0.05 or abs(na - away_pts) >= 0.05:
            pred.adjustments.append({
                "label": f"Market input (sportsbook line, weight {corr.margin.market_weight:.0%}/{corr.total.market_weight:.0%})",
                "home": round(nh - home_pts, 2), "away": round(na - away_pts, 2),
            })
        home_pts, away_pts = nh, na

    # Keep the output inside the bounds of a plausible scoreline; a value
    # outside them is a bug, and clamping silently would hide it.
    lo, hi = get_league(league).sane_team_points
    for label, v in (("home", home_pts), ("away", away_pts)):
        if not (lo - 10 <= v <= hi + 10):
            pred.warnings.append(
                f"Implausible {label} projection ({v:.1f}); expected roughly "
                f"{lo}-{hi}. Likely a data or matching fault."
            )

    pred.home_points = round(home_pts, 2)
    pred.away_points = round(away_pts, 2)

    # --- anchors ---------------------------------------------------------
    pred.anchors.append(
        Anchor(
            name="model",
            spread=pred.spread,
            total=pred.total,
            home_win_prob=pred.home_win_prob,
            source="opponent-adjusted form"
            + (" + injuries" if injury_impacts else ""),
        )
    )
    pr_margin = rs.predicted_margin(home, away, neutral=neutral)
    pred.anchors.append(
        Anchor(
            name="power_rating",
            spread=round(pr_margin, 2),
            home_win_prob=win_probability(pr_margin, pred.sigma_margin),
            source=f"ridge ratings, {rs.n_total} games",
        )
    )
    if market_spread is not None or market_total is not None or market_home_prob:
        pred.anchors.append(
            Anchor(
                name="market",
                # Books quote negative for a home favourite; flip to match.
                spread=-market_spread if market_spread is not None else None,
                total=market_total,
                home_win_prob=market_home_prob,
                source="sportsbook line (SharpAPI consensus, or ESPN where SharpAPI has none)",
            )
        )
    if pred.efficiency:
        em = pred.efficiency["home_points"] - pred.efficiency["away_points"]
        pred.anchors.append(
            Anchor(
                name="efficiency",
                spread=round(em, 2),
                total=round(pred.efficiency["home_points"] + pred.efficiency["away_points"], 2),
                home_win_prob=win_probability(em, pred.sigma_margin),
                source=f"tempo-free adjusted efficiency, {pred.efficiency['ratings_games']} games",
            )
        )
    if espn_predictor_prob is not None:
        pred.anchors.append(
            Anchor(name="espn_predictor", home_win_prob=espn_predictor_prob,
                   source="ESPN matchup predictor")
        )

    for warn in (pred.outlier_warning, pred.total_outlier_warning):
        if warn:
            pred.warnings.append(warn)
    return pred


def save(
    conn: sqlite3.Connection,
    pred: Prediction,
    *,
    matchup: str | None = None,
    slate_date: str | None = None,
) -> int:
    """Persist a prediction so manual checks accumulate into a record.

    `matchup` and `slate_date` are copied in rather than joined later: the
    games table is pruned at season rollover, and a record that needs it to
    say who played is a record that expires.
    """
    return store.save_prediction(
        conn,
        {
            "game_id": pred.game_id,
            "league": pred.league,
            "as_of": pred.as_of,
            "matchup": matchup,
            "slate_date": slate_date,
            "home_team": pred.home_team,
            "away_team": pred.away_team,
            "pred_home_points": pred.home_points,
            "pred_away_points": pred.away_points,
            "pred_spread": pred.spread,
            "pred_total": pred.total,
            "model_version": pred.model_version,
            "payload": {
                "sigma_margin": pred.sigma_margin,
                "sigma_total": pred.sigma_total,
                "calibrated": pred.calibrated,
                "weights": pred.weights,
                "injury_adj": [pred.injury_adj_home, pred.injury_adj_away],
                "injury_confidence": pred.injury_confidence,
                "anchors": [
                    {"name": a.name, "spread": a.spread, "total": a.total,
                     "home_win_prob": a.home_win_prob}
                    for a in pred.anchors
                ],
                "warnings": pred.warnings,
                "base_points": [pred.base_home_points, pred.base_away_points],
                "adjustments": pred.adjustments,
            },
        },
    )


def grade(pred: Prediction, game: Any) -> dict[str, Any] | None:
    """Compare a prediction with the actual result, for the lookback view."""
    hs, as_ = game["home_score"], game["away_score"]
    if hs is None or as_ is None or not pred.usable:
        return None
    actual_margin = float(hs) - float(as_)
    actual_total = float(hs) + float(as_)
    out: dict[str, Any] = {
        "actual_home": hs,
        "actual_away": as_,
        "actual_spread": actual_margin,
        "actual_total": actual_total,
        "spread_error": pred.spread - actual_margin,  # type: ignore[operator]
        "total_error": pred.total - actual_total,     # type: ignore[operator]
        "picked_winner": (pred.spread > 0) == (actual_margin > 0),
    }
    mkt = pred.anchor("market")
    if mkt and mkt.spread is not None:
        out["market_spread_error"] = mkt.spread - actual_margin
        out["beat_market_spread"] = abs(out["spread_error"]) < abs(
            out["market_spread_error"]
        )
    if mkt and mkt.total is not None:
        out["market_total_error"] = mkt.total - actual_total
        out["beat_market_total"] = abs(out["total_error"]) < abs(
            out["market_total_error"]
        )
    return out
