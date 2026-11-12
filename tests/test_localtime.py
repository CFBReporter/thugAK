"""Tests for the Central-Time layer.

Every bug this guards against hides data rather than raising. A slate query
whose window is an hour wrong simply returns fewer games, and a UTC "today"
after 6pm shows tomorrow's slate -- both look like a quiet evening rather
than a fault, which is why they survived a whole build undetected.

The DST cases are the ones worth having: a fixed -6 offset passes every test
written in January and is wrong for the entire football season.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import localtime as lt  # noqa: E402
from thugsak import store  # noqa: E402
from thugsak.model import asof  # noqa: E402


# --------------------------------------------------------------------------
# parsing: the shapes these sources actually emit
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "2026-11-11T19:00Z",              # ESPN
    "2026-11-11T19:00:00+00:00",      # our own writes
    "2026-11-11T19:00:00",            # naive, assumed UTC
    "2026-11-11T13:00:00-06:00",      # an explicit CT offset
])
def test_every_stored_shape_parses_to_the_same_instant(raw):
    assert lt.parse_ts(raw) == datetime(2026, 11, 11, 19, 0, tzinfo=timezone.utc)


def test_epoch_seconds_parse():
    """Kalshi candlesticks carry epoch seconds, not ISO strings."""
    assert lt.parse_ts(1794423600) == datetime(
        2026, 11, 11, 19, 0, tzinfo=timezone.utc
    )


def test_asof_reexports_the_same_parser():
    """One parser, so the as-of guard and the display layer cannot disagree
    about what an instant is."""
    assert asof.parse_ts is lt.parse_ts


# --------------------------------------------------------------------------
# DST, which a fixed offset gets wrong half the year
# --------------------------------------------------------------------------

def test_offset_follows_the_tz_database_not_a_constant():
    nov = lt.to_local("2026-11-11T19:00Z")   # CST, UTC-6
    jul = lt.to_local("2026-07-11T19:00Z")   # CDT, UTC-5
    assert (nov.hour, lt.tz_abbrev(nov)) == (13, "CST")
    assert (jul.hour, lt.tz_abbrev(jul)) == (14, "CDT")


def test_day_bounds_are_24_hours_on_an_ordinary_day():
    lo, hi = lt.day_bounds_utc("2026-11-11")
    assert (lo, hi) == ("2026-11-11T06:00", "2026-11-12T06:00")


def test_day_bounds_shrink_in_spring_and_stretch_in_autumn():
    """The DST transition days are 23 and 25 hours long. Hardcoding 24 would
    drop an hour of games on one and double-count on the other."""
    def hours(d):
        lo, hi = lt.day_bounds_utc(d)
        fmt = "%Y-%m-%dT%H:%M"
        span = datetime.strptime(hi, fmt) - datetime.strptime(lo, fmt)
        return span.total_seconds() / 3600

    assert hours("2026-03-08") == 23    # spring forward
    assert hours("2026-11-01") == 25    # fall back
    assert hours("2026-11-11") == 24


def test_bounds_sort_lexicographically_against_both_stored_shapes():
    """The window is compared in SQL against substr(start_time,1,16). That is
    only sound if the truncated forms of both stored shapes sort against the
    bounds -- the full strings do not, since a trailing Z sorts above a +.
    """
    lo, hi = lt.day_bounds_utc("2026-11-11")
    espn = "2026-11-12T01:00Z"[:16]
    ours = "2026-11-12T01:00:00+00:00"[:16]
    assert espn == ours
    assert lo <= espn < hi


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def test_clock_time_has_no_leading_zero_and_names_its_zone():
    assert lt.fmt_time("2026-11-12T01:00Z") == "7:00 PM CST"
    assert lt.fmt_time("2026-11-11T19:30Z") == "1:30 PM CST"


def test_datetime_format_gives_the_central_day_not_the_utc_one():
    """A 7pm CT tip-off is already tomorrow in UTC; the label must say the
    day the person would call it."""
    assert lt.fmt_datetime("2026-11-12T01:00Z") == "Wed Nov 11, 7:00 PM CST"


def test_local_date_of_a_late_tipoff_is_the_day_it_tipped_off():
    assert lt.local_date("2026-11-12T01:00Z") == "2026-11-11"
    assert lt.local_date("2026-11-12T06:30Z") == "2026-11-12"


# --------------------------------------------------------------------------
# the slate query: the bug that hid a 67-game night
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    return path


def _game(conn, gid, start, *, slate=None):
    store.upsert_game(conn, {
        "game_id": gid, "league": "ncaamb", "season": 2027,
        "season_type": "regular", "start_time": start,
        "home_team": f"ncaamb:h{gid}", "away_team": f"ncaamb:a{gid}",
        "completed": 0, "neutral_site": 0,
        "source": "espn", "slate_date": slate,
    })


def test_an_evening_slate_is_not_split_across_two_dates(db):
    """Every one of these tips off on Nov 11 in Central, though three of the
    four are already Nov 12 in UTC. Before the fix only the first was found."""
    with store.connect(db) as conn:
        _game(conn, "g1", "2026-11-11T23:00Z")   # 5:00 PM CST
        _game(conn, "g2", "2026-11-12T00:00Z")   # 6:00 PM CST
        _game(conn, "g3", "2026-11-12T01:30Z")   # 7:30 PM CST
        _game(conn, "g4", "2026-11-12T04:00Z")   # 10:00 PM CST
        conn.commit()
        found = {
            r["game_id"]
            for r in store.games_on_date(conn, "ncaamb", "2026-11-11")
        }
    assert found == {"g1", "g2", "g3", "g4"}


def test_the_next_morning_belongs_to_the_next_slate(db):
    """The old fallback swept anything before midday UTC the following day
    into the slate, which pulled genuine next-day games backwards."""
    with store.connect(db) as conn:
        _game(conn, "late", "2026-11-12T04:00Z")   # 10:00 PM CST Nov 11
        _game(conn, "next", "2026-11-12T17:00Z")   # 11:00 AM CST Nov 12
        conn.commit()
        d11 = {
            r["game_id"]
            for r in store.games_on_date(conn, "ncaamb", "2026-11-11")
        }
        d12 = {
            r["game_id"]
            for r in store.games_on_date(conn, "ncaamb", "2026-11-12")
        }
    assert d11 == {"late"}
    assert d12 == {"next"}


def test_slate_date_is_derived_from_central_on_write(db):
    with store.connect(db) as conn:
        _game(conn, "g", "2026-11-12T01:30Z")
        conn.commit()
        row = store.get_game(conn, "g")
    assert row["slate_date"] == "2026-11-11"


def test_an_explicit_slate_date_wins_over_the_derived_one(db):
    """ESPN is the authority on which slate it filed a game under; if it says
    Nov 12, we do not second-guess it from the clock."""
    with store.connect(db) as conn:
        _game(conn, "g", "2026-11-12T01:30Z", slate="2026-11-12")
        conn.commit()
        assert store.get_game(conn, "g")["slate_date"] == "2026-11-12"
        assert not store.games_on_date(conn, "ncaamb", "2026-11-11")
        assert len(store.games_on_date(conn, "ncaamb", "2026-11-12")) == 1


def test_rows_predating_the_backfill_are_stamped_once(db):
    """A database built by an earlier version has slate_date NULL throughout.
    init_db has to fix those in place, or they only ever match via the
    fallback window."""
    with store.connect(db) as conn:
        _game(conn, "g", "2026-11-12T01:30Z")
        conn.execute("UPDATE games SET slate_date=NULL")
        conn.execute("DELETE FROM meta WHERE key='slate_backfill'")
        conn.commit()

    store.init_db(db)
    with store.connect(db) as conn:
        assert store.get_game(conn, "g")["slate_date"] == "2026-11-11"
        # and it does not run a second time
        assert store.get_meta(conn, "slate_backfill") == store.SLATE_BACKFILL_VERSION
