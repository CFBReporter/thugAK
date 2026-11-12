"""Tests for normalising team names across sources.

Every source names teams differently, and when two of them fail to agree the
game simply shows no Kalshi markets — there is no error, just a fixture you
cannot analyse. One evening that was 15 of 29 college basketball games and
the only NFL game on the card.

The causes, each pinned below:

- **Kalshi's ticker codes agree with nobody** (`WAS` where ESPN says `WSH`),
  and for most college markets `rules_secondary` carries no "A vs B" phrasing
  either, so the codes were the only source tried. The market *titles* name
  the teams perfectly well and nothing looked at them.
- **Kalshi truncates a nickname to an initial** when a city fields two teams:
  "New York G". As a word, `g` is missing from "New York Giants" and
  disqualifies the correct team.
- **Accents** — ESPN writes "San José State", Kalshi does not.
- **Parenthetical qualifiers** — "Johnson & Wales (RI)". Dropping them always
  is wrong too: it merges "Miami (OH)" into "Miami".
- **Optional words** — "college" and "university" vary between sources, but
  dropping them unconditionally collapses Boston College and Boston
  University onto one token.
- **Ambiguity** — "New York" fits the Giants and the Jets equally. Picking
  one silently is how a bet lands on the wrong team.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import matching as m  # noqa: E402
from thugsak import store  # noqa: E402


class Team:
    """The shape `register_espn_team` reads."""

    def __init__(self, espn_id, abbrev, display, location="", nickname=""):
        self.espn_id = espn_id
        self.abbrev = abbrev
        self.display_name = display
        self.location = location
        self.nickname = nickname
        self.short_name = ""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    with store.connect(path) as conn:
        for t in (
            Team("19", "NYG", "New York Giants", "New York", "Giants"),
            Team("20", "NYJ", "New York Jets", "New York", "Jets"),
            Team("28", "WSH", "Washington Commanders", "Washington",
                 "Commanders"),
            Team("24", "LAC", "Los Angeles Chargers", "Los Angeles",
                 "Chargers"),
        ):
            m.register_espn_team(conn, "nfl", t, is_d1=None)
        for t in (
            Team("13", "LAL", "Los Angeles Lakers", "Los Angeles", "Lakers"),
            Team("12", "LAC", "LA Clippers", "LA", "Clippers"),
        ):
            m.register_espn_team(conn, "nba", t, is_d1=None)
        for t in (
            Team("103", "BC", "Boston College Eagles", "Boston College"),
            Team("104", "BU", "Boston University Terriers",
                 "Boston University"),
            Team("193", "M-OH", "Miami (OH) RedHawks", "Miami (OH)"),
            Team("2390", "MIA", "Miami Hurricanes", "Miami"),
            Team("23", "SJSU", "San José State Spartans", "San José State"),
            Team("333", "ALA", "Alabama Crimson Tide", "Alabama"),
            Team("2011", "ALST", "Alabama State Hornets", "Alabama State"),
            Team("9000", "JWU", "Johnson & Wales Griffins",
                 "Johnson & Wales"),
        ):
            m.register_espn_team(conn, "ncaamb", t, is_d1=True)
        conn.commit()
    return path


def r(db, league, name):
    with store.connect(db) as conn:
        return m.resolve_team(conn, league, name)


# --------------------------------------------------------------------------
# the truncated nickname
# --------------------------------------------------------------------------

def test_a_trailing_initial_picks_the_right_team(db):
    """Kalshi writes "New York G" when a city fields two teams. Treated as a
    word, `g` is simply absent from "New York Giants" and rules it out."""
    assert r(db, "nfl", "New York G") == "nfl:NYG"
    assert r(db, "nfl", "New York J") == "nfl:NYJ"


def test_the_initial_must_actually_match(db):
    assert r(db, "nfl", "New York Z") is None


def test_a_bare_city_shared_by_two_teams_resolves_to_nothing(db):
    """The alternative is picking one, which is how a bet lands on the wrong
    team. An unmatched market is visible; a wrong team is not."""
    assert r(db, "nfl", "New York") is None


def test_a_city_with_one_team_still_resolves(db):
    assert r(db, "nfl", "Washington") == "nfl:WSH"


# --------------------------------------------------------------------------
# spelling differences
# --------------------------------------------------------------------------

def test_accents_are_folded(db):
    """ESPN writes San José, Kalshi writes San Jose. Unfolded, the accented
    character is stripped as punctuation and leaves "jos", which shares no
    token with "jose"."""
    assert r(db, "ncaamb", "San Jose St.") == "ncaamb:SJSU"
    assert r(db, "ncaamb", "San José State") == "ncaamb:SJSU"


def test_a_city_abbreviation_matches_its_spelled_out_form(db):
    """ESPN calls the Clippers "LA Clippers" while every other source says
    Los Angeles."""
    assert r(db, "nba", "Los Angeles C") == "nba:LAC"
    assert r(db, "nba", "LA Clippers") == "nba:LAC"
    assert r(db, "nba", "Los Angeles L") == "nba:LAL"


def test_contracting_the_city_does_not_touch_similar_names(db):
    """"Los Angeles" -> "la" is safe in a way that "la" -> "Los Angeles"
    would not be: the latter would mangle Louisiana Tech."""
    assert m._norm("Louisiana Tech") == "louisiana tech"


def test_st_still_means_saint_in_front_and_state_elsewhere(db):
    """Barttorvik writes "Alabama St." for Alabama State but "St. John's"
    for Saint John's; reading the abbreviation the same way both times
    silently merges two different schools. The apostrophe splits off as its
    own token, which is harmless because both sources are normalised the
    same way."""
    assert m._norm("Alabama St.") == "alabama state"
    assert m._norm("St. John's") == "saint john s"
    assert m._norm("St. John's Red Storm").startswith("saint john s")


# --------------------------------------------------------------------------
# optional words, which are not noise
# --------------------------------------------------------------------------

def test_college_and_university_distinguish_two_real_schools(db):
    """Dropped unconditionally these collapse both names onto "boston"."""
    assert r(db, "ncaamb", "Boston College") == "ncaamb:BC"
    assert r(db, "ncaamb", "Boston University") == "ncaamb:BU"


def test_a_parenthetical_qualifier_is_optional_not_ignored(db):
    """Keeping it always loses Johnson & Wales; dropping it always merges
    Miami (OH) into Miami. It has to be tried both ways, strict first."""
    assert r(db, "ncaamb", "Johnson & Wales (RI)") == "ncaamb:JWU"
    assert r(db, "ncaamb", "Miami (OH)") == "ncaamb:M-OH"
    assert r(db, "ncaamb", "Miami") == "ncaamb:MIA"


def test_a_longer_school_name_does_not_fall_back_to_the_shorter_one(db):
    """"Alabama State" must not resolve to Alabama."""
    assert r(db, "ncaamb", "Alabama") == "ncaamb:ALA"
    assert r(db, "ncaamb", "Alabama State") == "ncaamb:ALST"


# --------------------------------------------------------------------------
# reading the team out of a market title
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Oklahoma St. wins", "Oklahoma St."),
    ("Washington wins", "Washington"),
    ("New York G wins", "New York G"),
    ("Johnson & Wales (RI) wins", "Johnson & Wales (RI)"),
    ("Boston University", "Boston University"),
])
def test_the_title_suffix_is_stripped(title, expected):
    """"Kansas City wins" resolves to nothing; "Kansas City" resolves at
    once. The title is the only readable team name in most college
    payloads."""
    assert m.team_from_title(title) == expected


# --------------------------------------------------------------------------
# contested aliases at registration
# --------------------------------------------------------------------------

def test_a_shared_location_is_registered_to_neither_team(db):
    """Both New York teams claim "New York" with equal right, so the alias
    belongs to neither. It really was bound to the Giants."""
    with store.connect(db) as conn:
        assert store.resolve_alias(conn, "nfl", "new york") is None


def test_an_unequal_contest_goes_to_the_d1_programme(db):
    """"Ohio State" fits a top-25 programme and a Division III branch
    campus. There *is* a right answer there, unlike the New York case."""
    with store.connect(db) as conn:
        m.register_espn_team(
            conn, "ncaaf",
            Team("194", "OSU", "Ohio State Buckeyes", "Ohio State"),
            is_d1=True,
        )
        m.register_espn_team(
            conn, "ncaaf",
            Team("9999", "OSU", "Ohio State Newark Titans", "Ohio State"),
            is_d1=False,
        )
        conn.commit()
        assert m.resolve_team(conn, "ncaaf", "Ohio State") == "ncaaf:OSU"


def test_the_cleanup_drops_only_genuinely_ambiguous_aliases(db):
    """A first attempt deleted 208 good college aliases, because "Alabama"
    is a token-subset of both "Alabama Crimson Tide" and "Alabama State
    Hornets". It plainly means the former."""
    with store.connect(db) as conn:
        dropped = m.drop_ambiguous_aliases(conn, "ncaamb")
        conn.commit()
        assert dropped == []
        assert m.resolve_team(conn, "ncaamb", "Alabama") == "ncaamb:ALA"
        assert m.resolve_team(conn, "ncaamb", "Boston College") == "ncaamb:BC"


