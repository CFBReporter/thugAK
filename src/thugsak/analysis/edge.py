"""Compare model probabilities against Kalshi ladder prices, net of fees.

This is where a prediction becomes a decision. Kalshi lists spread and total
markets as ladders of independent binaries, so one predicted total implies a
probability at every strike, and each strike has its own price and its own
edge. Ladder tails are where mispricings concentrate, because the market's
attention sits near the line.

Every number here is **after fees**. The quoted ask is not the cost: the
all-in cost is ask plus the per-contract fee, and that is what a model
probability has to beat. Comparing a model probability against a raw ask is
the standard way these tools invent edges that evaporate on contact.

Edges are also discounted by liquidity. A 3-cent edge against 40 resting
contracts is not a 3-cent edge if the intended position is 500, so the
orderbook is walked and the achievable fill price is used where a book is
available.

A sanity backstop runs last: anything claiming more than ~15% edge is far more
likely a matching or parsing fault than a real opportunity, and it is labelled
as suspect rather than ranked first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Sequence

from . import fees
from ..matching import parse_ticker

# Above this, an "edge" is treated as a bug until proven otherwise.
SUSPECT_EDGE = 0.15
# Default position size used for fee-per-contract and fill-cost maths.
DEFAULT_CONTRACTS = 100


@dataclass
class Rung:
    """One tradeable market with the model's view of it."""

    ticker: str
    kind: str                     # game | spread | total
    title: str
    threshold: float | None
    subject_team: str | None

    # Which half of the binary this rung prices. Kalshi lists totals only as
    # "Over X", so the under is bought as NO on an over market -- without this
    # there is no way to recommend an under at all.
    side: str = "yes"

    ask: Decimal | None = None
    bid: Decimal | None = None
    # Last traded price, kept only so the dollar estimates below survive a
    # market with no live quote. A closed or suspended market still has a
    # trading history, and reporting "$0 traded" on one that has turned over
    # six figures misstates the single input the recommendation leans on most.
    last_price: Decimal | None = None
    model_prob: float | None = None

    fee_per_contract: Decimal | None = None
    all_in_cost: Decimal | None = None
    net_edge: float | None = None        # model_prob - all_in_cost
    ev_per_100: Decimal | None = None
    breakeven: Decimal | None = None

    volume: float = 0.0
    open_interest: float = 0.0
    fill_price: Decimal | None = None
    fillable: float | None = None
    liquidity_note: str = ""

    suspect: bool = False
    notes: list[str] = field(default_factory=list)
    # net_edge scaled by the model's measured skill at this quantity. Ranking
    # uses this; net_edge stays as the unmodified arithmetic.
    adjusted_edge: float | None = None

    @property
    def bet_title(self) -> str:
        """What you are actually buying, phrased so it reads as a bet.

        A NO on "Over 252.5 points scored" is an under, and saying so is the
        difference between a recommendation and a puzzle.
        """
        if self.side == "yes":
            return self.title
        if self.kind == "total" and self.threshold is not None:
            return f"Under {self.threshold} points"
        if self.kind == "spread" and self.threshold is not None:
            return f"NOT {self.title}"
        return f"NOT {self.title}"

    @property
    def _reference_price(self) -> float | None:
        """A price to value contracts at, preferring the live quote.

        Falls back to bid then last trade, because a market can be quoted on
        one side only, or not at all once it has closed, while still having
        traded heavily.
        """
        for px in (self.ask, self.bid, self.last_price):
            if px is not None and float(px) > 0:
                return float(px)
        return None

    @property
    def dollars_traded(self) -> float:
        """Roughly how much money has changed hands on this market.

        Contracts traded times a representative price. It is an estimate --
        the exact figure would weight every trade by its own price -- but the
        decision only needs the order of magnitude: whether the price in front
        of you is backed by $200 or $200,000. That distinction decides whether
        a disagreement with it is information or noise.
        """
        px = self._reference_price
        return self.volume * px if px is not None else 0.0

    @property
    def dollars_at_risk(self) -> float:
        """Money currently held in open positions, as opposed to money that
        has merely traded. Open interest is the better gauge of conviction:
        volume counts a contract every time it changes hands."""
        px = self._reference_price
        return self.open_interest * px if px is not None else 0.0

    @property
    def tradeable(self) -> bool:
        return (
            self.net_edge is not None
            and self.net_edge > 0
            and not self.suspect
            and self.ask is not None
        )

    @property
    def edge_pct(self) -> float | None:
        return self.net_edge * 100 if self.net_edge is not None else None

    def describe(self) -> str:
        if self.model_prob is None or self.ask is None:
            return f"{self.title}: no model view"
        line = (
            f"{self.title} | ask {float(self.ask):.2f} "
            f"(all-in {float(self.all_in_cost or 0):.4f}) | "
            f"model {self.model_prob:.1%} | edge {self.net_edge:+.1%}"
        )
        if self.suspect:
            line += "  [SUSPECT]"
        if self.liquidity_note:
            line += f"  ({self.liquidity_note})"
        return line


