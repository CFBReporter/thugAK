"""Tests for the five-bet output and the money that gates it.

Three bug classes are pinned here, all of which were live and none of which
raised an error:

1. **Tail selection.** Ranking a ladder by raw edge *systematically picks its
   tails*, because a 1-cent market needs only a 2-cent model probability to
   show a huge relative edge. The first version duly recommended a 1-cent
   longshot and an "over" 21 points above its own projection. Both were
   arithmetically +EV and both were artefacts of reading a normal curve far
   outside where it was fitted.

2. **Money ignored.** A price with $200 behind it was treated as being as
   informative as one with $2m, so a 3% disagreement with a deeply traded
   market read as an opportunity rather than as evidence we were wrong.

3. **Unbettable unders.** Kalshi lists totals only as "Over X", so until the
   NO side was priced there was no way to recommend an under at all -- the
   total slot could only ever say over or nothing.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.analysis import money as mn  # noqa: E402
from thugsak.analysis import verdict as vd  # noqa: E402

HOME, AWAY = "x:HOME", "x:AWAY"
LABELS = {HOME: "Home Team", AWAY: "Away Team"}
SKILLED = {"skill_margin": 0.30, "skill_total": 0.30}

# Enough dollars to clear the usability floor and sit in the "active" tier,
# where the required edge is 4%.
ACTIVE = 20_000.0


class FakeRung:
    def __init__(self, kind, title, ask, model_prob, edge, *, threshold=None,
                 dollars=ACTIVE, suspect=False, subject=None, side="yes"):
        self.kind = kind
        self.title = title
        self.side = side
        self.ticker = f"KX-{kind}-{title[:8]}-{side}"
        self.ask = Decimal(str(ask))
        self.model_prob = model_prob
        self.net_edge = edge
        self.adjusted_edge = edge
        self.all_in_cost = Decimal(str(ask)) + Decimal("0.01")
        self.ev_per_100 = Decimal("1.00")
        self.threshold = threshold
        self.volume = dollars / max(float(ask), 0.01)
        self.open_interest = self.volume / 2
        self.suspect = suspect
        self.liquidity_note = ""
        self.subject_team = subject
        self.notes: list[str] = []

    @property
    def bet_title(self):
        if self.side == "yes":
            return self.title
        if self.kind == "total" and self.threshold is not None:
            return f"Under {self.threshold} points"
        return f"NOT {self.title}"

    @property
    def dollars_traded(self):
        return self.volume * float(self.ask)

    @property
    def dollars_at_risk(self):
        return self.open_interest * float(self.ask)


class FakePred:
    def __init__(self, *, total=150.0, spread=5.0, market_total=None,
                 market_spread=None, market_prob=None, sigma_m=13.0,
                 sigma_t=17.0, games=10):
        self.total = total
        self.spread = spread
        self.home_points = (total + spread) / 2
        self.away_points = (total - spread) / 2
        self.sigma_margin = sigma_m
        self.sigma_total = sigma_t
        self.home_team = HOME
        self.away_team = AWAY
        self.home_form = {"games_played": games,
                          "last7": {"adj_points_for": 75.0,
                                    "adj_points_against": 70.0,
                                    "record": "4-3"}}
        self.away_form = {"games_played": games,
                          "last7": {"adj_points_for": 72.0,
                                    "adj_points_against": 73.0,
                                    "record": "3-4"}}
        self.injury_adj_home = 0.0
        self.injury_adj_away = 0.0
        self.injury_confidence = "none"
        self.weights = {}
        self.warnings: list[str] = []
        self._mt, self._ms, self._mp = market_total, market_spread, market_prob

    @property
    def home_win_prob(self):
        from thugsak.model.predict import win_probability

        return win_probability(self.spread, self.sigma_margin)

    def anchor(self, name):
        if name == "market":
            class A:
                total = self._mt
                spread = self._ms
                home_win_prob = self._mp
            return A()
        if name == "power_rating":
            class P:
                spread = self.spread
                total = None
                source = "ridge ratings, 1000 games"
            return P()
        return None


def _slot(bets, slot):
    return next(b for b in bets if b.slot == slot)


# --------------------------------------------------------------------------
# shape: always five, always in order
# --------------------------------------------------------------------------

def test_exactly_five_bets_in_a_fixed_order():
    """A fixed shape is the whole point -- the reader should never have to
    hunt for the row that matters, and "no bet" must occupy the same place a
    recommendation would."""
    bets = vd.build_bets(FakePred(), [], calibration=SKILLED, team_labels=LABELS)
    assert [b.slot for b in bets] == [
        "moneyline_away", "moneyline_home", "spread_away", "spread_home", "total",
    ]


def test_rows_are_labelled_with_the_team_not_a_bare_sign():
    bets = vd.build_bets(FakePred(), [], calibration=SKILLED, team_labels=LABELS)
    assert _slot(bets, "moneyline_home").label == "Moneyline - Home Team"
    assert _slot(bets, "spread_away").label == "Spread - Away Team"


def test_a_missing_market_is_a_no_with_a_reason():
    bets = vd.build_bets(FakePred(), [], calibration=SKILLED, team_labels=LABELS)
    for b in bets:
        assert b.recommendation == "NO"
        assert b.why
        assert b.dollars_traded == 0.0


def test_slot_money_is_reported_even_when_nothing_is_quotable():
    """A market can stop quoting while still having turned over six figures.
    Reporting $0 on it misstates the input the decision leans on hardest."""
    rungs = [
        FakeRung("game", "Home wins", "1.00", 0.9, None, subject=HOME,
                 dollars=150_000.0),
        FakeRung("game", "Home by a lot", "1.00", 0.9, None, subject=HOME,
                 dollars=30_000.0),
    ]
    bets = vd.build_bets(FakePred(), rungs, calibration=SKILLED,
                         team_labels=LABELS)
    ml = _slot(bets, "moneyline_home")
    assert ml.recommendation == "NO"
    assert ml.dollars_traded == pytest.approx(180_000.0, rel=1e-6)
    assert "$180,000 has traded" in ml.why


def test_each_team_gets_its_own_side_of_the_ladder():
    """Spread rungs are split by subject team; crossing them would recommend
    a team to cover on the opponent's strike."""
    rungs = [
        FakeRung("spread", "Home by over 3.5", "0.45", 0.55, 0.06,
                 threshold=3.5, subject=HOME),
        FakeRung("spread", "Away by over 3.5", "0.20", 0.22, 0.01,
                 threshold=3.5, subject=AWAY),
    ]
    bets = vd.build_bets(FakePred(), rungs, calibration=SKILLED,
                         team_labels=LABELS)
    assert _slot(bets, "spread_home").bet == "Home by over 3.5"
    assert _slot(bets, "spread_away").bet == "Away by over 3.5"


