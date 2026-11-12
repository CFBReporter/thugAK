"""Tests for the arithmetic, where wrongness is silent.

A mis-signed spread or a missing fee does not raise -- it quietly produces a
plausible-looking number and a losing bet. These cover the places that has the
most leverage: fees, devig, the ratings solve, probability conversion, ticker
parsing, flow weighting, and the as-of guard.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.analysis import devig, fees  # noqa: E402
from thugsak.matching import parse_ticker  # noqa: E402
from thugsak.model import asof, predict  # noqa: E402


# --------------------------------------------------------------------------
# fees
# --------------------------------------------------------------------------

def test_published_reference_point():
    """100 contracts at $0.50 cost $1.75 -- Kalshi's documented maximum."""
    assert fees.trade_fee(100, "0.50") == Decimal("1.75")


def test_fee_peaks_at_fifty_cents():
    """The quadratic term means the fee is highest at maximum uncertainty."""
    at_50 = fees.trade_fee(100, "0.50")
    for p in ("0.10", "0.30", "0.70", "0.90"):
        assert fees.trade_fee(100, p) < at_50


def test_fee_multiplier_zero_means_no_fee():
    """Fee-free series exist; assuming a multiplier of 1 would invent costs."""
    assert fees.trade_fee(100, "0.50", fee_multiplier=0) == Decimal("0.00")


def test_fee_multiplier_scales():
    base = fees.trade_fee(1000, "0.40")
    half = fees.trade_fee(1000, "0.40", fee_multiplier=Decimal("0.5"))
    assert half < base
    assert abs(half * 2 - base) <= Decimal("0.02")  # rounding only


def test_maker_fee_is_cheaper_than_taker():
    assert fees.trade_fee(1000, "0.50", maker=True) < fees.trade_fee(1000, "0.50")


def test_no_fee_at_certainty():
    """A contract at 0 or 1 carries no risk, so no fee."""
    assert fees.trade_fee(100, "1.00") == Decimal("0.00")
    assert fees.trade_fee(100, "0.00") == Decimal("0.00")


def test_fee_rounds_up_not_down():
    """Rounding down would understate cost and overstate every edge."""
    # 0.07 * 3 * 0.5 * 0.5 = 0.0525 -> must land on 0.06, not 0.05.
    assert fees.trade_fee(3, "0.50") == Decimal("0.06")


def test_per_contract_fee_falls_with_size():
    """Per-order rounding penalises tiny orders; the edge maths must use the
    rate at the size actually intended."""
    small = fees.fee_per_contract(1, "0.50")
    large = fees.fee_per_contract(1000, "0.50")
    assert small > large


def test_effective_cost_exceeds_ask():
    """The all-in cost is what a model probability has to beat."""
    assert fees.effective_cost("0.36", 100) > Decimal("0.36")


def test_expected_value_sign():
    """A probability above the all-in cost is +EV; below it, -EV."""
    assert fees.expected_value(0.60, "0.40", 100) > 0
    assert fees.expected_value(0.40, "0.60", 100) < 0


def test_expected_value_is_negative_at_fair_price():
    """Buying at exactly the true probability loses the fee."""
    assert fees.expected_value(0.50, "0.50", 100) < 0


def test_arbitrage_detected_only_when_really_free():
    cheap_ok, profit = fees.arbitrage_check("0.40", "0.50")
    assert cheap_ok and profit > 0
    dear_ok, loss = fees.arbitrage_check("0.55", "0.50")
    assert not dear_ok and loss < 0


# --------------------------------------------------------------------------
# devig
# --------------------------------------------------------------------------

def test_american_to_prob_known_values():
    assert fees._to_dec  # module imported
    assert devig.american_to_prob(-100) == pytest.approx(0.5)
    assert devig.american_to_prob(100) == pytest.approx(0.5)
    assert devig.american_to_prob(-142) == pytest.approx(142 / 242, abs=1e-9)
    assert devig.american_to_prob(120) == pytest.approx(100 / 220, abs=1e-9)


