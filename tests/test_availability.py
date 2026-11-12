"""Tests for derived player availability.

This is the only injury signal that exists for college, so it has to be right
about the thing that is easy to get backwards: a backup who racked up usage
covering for an injured starter, then stops playing when that starter returns.
Read naively, that is a high-usage player going missing. In reality the team
just got stronger, and recommending against them would be exactly wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.model import availability as av  # noqa: E402


def box(players: dict[str, tuple[str, str, float]], *, dnp: set[str] = frozenset()):
    """Build a basketball-shaped box score for one team.

    `players` maps athlete id -> (name, position, minutes).
    """
    return {
        "boxscore": {
            "players": [
                {
                    "team": {"abbreviation": "AAA"},
                    "statistics": [
                        {
                            "labels": ["MIN", "PTS"],
                            "athletes": [
                                {
                                    "athlete": {
                                        "id": aid,
                                        "displayName": name,
                                        "position": {"abbreviation": pos},
                                    },
                                    "stats": [str(mins), "10"],
                                    "didNotPlay": aid in dnp,
                                }
                                for aid, (name, pos, mins) in players.items()
                            ],
                        }
                    ],
                }
            ]
        }
    }


# --------------------------------------------------------------------------
# box score parsing
# --------------------------------------------------------------------------

def test_parse_boxscore_reads_minutes_and_identity():
    parsed = av.parse_boxscore(box({"1": ("Alice", "PG", 34.0)}))
    assert "AAA" in parsed
    rec = parsed["AAA"]["1"]
    assert rec["name"] == "Alice"
    assert rec["position"] == "PG"
    assert rec["usage"] == 34.0
    assert rec["dnp"] is False


def test_parse_boxscore_flags_dressed_but_unused():
    """A player listed with didNotPlay was available but unused, which is not
    the same as being absent from the sheet entirely."""
    parsed = av.parse_boxscore(box({"1": ("Alice", "PG", 0.0)}, dnp={"1"}))
    assert parsed["AAA"]["1"]["dnp"] is True


def test_football_usage_uses_attempts_from_a_split_stat():
    """Football box scores write completions as "20/39"; the attempt count is
    the usable half."""
    assert av._usage_from_football("passing", ["20/39", "193"]) == 39.0
    assert av._usage_from_football("rushing", ["17", "68"]) == 17.0
    # Groups that do not represent volume contribute nothing.
    assert av._usage_from_football("fumbles", ["1", "1"]) == 0.0


def test_empty_boxscore_yields_nothing():
    assert av.parse_boxscore({}) == {}
    assert av.parse_boxscore({"boxscore": {"players": []}}) == {}


# --------------------------------------------------------------------------
# PlayerSpell semantics
# --------------------------------------------------------------------------

def _spell(pattern, usage=30.0, kind="absent", pos="PG", appearances=None):
    sp = av.PlayerSpell(
        athlete_id="1", name="Alice", position=pos,
        appearances=appearances if appearances is not None else sum(pattern),
        window=len(pattern), avg_usage=usage, usage_unit="minutes", kind=kind,
    )
    sp.pattern = list(pattern)
    for appeared in sp.pattern:
        if appeared:
            break
        sp.consecutive_missed += 1
    return sp


def test_consecutive_misses_counted_from_the_most_recent_game():
    """The pattern is newest-first, so a leading run of absences is current."""
    assert _spell([False, False, True, True]).consecutive_missed == 2
    assert _spell([True, False, False, True]).consecutive_missed == 0


def test_two_misses_reads_stronger_than_one():
    assert _spell([False, False, True, True]).confidence == "likely out"
    assert _spell([False, True, True, True]).confidence == "possibly out"
    assert _spell([True, True, True, True]).confidence == "playing"


def test_unestablished_player_is_not_a_finding():
    """One appearance then absence is not evidence of anything."""
    sp = _spell([False, False, False, True], appearances=1)
    assert not sp.established
    assert sp.confidence == "none"


def test_stand_in_is_not_reported_as_a_loss():
    """The case this module exists to get right."""
    sp = _spell([False, True, True, True], kind="stand_in")
    assert "stood in" in sp.confidence
    assert "out" not in sp.confidence.split()[0]


def test_replaced_is_distinguished_from_absent():
    # Needs two appearances to count as established, or confidence is "none"
    # regardless of kind.
    assert _spell([False, False, True, True], kind="replaced").confidence == (
        "out, but the role has been absorbed"
    )
    assert _spell([False, False, True, True], kind="absent").confidence == "likely out"


# --------------------------------------------------------------------------
# the substitution classifier
# --------------------------------------------------------------------------

def _report(spells, order, league="ncaamb"):
    rep = av.AvailabilityReport(team_id="x:AAA", league=league, as_of="2026-11-11T00:00:00+00:00")
    rep.games_read = len(order)
    rep.usage_unit = "minutes"
    rep.absent = [s for s in spells.values() if s.consecutive_missed and s.established]
    rep.roster_usage = {k: v.avg_usage for k, v in spells.items()}
    return rep


def test_genuine_absence_when_position_output_falls():
    """Starter out, nobody covers: the position's output drops, so it counts."""
    starter = _spell([False, True, True, True], usage=34.0)
    starter.athlete_id = "star"
    bench = _spell([True, True, True, True], usage=4.0)
    bench.athlete_id = "bench"
    spells = {"star": starter, "bench": bench}
    order = [{"bench"}, {"star", "bench"}, {"star", "bench"}, {"star", "bench"}]
    rep = _report(spells, order)
    av._classify_absences(rep, spells, order, {})
    assert starter.kind == "absent"
    assert starter.position_retained is not None
    assert starter.position_retained < 0.85