# --------------------------------------------------------------------------
# the under, which did not exist before
# --------------------------------------------------------------------------

def test_the_total_slot_can_recommend_an_under():
    """Kalshi lists only "Over X", so without the NO side the total slot could
    never say under."""
    rungs = [
        FakeRung("total", "Over 152.5 points", "0.60", 0.50, -0.02,
                 threshold=152.5, side="yes"),
        FakeRung("total", "Over 152.5 points", "0.40", 0.50, 0.09,
                 threshold=152.5, side="no"),
    ]
    bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                         calibration=SKILLED, team_labels=LABELS)
    total = _slot(bets, "total")
    assert total.recommendation == "BET"
    assert total.bet == "Under 152.5 points"


# --------------------------------------------------------------------------
# tail exclusion
# --------------------------------------------------------------------------

def test_penny_longshot_is_never_recommended():
    """A 1c market with a 7% model probability shows a +6% edge and must
    still be refused: that is tail noise, not an opportunity."""
    rungs = [FakeRung("total", "Over 177.5", "0.01", 0.07, 0.059,
                      threshold=177.5)]
    bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                         calibration=SKILLED, team_labels=LABELS)
    total = _slot(bets, "total")
    assert total.recommendation == "NO"
    assert "longshot" in total.why


def test_near_certainty_is_also_excluded():
    """The mirror case: a 93% favourite at 0.97 is not a bet either."""
    rungs = [FakeRung("game", "Home wins", "0.97", 0.93, 0.01, subject=HOME)]
    bets = vd.build_bets(FakePred(spread=20.0), rungs, calibration=SKILLED,
                         team_labels=LABELS)
    assert _slot(bets, "moneyline_home").recommendation == "NO"