# --------------------------------------------------------------------------
# a source's own abbreviations
# --------------------------------------------------------------------------

def test_source_codes_resolve_without_claiming_the_name_generally(db):
    """nflverse writes "LA" for the Rams, but "LA" on its own is a city two
    NFL teams share. Scoping the alias to the source disambiguates the
    import without handing the city to one team everywhere else."""
    with store.connect(db) as conn:
        m.register_espn_team(
            conn, "nfl",
            Team("14", "LAR", "Los Angeles Rams", "Los Angeles", "Rams"),
            is_d1=None,
        )
        seeded = m.seed_source_codes(conn, "nflverse", "nfl")
        conn.commit()
        assert seeded >= 1
        assert m.resolve_team(conn, "nfl", "LA", source="nflverse") == "nfl:LAR"
        # Unscoped, the city is still ambiguous between the Rams and Chargers.
        assert m.resolve_team(conn, "nfl", "Los Angeles") is None


def test_relocated_franchises_keep_one_continuous_history(db):
    """St. Louis, San Diego and Oakland map to the current club, so a team's
    record does not split in two at the move."""
    with store.connect(db) as conn:
        for t in (
            Team("14", "LAR", "Los Angeles Rams", "Los Angeles", "Rams"),
            Team("13", "LV", "Las Vegas Raiders", "Las Vegas", "Raiders"),
        ):
            m.register_espn_team(conn, "nfl", t, is_d1=None)
        m.seed_source_codes(conn, "nflverse", "nfl")
        conn.commit()
        assert m.resolve_team(conn, "nfl", "STL", source="nflverse") == "nfl:LAR"
        assert m.resolve_team(conn, "nfl", "OAK", source="nflverse") == "nfl:LV"


def test_seeding_is_idempotent(db):
    with store.connect(db) as conn:
        m.register_espn_team(
            conn, "nfl",
            Team("14", "LAR", "Los Angeles Rams", "Los Angeles", "Rams"),
            is_d1=None,
        )
        first = m.seed_source_codes(conn, "nflverse", "nfl")
        second = m.seed_source_codes(conn, "nflverse", "nfl")
        conn.commit()
        assert first == second