def test_stand_in_detected_when_the_incumbent_returns():
    """A deputy who played only while the starter was out, and has now
    stopped, must not be reported as a loss."""
    deputy = _spell([False, True, True, False], usage=30.0)
    deputy.athlete_id = "deputy"
    # The starter missed the middle games and is back for the most recent one.
    starter = _spell([True, False, False, True], usage=32.0)
    starter.athlete_id = "starter"
    spells = {"deputy": deputy, "starter": starter}
    order = [{"starter"}, {"deputy"}, {"deputy"}, {"starter"}]
    rep = _report(spells, order)
    av._classify_absences(rep, spells, order, {})
    assert deputy.kind == "stand_in"
    assert "stood in" in deputy.confidence


def test_classifier_needs_a_position_to_compare_within():
    """Without a position there are no peers, so no claim is made."""
    sp = _spell([False, True, True], usage=20.0, pos="")
    sp.athlete_id = "a"
    spells = {"a": sp}
    order = [set(), {"a"}, {"a"}]
    rep = _report(spells, order)
    av._classify_absences(rep, spells, order, {})
    assert sp.kind == "absent"
    assert sp.position_retained is None


def test_classifier_is_inert_on_a_single_game_window():
    """One game cannot distinguish a substitution from an absence."""
    sp = _spell([False], usage=20.0)
    sp.athlete_id = "a"
    spells = {"a": sp}
    rep = _report(spells, [set()])
    av._classify_absences(rep, spells, [set()], {})
    assert sp.kind == "absent"


# --------------------------------------------------------------------------
# report aggregation
# --------------------------------------------------------------------------

def test_share_missing_weighs_absences_by_usage():
    """Two absent reserves matter far less than one absent starter, and the
    share is what expresses that."""
    rep = av.AvailabilityReport(team_id="x:AAA", league="ncaamb", as_of="2026-11-11T00:00:00+00:00")
    rep.games_read = 4
    rep.roster_usage = {"a": 34.0, "b": 30.0, "c": 6.0, "d": 5.0}
    rep.absent = [_spell([False, True, True, True], usage=34.0)]
    assert rep.share_missing == pytest.approx(34.0 / 75.0, abs=1e-6)


def test_summary_says_so_when_nothing_is_missing():
    rep = av.AvailabilityReport(team_id="x:AAA", league="ncaamb", as_of="2026-11-11T00:00:00+00:00")
    rep.games_read = 3
    assert "No established contributor" in rep.summary()


def test_summary_reports_no_data_separately_from_no_absences():
    """Having read nothing is not the same as having found nothing."""
    rep = av.AvailabilityReport(team_id="x:AAA", league="ncaamb", as_of="2026-11-11T00:00:00+00:00")
    assert "No recent box scores" in rep.summary()


def test_merge_separates_unreported_absences():
    """The college case: absent and on no injury report at all."""
    rep = av.AvailabilityReport(team_id="x:AAA", league="ncaamb", as_of="2026-11-11T00:00:00+00:00")
    rep.games_read = 4
    rep.usage_unit = "minutes"
    known = _spell([False, True, True, True], usage=30.0)
    known.athlete_id = "known"
    unknown = _spell([False, False, True, True], usage=28.0)
    unknown.athlete_id = "unknown"
    rep.absent = [known, unknown]
    rep.roster_usage = {"known": 30.0, "unknown": 28.0}

    merged = av.merge_with_injuries(rep, injury_ids={"known"})
    assert [p["name"] for p in merged["absent_and_reported"]] == ["Alice"]
    assert len(merged["absent_and_unreported"]) == 1
    assert "box scores" in merged["caveat"]


def test_merge_carries_the_backward_looking_caveat():
    """The output must never read as a statement about the next game."""
    rep = av.AvailabilityReport(team_id="x:AAA", league="ncaamb", as_of="2026-11-11T00:00:00+00:00")
    rep.games_read = 3
    merged = av.merge_with_injuries(rep, injury_ids=set())
    assert "return without notice" in merged["caveat"]