def test_credible_strike_preferred_over_a_bigger_tail_edge():
    """Given both, the sensible strike wins even though the tail's arithmetic
    edge is larger -- that preference is the whole point."""
    rungs = [
        FakeRung("total", "Over 180.5", "0.01", 0.08, 0.069, threshold=180.5),
        FakeRung("total", "Over 152.5", "0.45", 0.50, 0.05, threshold=152.5),
    ]
    bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                         calibration=SKILLED, team_labels=LABELS)
    assert _slot(bets, "total").bet == "Over 152.5"


def test_ask_of_one_dollar_is_not_tradeable():
    """Paying $1 for a contract that pays $1 cannot profit. Left in the pool
    this produced a null edge that surfaced as "no priced market matched" on
    a game with 33 live markets."""
    rungs = [FakeRung("game", "Home wins", "1.00", 0.93, None, subject=HOME)]
    bets = vd.build_bets(FakePred(spread=20.0), rungs, calibration=SKILLED,
                         team_labels=LABELS)
    ml = _slot(bets, "moneyline_home")
    assert ml.recommendation == "NO"
    assert "none is quoting a price you could pay" in ml.why
    # The money is still reported: a refusal has to read as a judgement about
    # a real market, not as missing data.
    assert ml.dollars_traded > 0


# --------------------------------------------------------------------------
# money as an input
# --------------------------------------------------------------------------

def test_the_same_edge_passes_on_thin_money_and_fails_on_deep():
    """The crux of the money rule. A 4.5% edge is worth a small position
    against $20k and is *not* worth taking against $5m, because at $5m the
    likelier explanation is that the model is wrong."""
    def verdict(dollars):
        rungs = [FakeRung("total", "Over 152.5", "0.45", 0.505, 0.045,
                          threshold=152.5, dollars=dollars)]
        bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                             calibration=SKILLED, team_labels=LABELS)
        return _slot(bets, "total")

    assert verdict(20_000).recommendation == "BET"      # active: needs 4%
    deep = verdict(5_000_000)                            # deep: needs 5%
    assert deep.recommendation == "NO"
    assert "deep money" in deep.why


def test_a_market_with_almost_no_money_is_refused_outright():
    """A 100-contract order would be most of the money that has ever traded,
    so the quoted price is not a real market."""
    rungs = [FakeRung("total", "Over 152.5", "0.45", 0.60, 0.14,
                      threshold=152.5, dollars=100.0)]
    bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                         calibration=SKILLED, team_labels=LABELS)
    total = _slot(bets, "total")
    assert total.recommendation == "NO"
    assert "not a real market" in total.why


def test_thin_money_caps_a_good_edge_at_a_small_position():
    rungs = [FakeRung("total", "Over 152.5", "0.45", 0.56, 0.10,
                      threshold=152.5, dollars=900.0)]
    bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                         calibration=SKILLED, team_labels=LABELS)
    total = _slot(bets, "total")
    assert total.recommendation == "SMALL"
    assert "too thin to trust with size" in total.why


def test_dollars_are_reported_on_every_row_including_refusals():
    """A NO has to be explicable. The money is the reason more often than the
    edge is, so it travels with the row either way."""
    rungs = [FakeRung("total", "Over 152.5", "0.45", 0.46, 0.005,
                      threshold=152.5, dollars=50_000.0)]
    bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                         calibration=SKILLED, team_labels=LABELS)
    total = _slot(bets, "total")
    assert total.recommendation == "NO"
    assert total.dollars_traded == pytest.approx(50_000.0, rel=1e-6)
    assert "$50,000" in total.money_note


def test_flow_against_the_bet_downgrades_it_to_small():
    """Heavy one-sided money on the other side does not veto a bet -- large
    money is often wrong -- but taking the other end of an informed flow
    warrants a smaller stake, and saying so."""
    rung = FakeRung("total", "Over 152.5", "0.42", 0.55, 0.11, threshold=152.5)
    pred = FakePred(total=150.0, market_total=150.0)
    flows = {rung.ticker: {"imbalance": -0.8, "total_notional_usd": 50_000.0}}

    plain = _slot(vd.build_bets(pred, [rung], calibration=SKILLED,
                                team_labels=LABELS), "total")
    against = _slot(vd.build_bets(pred, [rung], calibration=SKILLED,
                                  team_labels=LABELS, flows=flows), "total")
    assert plain.recommendation == "BET"
    assert against.recommendation == "SMALL"
    assert "*against* this side" in against.why


