"""Tests for edge scoring and the guards against correlated bets.

The failure these exist to prevent is the project's most dangerous: a model
whose total sits a few points off the market's makes *every* strike on a
totals ladder look cheap, and a ranked list of them reads as a dozen
independent opportunities. It is one guess, repeated. Stacking real money
across those rungs turns a modest model error into a large loss.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.analysis import edge as edge_mod  # noqa: E402


class FakeMarket:
    def __init__(self, ticker, title, ask, volume=500.0, subject=None):
        self.ticker = ticker
        self.title = title
        self.yes_ask = Decimal(str(ask))
        self.yes_bid = Decimal(str(ask)) - Decimal("0.01")
        self.volume = volume
        self.open_interest = volume
        self._subject_team = subject


class FakePred:
    """Minimal stand-in exposing what the edge layer reads."""

    def __init__(self, total=44.0, spread=1.0, sigma_t=13.5, sigma_m=13.1,
                 market_total=None, market_spread=None):
        self.total = total
        self.spread = spread
        self.sigma_total = sigma_t
        self.sigma_margin = sigma_m
        self.home_team = "nfl:BAL"
        self.away_team = "nfl:LAC"
        self._market_total = market_total
        self._market_spread = market_spread

    def prob_total_over(self, strike):
        from thugsak.model.predict import prob_over

        return prob_over(self.total, strike, self.sigma_total)

    def prob_spread_over(self, strike, team=None):
        from thugsak.model.predict import prob_margin_over

        margin = self.spread if (team is None or team == self.home_team) else -self.spread
        return prob_margin_over(margin, strike, self.sigma_margin)

    @property
    def home_win_prob(self):
        from thugsak.model.predict import win_probability

        return win_probability(self.spread, self.sigma_margin)

    def anchor(self, name):
        if name != "market":
            return None

        class A:
            total = self._market_total
            spread = self._market_spread

        return A()


def _total_ladder(strikes):
    return [
        FakeMarket(
            f"KXNFLTOTAL-26NOV16LACBAL-{int(s + 0.5)}",
            f"Full Game: over {s} points scored?",
            ask,
        )
        for s, ask in strikes
    ]


# --------------------------------------------------------------------------
# basic scoring
# --------------------------------------------------------------------------

def test_edge_is_net_of_fees():
    """The quoted ask is not the cost; the all-in cost is what must be beaten."""
    pred = FakePred(total=44.0)
    m = FakeMarket("KXNFLTOTAL-26NOV16LACBAL-45", "Full Game: over 44.5 points scored?", "0.49")
    r = edge_mod.evaluate_market(pred, m)
    assert r.fee_per_contract > 0
    assert r.all_in_cost > r.ask
    # The raw difference would overstate the edge by exactly the fee.
    assert r.net_edge == pytest.approx(
        float(Decimal(str(r.model_prob)) - r.all_in_cost), abs=1e-9
    )


def test_fair_priced_market_shows_no_edge():
    """Buying at the model's own probability must not look profitable -- the
    fee alone makes it negative."""
    pred = FakePred(total=44.0)
    prob = pred.prob_total_over(44.5)
    m = FakeMarket(
        "KXNFLTOTAL-26NOV16LACBAL-45",
        "Full Game: over 44.5 points scored?",
        round(prob, 2),
    )
    r = edge_mod.evaluate_market(pred, m)
    assert r.net_edge < 0


def test_implausible_edge_is_flagged_suspect():
    """A 40-point edge is a parsing fault, not an opportunity."""
    pred = FakePred(total=44.0)
    m = FakeMarket("KXNFLTOTAL-26NOV16LACBAL-38", "Full Game: over 37.5 points scored?", "0.10")
    r = edge_mod.evaluate_market(pred, m)
    assert r.suspect
    assert not r.tradeable
    assert any("plausibility" in n for n in r.notes)


def test_suspect_rungs_sort_last():
    pred = FakePred(total=44.0)
    markets = _total_ladder([(37.5, "0.10"), (44.5, "0.47")])
    rungs = edge_mod.evaluate_ladder(pred, markets)
    assert not rungs[0].suspect
    assert rungs[-1].suspect


def test_thin_market_is_noted():
    pred = FakePred(total=44.0)
    m = FakeMarket("KXNFLTOTAL-26NOV16LACBAL-45",
                   "Full Game: over 44.5 points scored?", "0.40", volume=2.0)
    r = edge_mod.evaluate_market(pred, m)
    assert any("thin" in n.lower() for n in r.notes)


# --------------------------------------------------------------------------
# the correlated-ladder guard
# --------------------------------------------------------------------------

def test_one_sided_totals_ladder_is_flagged_as_a_single_position():
    """The core guard. A model total above the market's makes every OVER look
    cheap; that is one disagreement, not a list of bets."""
    pred = FakePred(total=44.2, market_total=38.5)
    # Priced to the market's 38.5, so every rung looks cheap to our 44.2.
    markets = _total_ladder(
        [(36.5, "0.59"), (37.5, "0.55"), (38.5, "0.51"),
         (39.5, "0.48"), (40.5, "0.45"), (41.5, "0.43")]
    )
    rungs = edge_mod.evaluate_ladder(pred, markets)
    diag = edge_mod.diagnose_ladder(rungs, pred, {"skill_total": 0.16})

    assert "total" in diag["one_sided"]
    assert diag["one_sided"]["total"]["rungs_with_edge"] >= 3
    assert any("not" in w and "independent bets" in w for w in diag["warnings"])


def test_totals_ladder_is_not_attributed_to_a_team():
    """A total is about the game; naming a team there would be meaningless."""
    pred = FakePred(total=44.2, market_total=38.5)
    markets = _total_ladder([(36.5, "0.59"), (37.5, "0.55"), (38.5, "0.51"), (39.5, "0.48")])
    rungs = edge_mod.evaluate_ladder(pred, markets)
    diag = edge_mod.diagnose_ladder(rungs, pred, {"skill_total": 0.16})
    assert diag["one_sided"]["total"]["all_on_one_team"] is None


def test_spread_edges_concentrated_on_one_team_are_flagged():
    """A spread ladder carries both teams, so a one-directional view shows up
    as concentration rather than a high share -- the share test alone misses
    it."""
    pred = FakePred(spread=-6.0, market_spread=1.0, sigma_m=13.1)
    markets = [
        FakeMarket(f"KXNFLSPREAD-26NOV16LACBAL-LAC{i}",
                   f"Los Angeles C wins by over {i - 0.5} points?",
                   "0.10", subject="nfl:LAC")
        for i in (2, 4, 6, 8)
    ] + [
        FakeMarket(f"KXNFLSPREAD-26NOV16LACBAL-BAL{i}",
                   f"Baltimore wins by over {i - 0.5} points?",
                   "0.60", subject="nfl:BAL")
        for i in (2, 4, 6, 8, 10, 12)
    ]
    rungs = edge_mod.evaluate_ladder(pred, markets)
    diag = edge_mod.diagnose_ladder(rungs, pred, {"skill_margin": 0.16})
    assert "spread" in diag["one_sided"]
    assert diag["one_sided"]["spread"]["all_on_one_team"] == "nfl:LAC"


def test_balanced_ladder_is_not_flagged():
    """A genuine one-off mispricing must not trip the guard."""
    pred = FakePred(total=44.0, market_total=44.0)
    markets = _total_ladder(
        [(40.5, "0.66"), (42.5, "0.59"), (44.5, "0.50"),
         (46.5, "0.42"), (48.5, "0.34")]
    )
    rungs = edge_mod.evaluate_ladder(pred, markets)
    diag = edge_mod.diagnose_ladder(rungs, pred, {"skill_total": 0.16})
    assert "total" not in diag["one_sided"]


# --------------------------------------------------------------------------
# skill discounting
# --------------------------------------------------------------------------

def test_no_skill_discounts_the_edge_to_nothing():
    """If the model is no better than the league average at totals, its
    disagreement with the market carries no information however large."""
    pred = FakePred(total=44.2, market_total=38.5)
    markets = _total_ladder([(38.5, "0.51"), (40.5, "0.45")])
    rungs = edge_mod.evaluate_ladder(pred, markets)
    edge_mod.apply_skill_discount(rungs, {"skill_total": 0.02})
    for r in rungs:
        assert r.net_edge > 0.05          # the arithmetic still looks good
        assert r.adjusted_edge < 0.02     # but it is discounted away
    assert edge_mod.best_bets(rungs) == []


def test_demonstrated_skill_preserves_the_edge():
    pred = FakePred(total=44.2, market_total=38.5)
    markets = _total_ladder([(38.5, "0.51")])
    rungs = edge_mod.evaluate_ladder(pred, markets)
    edge_mod.apply_skill_discount(rungs, {"skill_total": 0.30})
    assert rungs[0].adjusted_edge == pytest.approx(rungs[0].net_edge, abs=1e-9)


def test_discount_is_proportional_to_skill():
    pred = FakePred(total=44.2, market_total=38.5)
    low = edge_mod.evaluate_ladder(pred, _total_ladder([(38.5, "0.51")]))
    high = edge_mod.evaluate_ladder(pred, _total_ladder([(38.5, "0.51")]))
    edge_mod.apply_skill_discount(low, {"skill_total": 0.05})
    edge_mod.apply_skill_discount(high, {"skill_total": 0.20})
    assert low[0].adjusted_edge < high[0].adjusted_edge


def test_low_skill_warning_is_emitted():
    pred = FakePred(total=44.2, market_total=38.5)
    rungs = edge_mod.evaluate_ladder(pred, _total_ladder([(38.5, "0.51")]))
    diag = edge_mod.diagnose_ladder(rungs, pred, {"skill_total": 0.02})
    assert any("no better than" in w for w in diag["warnings"])


def test_best_bets_ranks_on_the_discounted_edge():
    """A spread edge from a skilled model should outrank a larger raw total
    edge from an unskilled one."""
    pred = FakePred(total=44.2, spread=-6.0, market_total=38.5, market_spread=1.0)
    markets = _total_ladder([(38.5, "0.51")]) + [
        # Priced for a modest ~4% edge. An ask cheap enough to imply a huge
        # edge would (correctly) trip the suspect guard instead.
        FakeMarket("KXNFLSPREAD-26NOV16LACBAL-LAC4",
                   "Los Angeles C wins by over 3.5 points?",
                   "0.52", subject="nfl:LAC")
    ]
    rungs = edge_mod.evaluate_ladder(pred, markets)
    edge_mod.apply_skill_discount(rungs, {"skill_total": 0.02, "skill_margin": 0.30})
    picks = edge_mod.best_bets(rungs)
    assert picks
    assert picks[0].kind == "spread"
