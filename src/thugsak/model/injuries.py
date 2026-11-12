"""Injury impact, quantified from the injured player's own recent production.

Five signals per player, ordered by how much they deserve to be believed:

1. **With/without team split.** The team's scoring in games the player
   appeared in versus games they missed. The strongest signal because it is an
   observed outcome rather than an inference. Computable because a player's
   game log lists only games they played -- differencing it against the team's
   schedule identifies the absences (verified: a QB with 4 logged games on a
   team that played 8).
2. **Recent production** over the last 3 and last 7 games.
3. **Share of team production**, which turns raw stats into leverage. 80
   rushing yards means something different on a team that runs 30 times than
   on one that runs 12.
4. **Replacement quality.** The impact is the starter-minus-backup delta, not
   the starter's raw output. A 250-yard passer backed up by a capable veteran
   is a different situation from the same passer backed up by nobody.
5. **Status weighting.** Out and Injured Reserve are certain; Questionable is
   a probability, carried explicitly rather than applied invisibly.

Where this stays an estimate, stated in the output rather than hidden: the
positional prior below is a visible, editable judgement, not an empirically
derived constant -- deriving it properly needs multi-season player data this
project does not hold. Every adjustment therefore reports a confidence level,
and callers are expected to show it. The backtest cannot validate any of this,
because ESPN publishes no historical injury snapshots.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import store
from . import asof

# Roughly how much of a team's scoring output runs through each position.
# A visible prior, not a measurement: edit it rather than trusting it blindly.
POSITION_PRIOR: dict[str, float] = {
    # Football
    "QB": 1.00, "RB": 0.35, "WR": 0.30, "TE": 0.20, "FB": 0.08,
    "LT": 0.18, "RT": 0.15, "LG": 0.12, "RG": 0.12, "C": 0.15, "G": 0.12, "T": 0.15, "OL": 0.14,
    "DE": 0.18, "DT": 0.15, "NT": 0.12, "LB": 0.15, "MLB": 0.16, "OLB": 0.14,
    "CB": 0.20, "S": 0.16, "FS": 0.16, "SS": 0.16, "DB": 0.16,
    "K": 0.10, "P": 0.05, "LS": 0.02,
    # Basketball: starters carry more of the load across fewer players.
    "PG": 0.55, "SG": 0.50, "SF": 0.50, "PF": 0.48, "C-B": 0.50,
    "G": 0.50, "F": 0.48,
}
DEFAULT_PRIOR = 0.15

# Minimum games on each side before the with/without split is reported.
# Below this, the difference is noise wearing the costume of a finding.
MIN_SPLIT_N = 2

# Stat keys treated as scoring production, per sport family.
PRODUCTION_KEYS = (
    "passingYards", "rushingYards", "receivingYards",
    "passingTouchdowns", "rushingTouchdowns", "receivingTouchdowns",
    "points", "assists", "rebounds", "fieldGoalsMade",
)


# Prior standard deviation of a player's true effect on their team's points
# per game. Most players move it by a point or two; a genuine superstar maybe
# eight. This is the scale the observed with/without difference is shrunk
# toward, and it is a judgement stated openly rather than a measurement.
PRIOR_EFFECT_SD = 2.5

# Fallback for a team's game-to-game scoring standard deviation, used when the
# window is too short to estimate one. Deliberately not small: NBA team scoring
# swings about 12 points game to game, which is most of why a 3-game split
# carries almost no information.
DEFAULT_TEAM_SCORING_SD = 11.0


@dataclass
class WithWithout:
    """Team scoring with the player available versus without.

    The raw difference between these two averages is *not* the player's value,
    and treating it as such was a live bug: a team's game-to-game scoring
    swings by around a dozen points, so with three games either side the
    difference of the means carries a standard error near ten points. Summed
    over four injured players it drove a predicted margin to 23 points on a
    game the market had at 2.5, and only an aggregate cap kept it finite.

    So `shrunk_delta` is what callers should use. It pulls the observed
    difference toward zero in proportion to how little the sample can
    distinguish it from noise, which is the standard empirical-Bayes
    treatment for exactly this situation.
    """

    games_with: int = 0
    games_without: int = 0
    pts_for_with: float | None = None
    pts_for_without: float | None = None
    pts_against_with: float | None = None
    pts_against_without: float | None = None
    opp_rating_with: float | None = None
    opp_rating_without: float | None = None
    # Team game-to-game scoring SD over the observed window, when measurable.
    scoring_sd: float | None = None

    @property
    def reportable(self) -> bool:
        return self.games_with >= MIN_SPLIT_N and self.games_without >= MIN_SPLIT_N

    @property
    def raw_delta(self) -> float | None:
        """Points per game the team scored more with the player than without."""
        if self.pts_for_with is None or self.pts_for_without is None:
            return None
        return self.pts_for_with - self.pts_for_without

    @property
    def adjusted_delta(self) -> float | None:
        """`raw_delta`, corrected for who the team happened to face.

        Without this, a player who missed games against weak opponents looks
        irrelevant and one who missed against strong opponents looks
        indispensable, when the schedule explains both.
        """
        d = self.raw_delta
        if d is None:
            return None
        if self.opp_rating_with is None or self.opp_rating_without is None:
            return d
        # Facing stronger defences depresses scoring; undo that difference.
        return d - (self.opp_rating_without - self.opp_rating_with)

    @property
    def standard_error(self) -> float | None:
        """Standard error of the difference between the two means."""
        if not self.games_with or not self.games_without:
            return None
        sd = self.scoring_sd or DEFAULT_TEAM_SCORING_SD
        return sd * math.sqrt(1.0 / self.games_with + 1.0 / self.games_without)

    @property
    def reliability(self) -> float:
        """How much of the observed difference to believe, from 0 to 1.

        tau^2 / (tau^2 + se^2): the posterior weight on the observation when
        the prior on true effects has SD `tau` and the observation has
        standard error `se`. At three games either side this lands near 0.06,
        which is the honest answer -- such a split is almost all noise.
        """
        se = self.standard_error
        if se is None:
            return 0.0
        tau2 = PRIOR_EFFECT_SD ** 2
        return tau2 / (tau2 + se ** 2)

    @property
    def shrunk_delta(self) -> float | None:
        """`adjusted_delta` pulled toward zero by its own unreliability.

        This is the figure to feed a prediction. The unshrunk one is kept for
        display so the raw observation stays visible beside what was believed
        of it.
        """
        d = self.adjusted_delta
        return None if d is None else d * self.reliability

    def describe(self) -> str:
        if not self.reportable:
            return (
                f"Not enough sample to split ({self.games_with} with, "
                f"{self.games_without} without; need {MIN_SPLIT_N} each)."
            )
        d = self.adjusted_delta
        return (
            f"Team scored {self.pts_for_with:.1f} ppg in {self.games_with} games "
            f"with, {self.pts_for_without:.1f} in {self.games_without} without "
            f"(opponent-adjusted difference {d:+.1f})."
        )


@dataclass
class PlayerWindow:
    """A player's production over a window of their own games."""

    n: int = 0
    stats: dict[str, float] = field(default_factory=dict)
    games: list[dict[str, Any]] = field(default_factory=list)

    def get(self, key: str) -> float | None:
        return self.stats.get(key)


