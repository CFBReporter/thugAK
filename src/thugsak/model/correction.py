"""The learned correction layer: the engine's habitual errors, measured and removed.

The engine (opponent-adjusted form + ratings + home advantage) produces a
base score. Replaying past games as of their kickoff shows where that base is
systematically wrong -- totals running low in a high-scoring season, tired
teams on a back-to-back, early-season noise -- and this layer learns those
patterns as a small ridge regression on the base's residuals:

    actual_margin - base_margin = a + b . features
    actual_total  - base_total  = c + d . features

Linear and regularised on purpose: a few thousand games per league support a
handful of coefficients well and a flexible model badly, and every
coefficient here is readable, which is what lets the app show *why* a score
moved ("back-to-back: -1.6 NYK").

The market (the sportsbook line, when one exists) is an input, not the
target: after the correction, the prediction moves toward the line by a
learned weight that is capped at MARKET_WEIGHT_CAP, and accuracy is reported
with and without it so the model's own progress stays visible.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

import numpy as np

# The order is the coefficient order; labels group them for display.
FEATURES: tuple[str, ...] = (
    "base_margin",
    "base_total",
    "rating_gap",
    "early_season",
    "rest_home",
    "rest_away",
    "b2b_home",
    "b2b_away",
    "neutral",
)
LABELS: dict[str, str] = {
    "base_margin": "Margin size (engine over/under-shoots)",
    "base_total": "Scoring level this season",
    "rating_gap": "Power ratings disagree with recent form",
    "early_season": "Early-season uncertainty",
    "rest_home": "Rest",
    "rest_away": "Rest",
    "b2b_home": "Back-to-back",
    "b2b_away": "Back-to-back",
    "neutral": "Neutral site",
}
# Which inputs each target may use. The margin is about who is better and who
# is tired; the total is about the scoring level and pace. Letting the margin
# see the scoring level (and vice versa) fitted season-to-season accidents --
# on NBA replay it moved margins 3 points per standard step of total.
MARGIN_FEATURES = frozenset({
    "base_margin", "rating_gap", "early_season", "rest_home", "rest_away",
    "b2b_home", "b2b_away", "neutral",
})
TOTAL_FEATURES = frozenset({
    "base_total", "early_season", "rest_home", "rest_away", "b2b_home", "b2b_away",
})
MARKET_WEIGHT_CAP = 0.15
REST_CAP_DAYS = 7.0
LAMBDAS = (1.0, 10.0, 100.0, 1000.0)
# Recent games count more: a game's weight halves every HALF_LIFE_DAYS before
# the newest training game. Scoring levels and team strength drift within and
# across seasons, and a correction learned evenly from last season carried
# last season's scoring level into this one.
HALF_LIFE_DAYS = 365.0


def features(
    *, base_margin: float, base_total: float, rating_margin: float | None,
    games_home: int, games_away: int, rest_home: float | None,
    rest_away: float | None, neutral: bool,
) -> dict[str, float]:
    """The inputs the correction sees, computed identically live and in replay."""
    def rest(d: float | None) -> float:
        return REST_CAP_DAYS if d is None else max(0.0, min(REST_CAP_DAYS, d))

    rh, ra = rest(rest_home), rest(rest_away)
    return {
        "base_margin": float(base_margin),
        "base_total": float(base_total),
        "rating_gap": float((rating_margin if rating_margin is not None else base_margin) - base_margin),
        "early_season": max(0.0, 1.0 - min(games_home, games_away) / 10.0),
        "rest_home": rh,
        "rest_away": ra,
        "b2b_home": 1.0 if rh <= 1.0 else 0.0,
        "b2b_away": 1.0 if ra <= 1.0 else 0.0,
        "neutral": 1.0 if neutral else 0.0,
    }


@dataclass
class Target:
    """One fitted regression: intercept + coefficients on standardized inputs."""
    intercept: float = 0.0
    coef: list[float] = field(default_factory=lambda: [0.0] * len(FEATURES))
    lam: float = 0.0
    market_weight: float = 0.0

    def contributions(self, x: dict[str, float], mean: Sequence[float], std: Sequence[float]) -> dict[str, float]:
        out = {}
        for i, name in enumerate(FEATURES):
            s = std[i] or 1.0
            out[name] = self.coef[i] * (x[name] - mean[i]) / s
        return out


@dataclass
class Correction:
    league: str
    version: str
    mean: list[float]
    std: list[float]
    margin: Target
    total: Target
    n_rows: int = 0

    # -- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Correction":
        return cls(
            league=d["league"], version=d["version"], mean=list(d["mean"]),
            std=list(d["std"]), margin=Target(**d["margin"]),
            total=Target(**d["total"]), n_rows=int(d.get("n_rows", 0)),
        )

    # -- applying -----------------------------------------------------------
    def adjust(self, x: dict[str, float]) -> tuple[float, float, list[dict[str, Any]]]:
        """(margin adjustment, total adjustment, readable breakdown)."""
        cm = self.margin.contributions(x, self.mean, self.std)
        ct = self.total.contributions(x, self.mean, self.std)
        dm = self.margin.intercept + sum(cm.values())
        dt = self.total.intercept + sum(ct.values())

        groups: dict[str, list[float]] = {}
        for name in FEATURES:
            g = groups.setdefault(LABELS[name], [0.0, 0.0])
            g[0] += cm[name]
            g[1] += ct[name]
        g = groups.setdefault("League-wide bias", [0.0, 0.0])
        g[0] += self.margin.intercept
        g[1] += self.total.intercept
        breakdown = [
            {"label": label, "home": round((t + m) / 2, 2), "away": round((t - m) / 2, 2)}
            for label, (m, t) in groups.items()
            if abs(m) >= 0.05 or abs(t) >= 0.05
        ]
        breakdown.sort(key=lambda b: -(abs(b["home"]) + abs(b["away"])))
        return dm, dt, breakdown


def blend_market(value: float, market: float | None, weight: float) -> float:
    if market is None or not math.isfinite(market):
        return value
    return value + weight * (market - value)


# --------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------

def _matrix(rows: Sequence[dict[str, Any]]) -> np.ndarray:
    return np.array([[r["features"][f] for f in FEATURES] for r in rows], dtype=float)


def _ridge(X: np.ndarray, y: np.ndarray, lam: float, w: np.ndarray | None = None) -> tuple[float, np.ndarray]:
    """Weighted ridge with an unpenalised intercept, on standardized X."""
    w = np.ones(len(y)) if w is None else w
    w = w * (len(y) / w.sum())          # keep lambda on the same scale
    x_mean = (w[:, None] * X).sum(axis=0) / w.sum()
    y_mean = float((w * y).sum() / w.sum())
    Xc, yc = X - x_mean, y - y_mean
    k = X.shape[1]
    beta = np.linalg.solve(Xc.T @ (w[:, None] * Xc) + lam * np.eye(k), Xc.T @ (w * yc))
    return y_mean - float(x_mean @ beta), beta


def _fit_target(Xs: np.ndarray, y: np.ndarray, w: np.ndarray, allowed: frozenset[str]) -> Target:
    """Pick lambda on the latest 20% of rows (time-ordered), refit on all.
    Inputs outside `allowed` get a zero coefficient."""
    keep = [i for i, f in enumerate(FEATURES) if f in allowed]
    full = Xs
    Xs = Xs[:, keep]
    n = len(y)
    cut = max(int(n * 0.8), 1)
    best_lam, best_err = LAMBDAS[-1], math.inf
    if n - cut >= 30:
        for lam in LAMBDAS:
            a, b = _ridge(Xs[:cut], y[:cut], lam, w[:cut])
            err = float(np.mean(np.abs(y[cut:] - (a + Xs[cut:] @ b))))
            if err < best_err:
                best_lam, best_err = lam, err
    a, b = _ridge(Xs, y, best_lam, w)
    coef = [0.0] * full.shape[1]
    for j, i in enumerate(keep):
        coef[i] = float(b[j])
    return Target(intercept=a, coef=coef, lam=best_lam)


def _recency_weights(rows: Sequence[dict[str, Any]]) -> np.ndarray:
    from ..localtime import parse_ts

    t = np.array([parse_ts(r["start_time"]).timestamp() for r in rows], dtype=float)
    age_days = (t.max() - t) / 86400.0
    return np.power(0.5, age_days / HALF_LIFE_DAYS)


def _market_weight(pred: np.ndarray, market: np.ndarray, actual: np.ndarray) -> float:
    """Least-squares weight toward the market, clipped to [0, cap]."""
    ok = np.isfinite(market)
    if ok.sum() < 30:
        return 0.0
    d = market[ok] - pred[ok]
    r = actual[ok] - pred[ok]
    denom = float(d @ d)
    if denom <= 0:
        return 0.0
    return float(min(MARKET_WEIGHT_CAP, max(0.0, (d @ r) / denom)))


def fit(rows: Sequence[dict[str, Any]], *, league: str, version: str) -> Correction:
    """Fit from replay rows (time-ordered dicts with features, base and actual)."""
    X = _matrix(rows)
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0
    Xs = (X - mean) / std

    base_m = np.array([r["base_margin"] for r in rows], dtype=float)
    base_t = np.array([r["base_total"] for r in rows], dtype=float)
    act_m = np.array([r["actual_margin"] for r in rows], dtype=float)
    act_t = np.array([r["actual_total"] for r in rows], dtype=float)

    w = _recency_weights(rows)
    tm = _fit_target(Xs, act_m - base_m, w, MARGIN_FEATURES)
    tt = _fit_target(Xs, act_t - base_t, w, TOTAL_FEATURES)
    corr_m = base_m + tm.intercept + Xs @ np.array(tm.coef)
    corr_t = base_t + tt.intercept + Xs @ np.array(tt.coef)
    mkt_m = np.array([r.get("market_margin") if r.get("market_margin") is not None else np.nan for r in rows], dtype=float)
    mkt_t = np.array([r.get("market_total") if r.get("market_total") is not None else np.nan for r in rows], dtype=float)
    tm.market_weight = _market_weight(corr_m, mkt_m, act_m)
    tt.market_weight = _market_weight(corr_t, mkt_t, act_t)
    return Correction(
        league=league, version=version, mean=[float(v) for v in mean],
        std=[float(v) for v in std], margin=tm, total=tt, n_rows=len(rows),
    )


# --------------------------------------------------------------------------
# scoring a model on rows
# --------------------------------------------------------------------------

def predict_row(corr: "Correction | None", row: dict[str, Any], *, market: bool) -> tuple[float, float]:
    """(margin, total) for one replay row under a correction (None = engine only)."""
    m, t = row["base_margin"], row["base_total"]
    if corr is not None:
        dm, dt, _ = corr.adjust(row["features"])
        m, t = m + dm, t + dt
        if market:
            m = blend_market(m, row.get("market_margin"), corr.margin.market_weight)
            t = blend_market(t, row.get("market_total"), corr.total.market_weight)
    return m, t


def evaluate(corr: "Correction | None", rows: Sequence[dict[str, Any]], *, market: bool = False) -> dict[str, Any]:
    """Accuracy on rows: mean absolute miss of margin, total, each team's
    points, and how often the predicted winner won."""
    if not rows:
        return {"n": 0}
    em, et, eh, ea, right = [], [], [], [], 0
    for r in rows:
        m, t = predict_row(corr, r, market=market)
        am, at = r["actual_margin"], r["actual_total"]
        em.append(abs(m - am))
        et.append(abs(t - at))
        eh.append(abs((t + m) / 2 - (at + am) / 2))
        ea.append(abs((t - m) / 2 - (at - am) / 2))
        if am != 0 and (m > 0) == (am > 0):
            right += 1
    decided = sum(1 for r in rows if r["actual_margin"] != 0)
    return {
        "n": len(rows),
        "mae_margin": round(float(np.mean(em)), 2),
        "mae_total": round(float(np.mean(et)), 2),
        "mae_points": round(float(np.mean(eh + ea)), 2),
        "winner_pct": round(right / decided, 3) if decided else None,
    }


def evaluate_book(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The sportsbook line's own accuracy on the rows that had one -- the
    yardstick the app shows beside the model, not a target."""
    have = [r for r in rows if r.get("market_margin") is not None and r.get("market_total") is not None]
    if not have:
        return {"n": 0}
    em = [abs(r["market_margin"] - r["actual_margin"]) for r in have]
    et = [abs(r["market_total"] - r["actual_total"]) for r in have]
    return {"n": len(have), "mae_margin": round(float(np.mean(em)), 2), "mae_total": round(float(np.mean(et)), 2)}
