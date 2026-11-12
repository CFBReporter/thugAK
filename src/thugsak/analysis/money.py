"""How much money is on a market, and what that does to a recommendation.

A price is an estimate, and like any estimate it has a standard error. On
Kalshi that error is governed by how much money stands behind the price: a
moneyline with $1,000,000 traded has been examined by people with real
capital at stake, while a college basketball total with $150 traded is one
person's guess that nobody has bothered to contradict. Treating those two
prices as equally informative is the single biggest way a model like this
talks itself into bad bets.

So money enters the decision twice, in opposite directions:

1. **Against a deep market, disagreement needs to be bigger to count.** The
   price is well-informed, so the prior that *we* are wrong rises. The
   required edge goes up with the money.
2. **Against a thin market, the edge may well be real but is often
   unusable.** The price can be stale rather than wrong, the book is shallow,
   and a 1-cent mispricing on a $200 market is not an opportunity. So thin
   markets are capped at a small position and flagged, never refused outright
   on the grounds of thinness alone -- genuine mispricings do concentrate
   there, which is the whole reason to look.

The thresholds below are judgements, not measurements. They are stated here
in one place so they can be argued with, and the figures that drive them are
always reported alongside the recommendation.

**Direction of flow is separate from amount.** It lives in `flow.py` and is
corroboration, not a forecast: large money is frequently wrong, and following
it would simply reproduce the market price with a lag. Here it only sharpens
or softens an already-independent conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

# Dollar bands. Chosen on executability rather than on a percentile of the
# slate: $500 is thin whether it sits on a Tuesday MAC game or the Super Bowl,
# because what it governs is whether a 100-contract order moves the price.
TIERS: tuple[tuple[float, str], ...] = (
    (100_000, "deep"),
    (10_000, "active"),
    (1_000, "moderate"),
    (100, "thin"),
    (0, "negligible"),
)

# Minimum skill-adjusted edge, after fees, worth acting on at each tier.
# A deep market has to be wrong by more before we believe it is us who is
# right; a thin one needs less, because nobody has priced it carefully.
REQUIRED_EDGE: dict[str, float] = {
    "deep": 0.05,
    "active": 0.04,
    "moderate": 0.03,
    "thin": 0.03,
    "negligible": 0.03,
}

# Tiers at or below this are capped at a small position whatever the edge.
SMALL_ONLY = ("thin", "negligible")

# Below this there is no usable market: a 100-contract order is most of the
# money that has ever traded, so the quoted price says nothing and would move
# the moment it was touched.
UNUSABLE_BELOW = 250.0


def tier(dollars: float) -> str:
    """Which money band a figure falls in."""
    for floor, name in TIERS:
        if dollars >= floor:
            return name
    return "negligible"


def required_edge(dollars: float) -> float:
    return REQUIRED_EDGE[tier(dollars)]


def describe(dollars: float) -> str:
    """One phrase for how much money is here, in plain words."""
    t = tier(dollars)
    money = f"${dollars:,.0f}"
    return {
        "deep": f"{money} traded -- heavily contested, so the price is well-informed",
        "active": f"{money} traded -- actively priced",
        "moderate": f"{money} traded -- moderately priced",
        "thin": f"{money} traded -- thin, so the price may be stale rather than wrong",
        "negligible": f"{money} traded -- almost no money here, so the price means little",
    }[t]


@dataclass
class GameMoney:
    """Total money on one game's Kalshi markets, and where it sits.

    Reported as a headline because it answers a question the user would
    otherwise have to infer from 60 rows: is this a market worth taking
    seriously at all?
    """

    total_traded: float = 0.0
    total_at_risk: float = 0.0
    by_type: dict[str, float] = field(default_factory=dict)
    markets_counted: int = 0
    busiest_ticker: str | None = None
    busiest_dollars: float = 0.0
    busiest_title: str = ""

    @property
    def tier(self) -> str:
        return tier(self.total_traded)

    def summary(self) -> str:
        if not self.markets_counted:
            return "No Kalshi markets priced for this game."
        parts = [f"${self.total_traded:,.0f} traded across "
                 f"{self.markets_counted} Kalshi markets"]
        if self.by_type:
            split = ", ".join(
                f"{k} ${v:,.0f}"
                for k, v in sorted(self.by_type.items(), key=lambda kv: -kv[1])
                if v > 0
            )
            if split:
                parts.append(split)
        return " · ".join(parts)


_KIND_LABEL = {"game": "moneyline", "spread": "spread", "total": "total"}


def game_money(rungs: Sequence[Any]) -> GameMoney:
    """Aggregate dollars across a game's markets.

    Counts each *market* once even though a market may appear as two rungs
    (a yes side and a no side); double-counting would inflate every total
    market by a factor of two and make a quiet game look busy.
    """
    gm = GameMoney()
    seen: set[str] = set()
    for r in rungs:
        if r.ticker in seen:
            continue
        seen.add(r.ticker)
        d = r.dollars_traded
        gm.total_traded += d
        gm.total_at_risk += r.dollars_at_risk
        gm.markets_counted += 1
        label = _KIND_LABEL.get(r.kind, r.kind)
        gm.by_type[label] = gm.by_type.get(label, 0.0) + d
        if d > gm.busiest_dollars:
            gm.busiest_dollars = d
            gm.busiest_ticker = r.ticker
            gm.busiest_title = r.title
    return gm


@dataclass
class FlowRead:
    """What the money on one market has been doing, as corroboration."""

    imbalance: float | None = None        # -1 all NO aggression .. +1 all YES
    whale_yes_share: float | None = None
    dollars: float = 0.0
    note: str = ""
    agrees: bool | None = None            # with the side we are considering


def read_flow(
    flow: dict[str, Any] | None, *, side: str, model_prob: float | None
) -> FlowRead:
    """Interpret a money-flow summary for the side being recommended.

    `side` is "yes" or "no", because an under is a NO and heavy YES
    aggression is then money against us, not for us. Getting that backwards
    would turn a warning into a confirmation.
    """
    out = FlowRead()
    if not flow:
        return out
    imb = flow.get("imbalance")
    out.imbalance = imb
    out.whale_yes_share = flow.get("whale_consensus_yes_share")
    out.dollars = float(flow.get("total_notional_usd") or 0.0)
    if imb is None:
        return out

    # Aggression toward the side we would be buying.
    toward_us = imb if side == "yes" else -imb
    if abs(imb) < 0.15:
        out.note = "money is split roughly evenly"
        out.agrees = None
    elif toward_us > 0:
        out.agrees = True
        out.note = (
            f"the money is buying the same side ({abs(imb):.0%} one-sided), "
            f"which is corroboration, not proof"
        )
    else:
        out.agrees = False
        out.note = (
            f"the money is {abs(imb):.0%} one-sided *against* this side -- "
            f"the model is taking the other end of an informed flow"
        )
    return out
