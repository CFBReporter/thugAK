"""Barttorvik team names that cost their schools every stored game.

`import_ncaamb` skips a row when either side fails to resolve. That is the
right call -- a game credited to the wrong team is worse than a missing one --
but it means one unresolvable name costs a school its entire history, and the
model then declines to predict any of its games. Eight Division I teams were
in that state: the UC Riverside at San Jose State fixture opened as an empty
screen while the games sat unread in the source file.

These tests pin the *names*, not the data, so they need no database and no
network: a normalisation change that breaks one of these mappings fails here
rather than silently emptying a team's history again months later.
"""

from __future__ import annotations

import pytest

from thugsak.matching import NAME_OVERRIDES, _norm

# (Barttorvik's spelling, the canonical code it must reach). Every one of
# these appeared 65+ times across three seasons of `super_sked` -- a full
# Division I schedule -- and resolved to nothing.
BARTTORVIK_NAMES = [
    ("McNeese St.", "MCN"),
    ("Texas A&M Corpus Chris", "AMCC"),
    ("Grambling St.", "GRAM"),
    ("Nicholls St.", "NICH"),
    ("Tennessee Martin", "UTM"),
    ("Hawaii", "HAW"),
    ("Southeastern Louisiana", "SELA"),
    ("Sam Houston St.", "SHSU"),
    ("USC Upstate", "UPST"),
    ("Cal Baptist", "CBU"),
    ("Albany", "UALB"),
    ("Appalachian St.", "APP"),
]


@pytest.mark.parametrize("name,code", BARTTORVIK_NAMES)
def test_barttorvik_name_has_an_override(name, code):
    assert NAME_OVERRIDES["ncaamb"].get(_norm(name)) == code


def test_st_means_state_not_saint_for_these_schools():
    # The single most common cause: "St." expanded the wrong way sends
    # "McNeese St." looking for a saint.
    for name in ("McNeese St.", "Grambling St.", "Nicholls St.", "Sam Houston St."):
        assert _norm(name).endswith("state"), name


def test_saint_francis_is_left_unmapped_on_purpose():
    """Not an oversight, and not to be "fixed" by pointing it somewhere.

    Saint Francis (PA) is Division I and appears 65 times, but has no record
    in the teams table, so there is nothing to map it to. The nearest name
    present is San Francisco -- a different school. An override here would
    credit one team's games to another, which is worse than the games staying
    dropped. Registering the team is the fix.
    """
    assert _norm("Saint Francis") not in NAME_OVERRIDES["ncaamb"]


def test_overrides_are_keyed_in_normalised_form():
    """A key that is not already normalised can never be hit.

    `resolve_team` normalises before the override lookup, so an entry keyed
    "McNeese St." would sit in the table being useless -- the failure mode is
    silence, which is why it is asserted rather than trusted.
    """
    for league in ("ncaamb", "ncaaf"):
        for key in NAME_OVERRIDES[league]:
            assert key == _norm(key), f"{league}: {key!r} != {_norm(key)!r}"


def test_overrides_do_not_collide_with_each_other():
    # Two spellings may share a target, but one spelling must not have two.
    for league in ("ncaamb", "ncaaf"):
        keys = list(NAME_OVERRIDES[league])
        assert len(keys) == len(set(keys)), league