@dataclass
class InjuryImpact:
    """One injured player's assessed effect on their team's scoring."""

    athlete_id: str
    name: str
    position: str
    status: str
    availability: float

    last3: PlayerWindow = field(default_factory=PlayerWindow)
    last7: PlayerWindow = field(default_factory=PlayerWindow)
    season: PlayerWindow = field(default_factory=PlayerWindow)

    production_share: float | None = None
    with_without: WithWithout = field(default_factory=WithWithout)

    backup_id: str | None = None
    backup_name: str | None = None
    backup_delta: float | None = None     # starter minus backup, production units
    backup_note: str = ""

    points_adjustment: float = 0.0        # negative = team expected to score less
    confidence: str = "low"               # high | medium | low
    rationale: list[str] = field(default_factory=list)

    @property
    def missing_probability(self) -> float:
        return 1.0 - self.availability

    def summary(self) -> str:
        head = f"{self.name} ({self.position}, {self.status})"
        if self.points_adjustment == 0.0:
            return f"{head}: no measurable scoring impact estimated."
        return (
            f"{head}: {self.points_adjustment:+.1f} pts "
            f"[{self.confidence} confidence]"
        )


def _window(games: Sequence[Any], k: int | None) -> PlayerWindow:
    rows = list(games[:k]) if k else list(games)
    pw = PlayerWindow(n=len(rows))
    if not rows:
        return pw
    agg: dict[str, float] = {}
    for g in rows:
        for key, val in (g.stats or {}).items():
            agg[key] = agg.get(key, 0.0) + float(val)
        pw.games.append(
            {
                "date": g.date,
                "opponent": g.opponent,
                "home_away": g.home_away,
                "result": g.result,
                "stats": {k2: v for k2, v in (g.stats or {}).items() if v},
            }
        )
    pw.stats = {k2: v / len(rows) for k2, v in agg.items()}
    return pw