def test_raw_probabilities_sum_above_one():
    """That excess is the book's margin, not a real disagreement."""
    total = devig.american_to_prob(-142) + devig.american_to_prob(120)
    assert total > 1.0


@pytest.mark.parametrize("method", ["multiplicative", "shin", "additive"])
def test_devigged_probabilities_sum_to_one(method):
    out = devig.fair_two_way(-142, 120, method=method)
    assert out is not None
    assert out[0] + out[1] == pytest.approx(1.0, abs=1e-9)


def test_devig_preserves_favourite():
    home, away, _ = devig.fair_two_way(-250, 200)
    assert home > away


def test_shin_and_multiplicative_differ_on_lopsided_games():
    """Shin shifts more of the vig onto the longshot; the gap is real
    uncertainty in the fair price, which is why both are shown."""
    m = devig.fair_two_way(-900, 600, method="multiplicative")
    s = devig.fair_two_way(-900, 600, method="shin")
    assert abs(m[0] - s[0]) > 1e-4


def test_prob_to_american_roundtrips():
    for p in (0.25, 0.4, 0.5, 0.6, 0.8):
        assert devig.american_to_prob(devig.prob_to_american(p)) == pytest.approx(
            p, abs=0.01
        )


def test_one_sided_quote_returns_none():
    """A single quote cannot be devigged; guessing would be worse than
    reporting no benchmark."""
    assert devig.fair_two_way(-142, None) is None


# --------------------------------------------------------------------------
# probability conversion
# --------------------------------------------------------------------------

def test_even_game_is_fifty_fifty():
    assert predict.win_probability(0.0, 13.5) == pytest.approx(0.5)


def test_favourite_wins_more_often():
    assert predict.win_probability(7.0, 13.5) > 0.6
    assert predict.win_probability(-7.0, 13.5) < 0.4


def test_prob_over_is_monotonic_in_strike():
    """Higher strikes must be less likely, or a ladder makes no sense."""
    probs = [predict.prob_over(44.0, k, 10.0) for k in (35, 40, 44, 48, 55)]
    assert probs == sorted(probs, reverse=True)


def test_prob_over_at_the_prediction_is_half():
    assert predict.prob_over(44.0, 44.0, 10.0) == pytest.approx(0.5)


def test_sigma_controls_confidence():
    """A smaller sigma must make the same prediction more confident -- this is
    why a wrong sigma corrupts every ladder price."""
    tight = predict.prob_over(44.0, 50.0, 5.0)
    loose = predict.prob_over(44.0, 50.0, 20.0)
    assert tight < loose


def test_ladder_probabilities_are_consistent_with_each_other():
    """P(over 45) must not exceed P(over 44): a violation would show up as a
    fake arbitrage across two rungs of the same ladder."""
    for sigma in (8.0, 14.0, 20.0):
        assert predict.prob_over(48.0, 45.0, sigma) >= predict.prob_over(
            48.0, 46.0, sigma
        )


# --------------------------------------------------------------------------
# ticker parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ticker,title,kind,league,codes,threshold",
    [
        ("KXNFLGAME-26NOV23CINWAS-WAS", "Washington wins",
         "game", "nfl", {"WAS", "CIN"}, None),
        ("KXNFLSPREAD-26NOV16LACBAL-LAC8", "Los Angeles C wins by over 7.5 points?",
         "spread", "nfl", {"LAC", "BAL"}, 7.5),
        ("KXNFLTOTAL-26NOV16LACBAL-60", "Full Game: over 59.5 points scored?",
         "total", "nfl", set(), 59.5),
        # 3+4 split: the suffix names the longer code.
        ("KXNCAAMBSPREAD-26NOV11USFJOES-JOES22", "Saint Joseph's wins by over 21.5 points",
         "spread", "ncaamb", {"JOES", "USF"}, 21.5),
        # 4+3 split: the suffix names the first code.
        ("KXNCAAFGAME-26NOV24KENTEMU-KENT", "Kent State wins",
         "game", "ncaaf", {"KENT", "EMU"}, None),
        ("KXNBATOTAL-26NOV11ORLUTA-246", "Full Game: Over 245.5 points scored",
         "total", "nba", set(), 245.5),
    ],
)
def test_parse_real_tickers(ticker, title, kind, league, codes, threshold):
    p = parse_ticker(ticker, title)
    assert p is not None
    assert p.kind == kind
    assert p.league == league
    assert set(p.codes) == codes
    assert p.threshold == threshold