def _model_prob_for(pred: Any, parsed: Any, market: Any) -> tuple[float | None, str]:
    """The model's probability that this specific market resolves YES."""
    kind = parsed.kind

    if kind == "total":
        if parsed.threshold is None:
            return None, "total market with no readable strike"
        p = pred.prob_total_over(parsed.threshold)
        return p, f"P(total > {parsed.threshold})"

    if kind == "spread":
        if parsed.threshold is None:
            return None, "spread market with no readable strike"
        team = parsed.team_code
        # The ticker's team code is Kalshi's abbreviation; the prediction uses
        # canonical ids, so compare on the resolved subject where available.
        subject = getattr(market, "_subject_team", None)
        p = pred.prob_spread_over(parsed.threshold, team=subject)
        who = subject or team or "home"
        return p, f"P({who} wins by > {parsed.threshold})"

    if kind == "game":
        subject = getattr(market, "_subject_team", None)
        hp = pred.home_win_prob
        if hp is None:
            return None, "no win probability"
        if subject is None:
            return None, "winner market with unresolved subject team"
        p = hp if subject == pred.home_team else 1.0 - hp
        return p, f"P({subject} wins)"

    return None, f"unsupported market kind {kind!r}"


def _last_for_side(market: Any, side: str) -> Decimal | None:
    """Last trade price from the requested side's point of view.

    Kalshi reports one last price, quoted YES. The NO side of the same trade
    settled at one minus that, so a NO rung valued at the YES price would
    misstate its dollars whenever the market is far from even.
    """
    last = getattr(market, "last_price", None)
    if last is None:
        return None
    return last if side == "yes" else Decimal(1) - last


def evaluate_market(
    pred: Any,
    market: Any,
    *,
    side: str = "yes",
    fee_multiplier: Decimal | float = 1,
    contracts: float = DEFAULT_CONTRACTS,
    book: dict[str, list[tuple[Decimal, float]]] | None = None,
) -> Rung | None:
    """Score one side of one market against the prediction.

    `side` selects which half of the binary is being bought. YES is the
    default and covers moneylines and team spreads, both of which Kalshi
    lists as separate markets per team. Totals are listed only as "Over X",
    so an under is NO on an over market -- the model probability is the
    complement and the price is the no-ask, which is not one minus the
    yes-ask once the spread is wide.
    """
    parsed = parse_ticker(market.ticker, getattr(market, "title", ""))
    if parsed is None:
        return None
    if side not in ("yes", "no"):
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

    rung = Rung(
        ticker=market.ticker,
        kind=parsed.kind,
        title=getattr(market, "title", "") or market.ticker,
        threshold=parsed.threshold,
        subject_team=getattr(market, "_subject_team", None) or parsed.team_code,
        side=side,
        ask=market.yes_ask if side == "yes" else market.no_ask,
        bid=market.yes_bid if side == "yes" else market.no_bid,
        last_price=_last_for_side(market, side),
        volume=float(getattr(market, "volume", 0) or 0),
        open_interest=float(getattr(market, "open_interest", 0) or 0),
    )

    prob, basis = _model_prob_for(pred, parsed, market)
    if prob is not None and side == "no":
        prob = 1.0 - prob
        basis = f"1 - [{basis}]"
    rung.model_prob = prob
    rung.notes.append(basis)
    if prob is None or rung.ask is None or rung.ask <= 0 or rung.ask >= 1:
        if rung.ask is None:
            rung.notes.append(f"no {side}-ask quoted")
        return rung

    # Walk the book when we have one: the achievable price, not the top tick.
    price = rung.ask
    if book:
        from ..clients.kalshi import cost_to_fill

        # Buying one side lifts the resting orders on the other.
        levels = book.get("no" if side == "yes" else "yes") or []
        avg, filled = cost_to_fill(levels, contracts)
        if avg is not None:
            rung.fill_price, rung.fillable = avg, filled
            if filled < contracts:
                rung.liquidity_note = (
                    f"only {filled:,.0f} of {contracts:,.0f} fillable"
                )
            if avg > price:
                price = avg
                rung.liquidity_note = (
                    rung.liquidity_note
                    or f"fill at {float(avg):.3f} vs ask {float(rung.ask):.2f}"
                )

    rung.fee_per_contract = fees.fee_per_contract(
        contracts, price, fee_multiplier=fee_multiplier
    )
    rung.all_in_cost = price + rung.fee_per_contract
    rung.breakeven = rung.all_in_cost
    rung.net_edge = float(Decimal(str(prob)) - rung.all_in_cost)
    rung.ev_per_100 = fees.expected_value(
        prob, price, 100, fee_multiplier=fee_multiplier
    )

    if rung.net_edge > SUSPECT_EDGE:
        rung.suspect = True
        rung.notes.append(
            f"Claimed edge of {rung.net_edge:+.1%} exceeds the {SUSPECT_EDGE:.0%} "
            f"plausibility bound. Almost always a matching or parsing fault "
            f"rather than a real opportunity -- verify the strike and teams "
            f"before acting."
        )
    if rung.volume < 10:
        rung.notes.append(
            f"Very thin: {rung.volume:,.0f} contracts traded. The price may be "
            f"stale rather than wrong."
        )
    return rung


