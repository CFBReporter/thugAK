"""Money-flow analysis: where the big money is, weighted by how big it is.

The guiding principle is that a 3,406-contract trade and a 1-contract trade are
not one vote each. Every aggregate here is weighted by **notional** --
``price x contracts`` -- rather than counted, because that is the dollar
conviction behind a position. Ranking by contract count alone would rate 1,000
contracts at 3c (a $30 punt) above 100 at 90c (a $90 position).

What the data supports, verified live on Kalshi's most liquid NFL market: the
last 1,000 trades split 932 YES vs 68 NO takers with individual trades up to
3,406 contracts, so the asymmetry is real and measurable.

Signals, in rough order of how much weight they deserve:

1. **Taker imbalance** -- the aggressor crossed the spread to get filled, which
   is a stronger statement than resting an order and waiting.
2. **Whale consensus** -- what the largest positions did, independently of the
   long tail of small trades.
3. **Open interest against price** -- distinguishes new money backing a move
   from shorts merely covering, which look identical on a price chart.
4. **Block trades** -- negotiated off-book and reported separately, never
   averaged into the general pool.

Whale thresholds are computed **per market** from its own size distribution: a
whale on a thin NCAAMB market is a minnow on NFL primetime, so a fixed
contract threshold would find whales everywhere or nowhere.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..model.asof import parse_ts

# Exponential decay applied to trade weights: this morning's money should
# outweigh last Tuesday's without discarding it.
DEFAULT_HALFLIFE_HOURS = 12.0
# Trades at or above this size percentile within the market count as whales.
WHALE_PERCENTILE = 0.95
# How many of the largest trades the "whale consensus" reads.
TOP_N_WHALES = 20


@dataclass
class WhaleTrade:
    """A single large trade, as surfaced to the user."""

    trade_id: str
    created_time: str
    side: str                 # "yes" | "no" -- the aggressor's direction
    contracts: float
    price: float
    notional: float
    is_block: bool
    age_hours: float

    @property
    def label(self) -> str:
        tag = " BLOCK" if self.is_block else ""
        return (
            f"{self.side.upper()} {self.contracts:,.0f} @ {self.price:.2f} "
            f"= ${self.notional:,.0f}{tag}"
        )


@dataclass
class FlowAnalysis:
    ticker: str
    n_trades: int = 0
    total_contracts: float = 0.0
    total_notional: float = 0.0

    # -1 (all NO) .. +1 (all YES), weighted by notional and time-decayed.
    imbalance: float = 0.0
    imbalance_raw: float = 0.0        # same, without time decay
    count_imbalance: float = 0.0      # unweighted, for comparison

    yes_notional: float = 0.0
    no_notional: float = 0.0

    whale_threshold: float = 0.0      # contracts
    whales: list[WhaleTrade] = field(default_factory=list)
    whale_consensus: float | None = None   # notional-weighted YES share of whales
    whale_notional: float = 0.0

    blocks: list[WhaleTrade] = field(default_factory=list)
    block_notional: float = 0.0

    # Open-interest reading, from candlesticks.
    price_change: float | None = None
    oi_change: float | None = None
    oi_reading: str = "unknown"
    oi_strength: str = "unknown"

    vwap: float | None = None
    last_price: float | None = None

    window_hours: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def direction(self) -> str:
        if self.imbalance > 0.15:
            return "yes"
        if self.imbalance < -0.15:
            return "no"
        return "balanced"

    @property
    def conviction(self) -> str:
        """How much the imbalance deserves to be leaned on."""
        a = abs(self.imbalance)
        if self.n_trades < 10 or self.total_notional < 500:
            return "insufficient"
        if a > 0.6:
            return "strong"
        if a > 0.3:
            return "moderate"
        if a > 0.15:
            return "slight"
        return "none"

    def summary(self) -> str:
        if self.conviction == "insufficient":
            return (
                f"Too little flow to read ({self.n_trades} trades, "
                f"${self.total_notional:,.0f} notional)."
            )
        side = self.direction.upper()
        parts = [
            f"{self.conviction.capitalize()} {side} flow "
            f"(imbalance {self.imbalance:+.2f}) on ${self.total_notional:,.0f} "
            f"across {self.n_trades} trades."
        ]
        if self.whale_consensus is not None:
            parts.append(
                f"Largest {len(self.whales)} trades "
                f"({self.whale_notional / max(self.total_notional, 1):.0%} of notional) "
                f"were {self.whale_consensus:.0%} YES."
            )
        if self.blocks:
            parts.append(
                f"{len(self.blocks)} block trade(s), ${self.block_notional:,.0f}."
            )
        if self.oi_reading != "unknown":
            parts.append(self.oi_reading)
        return " ".join(parts)

    def divergence(self, market_price: float | None) -> str | None:
        """Flag flow that the price has not yet reflected.

        Heavy one-sided aggression on a market that has barely moved is the
        most actionable pattern here: someone is paying up and the price has
        not caught up yet.
        """
        if market_price is None or self.price_change is None:
            return None
        if self.conviction in ("insufficient", "none", "slight"):
            return None
        moved_up = self.price_change > 0.02
        moved_down = self.price_change < -0.02
        if self.direction == "yes" and not moved_up:
            return (
                "Strong YES flow with price flat or falling -- money is going in "
                "that the price has not reflected."
            )
        if self.direction == "no" and not moved_down:
            return (
                "Strong NO flow with price flat or rising -- money is going in "
                "that the price has not reflected."
            )
        return None


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = q * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def analyze_trades(
    ticker: str,
    trades: Sequence[Any],
    *,
    now: datetime | None = None,
    halflife_hours: float = DEFAULT_HALFLIFE_HOURS,
    whale_percentile: float = WHALE_PERCENTILE,
    top_n: int = TOP_N_WHALES,
) -> FlowAnalysis:
    """Analyse a market's trade history.

    `trades` are `clients.kalshi.Trade` objects (or anything exposing the same
    attributes).
    """
    fa = FlowAnalysis(ticker=ticker)
    if not trades:
        fa.notes.append("no trades in window")
        return fa

    now = now or datetime.now(timezone.utc)
    rows: list[tuple[Any, float, float, float]] = []  # trade, notional, age_h, weight

    for t in trades:
        count = float(getattr(t, "count", 0) or 0)
        if count <= 0:
            continue
        notional = float(getattr(t, "notional", 0) or 0)
        try:
            age_h = max((now - parse_ts(t.created_time)).total_seconds() / 3600.0, 0.0)
        except Exception:
            age_h = 0.0
        weight = 0.5 ** (age_h / halflife_hours) if halflife_hours > 0 else 1.0
        rows.append((t, notional, age_h, weight))

    if not rows:
        fa.notes.append("no sized trades in window")
        return fa

    fa.n_trades = len(rows)
    fa.total_contracts = sum(float(t.count) for t, _, _, _ in rows)
    fa.total_notional = sum(n for _, n, _, _ in rows)
    ages = [a for _, _, a, _ in rows]
    fa.window_hours = max(ages) if ages else 0.0

    # Notional-weighted directional flow, with and without time decay.
    yes_n = sum(n for t, n, _, _ in rows if t.taker_side == "yes")
    no_n = sum(n for t, n, _, _ in rows if t.taker_side == "no")
    fa.yes_notional, fa.no_notional = yes_n, no_n
    if yes_n + no_n > 0:
        fa.imbalance_raw = (yes_n - no_n) / (yes_n + no_n)

    yes_w = sum(n * w for t, n, _, w in rows if t.taker_side == "yes")
    no_w = sum(n * w for t, n, _, w in rows if t.taker_side == "no")
    if yes_w + no_w > 0:
        fa.imbalance = (yes_w - no_w) / (yes_w + no_w)

    # Unweighted count imbalance, kept so the UI can show how much of the
    # signal comes from size rather than from the number of participants.
    n_yes = sum(1 for t, _, _, _ in rows if t.taker_side == "yes")
    n_no = sum(1 for t, _, _, _ in rows if t.taker_side == "no")
    if n_yes + n_no:
        fa.count_imbalance = (n_yes - n_no) / (n_yes + n_no)

    # Whale tier, from this market's own distribution.
    counts = [float(t.count) for t, _, _, _ in rows]
    fa.whale_threshold = _percentile(counts, whale_percentile)

    def mk(t: Any, notional: float, age_h: float) -> WhaleTrade:
        price = float(t.yes_price if t.taker_side == "yes" else t.no_price)
        return WhaleTrade(
            trade_id=getattr(t, "trade_id", ""),
            created_time=getattr(t, "created_time", ""),
            side=getattr(t, "taker_side", ""),
            contracts=float(t.count),
            price=price,
            notional=notional,
            is_block=bool(getattr(t, "is_block_trade", False)),
            age_hours=age_h,
        )

    ranked = sorted(rows, key=lambda r: r[1], reverse=True)
    fa.whales = [mk(t, n, a) for t, n, a, _ in ranked[:top_n]]
    fa.whale_notional = sum(w.notional for w in fa.whales)
    if fa.whale_notional > 0:
        yes_whale = sum(w.notional for w in fa.whales if w.side == "yes")
        fa.whale_consensus = yes_whale / fa.whale_notional

    # Block trades: negotiated, so reported on their own terms.
    fa.blocks = [
        mk(t, n, a) for t, n, a, _ in rows if bool(getattr(t, "is_block_trade", False))
    ]
    fa.block_notional = sum(b.notional for b in fa.blocks)

    # VWAP across the window, and where the price sits relative to it.
    if fa.total_contracts > 0:
        fa.vwap = sum(
            float(t.yes_price) * float(t.count) for t, _, _, _ in rows
        ) / fa.total_contracts
    newest = min(rows, key=lambda r: r[2])[0]
    fa.last_price = float(newest.yes_price)

    if fa.whale_consensus is not None and fa.n_trades >= 20:
        # Whales disagreeing with the broad tape is worth saying out loud.
        broad_yes = (fa.imbalance + 1) / 2
        if abs(fa.whale_consensus - broad_yes) > 0.25:
            fa.notes.append(
                f"Large trades ({fa.whale_consensus:.0%} YES) diverge from the "
                f"broader tape ({broad_yes:.0%} YES)."
            )
    return fa


def analyze_open_interest(
    fa: FlowAnalysis, candles: Sequence[Any]
) -> FlowAnalysis:
    """Add the open-interest reading from candlesticks.

    Price and open interest together separate cases a price chart alone
    cannot: rising price on rising OI is new money backing the move, while
    rising price on falling OI is mostly position-closing and tends to revert.
    """
    usable = [c for c in candles if getattr(c, "close", None) is not None]
    if len(usable) < 2:
        fa.notes.append("not enough candle history for an open-interest read")
        return fa

    first, last = usable[0], usable[-1]
    fa.price_change = float(last.close) - float(first.close)
    fa.oi_change = float(last.open_interest) - float(first.open_interest)

    px_up = fa.price_change > 0.005
    px_dn = fa.price_change < -0.005
    oi_up = fa.oi_change > 0

    if not (px_up or px_dn):
        fa.oi_reading = "Price flat; open interest gives no directional read."
        fa.oi_strength = "neutral"
    elif px_up and oi_up:
        fa.oi_reading = (
            f"Price up {fa.price_change:+.3f} on rising open interest "
            f"({fa.oi_change:+,.0f}): new money backing YES."
        )
        fa.oi_strength = "strong-yes"
    elif px_up and not oi_up:
        fa.oi_reading = (
            f"Price up {fa.price_change:+.3f} but open interest fell "
            f"({fa.oi_change:+,.0f}): position-closing, not new conviction."
        )
        fa.oi_strength = "weak-yes"
    elif px_dn and oi_up:
        fa.oi_reading = (
            f"Price down {fa.price_change:+.3f} on rising open interest "
            f"({fa.oi_change:+,.0f}): new money backing NO."
        )
        fa.oi_strength = "strong-no"
    else:
        fa.oi_reading = (
            f"Price down {fa.price_change:+.3f} with open interest falling "
            f"({fa.oi_change:+,.0f}): unwinding, low information."
        )
        fa.oi_strength = "weak-no"
    return fa


def depth_summary(
    book: dict[str, list[tuple[Any, float]]], *, target: float = 500
) -> dict[str, Any]:
    """Summarise resting liquidity and the true cost to fill `target`.

    Large resting size is passive whale positioning, and the walked average
    price is what a real order would pay -- top-of-book flatters thin markets.
    """
    from ..clients.kalshi import cost_to_fill

    out: dict[str, Any] = {}
    for side in ("yes", "no"):
        levels = book.get(side) or []
        if not levels:
            out[side] = {"best": None, "depth": 0.0, "fill_price": None, "filled": 0.0}
            continue
        best_price, best_size = levels[0]
        avg, filled = cost_to_fill(levels, target)
        out[side] = {
            "best": float(best_price),
            "best_size": best_size,
            "depth": sum(s for _, s in levels),
            "levels": len(levels),
            "fill_price": float(avg) if avg is not None else None,
            "filled": filled,
            "slippage": (float(avg) - float(best_price)) if avg is not None else None,
            "fully_filled": filled >= target,
        }
    return out
