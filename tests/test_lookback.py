"""Tests for the completed-game lookback.

A lookback is only worth anything if it cannot cheat, and the ways it cheats
are all silent — each one makes the model look *better*, which is the
direction nobody checks.

Pinned here:

1. **Price.** The reconstruction is charged the historical ask, not the last
   trade. Pricing at the trade flatters every edge by the spread, which on
   these markets is most of a thin edge.
2. **Money.** Candle volume is per-interval and open interest is a level,
   both read as of the cutoff. One NBA moneyline held $87k of open interest
   before tip-off and $2.7m after; using the lifetime figure would put it in
   a different money tier and produce a different recommendation.
3. **Time.** Candles after the cutoff are ignored even though they are in
   hand.
4. **Settlement.** The sign conventions for grading a spread are easy to
   invert, and inverting one turns a losing record into a winning one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.analysis import lookback as lb  # noqa: E402
from thugsak.clients.kalshi import Candle, Market  # noqa: E402

CUTOFF = "2026-11-12T00:00:00+00:00"          # 6pm CST
HOME, AWAY = "nba:MEM", "nba:DEN"


def candle(hour_utc, *, ask, bid, close, volume, oi):
    """One hourly candle on 2026-11-11/12, UTC."""
    import datetime as _dt

    ts = int(
        _dt.datetime(2026, 11, 11, tzinfo=_dt.timezone.utc).timestamp()
    ) + hour_utc * 3600
    return Candle(
        end_ts=ts, open=close, high=close, low=close, close=close, mean=close,
        volume=volume, open_interest=oi,
        yes_ask=Decimal(str(ask)), yes_bid=Decimal(str(bid)),
    )


# 22:00 and 23:00 UTC are before the cutoff; 01:00 and 02:00 next day are after.
SERIES = [
    candle(22, ask=0.61, bid=0.60, close=0.60, volume=1_000, oi=10_000),
    candle(23, ask=0.63, bid=0.62, close=0.62, volume=2_000, oi=14_000),
    candle(24, ask=0.64, bid=0.63, close=0.63, volume=4_000, oi=20_000),  # 00:00, at cutoff
    candle(25, ask=0.84, bid=0.83, close=0.84, volume=400_000, oi=500_000),
    candle(26, ask=0.99, bid=0.98, close=0.99, volume=900_000, oi=1_300_000),
]


# --------------------------------------------------------------------------
# the snapshot
# --------------------------------------------------------------------------

def test_snapshot_takes_the_last_candle_at_or_before_the_cutoff():
    snap = lb.snapshot_from_candles("T", SERIES, CUTOFF)
    assert snap.yes_ask == Decimal("0.64")
    assert snap.open_interest == 20_000
    assert snap.candles_seen == 3


def test_in_game_candles_are_ignored_even_though_they_are_in_hand():
    """The price ran to 0.99 during the game. A lookback that saw it would
    look prescient and be worthless."""
    snap = lb.snapshot_from_candles("T", SERIES, CUTOFF)
    assert snap.yes_ask < Decimal("0.80")
    assert snap.open_interest < 100_000


def test_volume_is_summed_not_taken_from_the_last_candle():
    """Candle volume is per-interval, so the pre-game figure is a sum. Taking
    the last interval alone would understate a quiet build-up."""
    snap = lb.snapshot_from_candles("T", SERIES, CUTOFF)
    assert snap.volume_before == 7_000      # 1k + 2k + 4k, not 4k


def test_the_ask_is_used_not_the_last_trade():
    """Buying costs the ask. At the cutoff the last trade was 0.63 and the
    ask 0.64 -- a cent that is most of a thin edge."""
    snap = lb.snapshot_from_candles("T", SERIES, CUTOFF)
    assert snap.last_price == Decimal("0.63")
    assert snap.yes_ask == Decimal("0.64")
    assert snap.yes_ask > snap.last_price


def test_the_no_side_comes_from_the_yes_bid_not_one_minus_the_ask():
    """Buying NO lifts the resting YES bid. Deriving it from the ask instead
    would make the under a cent cheaper than it ever was."""
    snap = lb.snapshot_from_candles("T", SERIES, CUTOFF)
    assert snap.no_ask == Decimal("1") - Decimal("0.63")   # 1 - yes_bid
    assert snap.no_ask != Decimal("1") - snap.yes_ask


def test_a_market_with_no_history_before_the_cutoff_is_unusable():
    snap = lb.snapshot_from_candles("T", SERIES[3:], CUTOFF)
    assert not snap.usable
    assert "had not opened" in snap.note


def test_a_truncated_window_says_the_money_is_a_lower_bound():
    """Kalshi caps the response, so a market listed weeks ahead has history
    the reconstruction cannot see. Claiming the figure is complete would
    overstate how thin the market was."""
    full = [SERIES[0]] * lb.MAX_HOURLY_CANDLES
    snap = lb.snapshot_from_candles("T", full, CUTOFF)
    assert snap.truncated
    assert "lower bound" in snap.note


# --------------------------------------------------------------------------
# rewinding a market
# --------------------------------------------------------------------------

def _market():
    m = Market(
        ticker="KXNBAGAME-26NOV11DENMEM-MEM", event_ticker="E", title="Memphis wins",
        status="finalized", yes_bid=Decimal("0.98"), yes_ask=Decimal("1.00"),
        no_bid=Decimal("0.00"), no_ask=Decimal("0.02"),
        last_price=Decimal("0.99"), volume=2_700_000.0, volume_24h=2_700_000.0,
        open_interest=1_300_000.0, close_time=None,
    )
    setattr(m, "_subject_team", HOME)
    return m


def test_rewinding_replaces_the_settled_quotes_and_money():
    snap = lb.snapshot_from_candles("T", SERIES, CUTOFF)
    out = lb.market_as_of(_market(), snap)
    assert out.yes_ask == Decimal("0.64")
    assert out.volume == 7_000
    assert out.open_interest == 20_000


def test_rewinding_keeps_the_resolved_subject_team():
    """`dataclasses.replace` drops attributes that are not declared fields,
    and the subject team is attached dynamically. Losing it unassigns every
    ladder rung from its team, so the per-team rows find an empty pool and
    report "$0 traded" on a game with $180,000 on it."""
    out = lb.market_as_of(_market(), lb.snapshot_from_candles("T", SERIES, CUTOFF))
    assert getattr(out, "_subject_team", None) == HOME


def test_rewinding_leaves_the_original_untouched():
    m = _market()
    lb.market_as_of(m, lb.snapshot_from_candles("T", SERIES, CUTOFF))
    assert m.yes_ask == Decimal("1.00")


# --------------------------------------------------------------------------
# what happened
# --------------------------------------------------------------------------

ACTUAL = lb.Actual(
    home_team=HOME, away_team=AWAY, home_label="MEM", away_label="DEN",
    home_score=118, away_score=123,
)


def test_actual_uses_the_home_relative_margin_convention():
    """Positive means the home team won, matching the model. Flipping this
    inverts every spread grade."""
    assert ACTUAL.margin == -5
    assert ACTUAL.total == 241
    assert ACTUAL.winner == AWAY
    assert ACTUAL.score_line == "DEN 123 - MEM 118"


def test_margin_for_is_relative_to_the_team_asked_about():
    assert ACTUAL.margin_for(AWAY) == 5
    assert ACTUAL.margin_for(HOME) == -5


@dataclass
class FakeBet:
    kind: str | None = None
    side: str = "yes"
    threshold: float | None = None
    subject_team: str | None = None
    recommendation: str = "BET"
    all_in_cost: float | None = 0.40
    bet: str = "a bet"

    @property
    def actionable(self):
        return self.recommendation in ("BET", "SMALL")


def test_moneyline_grades_against_the_winner():
    won = lb.grade_bet(FakeBet(kind="game", subject_team=AWAY), ACTUAL)
    lost = lb.grade_bet(FakeBet(kind="game", subject_team=HOME), ACTUAL)
    assert won["outcome"] == "WON"
    assert lost["outcome"] == "LOST"


def test_spread_grades_on_the_subject_teams_own_margin():
    """Denver won by 5. "Denver by over 3.5" pays; "over 7.5" does not."""
    assert lb.grade_bet(
        FakeBet(kind="spread", subject_team=AWAY, threshold=3.5), ACTUAL
    )["outcome"] == "WON"
    assert lb.grade_bet(
        FakeBet(kind="spread", subject_team=AWAY, threshold=7.5), ACTUAL
    )["outcome"] == "LOST"
    # And the losing side's ladder never pays.
    assert lb.grade_bet(
        FakeBet(kind="spread", subject_team=HOME, threshold=0.5), ACTUAL
    )["outcome"] == "LOST"


def test_total_grades_over_and_under_in_opposite_directions():
    """The under is a NO on an over market, so its grade is the complement.
    Getting this backwards would score every under exactly wrong."""
    assert lb.grade_bet(
        FakeBet(kind="total", threshold=230.5), ACTUAL
    )["outcome"] == "WON"                      # 241 > 230.5
    assert lb.grade_bet(
        FakeBet(kind="total", threshold=230.5, side="no"), ACTUAL
    )["outcome"] == "LOST"
    assert lb.grade_bet(
        FakeBet(kind="total", threshold=250.5, side="no"), ACTUAL
    )["outcome"] == "WON"                      # 241 < 250.5


def test_profit_is_net_of_the_fee_already_in_the_cost():
    won = lb.grade_bet(
        FakeBet(kind="game", subject_team=AWAY, all_in_cost=0.40), ACTUAL
    )
    lost = lb.grade_bet(
        FakeBet(kind="game", subject_team=HOME, all_in_cost=0.40), ACTUAL
    )
    assert won["profit_per_100_contracts_usd"] == pytest.approx(60.0)
    assert lost["profit_per_100_contracts_usd"] == pytest.approx(-40.0)


def test_a_bet_that_was_not_recommended_is_not_graded():
    """Grading a NO as a loss would invent a record of bets never placed."""
    g = lb.grade_bet(FakeBet(kind="game", subject_team=HOME,
                             recommendation="NO"), ACTUAL)
    assert g["outcome"] == "not recommended"
    assert g["profit_per_contract"] is None


def test_an_ungradeable_bet_says_so_rather_than_guessing():
    g = lb.grade_bet(FakeBet(kind="spread", subject_team=AWAY), ACTUAL)
    assert g["outcome"] == "could not grade"


# --------------------------------------------------------------------------
# grading the prediction
# --------------------------------------------------------------------------

class FakeOutlook:
    def __init__(self, margin, total):
        self.margin = margin
        self.total = total


def test_prediction_errors_are_signed_so_bias_is_visible():
    """A model nine points out in the same direction every time has a bias,
    which a mean absolute error hides completely."""
    g = lb.grade_prediction(FakeOutlook(margin=2.4, total=227.1), ACTUAL)
    assert g["margin_error"] == pytest.approx(7.4)     # predicted +2.4, actual -5
    assert g["total_error"] == pytest.approx(-13.9)
    assert g["margin_error_abs"] == pytest.approx(7.4)


def test_picking_the_winner_is_reported_separately_from_the_margin():
    """Being three points out matters less than being on the wrong side."""
    wrong = lb.grade_prediction(FakeOutlook(margin=2.0, total=240), ACTUAL)
    right = lb.grade_prediction(FakeOutlook(margin=-2.0, total=240), ACTUAL)
    assert wrong["picked_the_winner"] is False
    assert right["picked_the_winner"] is True


def test_summary_counts_only_bets_that_were_placed():
    bets = [
        FakeBet(kind="game", subject_team=AWAY),
        FakeBet(kind="game", subject_team=HOME),
        FakeBet(kind="total", recommendation="NO"),
    ]
    grades = [lb.grade_bet(b, ACTUAL) for b in bets]
    out = lb.summarise_grades(bets, grades)
    assert "1 of 2" in out


def test_summary_says_plainly_when_nothing_was_recommended():
    bets = [FakeBet(kind="game", subject_team=AWAY, recommendation="NO")]
    out = lb.summarise_grades(bets, [lb.grade_bet(b, ACTUAL) for b in bets])
    assert "nothing to grade" in out