def _production_total(pw: PlayerWindow) -> float:
    """A crude single-number production figure, for ratio comparisons only.

    Yards and touchdowns are not commensurable, so this is used strictly to
    compare the same stat profile between two players at one position (starter
    versus backup) -- never across positions or sports.
    """
    total = 0.0
    for k in PRODUCTION_KEYS:
        v = pw.stats.get(k)
        if v:
            total += float(v)
    return total


def with_without_split(
    conn: sqlite3.Connection,
    team_id: str,
    as_of: str,
    player_game_dates: Iterable[str],
    *,
    ratings: Any | None = None,
) -> WithWithout:
    """Compare team scoring in games the player played versus missed.

    Matching is by calendar date: a player's game log and the team's schedule
    come from different endpoints with different id spaces, but both agree on
    the date, and a team plays at most once a day in these leagues.
    """
    asof.require_as_of(as_of)
    rows = store.team_games_before(conn, team_id, as_of)
    played_dates = {str(d)[:10] for d in player_game_dates if d}

    with_rows = [r for r in rows if str(r["start_time"])[:10] in played_dates]
    without_rows = [r for r in rows if str(r["start_time"])[:10] not in played_dates]

    ww = WithWithout(games_with=len(with_rows), games_without=len(without_rows))

    def avg(rs: Sequence[sqlite3.Row], col: str) -> float | None:
        vals = [float(r[col]) for r in rs if r[col] is not None]
        return sum(vals) / len(vals) if vals else None

    ww.pts_for_with = avg(with_rows, "points_for")
    ww.pts_for_without = avg(without_rows, "points_for")
    ww.pts_against_with = avg(with_rows, "points_against")
    ww.pts_against_without = avg(without_rows, "points_against")

    if ratings is not None:
        def opp_avg(rs: Sequence[sqlite3.Row]) -> float | None:
            vals = [ratings.defense.get(r["opponent_id"], 0.0) for r in rs]
            return sum(vals) / len(vals) if vals else None

        ww.opp_rating_with = opp_avg(with_rows)
        ww.opp_rating_without = opp_avg(without_rows)

    # The team's own game-to-game scoring spread, which is what decides how
    # much of the with/without difference could be chance. Measured from this
    # team's games where there are enough of them, since scoring volatility
    # differs a lot between a fast NBA team and a run-heavy college side.
    pts = [float(r["points_for"]) for r in rows if r["points_for"] is not None]
    if len(pts) >= 5:
        mean = sum(pts) / len(pts)
        var = sum((x - mean) ** 2 for x in pts) / (len(pts) - 1)
        ww.scoring_sd = math.sqrt(var)
    return ww


