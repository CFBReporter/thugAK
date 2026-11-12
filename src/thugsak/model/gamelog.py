"""Team form windows (last 3, last 7, season) and home/away splits.

The last-3 and last-7 windows are reported separately rather than collapsed
into one number, because the *gap* between them carries information: a team
whose last 3 are well above its last 7 is trending, and that is visible only
if both are shown.

Every window is opponent-adjusted. Raw scoring averages mislead badly -- 30
points against the league's worst defence is a weaker signal than 24 against
its best -- so each game's points are re-based by that opponent's defensive
rating before averaging.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from .. import store
from . import asof, ratings as ratings_mod

WINDOWS: tuple[tuple[str, int | None], ...] = (
    ("last3", 3),
    ("last7", 7),
    ("season", None),
)


@dataclass
class WindowStats:
    """One team's form over one window."""

    name: str
    n: int = 0
    points_for: float | None = None
    points_against: float | None = None
    adj_points_for: float | None = None
    adj_points_against: float | None = None
    wins: int = 0
    losses: int = 0
    ties: int = 0
    opponents: list[str] = field(default_factory=list)
    avg_opponent_rating: float | None = None

    @property
    def record(self) -> str:
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base

    @property
    def margin(self) -> float | None:
        if self.points_for is None or self.points_against is None:
            return None
        return self.points_for - self.points_against

    @property
    def total(self) -> float | None:
        if self.points_for is None or self.points_against is None:
            return None
        return self.points_for + self.points_against

    @property
    def usable(self) -> bool:
        return self.n > 0 and self.adj_points_for is not None


@dataclass
class TeamForm:
    """Everything the prediction needs about one team's recent play."""

    team_id: str
    as_of: str
    windows: dict[str, WindowStats] = field(default_factory=dict)
    home: WindowStats | None = None
    away: WindowStats | None = None
    games_played: int = 0

    def window(self, name: str) -> WindowStats | None:
        w = self.windows.get(name)
        return w if w and w.usable else None

    @property
    def trend(self) -> float | None:
        """Last-3 minus last-7 adjusted scoring: positive means heating up."""
        w3, w7 = self.window("last3"), self.window("last7")
        if not w3 or not w7:
            return None
        return w3.adj_points_for - w7.adj_points_for  # type: ignore[operator]

    @property
    def home_away_delta(self) -> float | None:
        """Points-per-game difference at home versus away, for this team.

        Used only when both samples are large enough; below that the league
        average home edge is the better estimate.
        """
        if not self.home or not self.away:
            return None
        if self.home.n < 3 or self.away.n < 3:
            return None
        if self.home.points_for is None or self.away.points_for is None:
            return None
        return self.home.points_for - self.away.points_for

    @property
    def sparse(self) -> bool:
        return self.games_played < 3


def _summarise(
    name: str,
    rows: Sequence[sqlite3.Row],
    rs: ratings_mod.RatingSet | None,
) -> WindowStats:
    ws = WindowStats(name=name, n=len(rows))
    if not rows:
        return ws

    pf = [float(r["points_for"]) for r in rows]
    pa = [float(r["points_against"]) for r in rows]
    ws.points_for = sum(pf) / len(pf)
    ws.points_against = sum(pa) / len(pa)

    for r in rows:
        f, a = float(r["points_for"]), float(r["points_against"])
        if f > a:
            ws.wins += 1
        elif f < a:
            ws.losses += 1
        else:
            ws.ties += 1
        ws.opponents.append(r["opponent_id"])

    if rs is not None:
        # Points scored, less what an average team would have managed against
        # this opponent's defence; and points conceded, less what an average
        # team would have conceded to this opponent's offence.
        adj_f, adj_a, opp_r = [], [], []
        for r in rows:
            opp = r["opponent_id"]
            adj_f.append(float(r["points_for"]) - rs.defense.get(opp, 0.0))
            adj_a.append(float(r["points_against"]) - rs.offense.get(opp, 0.0))
            opp_r.append(rs.rating(opp))
        ws.adj_points_for = sum(adj_f) / len(adj_f)
        ws.adj_points_against = sum(adj_a) / len(adj_a)
        ws.avg_opponent_rating = sum(opp_r) / len(opp_r)
    else:
        # Without ratings the raw averages are all there is; flagged by the
        # absence of avg_opponent_rating so callers can see it is unadjusted.
        ws.adj_points_for = ws.points_for
        ws.adj_points_against = ws.points_against
    return ws