def test_flow_direction_is_read_relative_to_the_side_being_bought():
    """An under is a NO, so heavy YES aggression is money *against* it.
    Reading the sign without the side would turn a warning into a
    confirmation."""
    yes_heavy = {"imbalance": +0.8, "total_notional_usd": 50_000.0}
    assert mn.read_flow(yes_heavy, side="yes", model_prob=0.5).agrees is True
    assert mn.read_flow(yes_heavy, side="no", model_prob=0.5).agrees is False


def test_chosen_tickers_are_deduplicated_for_the_flow_fetch():
    rungs = [
        FakeRung("game", "Home wins", "0.45", 0.50, 0.04, subject=HOME),
        FakeRung("game", "Away wins", "0.55", 0.50, 0.04, subject=AWAY),
    ]
    bets = vd.build_bets(FakePred(), rungs, calibration=SKILLED,
                         team_labels=LABELS)
    tickers = vd.chosen_tickers(bets)
    assert len(tickers) == len(set(tickers))


# --------------------------------------------------------------------------
# skill gating
# --------------------------------------------------------------------------

def test_no_skill_blocks_the_bet_outright():
    """If the model cannot predict totals in this league, its disagreement
    with the market is not evidence however large."""
    rungs = [FakeRung("total", "Over 145.5", "0.40", 0.60, 0.19,
                      threshold=145.5)]
    bets = vd.build_bets(
        FakePred(total=150.0, market_total=140.0), rungs,
        calibration={"skill_total": 0.01}, team_labels=LABELS,
    )
    total = _slot(bets, "total")
    assert total.recommendation == "NO"
    assert "variance" in total.why


def test_negative_edge_says_the_price_is_already_dear():
    rungs = [FakeRung("total", "Over 151.5", "0.55", 0.50, -0.06,
                      threshold=151.5)]
    bets = vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                         calibration=SKILLED, team_labels=LABELS)
    assert "dearer than the model" in _slot(bets, "total").why


# --------------------------------------------------------------------------
# the predicted score
# --------------------------------------------------------------------------

def test_score_line_names_both_teams_and_rounds_to_whole_points():
    """Decimals on a predicted score are false precision when sigma is 13."""
    o = vd.build_outlook(FakePred(total=215.0, spread=-6.0),
                         calibration=SKILLED, team_labels=LABELS)
    assert o.score_line == "Away Team 110 - Home Team 104"


def test_outlook_names_the_winner_rather_than_a_signed_margin():
    home = vd.build_outlook(FakePred(spread=6.0), calibration=SKILLED,
                            team_labels=LABELS)
    away = vd.build_outlook(FakePred(spread=-6.0), calibration=SKILLED,
                            team_labels=LABELS)
    assert home.winner == "Home Team"
    assert away.winner == "Away Team"
    assert "Away Team by 6" in away.verdict_line


def test_win_probability_follows_the_predicted_winner():
    """Reported for whoever is favoured, not always for the home team, or a
    20-point underdog reads as a 7% favourite."""
    o = vd.build_outlook(FakePred(spread=-20.0), calibration=SKILLED,
                         team_labels=LABELS)
    assert o.winner == "Away Team"
    assert o.win_prob > 0.5


def test_basis_cites_the_historical_inputs_in_words():
    o = vd.build_outlook(
        FakePred(total=150.0, spread=5.0, market_total=148.0, market_spread=3.0),
        calibration=SKILLED, team_labels=LABELS,
    )
    joined = " ".join(o.based_on)
    assert "last 7" in joined
    assert "Ratings solved from every result" in joined
    assert "variance in margins" in joined


def test_thin_sample_is_called_weak_however_good_the_league():
    """A score predicted from two games is a guess. This read the wrong key
    once and reported every game as weak, so both directions are pinned."""
    thin = vd.build_outlook(FakePred(games=2), calibration=SKILLED,
                            team_labels=LABELS)
    full = vd.build_outlook(FakePred(games=20), calibration=SKILLED,
                            team_labels=LABELS)
    assert "weak" in thin.confidence
    assert "played only 2" in thin.confidence
    assert "weak" not in full.confidence


def test_missing_book_line_is_flagged_as_a_caution():
    """Without a book line the model has no independent check, and that is
    materially weaker than a disagreement with one."""
    o = vd.build_outlook(FakePred(market_total=None, market_spread=None),
                         calibration=SKILLED, team_labels=LABELS)
    assert any("no check on the model" in c for c in o.cautions)


