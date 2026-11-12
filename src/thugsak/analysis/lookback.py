"""What the model would have said before a game, and whether it was right.

A prediction you cannot check is just an opinion, so a completed game renders
exactly as it would have an hour before tip-off, with the result beside it.

Three things have to be true for that to mean anything, and each is enforced
rather than assumed:

**The model must not see the result.** Every history query runs through
`model.asof` with a cutoff of kickoff minus an hour, so the game being graded
and everything after it are invisible. Without that guard a lookback quietly
includes the outcome it is predicting and looks uncanny -- the exact failure
that makes people trust a broken model.

**The price must be the price you could have paid.** Kalshi's candlesticks
carry the historical `yes_ask` and `yes_bid`, not merely the last trade, so a
reconstruction is charged the ask as a real buyer would be. Pricing at the
last trade instead would flatter every edge by the width of the spread, which
on these markets is most of a thin edge.

**The money must be the money that was there.** Candle volume is per-interval
and open interest is a level, so both are read as of the cutoff. Using the
market's lifetime `volume` field would fold in all the in-game trading that
followed: one NBA moneyline sat at $87k of open interest before tip-off and
$2.7m after, which is two different money tiers and therefore two different
recommendations.

**What cannot be reconstructed: the injury report.** ESPN publishes current
availability only, with no historical snapshots. Today's report applied to a
game played last week is simply wrong data, so the injury adjustment is
**switched off** in lookback mode and the caveat travels with the output. It
is the one component of the model that a lookback cannot test.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Sequence

from ..model.asof import parse_ts

# Kalshi returns at most 96 hourly candles, so a market listed more than four
# days ahead has history the reconstruction cannot see. Volume before the
# window is therefore missing and the pre-game figure is a lower bound.
MAX_HOURLY_CANDLES = 96


@dataclass
class Snapshot:
    """A market's quotes and money as they stood at a moment."""

    ticker: str
    as_of: str
    yes_ask: Decimal | None = None
    yes_bid: Decimal | None = None
    no_ask: Decimal | None = None
    no_bid: Decimal | None = None
    last_price: Decimal | None = None
    volume_before: float = 0.0       # contracts traded up to the cutoff
    open_interest: float = 0.0       # level at the cutoff
    candles_seen: int = 0
    truncated: bool = False          # history older than the window exists
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.yes_ask is not None or self.no_ask is not None


def snapshot_from_candles(
    ticker: str, candles: Sequence[Any], as_of: str
) -> Snapshot:
    """Reconstruct one market's state at `as_of` from its candle history.

    Takes the last candle that closed at or before the cutoff. Candles after
    it are deliberately ignored even though they are in hand -- that is the
    whole discipline of a lookback.
    """
    snap = Snapshot(ticker=ticker, as_of=as_of)
    cutoff = parse_ts(as_of).timestamp()

    before = [c for c in candles if c.end_ts and c.end_ts <= cutoff]
    snap.candles_seen = len(before)
    if not before:
        snap.note = (
            "No candle history before the cutoff: the market had not opened, "
            "or opened too long ago for Kalshi's window to reach."
        )
        return snap

    snap.volume_before = sum(c.volume for c in before)
    last = before[-1]
    snap.open_interest = last.open_interest
    snap.yes_ask, snap.yes_bid = last.yes_ask, last.yes_bid
    snap.no_ask, snap.no_bid = last.no_ask, last.no_bid
    snap.last_price = Decimal(str(last.close)) if last.close is not None else None

    if len(candles) >= MAX_HOURLY_CANDLES:
        snap.truncated = True
        snap.note = (
            f"Kalshi serves at most {MAX_HOURLY_CANDLES} hourly candles, so "
            f"any trading before that window is not counted. The pre-game "
            f"money figure is a lower bound."
        )
    if snap.yes_ask is None and snap.yes_bid is None:
        snap.note = (
            "The market had candle history but no quote on either side at the "
            "cutoff, so there was nothing to buy an hour before tip-off."
        )
    return snap


def market_as_of(market: Any, snap: Snapshot) -> Any:
    """A copy of a live market with its quotes and money rewound.

    Returned as the same `Market` type so `edge.evaluate_market` scores a
    historical market by exactly the same code path as a live one -- a second
    implementation would be free to drift, and drift here looks like skill.
    """
    out = replace(
        market,
        yes_ask=snap.yes_ask,
        yes_bid=snap.yes_bid,
        no_ask=snap.no_ask,
        no_bid=snap.no_bid,
        last_price=snap.last_price,
        volume=snap.volume_before,
        open_interest=snap.open_interest,
    )
    # The resolved subject team is attached dynamically rather than being a
    # declared field, so `dataclasses.replace` drops it. Losing it silently
    # unassigns every ladder rung from its team: the per-team rows then find
    # an empty pool and report "$0 traded" on a game with $180,000 on it.
    subject = getattr(market, "_subject_team", None)
    if subject is not None:
        setattr(out, "_subject_team", subject)
    return out