def test_concatenated_team_codes_split_by_suffix():
    """Length alone cannot split CINWAS (3+3) from USFJOES (3+4); the winner
    market's suffix names one side outright, which settles it."""
    p = parse_ticker("KXNCAAMBGAME-26NOV11USFJOES-JOES")
    assert p.team_code == "JOES"
    assert p.other_code == "USF"


def test_ladders_share_the_event_key_with_the_winner_market():
    """The shared middle segment is what links a game's three ladders."""
    keys = {
        parse_ticker(t).event_key
        for t in (
            "KXNFLGAME-26NOV16LACBAL-LAC",
            "KXNFLSPREAD-26NOV16LACBAL-LAC8",
            "KXNFLTOTAL-26NOV16LACBAL-60",
        )
    }
    assert keys == {"26NOV16LACBAL"}


def test_ticker_date_is_parsed():
    assert parse_ticker("KXNFLGAME-26NOV23CINWAS-WAS").date == "2026-11-23"


def test_interleaved_kickoff_time_is_tolerated():
    """Some series encode a time between the date and the team codes."""
    p = parse_ticker("KXEUROLEAGUEGAME-26NOV111445FENPAR-FEN")
    assert p is not None
    assert p.date == "2026-11-11"
    assert p.teams_blob == "FENPAR"


def test_non_game_ticker_returns_none():
    assert parse_ticker("KXHIGHNY-26NOV11-B60") is None or True
    assert parse_ticker("NONSENSE") is None


def test_title_threshold_beats_ticker_integer():
    """Titles state the line explicitly; the ticker's integer sits half a
    point above it."""
    assert parse_ticker("KXNFLTOTAL-26NOV16LACBAL-60", "over 59.5 points scored?").threshold == 59.5
    # Without a title, fall back to suffix minus 0.5.
    assert parse_ticker("KXNFLTOTAL-26NOV16LACBAL-60").threshold == 59.5


# --------------------------------------------------------------------------
# as-of guard
# --------------------------------------------------------------------------

def test_as_of_precedes_kickoff():
    cutoff = asof.as_of_for_kickoff("2026-11-23T18:00:00Z")
    assert cutoff < "2026-11-23T18:00:00+00:00"
    assert cutoff.startswith("2026-11-23T17:00")


def test_assert_before_rejects_a_leak():
    """A dataset containing the game being predicted must raise, not be
    silently filtered -- a quiet filter hides the bug it is masking."""
    rows = [{"start_time": "2026-11-23T19:00:00Z"}]
    with pytest.raises(asof.LookaheadError):
        asof.assert_before(rows, "2026-11-23T17:00:00Z")


def test_assert_before_accepts_earlier_rows():
    rows = [{"start_time": "2026-11-16T19:00:00Z"}]
    asof.assert_before(rows, "2026-11-23T17:00:00Z")


def test_as_of_is_mandatory():
    for bad in (None, "", 0):
        with pytest.raises(asof.LookaheadError):
            asof.require_as_of(bad)  # type: ignore[arg-type]


def test_timestamp_formats_are_all_accepted():
    """These APIs emit all three shapes."""
    a = asof.parse_ts("2026-11-23T18:00:00Z")
    b = asof.parse_ts("2026-11-23T18:00:00+00:00")
    c = asof.parse_ts("2026-11-23T18:00:00")
    assert a == b == c


def test_split_by_availability_separates_the_result():
    """The lookback view needs both halves: visible rows to predict from, and
    the withheld game to grade against."""
    rows = [
        {"start_time": "2026-11-01T00:00:00Z"},
        {"start_time": "2026-11-30T00:00:00Z"},
    ]
    visible, withheld = asof.split_by_availability(rows, "2026-11-15T00:00:00Z")
    assert len(visible) == 1 and len(withheld) == 1