def test_book_comparison_states_both_numbers():
    o = vd.build_outlook(
        FakePred(total=150.0, spread=5.0, market_total=158.0, market_spread=1.0),
        calibration=SKILLED, team_labels=LABELS,
    )
    assert "book has Home Team by 1.0" in o.market_check
    assert "-8.0" in o.market_check


# --------------------------------------------------------------------------
# census and summary
# --------------------------------------------------------------------------

def test_census_separates_listed_from_tradeable():
    """A NO has to be explicable as a judgement, not a missing feature."""
    rungs = [
        FakeRung("total", "Over 150.5", "1.00", 0.90, None),
        FakeRung("total", "Over 152.5", "0.45", 0.50, 0.04, threshold=152.5),
        FakeRung("total", "Over 190.5", "0.01", 0.02, 0.009, threshold=190.5),
    ]
    census = vd.market_census(rungs)["total"]
    assert census["listed"] == 3
    assert census["quotable"] == 2     # the $1.00 ask is not
    assert census["credible"] == 1     # nor is the 1c tail


def test_summarise_says_plainly_when_there_is_nothing():
    bets = vd.build_bets(FakePred(), [], calibration=SKILLED, team_labels=LABELS)
    assert "No bet worth placing" in vd.summarise(bets)


def test_summarise_lists_only_actionable_bets():
    rungs = [FakeRung("total", "Over 152.5", "0.42", 0.55, 0.11,
                      threshold=152.5)]
    out = vd.summarise(
        vd.build_bets(FakePred(total=150.0, market_total=150.0), rungs,
                      calibration=SKILLED, team_labels=LABELS)
    )
    assert "Total" in out
    assert "Moneyline" not in out


# --------------------------------------------------------------------------
# the money module itself
# --------------------------------------------------------------------------

def test_tiers_rise_with_dollars():
    assert mn.tier(5_000_000) == "deep"
    assert mn.tier(20_000) == "active"
    assert mn.tier(2_000) == "moderate"
    assert mn.tier(500) == "thin"
    assert mn.tier(10) == "negligible"


def test_required_edge_is_monotone_in_money():
    """Strictly non-decreasing: a deeper market may never demand less."""
    needs = [mn.required_edge(d) for d in (10, 500, 2_000, 20_000, 5_000_000)]
    assert needs == sorted(needs)
    assert needs[-1] > needs[0]


def test_game_money_counts_each_market_once():
    """A total market yields two rungs, a yes and a no. Summing both would
    double every total and make a quiet game look busy."""
    over = FakeRung("total", "Over 152.5", "0.40", 0.5, 0.0,
                    threshold=152.5, dollars=1_000.0, side="yes")
    under = FakeRung("total", "Over 152.5", "0.60", 0.5, 0.0,
                     threshold=152.5, dollars=1_000.0, side="no")
    under.ticker = over.ticker          # same market, other half
    gm = mn.game_money([over, under])
    assert gm.markets_counted == 1
    assert gm.total_traded == pytest.approx(1_000.0, rel=1e-6)


def test_game_money_splits_by_bet_type_and_finds_the_busiest():
    rungs = [
        FakeRung("game", "Home wins", "0.50", 0.5, 0.0, dollars=900_000.0),
        FakeRung("spread", "Home by over 3.5", "0.50", 0.5, 0.0,
                 threshold=3.5, dollars=10_000.0),
        FakeRung("total", "Over 152.5", "0.50", 0.5, 0.0,
                 threshold=152.5, dollars=5_000.0),
    ]
    gm = mn.game_money(rungs)
    assert gm.by_type["moneyline"] == pytest.approx(900_000.0, rel=1e-6)
    assert gm.busiest_title == "Home wins"
    assert gm.tier == "deep"
    assert "3 Kalshi markets" in gm.summary()


def test_flow_near_parity_is_reported_as_split_not_as_support():
    fr = mn.read_flow({"imbalance": 0.05, "total_notional_usd": 1_000.0},
                      side="yes", model_prob=0.5)
    assert fr.agrees is None
    assert "split" in fr.note


def test_absent_flow_makes_no_claim():
    fr = mn.read_flow(None, side="yes", model_prob=0.5)
    assert fr.agrees is None
    assert fr.note == ""