def evaluate_ladder(
    pred: Any,
    markets: Sequence[Any],
    *,
    fee_multiplier: Decimal | float = 1,
    contracts: float = DEFAULT_CONTRACTS,
    books: dict[str, dict[str, list[tuple[Decimal, float]]]] | None = None,
) -> list[Rung]:
    """Score every market for a game, best honest edge first.

    Suspect rungs are kept but sorted last, so they are visible for debugging
    without ever topping the list.
    """
    out: list[Rung] = []
    for m in markets:
        book = (books or {}).get(m.ticker)
        r = evaluate_market(
            pred, m, fee_multiplier=fee_multiplier, contracts=contracts, book=book
        )
        if r is not None:
            out.append(r)
    out.sort(
        key=lambda r: (
            r.suspect,
            -(r.net_edge if r.net_edge is not None else -99),
        )
    )
    return out


def best_bets(rungs: Iterable[Rung], *, min_edge: float = 0.02) -> list[Rung]:
    """Rungs worth acting on, judged on the skill-discounted edge.

    Uses `adjusted_edge` when present so a league where the model has no
    demonstrated skill at this quantity yields no recommendations, however
    large the raw arithmetic gap.
    """
    out = []
    for r in rungs:
        if not r.tradeable:
            continue
        edge = r.adjusted_edge if r.adjusted_edge is not None else r.net_edge
        if edge is not None and edge >= min_edge:
            out.append(r)
    out.sort(
        key=lambda r: -(r.adjusted_edge if r.adjusted_edge is not None else r.net_edge or 0)
    )
    return out


# A model whose point estimate is no better than the league average cannot
# meaningfully disagree with the market, however large the arithmetic gap
# looks. Below this measured skill, an edge derived from that estimate is
# treated as noise.
MIN_USEFUL_SKILL = 0.05
# More than this share of one ladder showing the same-direction edge means the
# model simply disagrees with the market's line, not that it found many bets.
ONE_SIDED_SHARE = 0.6