def team_form(
    conn: sqlite3.Connection,
    team_id: str,
    as_of: str,
    *,
    rs: ratings_mod.RatingSet | None = None,
    season: int | None = None,
) -> TeamForm:
    """Build every window and split for one team, as of a cutoff.

    `season` confines every window to the current campaign. Last-3 and last-7
    would rarely reach back across a season boundary anyway, but the
    season-to-date window and the home/away splits certainly would, and a
    team's form from two years ago is not form.
    """
    asof.require_as_of(as_of)
    all_rows = store.team_games_before(conn, team_id, as_of, season=season)
    tf = TeamForm(team_id=team_id, as_of=as_of, games_played=len(all_rows))

    for name, k in WINDOWS:
        rows = all_rows[:k] if k else all_rows
        tf.windows[name] = _summarise(name, rows, rs)

    tf.home = _summarise("home", [r for r in all_rows if r["is_home"]], rs)
    tf.away = _summarise("away", [r for r in all_rows if not r["is_home"]], rs)
    return tf


def blended_points(
    tf: TeamForm, weights: dict[str, float], *, adjusted: bool = True
) -> float | None:
    """Combine the windows into one expected-points figure.

    Weights come from `calibration` (fitted against three seasons), not from a
    guess. Any window with no data has its weight redistributed over the
    others rather than contributing zero, which would silently drag a
    short-season estimate down.
    """
    num, den = 0.0, 0.0
    for name, _ in WINDOWS:
        w = weights.get(f"w_{name}", 0.0)
        if w <= 0:
            continue
        ws = tf.window(name)
        if not ws:
            continue
        val = ws.adj_points_for if adjusted else ws.points_for
        if val is None:
            continue
        num += w * val
        den += w
    return num / den if den > 0 else None


def blended_points_against(
    tf: TeamForm, weights: dict[str, float], *, adjusted: bool = True
) -> float | None:
    num, den = 0.0, 0.0
    for name, _ in WINDOWS:
        w = weights.get(f"w_{name}", 0.0)
        if w <= 0:
            continue
        ws = tf.window(name)
        if not ws:
            continue
        val = ws.adj_points_against if adjusted else ws.points_against
        if val is None:
            continue
        num += w * val
        den += w
    return num / den if den > 0 else None


def describe(tf: TeamForm) -> dict[str, Any]:
    """Flatten a TeamForm for display or for handing to a model."""
    out: dict[str, Any] = {
        "team_id": tf.team_id,
        "games_played": tf.games_played,
        "sparse": tf.sparse,
        "trend_last3_vs_last7": tf.trend,
        "home_away_delta": tf.home_away_delta,
    }
    for name, _ in WINDOWS:
        ws = tf.windows.get(name)
        if not ws:
            continue
        out[name] = {
            "n": ws.n,
            "record": ws.record,
            "points_for": ws.points_for,
            "points_against": ws.points_against,
            "adj_points_for": ws.adj_points_for,
            "adj_points_against": ws.adj_points_against,
            "avg_opponent_rating": ws.avg_opponent_rating,
        }
    for label, ws in (("home", tf.home), ("away", tf.away)):
        if ws:
            out[label] = {
                "n": ws.n,
                "record": ws.record,
                "points_for": ws.points_for,
                "points_against": ws.points_against,
            }
    return out