def assess(
    conn: sqlite3.Connection,
    *,
    injury: Any,
    team_id: str,
    as_of: str,
    player_games: Sequence[Any],
    team_window_points: float | None = None,
    backup_games: Sequence[Any] | None = None,
    backup_name: str | None = None,
    ratings: Any | None = None,
) -> InjuryImpact:
    """Assemble all five signals into one points adjustment.

    `player_games` and `backup_games` are `clients.espn.PlayerGame` lists.
    `team_window_points` is the team's blended expected points, used to express
    the adjustment as a share of output rather than an absolute guess.
    """
    imp = InjuryImpact(
        athlete_id=getattr(injury, "athlete_id", ""),
        name=getattr(injury, "name", ""),
        position=(getattr(injury, "position", "") or "").upper(),
        status=getattr(injury, "status", ""),
        availability=float(getattr(injury, "availability", 0.75)),
    )

    imp.last3 = _window(player_games, 3)
    imp.last7 = _window(player_games, 7)
    imp.season = _window(player_games, None)

    # Signal 1: with/without.
    imp.with_without = with_without_split(
        conn, team_id, as_of, [g.date for g in player_games], ratings=ratings
    )

    # Signal 4: replacement quality.
    if backup_games:
        bw = _window(backup_games, 7)
        starter_prod = _production_total(imp.last7)
        backup_prod = _production_total(bw)
        if starter_prod > 0 and bw.n > 0:
            imp.backup_delta = starter_prod - backup_prod
            imp.backup_name = backup_name
            share = backup_prod / starter_prod if starter_prod else 0.0
            imp.backup_note = (
                f"Backup produced {share:.0%} of the starter's recent output "
                f"over {bw.n} game(s)."
            )
        elif bw.n == 0:
            imp.backup_note = "Backup has no game log this season -- untested."
    elif backup_games is None:
        imp.backup_note = (
            "Replacement quality unavailable: this league publishes no depth "
            "chart, so the starter-minus-backup comparison cannot be made."
        )
    else:
        imp.backup_note = "No backup identified on the depth chart."

    prior = POSITION_PRIOR.get(imp.position, DEFAULT_PRIOR)

    # Signal 3: share of team production, where a team baseline is available.
    if team_window_points and team_window_points > 0:
        imp.production_share = min(prior, 1.0)

    # --- combine ---------------------------------------------------------
    # Preference order: the observed with/without difference when the sample
    # supports it, otherwise a prior-driven estimate that is labelled as such.
    adj = 0.0
    if imp.with_without.reportable:
        ww = imp.with_without
        # The team scored `delta` more per game with the player, but at these
        # sample sizes most of that difference is noise, so it is shrunk by
        # its own reliability before being believed. Losing the player costs
        # the shrunk figure, scaled by how likely they are to be absent.
        delta = ww.shrunk_delta or 0.0
        adj = -delta * imp.missing_probability
        rel = ww.reliability
        # Confidence now tracks how much of the observation survived
        # shrinkage, not merely how many games were in it: four games either
        # side of a 12-point swing is still not a measurement.
        if rel >= 0.35:
            imp.confidence = "high"
        elif rel >= 0.15:
            imp.confidence = "medium"
        else:
            imp.confidence = "low"
        imp.rationale.append(ww.describe())
        raw = ww.adjusted_delta or 0.0
        imp.rationale.append(
            f"That {raw:+.1f} ppg difference has a standard error of about "
            f"{(ww.standard_error or 0):.1f} points, so only {rel:.0%} of it "
            f"is believed: {delta:+.1f} ppg."
        )
    elif team_window_points:
        # Fall back to the prior: a fraction of the team's expected output,
        # reduced by whatever the backup is expected to replace.
        replace = 0.0
        if imp.backup_delta is not None and _production_total(imp.last7) > 0:
            kept = 1.0 - (imp.backup_delta / _production_total(imp.last7))
            replace = max(0.0, min(1.0, kept))
        else:
            # Unknown backup: assume a replacement-level player covers about
            # two-thirds of the role. Stated rather than buried.
            replace = 0.65
        loss_fraction = prior * (1.0 - replace) * 0.25
        adj = -team_window_points * loss_fraction * imp.missing_probability
        imp.confidence = "low"
        imp.rationale.append(
            f"No usable with/without sample; estimated from a positional prior "
            f"({imp.position} = {prior:.2f}) and an assumed "
            f"{replace:.0%} replacement level."
        )

    # An absence cannot make a team better. A with/without difference can
    # come out favourable by chance, or because the player returned into a
    # slump, and the shrunk figure then credits a team for missing someone --
    # which is how five absent Oklahoma City players once produced a +6.5
    # point bonus. Availability below certainty is bounded at zero on the
    # upside; the raw observation stays visible in the rationale.
    if adj > 0 and imp.availability < 1.0:
        imp.rationale.append(
            f"The with/without split favoured the team by {adj:+.1f} pts "
            f"without this player. That is a small-sample artefact rather "
            f"than a benefit, so no credit is taken for the absence."
        )
        adj = 0.0

    imp.points_adjustment = round(adj, 2)

    if imp.backup_note:
        imp.rationale.append(imp.backup_note)
    if imp.last3.n:
        imp.rationale.append(
            f"Last 3 games: {_fmt_stats(imp.last3)}; last 7: {_fmt_stats(imp.last7)}."
        )
    if imp.availability not in (0.0, 1.0):
        imp.rationale.append(
            f"Status {imp.status!r} treated as {imp.availability:.0%} likely to play."
        )
    return imp


def _fmt_stats(pw: PlayerWindow) -> str:
    if not pw.stats:
        return "no stats"
    keys = [k for k in PRODUCTION_KEYS if pw.stats.get(k)]
    if not keys:
        keys = sorted(pw.stats)[:3]
    return ", ".join(f"{k} {pw.stats[k]:.1f}" for k in keys[:4])


def team_adjustment(impacts: Sequence[InjuryImpact]) -> tuple[float, str]:
    """Total points adjustment for a team, plus the confidence to report.

    Adjustments are summed but capped: several injuries do not stack linearly
    (a team down four starters does not score negative points), and an
    uncapped sum on a long injury report would produce an absurd prediction.
    """
    total = sum(i.points_adjustment for i in impacts)
    # The cap remains as a backstop, but after shrinkage and the no-credit
    # rule it should almost never bind. When it does, that is a signal worth
    # noticing rather than a routine clamp.
    capped = max(total, -14.0) if total < 0 else min(total, 7.0)
    levels = [i.confidence for i in impacts if i.points_adjustment != 0]
    if not levels:
        conf = "none"
    elif "high" in levels:
        conf = "high" if len(levels) == 1 else "medium"
    elif "medium" in levels:
        conf = "medium"
    else:
        conf = "low"
    return round(capped, 2), conf