def diagnose_ladder(
    rungs: Sequence[Rung],
    pred: Any,
    calibration: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Catch the failure mode where one disagreement masquerades as many bets.

    When a model's total sits above the market's, *every* OVER on the ladder
    prices as cheap and every UNDER as dear. Those rungs are not independent
    opportunities -- they are one directional view, repeated. Presented as a
    ranked list they invite stacking correlated positions on a single
    guess, which is how a modest model error becomes a large loss.

    Two further checks:

    - the model's disagreement with the market, which is the real quantity in
      question, rather than the per-rung arithmetic;
    - the league's *measured* skill at the quantity involved, so a total-based
      edge from a model with no total skill is discounted to nothing.
    """
    cal = calibration or {}
    out: dict[str, Any] = {"warnings": [], "one_sided": {}, "skill": {}}

    for kind, skill_key, pred_attr, market_attr in (
        ("total", "skill_total", "total", "total"),
        ("spread", "skill_margin", "spread", "spread"),
    ):
        group = [r for r in rungs if r.kind == kind and r.net_edge is not None]
        if not group:
            continue

        skill = cal.get(skill_key)
        if skill is not None:
            out["skill"][kind] = skill
            if skill < MIN_USEFUL_SKILL:
                out["warnings"].append(
                    f"The {kind} model explains only {skill:+.1%} of this "
                    f"league's variance, which is close to no better than "
                    f"predicting the league average. Any {kind} edge below is "
                    f"more likely model error than mispricing."
                )

        positive = [r for r in group if r.net_edge > 0.02]
        share = len(positive) / len(group)

        # A totals ladder is all OVERs, so a one-directional view shows up as
        # a high share of the whole ladder. A spread ladder carries strikes for
        # both teams, so the same view instead concentrates every positive
        # rung on one subject team while the other side goes negative -- which
        # the share test alone would miss entirely.
        #
        # Only spread and winner markets have a subject team; a totals market
        # is about the game, so attributing one there would be meaningless.
        subjects = (
            {r.subject_team for r in positive if r.subject_team}
            if kind != "total"
            else set()
        )
        concentrated = len(positive) >= 3 and len(subjects) == 1

        if (share >= ONE_SIDED_SHARE or concentrated) and len(positive) >= 3:
            out["one_sided"][kind] = {
                "rungs_with_edge": len(positive),
                "rungs_total": len(group),
                "share": round(share, 2),
                "all_on_one_team": sorted(subjects)[0] if concentrated else None,
            }
            model_val = getattr(pred, pred_attr, None)
            mkt = pred.anchor("market") if hasattr(pred, "anchor") else None
            mkt_val = getattr(mkt, market_attr, None) if mkt else None
            gap = (
                f" The model's {kind} is {model_val:+.1f} against the market's "
                f"{mkt_val:+.1f}."
                if model_val is not None and mkt_val is not None
                else ""
            )
            who = (
                f" Every one of them is on {sorted(subjects)[0]}."
                if concentrated else ""
            )
            out["warnings"].append(
                f"{len(positive)} of {len(group)} {kind} strikes show a "
                f"positive edge in the same direction.{who} That is one "
                f"disagreement with the market's {kind}, not "
                f"{len(positive)} independent bets.{gap} Treat them as a "
                f"single position and size accordingly."
            )
    return out


def apply_skill_discount(
    rungs: Sequence[Rung], calibration: dict[str, float] | None = None
) -> None:
    """Annotate each rung with a skill-discounted edge, in place.

    `net_edge` stays as the honest arithmetic. `adjusted_edge` scales it by how
    much the model is actually known to know about that quantity, which is what
    ranking should use -- otherwise a league where the model has no total skill
    still produces a confident-looking list of total bets.
    """
    cal = calibration or {}
    for r in rungs:
        if r.net_edge is None:
            continue
        key = "skill_total" if r.kind == "total" else "skill_margin"
        skill = cal.get(key)
        if skill is None:
            r.adjusted_edge = r.net_edge
            continue
        weight = max(0.0, min(1.0, skill / 0.25))
        r.adjusted_edge = r.net_edge * weight
        if weight < 0.5:
            r.notes.append(
                f"Edge discounted to {r.adjusted_edge:+.2%} because the model "
                f"explains only {skill:+.1%} of {r.kind} variance in this league."
            )


def find_arbitrage(
    markets: Sequence[Any], *, fee_multiplier: Decimal | float = 1
) -> list[dict[str, Any]]:
    """Complementary pairs that together cost under $1 after fees.

    Reported separately from model-driven edges because it needs no model to
    be true: if both sides are cheap enough, the payout is locked whatever
    happens. Pairs are grouped by event, then matched where two winner markets
    cover opposite outcomes of the same game.
    """
    from ..matching import group_by_event

    out: list[dict[str, Any]] = []
    for event_key, group in group_by_event(markets).items():
        winners = [
            m for m in group
            if (p := parse_ticker(m.ticker)) is not None and p.kind == "game"
        ]
        if len(winners) != 2:
            continue
        a, b = winners
        if a.yes_ask is None or b.yes_ask is None:
            continue
        ok, profit = fees.arbitrage_check(
            a.yes_ask, b.yes_ask, 100, fee_multiplier=fee_multiplier
        )
        if ok:
            out.append(
                {
                    "event": event_key,
                    "legs": [a.ticker, b.ticker],
                    "prices": [float(a.yes_ask), float(b.yes_ask)],
                    "profit_per_contract": float(profit),
                    "note": "Both sides after fees cost under $1; payout is locked.",
                }
            )
    return out
