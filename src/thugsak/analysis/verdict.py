"""The answer: a predicted score, and five bets with a yes or no on each.

The output is deliberately fixed in shape, because a variable-length ranked
table is what made earlier versions unreadable. Every game produces exactly
the same six things:

    Predicted score       Portland 112 - Oklahoma City 118
    Moneyline, away       BET / SMALL / NO
    Moneyline, home       BET / SMALL / NO
    Spread, away          BET / SMALL / NO
    Spread, home          BET / SMALL / NO
    Total, over or under  BET / SMALL / NO

Nothing else is evaluated. There is no scrolling to find the row that matters,
and "no bet here" is a first-class answer appearing in the same place as a
recommendation rather than an absence the reader has to notice.

Two design decisions carry most of the weight:

**One position per slot.** A 33-rung spread ladder is one disagreement with
the market repeated 33 times. Ranking it by edge reads as 33 independent
opportunities and also selects its own tails, since a 1-cent market needs only
a 2-cent model probability to show a vast relative edge. Each slot therefore
collapses to a single best strike inside a credible probability band.

**Money decides how much edge is enough.** `analysis.money` sets the required
edge from the dollars standing behind the price: a $1m moneyline has to be
wrong by more before we believe it is the market and not us. The dollars are
reported on every row, so a NO is explicable rather than mysterious.

Both the Streamlit page and the MCP tool read this module, so Claude's write-up
and the page cannot disagree about what the recommendation is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import money

# Below this measured skill the model's view of a quantity is not worth
# betting, whatever the arithmetic says.
MIN_SKILL = 0.05

# Credible probability band for a recommendation. Outside it the number comes
# from the tail of a normal distribution fitted to the middle, and real
# scoring margins do not behave normally that far out.
MIN_CREDIBLE_PROB = 0.15
MAX_CREDIBLE_PROB = 0.85
# Prices this low are lottery tickets, where one cent of mispricing swamps
# whatever resolution the model has.
MIN_CREDIBLE_PRICE = 0.07

BET = "BET"
SMALL = "SMALL"
NO = "NO"


# --------------------------------------------------------------------------
# the predicted outcome
# --------------------------------------------------------------------------

@dataclass
class Outlook:
    """The predicted score and what it rests on."""

    away_label: str = "?"
    home_label: str = "?"
    away_points: float | None = None
    home_points: float | None = None
    margin: float | None = None          # home-relative
    total: float | None = None
    winner: str = ""
    win_prob: float | None = None        # for the predicted winner
    confidence: str = "unknown"
    based_on: list[str] = field(default_factory=list)
    market_check: str = ""
    cautions: list[str] = field(default_factory=list)

    @property
    def score_line(self) -> str:
        """"Portland 112 - Oklahoma City 118", rounded to whole points.

        Decimals on a predicted score are false precision: the model's own
        error is around 13 points on an NBA margin, so a tenth of a point is
        noise dressed as accuracy.
        """
        if self.away_points is None or self.home_points is None:
            return "no prediction"
        return (
            f"{self.away_label} {self.away_points:.0f} "
            f"- {self.home_label} {self.home_points:.0f}"
        )

    @property
    def verdict_line(self) -> str:
        if self.margin is None or self.total is None:
            return "no prediction"
        who = f"{self.winner} by {abs(self.margin):.0f}"
        if self.win_prob is not None:
            who += f" ({self.win_prob:.0%} to win)"
        return f"{who}, {self.total:.0f} total points"


# --------------------------------------------------------------------------
# one bet
# --------------------------------------------------------------------------

@dataclass
class Bet:
    """One of the five slots, with a yes or no on it."""

    slot: str            # moneyline_away | moneyline_home | spread_away |
                         # spread_home | total
    label: str           # "Moneyline - Portland"
    bet: str             # what you would buy, in plain words
    recommendation: str = NO
    why: str = ""

    ticker: str | None = None
    # What the bet *is*, carried so it can be settled against a final score
    # later. Without these a completed game can show a recommendation but not
    # whether it won, which is the only thing that makes a lookback useful.
    kind: str | None = None          # game | spread | total
    side: str = "yes"
    threshold: float | None = None
    subject_team: str | None = None

    price: float | None = None
    all_in_cost: float | None = None
    model_prob: float | None = None
    market_prob: float | None = None
    edge: float | None = None            # skill-adjusted, after fees
    required_edge: float | None = None
    ev_per_100: float | None = None

    dollars_traded: float = 0.0
    money_note: str = ""
    flow_note: str = ""

    cautions: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.recommendation in (BET, SMALL)

    def one_liner(self) -> str:
        if not self.actionable:
            return f"{self.label}: NO - {self.why}"
        size = "" if self.recommendation == BET else " (small)"
        return (
            f"{self.label}: BET{size} {self.bet} at {self.price:.2f} - "
            f"edge {self.edge:+.1%} after fees"
        )


# --------------------------------------------------------------------------
# rung selection
# --------------------------------------------------------------------------

def _quotable(r: Any) -> bool:
    """Is there actually a price you could pay?

    An ask of exactly 1.00 means nobody is offering below a dollar: paying $1
    for a contract that pays $1 cannot profit, and Kalshi quotes this on
    near-locks. An ask of 0 is the mirror image. Neither is tradeable, and
    both yield a null edge -- which, left in the pool, once surfaced as "no
    priced market matched this game" on a game with 33 live markets.
    """
    if r.ask is None or r.model_prob is None:
        return False
    return 0.0 < float(r.ask) < 1.0


def _credible(r: Any) -> bool:
    if not _quotable(r):
        return False
    if not (MIN_CREDIBLE_PROB <= r.model_prob <= MAX_CREDIBLE_PROB):
        return False
    return float(r.ask) >= MIN_CREDIBLE_PRICE


def _score(r: Any) -> float:
    e = r.adjusted_edge if r.adjusted_edge is not None else r.net_edge
    return e if e is not None else -9.0


def _best(pool: Sequence[Any]) -> tuple[Any | None, bool]:
    """The best credible rung in a pool, and whether the band bit.

    Returns (rung, tail_only). When nothing in the credible band exists the
    nearest-to-coin-flip rung comes back with tail_only=True, so the caller
    reports the exclusion rather than silently recommending a longshot.
    """
    live = [
        r for r in pool
        if not r.suspect and _quotable(r) and r.net_edge is not None
    ]
    if not live:
        return None, False
    credible = [r for r in live if _credible(r)]
    if credible:
        return max(credible, key=_score), False
    return min(live, key=lambda r: abs((r.model_prob or 0.5) - 0.5)), True


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def _pool_dollars(pool: Sequence[Any]) -> float:
    """Money across every market in a slot, counting each market once.

    Reported even when no single strike is bettable. Without it a row whose
    markets have all stopped quoting read "$0 traded" on a moneyline that had
    turned over $177,000, which misstates the one input the recommendation
    leans on hardest and makes a refusal look like missing data.
    """
    seen: set[str] = set()
    total = 0.0
    for r in pool:
        if r.ticker in seen:
            continue
        seen.add(r.ticker)
        total += r.dollars_traded
    return total


def _decide(
    bet: Bet,
    rung: Any,
    *,
    tail_only: bool,
    skill: float | None,
    quantity: str,
    flow: dict[str, Any] | None,
    sigma: float,
    model_number: float | None,
    pool_dollars: float = 0.0,
) -> None:
    """Fill in recommendation and why. Order matters: cheapest refusal first."""
    if rung is None or rung.net_edge is None:
        bet.recommendation = NO
        bet.dollars_traded = pool_dollars
        if pool_dollars > 0:
            bet.money_note = money.describe(pool_dollars)
            bet.why = (
                f"Kalshi has markets here and ${pool_dollars:,.0f} has traded "
                f"on them, but none is quoting a price you could pay right "
                f"now -- nothing is offered below a dollar"
            )
        else:
            bet.why = (
                "Kalshi is not quoting a tradeable price on this -- there is "
                "nothing offered below a dollar"
            )
        return

    bet.ticker = rung.ticker
    bet.kind = rung.kind
    bet.side = rung.side
    bet.threshold = rung.threshold
    bet.subject_team = rung.subject_team
    bet.bet = rung.bet_title
    bet.price = float(rung.ask) if rung.ask is not None else None
    bet.all_in_cost = float(rung.all_in_cost) if rung.all_in_cost else None
    bet.model_prob = rung.model_prob
    bet.edge = (
        rung.adjusted_edge if rung.adjusted_edge is not None else rung.net_edge
    )
    bet.ev_per_100 = float(rung.ev_per_100) if rung.ev_per_100 else None
    bet.dollars_traded = rung.dollars_traded
    bet.money_note = money.describe(rung.dollars_traded)
    bet.required_edge = money.required_edge(rung.dollars_traded)

    fr = money.read_flow(flow, side=rung.side, model_prob=rung.model_prob)
    # A direction read off a market with no money in it is not a signal, and
    # printing "money is split roughly evenly" beside "$0 traded" reads as a
    # finding when it is an absence.
    if rung.dollars_traded >= money.UNUSABLE_BELOW:
        bet.flow_note = fr.note

    if skill is not None and skill < MIN_SKILL:
        bet.recommendation = NO
        bet.why = (
            f"the model explains only {skill:+.0%} of {quantity} variance in "
            f"this league, so disagreeing with the market is not evidence"
        )
        return

    if tail_only:
        bet.recommendation = NO
        bet.why = (
            f"the only strike showing an edge is a longshot "
            f"({rung.model_prob:.0%} at {bet.price:.2f}), where a curve fitted "
            f"to ordinary games says nothing reliable"
        )
        return

    if rung.dollars_traded < money.UNUSABLE_BELOW:
        bet.recommendation = NO
        bet.why = (
            f"only about ${rung.dollars_traded:,.0f} has ever traded here, so "
            f"the quoted price is not a real market and would move the moment "
            f"you touched it"
        )
        return

    edge, need = bet.edge, bet.required_edge
    tier = money.tier(rung.dollars_traded)
    if edge < 0:
        bet.recommendation = NO
        bet.why = (
            f"the price is already dearer than the model's own estimate "
            f"({edge:+.1%} after fees)"
        )
        return
    if edge < need:
        bet.recommendation = NO
        bet.why = (
            f"only {edge:+.1%} after fees, and against {tier} money "
            f"(${rung.dollars_traded:,.0f}) it would take {need:.0%} before "
            f"the market is likelier wrong than the model"
        )
        return

    # It clears. Size it.
    if tier in money.SMALL_ONLY:
        bet.recommendation = SMALL
        bet.why = (
            f"{edge:+.1%} after fees, but only ${rung.dollars_traded:,.0f} has "
            f"traded -- the edge looks real and the market is too thin to "
            f"trust with size"
        )
    elif fr.agrees is False:
        bet.recommendation = SMALL
        bet.why = (
            f"{edge:+.1%} after fees, but {fr.note} -- worth a small position, "
            f"not a confident one"
        )
    else:
        bet.recommendation = BET
        bet.why = (
            f"{edge:+.1%} after fees against ${rung.dollars_traded:,.0f} of money"
        )
        if fr.agrees:
            bet.why += ", and the flow is on the same side"

    if rung.liquidity_note:
        bet.cautions.append(rung.liquidity_note)
    if model_number is not None and rung.threshold is not None and sigma:
        z = abs(rung.threshold - abs(model_number)) / sigma
        if z > 1.0:
            bet.cautions.append(
                f"This strike sits {z:.1f}x the model's typical error from its "
                f"own projection, so the edge rests on the assumed shape of "
                f"the distribution more than on the projection."
            )


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def _label(labels: dict[str, str], team_id: str | None) -> str:
    if not team_id:
        return "?"
    return labels.get(team_id, team_id.split(":")[-1])


def build_outlook(
    pred: Any,
    *,
    calibration: dict[str, float] | None = None,
    team_labels: dict[str, str] | None = None,
    availability: dict[str, Any] | None = None,
) -> Outlook:
    """The predicted score, and the historical figures behind it."""
    cal = calibration or {}
    labels = team_labels or {}
    home = _label(labels, pred.home_team)
    away = _label(labels, pred.away_team)

    o = Outlook(
        away_label=away, home_label=home,
        away_points=pred.away_points, home_points=pred.home_points,
        margin=pred.spread, total=pred.total,
    )
    if pred.spread is not None:
        o.winner = home if pred.spread > 0 else away
        hp = pred.home_win_prob
        if hp is not None:
            o.win_prob = hp if pred.spread > 0 else 1.0 - hp

    # What it is based on, in the order it actually matters.
    power = pred.anchor("power_rating")
    if power is not None and power.spread is not None:
        fav = home if power.spread > 0 else away
        o.based_on.append(
            f"Ratings solved from every result so far this season favour "
            f"{fav} by {abs(power.spread):.1f} ({power.source})."
        )
    for form, label in ((pred.away_form, away), (pred.home_form, home)):
        w7 = (form or {}).get("last7") or {}
        if w7.get("adj_points_for") is not None:
            o.based_on.append(
                f"{label} over its last 7: {w7['adj_points_for']:.1f} scored, "
                f"{w7['adj_points_against']:.1f} allowed per game, adjusted "
                f"for who they played ({w7.get('record', '')})."
            )
        trend = (form or {}).get("trend_last3_vs_last7")
        if trend is not None and abs(trend) >= 3:
            o.based_on.append(
                f"{label}'s last 3 are {abs(trend):.1f} pts/game "
                f"{'above' if trend > 0 else 'below'} its last 7."
            )
    w = pred.weights or {}
    if w.get("w_season") is not None:
        o.based_on.append(
            f"Those windows are blended at weights fitted on three prior "
            f"seasons (last 3: {w.get('w_last3', 0):.2f}, last 7: "
            f"{w.get('w_last7', 0):.2f}, season: {w.get('w_season', 0):.2f})."
        )
    if pred.injury_adj_home or pred.injury_adj_away:
        o.based_on.append(
            f"Injury adjustment from the absent players' own recent output: "
            f"{home} {pred.injury_adj_home:+.1f}, {away} "
            f"{pred.injury_adj_away:+.1f} pts ({pred.injury_confidence} "
            f"confidence)."
        )
    for tid, blk in ((availability or {}).get("teams") or {}).items():
        for d in (blk.get("detail") or [])[:2]:
            if d.get("kind") != "absent":
                continue
            o.based_on.append(
                f"{blk.get('team_label', tid)}: {d['name']} ({d['position']}) "
                f"has missed {d['consecutive_missed']} straight -- "
                f"{d['usage_share']:.0%} of team usage."
            )

    # How it stacks against the book, which is the only independent check.
    mkt = pred.anchor("market")
    if mkt is None or (mkt.spread is None and mkt.total is None):
        o.cautions.append(
            "No sportsbook line for this game, so there is no check on the "
            "model other than Kalshi itself."
        )
    else:
        bits = []
        if mkt.spread is not None and pred.spread is not None:
            mfav = home if mkt.spread > 0 else away
            bits.append(
                f"book has {mfav} by {abs(mkt.spread):.1f} "
                f"(model differs by {abs(pred.spread - mkt.spread):.1f})"
            )
        if mkt.total is not None and pred.total is not None:
            bits.append(
                f"book total {mkt.total:.1f} "
                f"(model {pred.total - mkt.total:+.1f} against it)"
            )
        o.market_check = "; ".join(bits)

    # Confidence: measured skill and sample size, not a feeling.
    sm = cal.get("skill_margin")
    o.confidence = _confidence(sm, pred)
    if sm is not None:
        o.based_on.append(
            f"On three prior seasons this model explained {sm:+.0%} of the "
            f"variance in margins for this league, against simply predicting "
            f"the league average."
        )
    o.cautions += list(pred.warnings or [])
    return o


def _confidence(skill: float | None, pred: Any) -> str:
    """Plain-language confidence in the predicted score.

    Driven by measured skill and by whether the form windows have anything in
    them. Early in a season both teams may have played twice, and a score
    predicted from two games is a guess however good the model is.
    """
    def played(form: dict[str, Any] | None) -> int:
        # games_played sits at the top level of the form summary, not inside
        # a window. Reading it from last7 silently returned 0 and reported
        # every game as "weak".
        return int((form or {}).get("games_played") or 0)

    games = min(played(pred.home_form), played(pred.away_form))
    if skill is None:
        return "unknown -- this league has not been calibrated"
    if games < 3:
        return (
            f"weak -- one side has played only {games} game(s), so the form "
            f"windows are mostly noise"
        )
    if skill >= 0.30:
        return "fair -- among the better leagues for this model"
    if skill >= 0.15:
        return "moderate"
    return "weak -- the model has little edge on margins in this league"


def build_bets(
    pred: Any,
    rungs: Sequence[Any],
    *,
    calibration: dict[str, float] | None = None,
    team_labels: dict[str, str] | None = None,
    flows: dict[str, dict[str, Any]] | None = None,
) -> list[Bet]:
    """The five slots, always in the same order.

    `flows` maps a ticker to a money-flow summary, for whichever markets the
    caller chose to fetch. Absent, the money *amount* still counts but its
    direction does not -- so a first pass can build the bets, and a second
    pass re-run with flows for the five markets that were actually chosen.
    """
    cal = calibration or {}
    labels = team_labels or {}
    flows = flows or {}
    home_id, away_id = pred.home_team, pred.away_team
    home, away = _label(labels, home_id), _label(labels, away_id)

    skill_m, skill_t = cal.get("skill_margin"), cal.get("skill_total")
    out: list[Bet] = []

    for slot, team_id, team in (
        ("moneyline_away", away_id, away),
        ("moneyline_home", home_id, home),
    ):
        bet = Bet(slot=slot, label=f"Moneyline - {team}", bet=f"{team} to win")
        pool = [r for r in rungs if r.kind == "game" and r.subject_team == team_id]
        rung, tail = _best(pool)
        bet.market_prob = _market_win_prob(pred, team_id)
        _decide(bet, rung, tail_only=tail, skill=skill_m, quantity="margin",
                flow=flows.get(getattr(rung, "ticker", "") or ""),
                sigma=pred.sigma_margin, model_number=None,
                pool_dollars=_pool_dollars(pool))
        out.append(bet)

    for slot, team_id, team in (
        ("spread_away", away_id, away),
        ("spread_home", home_id, home),
    ):
        bet = Bet(slot=slot, label=f"Spread - {team}", bet=f"{team} to cover")
        pool = [r for r in rungs if r.kind == "spread" and r.subject_team == team_id]
        rung, tail = _best(pool)
        _decide(bet, rung, tail_only=tail, skill=skill_m, quantity="margin",
                flow=flows.get(getattr(rung, "ticker", "") or ""),
                sigma=pred.sigma_margin, model_number=pred.spread,
                pool_dollars=_pool_dollars(pool))
        out.append(bet)

    bet = Bet(slot="total", label="Total - over or under", bet="total points")
    pool = [r for r in rungs if r.kind == "total"]
    rung, tail = _best(pool)
    _decide(bet, rung, tail_only=tail, skill=skill_t, quantity="total",
            flow=flows.get(getattr(rung, "ticker", "") or ""),
            sigma=pred.sigma_total, model_number=pred.total,
            pool_dollars=_pool_dollars(pool))
    if skill_t is not None and 0 < skill_t < 0.10:
        bet.cautions.append(
            f"Totals are this model's weakest output in every league "
            f"({skill_t:+.0%} of variance here)."
        )
    out.append(bet)
    return out


def _market_win_prob(pred: Any, team_id: str) -> float | None:
    mkt = pred.anchor("market")
    if mkt is None or mkt.home_win_prob is None:
        return None
    return (
        mkt.home_win_prob if team_id == pred.home_team
        else 1.0 - mkt.home_win_prob
    )


def chosen_tickers(bets: Sequence[Bet]) -> list[str]:
    """The markets the five slots landed on, for a follow-up flow fetch."""
    seen: list[str] = []
    for b in bets:
        if b.ticker and b.ticker not in seen:
            seen.append(b.ticker)
    return seen


def market_census(rungs: Sequence[Any]) -> dict[str, dict[str, int]]:
    """Per bet type: how many strikes exist, are quotable, and are credible.

    Without this a NO looks like a missing feature rather than a judgement.
    """
    out: dict[str, dict[str, int]] = {}
    for kind in ("game", "spread", "total"):
        group = [r for r in rungs if r.kind == kind]
        out["moneyline" if kind == "game" else kind] = {
            "listed": len(group),
            "quotable": sum(1 for r in group if _quotable(r)),
            "credible": sum(1 for r in group if _credible(r)),
            "flagged_suspect": sum(1 for r in group if r.suspect),
        }
    return out


# Suggested stake, because "SMALL" on its own leaves the reader to guess what
# small means. Fractions of whatever the reader's normal unit is -- the app
# has no idea what that is and should not pretend to.
STAKE = {
    BET: "full stake",
    SMALL: "about a third of a normal stake",
}


def rank_bets(bets: Sequence[Bet]) -> list[Bet]:
    """Actionable bets, best first.

    Ordered by how far the edge clears the bar that market's money demands,
    not by raw edge. A 7% edge on a market needing 3% is a stronger call than
    an 8% edge on one needing 5%, and ranking by the raw number would get
    that backwards. Full-size bets sort above small ones regardless, since a
    downgrade already encodes a reason to be careful.
    """
    def key(b: Bet) -> tuple[int, float, float]:
        clearance = (b.edge or 0.0) - (b.required_edge or 0.0)
        return (0 if b.recommendation == BET else 1, -clearance, -b.dollars_traded)

    return sorted((b for b in bets if b.actionable), key=key)


def best_bet(bets: Sequence[Bet]) -> Bet | None:
    """The single bet to place, or None. The definitive answer."""
    ranked = rank_bets(bets)
    return ranked[0] if ranked else None


def the_call(bets: Sequence[Bet]) -> str:
    """One sentence a person can act on without reading the table.

    Five rows of BET/SMALL/NO is a finding, not an instruction. This states
    the instruction, including when the instruction is to do nothing -- which
    it usually is, and which is a real answer rather than a shrug.
    """
    ranked = rank_bets(bets)
    if not ranked:
        return "Do not bet this game. None of the five clears its bar."

    top = ranked[0]
    out = (
        f"Bet {top.bet} at {top.price:.2f} "
        f"({top.all_in_cost:.3f} all-in) -- {STAKE[top.recommendation]}. "
        f"Edge {top.edge:+.1%} after fees against a bar of "
        f"{top.required_edge:.0%}."
    )
    rest = ranked[1:]
    if rest:
        out += " Also playable: " + ", ".join(
            f"{b.bet} at {b.price:.2f} ({STAKE[b.recommendation]})" for b in rest
        )
        out += "."
    return out


def summarise(bets: Sequence[Bet]) -> str:
    """One line, for a header or a chat reply."""
    act = [b for b in bets if b.actionable]
    if not act:
        return "No bet worth placing on this game -- all five pass."
    return " | ".join(b.one_liner() for b in rank_bets(bets))
