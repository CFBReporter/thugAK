"""The call follows the predicted score; value against it is a longshot.

IND @ HOU, Nov 19 2026: the model had Houston 27-19, yet the call was
"Indianapolis wins by over 3.5" and "Indianapolis wins" -- good value, but
bets that lose whenever the prediction is right.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.analysis import verdict as v  # noqa: E402

PRED = SimpleNamespace(home_team="nfl:HOU", away_team="nfl:IND", home_points=27.0, away_points=19.0)


def _bet(slot, kind, team=None, strike=None, side="yes", edge=0.06, rec=v.BET):
    return v.Bet(slot=slot, label=slot, bet=slot, recommendation=rec, kind=kind,
                 subject_team=team, threshold=strike, side=side, price=0.2,
                 all_in_cost=0.21, model_prob=0.3, edge=edge, required_edge=0.03)


IND_COVER = _bet("spread_away", "spread", "nfl:IND", 3.5, edge=0.057)
IND_ML = _bet("moneyline_away", "game", "nfl:IND", edge=0.05)
HOU_ML = _bet("moneyline_home", "game", "nfl:HOU", edge=0.04)
UNDER_50 = _bet("total", "total", None, 50.5, side="no", edge=0.035)   # predicted 46


def test_bets_against_the_prediction_are_longshots():
    assert v.follows_prediction(IND_COVER, PRED) is False
    assert v.follows_prediction(IND_ML, PRED) is False
    assert v.follows_prediction(HOU_ML, PRED) is True
    assert v.follows_prediction(UNDER_50, PRED) is True


def test_the_call_uses_only_bets_that_agree_with_the_prediction():
    bets = [IND_COVER, IND_ML, HOU_ML, UNDER_50]
    agree, longshots = v.split_by_prediction(bets, PRED)
    assert agree == [HOU_ML, UNDER_50] and longshots == [IND_COVER, IND_ML]
    call = v.the_call(bets, PRED)
    assert call.startswith("Bet moneyline_home") and "spread_away" not in call
    assert v.best_bet(bets, PRED) is HOU_ML


def test_only_longshots_means_no_call_but_points_to_them():
    call = v.the_call([IND_COVER, IND_ML], PRED)
    assert "No bet on the predicted outcome" in call and "Longshots" in call
    assert v.best_bet([IND_COVER, IND_ML], PRED) is None