# --------------------------------------------------------------------------
# what actually happened
# --------------------------------------------------------------------------

@dataclass
class Actual:
    """The final score, with the labels needed to read it."""

    home_team: str
    away_team: str
    home_label: str
    away_label: str
    home_score: int
    away_score: int

    @property
    def margin(self) -> int:
        """Home-relative, matching the model's sign convention."""
        return self.home_score - self.away_score

    @property
    def total(self) -> int:
        return self.home_score + self.away_score

    @property
    def winner(self) -> str:
        if self.margin > 0:
            return self.home_team
        if self.margin < 0:
            return self.away_team
        return ""

    @property
    def score_line(self) -> str:
        return (
            f"{self.away_label} {self.away_score} - "
            f"{self.home_label} {self.home_score}"
        )

    def margin_for(self, team_id: str) -> int:
        return self.margin if team_id == self.home_team else -self.margin


def grade_prediction(outlook: Any, actual: Actual) -> dict[str, Any]:
    """How close the predicted score came.

    Errors are reported signed as well as absolute: a model that is wrong by
    nine points in the same direction every time has a bias worth fixing,
    which a mean absolute error hides completely.
    """
    out: dict[str, Any] = {
        "actual_score": actual.score_line,
        "actual_margin_home_relative": actual.margin,
        "actual_total": actual.total,
    }
    if outlook.margin is not None:
        err = outlook.margin - actual.margin
        out["margin_error"] = round(err, 1)
        out["margin_error_abs"] = round(abs(err), 1)
        out["picked_the_winner"] = (
            None if not actual.winner
            else (outlook.margin > 0) == (actual.margin > 0)
        )
    if outlook.total is not None:
        err = outlook.total - actual.total
        out["total_error"] = round(err, 1)
        out["total_error_abs"] = round(abs(err), 1)
    out["note"] = (
        "Errors are signed: positive margin error means the model favoured "
        "the home team by more than it should have. One game says almost "
        "nothing -- a model with a 13-point typical error will miss by 20 "
        "regularly. Patterns across a few dozen are what mean anything."
    )
    return out


def grade_bet(bet: Any, actual: Actual) -> dict[str, Any]:
    """Did this bet win, and what would it have returned?

    Returns a dict rather than mutating the bet, so a graded lookback and a
    live recommendation stay the same object.
    """
    out: dict[str, Any] = {"outcome": None, "profit_per_contract": None}
    if not bet.actionable:
        out["outcome"] = "not recommended"
        return out

    won = _resolved_yes(bet, actual)
    if won is None:
        out["outcome"] = "could not grade"
        out["why"] = (
            "The bet's strike or subject team was not recorded, so there is "
            "nothing to settle it against."
        )
        return out

    out["outcome"] = "WON" if won else "LOST"
    cost = bet.all_in_cost
    if cost is not None:
        # A contract settles at $1 or $0; the cost already includes the fee.
        out["profit_per_contract"] = round((1.0 - cost) if won else -cost, 4)
        out["profit_per_100_contracts_usd"] = round(
            100 * ((1.0 - cost) if won else -cost), 2
        )
    return out


def _resolved_yes(bet: Any, actual: Actual) -> bool | None:
    """Whether the side the bet bought actually paid out.

    Kalshi strikes carry a half point, so there are no pushes to handle -- but
    the sign conventions are easy to invert, and inverting one turns a losing
    record into a winning one. Each is derived from the stated bet rather than
    from a remembered rule.
    """
    kind, side, strike = bet.kind, bet.side, bet.threshold

    if kind == "game":
        if not bet.subject_team:
            return None
        team_won = actual.winner == bet.subject_team
        return team_won if side == "yes" else not team_won

    if kind == "spread":
        if strike is None or not bet.subject_team:
            return None
        # "T wins by over X" resolves YES when T's margin exceeds X.
        covered = actual.margin_for(bet.subject_team) > strike
        return covered if side == "yes" else not covered

    if kind == "total":
        if strike is None:
            return None
        over = actual.total > strike
        return over if side == "yes" else not over

    return None


def summarise_grades(bets: Sequence[Any], grades: Sequence[dict[str, Any]]) -> str:
    """One line on how the recommended bets did."""
    placed = [
        (b, g) for b, g in zip(bets, grades)
        if g.get("outcome") in ("WON", "LOST")
    ]
    if not placed:
        return "No bet was recommended on this game, so there is nothing to grade."
    won = sum(1 for _b, g in placed if g["outcome"] == "WON")
    profit = sum(
        g.get("profit_per_100_contracts_usd") or 0.0 for _b, g in placed
    )
    return (
        f"{won} of {len(placed)} recommended bet(s) won, "
        f"${profit:+,.2f} per 100 contracts each."
    )
