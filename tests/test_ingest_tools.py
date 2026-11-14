"""Agent 1's deterministic tools, driven by a fake provider session.

No network. The point is that these produce the same rows every run from the
same inputs, which is the property an LLM loop on top of them relies on. The
session double returns the shapes `clients.espn.EspnClient` returns, so a
change to those shapes breaks here rather than in production at 3am.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import store  # noqa: E402
from thugsak.agents import ingest_tools as it  # noqa: E402
from thugsak.clients.espn import Injury, PlayerGame  # noqa: E402
from thugsak.mcp import tools  # noqa: E402

LEAGUE = "nba"
HOME, AWAY = "nba:BOS", "nba:NYK"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    store.init_db(path)
    with store.connect(path) as conn:
        for tid, canon, name, eid in (
            (HOME, "BOS", "Boston Celtics", "2"),
            (AWAY, "NYK", "New York Knicks", "18"),
            ("nba:MIA", "MIA", "Miami Heat", "14"),
        ):
            store.upsert_team(conn, LEAGUE, canon, name, eid, is_d1=None)
            store.add_alias(conn, LEAGUE, canon, "espn", tid)
        # A completed game on the slate date and an earlier one for scope.
        store.upsert_game(conn, {
            "game_id": "nba:espn:401", "league": LEAGUE, "season": 2027,
            "start_time": "2026-11-12T00:00:00+00:00", "slate_date": "2026-11-11",
            "home_team": HOME, "away_team": AWAY, "home_score": 110, "away_score": 100,
            "completed": 1, "neutral_site": 0, "source": "test",
        })
        store.upsert_game(conn, {
            "game_id": "nba:espn:400", "league": LEAGUE, "season": 2027,
            "start_time": "2026-11-09T00:00:00+00:00", "slate_date": "2026-11-08",
            "home_team": HOME, "away_team": "nba:MIA", "home_score": 99, "away_score": 90,
            "completed": 1, "neutral_site": 0, "source": "test",
        })
        store.upsert_game(conn, {
            "game_id": "nba:espn:402", "league": LEAGUE, "season": 2027,
            "start_time": "2026-11-13T00:00:00+00:00", "slate_date": "2026-11-12",
            "home_team": AWAY, "away_team": HOME, "completed": 0, "neutral_site": 0,
            "source": "test",
        })
        conn.commit()
    return str(path)


class FakeSession:
    """Looks like EspnClient from the outside."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.injury_report = {
            "BOS": [Injury("p1", "Jay Tatum", "F", "Questionable", team_abbrev="BOS")],
            "NYK": [Injury("p2", "Jalen B", "G", "Out", team_abbrev="NYK")],
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def summary(self, league, event_id):
        self.calls.append(("summary", event_id))
        return {"boxscore": {"players": [
            {"team": {"abbreviation": "BOS"}, "statistics": [{
                "name": "", "labels": ["MIN", "PTS"],
                "athletes": [
                    {"athlete": {"id": "p1", "displayName": "Jay Tatum",
                                 "position": {"abbreviation": "F"}},
                     "stats": ["34", "28"], "didNotPlay": False},
                    {"athlete": {"id": "p3", "displayName": "Bench Guy",
                                 "position": {"abbreviation": "C"}},
                     "stats": ["0", "0"], "didNotPlay": True},
                ]}]},
            {"team": {"abbreviation": "NYK"}, "statistics": [{
                "name": "", "labels": ["MIN", "PTS"],
                "athletes": [
                    {"athlete": {"id": "p2", "displayName": "Jalen B",
                                 "position": {"abbreviation": "G"}},
                     "stats": ["30", "20"], "didNotPlay": False},
                ]}]},
        ]}}

    async def injuries(self, league):
        self.calls.append(("injuries",))
        return self.injury_report

    async def _get(self, url, params=None, ttl=None):
        self.calls.append(("roster", url))
        return {"athletes": [
            {"id": "p1", "displayName": "Jay Tatum", "position": {"abbreviation": "F"}},
            {"id": "p3", "displayName": "Bench Guy", "position": {"abbreviation": "C"}},
        ]}

    async def player_gamelog(self, league, athlete_id, *, season=None):
        self.calls.append(("gamelog", athlete_id))
        return [
            PlayerGame("401", "2026-11-12T00:00", "NYK", "vs", "W", {"pts": 28.0}),
            PlayerGame("400", "2026-11-09T00:00", "MIA", "vs", "W", {"pts": 22.0}),
        ]

    async def depth_chart(self, league, espn_team_id, *, season=None):
        self.calls.append(("depth", espn_team_id))
        return {"F": ["p1", "p9"], "C": ["p3"]}


@pytest.fixture()
def session(monkeypatch):
    s = FakeSession()
    monkeypatch.setattr(it, "_session", lambda client: s)
    return s


def _run(coro):
    return asyncio.run(coro)


# --- scope ------------------------------------------------------------------


def test_slate_scope_includes_last_opponents(db):
    with store.connect(db, readonly=True) as conn:
        scope = it.slate_scope(conn, LEAGUE, "2026-11-12")
    assert scope["games"] == ["nba:espn:402"]
    assert set(scope["on_slate"]) == {HOME, AWAY}
    # BOS played MIA on the 9th, so MIA is in scope as an opponent only.
    assert scope["opponents"] == ["nba:MIA"]
    assert "nba:MIA" in scope["teams"]


# --- box scores -------------------------------------------------------------


def test_box_scores_write_detail_and_player_rows(db, session):
    out = _run(it.ingest_box_scores(LEAGUE, ["nba:espn:401"], db=db))
    assert out["written"]["games"] == 1
    assert out["written"]["player_games"] == 3
    with store.connect(db, readonly=True) as conn:
        d = conn.execute(
            "SELECT detail FROM team_game_stats WHERE game_id='nba:espn:401' AND team_id=?",
            (HOME,),
        ).fetchone()["detail"]
        pg = store.player_games_for_team(conn, LEAGUE, HOME)
        scores = conn.execute(
            "SELECT home_score, away_score FROM games WHERE game_id='nba:espn:401'"
        ).fetchone()
    assert json.loads(d) == {"players": 2, "appeared": 1}
    assert {r["athlete_id"] for r in pg} == {"p1", "p3"}
    # Merging detail must not have blanked the score.
    assert (scores["home_score"], scores["away_score"]) == (110, 100)


def test_box_scores_skip_incomplete_games_without_fetching(db, session):
    out = _run(it.ingest_box_scores(LEAGUE, ["nba:espn:402", "nope"], db=db))
    assert out["skipped"] == ["nba:espn:402", "nope"]
    assert ("summary", "402") not in session.calls


def test_box_scores_are_idempotent(db, session):
    _run(it.ingest_box_scores(LEAGUE, ["nba:espn:401"], db=db))
    _run(it.ingest_box_scores(LEAGUE, ["nba:espn:401"], db=db))
    with store.connect(db, readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM player_games").fetchone()["c"]
    assert n == 3


# --- player logs ------------------------------------------------------------


def test_player_logs_roster_then_gamelog_per_player(db, session):
    out = _run(it.ingest_player_logs(LEAGUE, [HOME], db=db))
    assert out["written"] == {"teams": 1, "players": 2, "player_games": 4}
    gamelog_calls = [c for c in session.calls if c[0] == "gamelog"]
    assert sorted(c[1] for c in gamelog_calls) == ["p1", "p3"]
    with store.connect(db, readonly=True) as conn:
        p = conn.execute("SELECT * FROM players WHERE athlete_id='p1'").fetchone()
    assert p["team_id"] == HOME and p["position"] == "F"


def test_player_logs_cap_per_team(db, session):
    out = _run(it.ingest_player_logs(LEAGUE, [HOME], max_players_per_team=1, db=db))
    assert out["written"]["players"] == 1


def test_player_logs_report_teams_without_espn_id(db, session):
    with store.connect(db) as conn:
        store.upsert_team(conn, LEAGUE, "ZZZ", "No Id FC", None, is_d1=None)
        conn.commit()
    out = _run(it.ingest_player_logs(LEAGUE, ["nba:ZZZ"], db=db))
    assert out["no_espn_id"] == ["nba:ZZZ"]
    assert out["written"]["teams"] == 0


# --- injuries ---------------------------------------------------------------


def test_injuries_first_snapshot_is_all_new(db, session):
    out = _run(it.ingest_injuries(LEAGUE, db=db))
    assert out["snapshot_rows"] == 2
    assert out["changed"] == {
        "p1": {"from": None, "to": "Questionable"},
        "p2": {"from": None, "to": "Out"},
    }
    assert out["material"] is True


def test_injuries_unchanged_snapshot_is_not_material(db, session):
    _run(it.ingest_injuries(LEAGUE, db=db))
    out = _run(it.ingest_injuries(LEAGUE, db=db))
    assert out["changed"] == {} and out["cleared"] == []
    assert out["material"] is False


def test_injuries_status_flip_is_material(db, session):
    _run(it.ingest_injuries(LEAGUE, db=db))
    session.injury_report["BOS"] = [
        Injury("p1", "Jay Tatum", "F", "Out", team_abbrev="BOS")
    ]
    out = _run(it.ingest_injuries(LEAGUE, db=db))
    assert out["changed"] == {"p1": {"from": "Questionable", "to": "Out"}}
    assert out["material"] is True


def test_identical_reports_do_not_grow_the_snapshot_table(db, session):
    # The NFL report is ~800 rows, mostly `Active`. Appending it hourly would
    # be ~15k rows a day in a table that is never pruned. Unchanged -> no write.
    for _ in range(3):
        out = _run(it.ingest_injuries(LEAGUE, db=db))
    with store.connect(db, readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM injury_snapshots").fetchone()["c"]
    assert n == 2
    assert out["wrote_snapshot"] is False
    assert out["report_rows"] == 2


def test_a_status_flip_writes_a_full_snapshot(db, session):
    _run(it.ingest_injuries(LEAGUE, db=db))
    session.injury_report["BOS"] = [Injury("p1", "Jay Tatum", "F", "Out", team_abbrev="BOS")]
    out = _run(it.ingest_injuries(LEAGUE, db=db))
    assert out["wrote_snapshot"] is True and out["snapshot_rows"] == 2
    with store.connect(db, readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM injury_snapshots").fetchone()["c"]
        latest = store.latest_injury_status(conn, LEAGUE)
    assert n == 4                       # two complete pictures, not a delta
    assert latest == {"p1": "Out", "p2": "Out"}


def test_stale_snapshot_triggers_a_daily_heartbeat_write(db, session):
    # Same report as before, but the previous snapshot is a day and a half
    # old: write it again so the history has a daily point even when quiet.
    with store.connect(db) as conn:
        store.snapshot_injuries(
            conn, LEAGUE,
            [(HOME, {"athlete_id": "p1", "status": "Questionable"}),
             (AWAY, {"athlete_id": "p2", "status": "Out"})],
            as_of="2026-11-11T20:00:00+00:00",
        )
        conn.commit()
    out = _run(it.ingest_injuries(LEAGUE, db=db))
    assert out["material"] is False
    assert out["wrote_snapshot"] is True


# --- depth charts -----------------------------------------------------------


def test_depth_charts_written_for_nba(db, session):
    out = _run(it.ingest_depth_charts(LEAGUE, [HOME], db=db))
    assert out["written"] == 3
    with store.connect(db, readonly=True) as conn:
        rows = conn.execute(
            "SELECT position, athlete_id, rank FROM depth_chart WHERE team_id=? ORDER BY position, rank",
            (HOME,),
        ).fetchall()
    assert [tuple(r) for r in rows] == [("C", "p3", 1), ("F", "p1", 1), ("F", "p9", 2)]


def test_depth_charts_unsupported_for_college(db, session):
    out = _run(it.ingest_depth_charts("ncaamb", ["ncaamb:DUKE"], db=db))
    assert out["unsupported"] is True
    assert not [c for c in session.calls if c[0] == "depth"]


# --- audit ------------------------------------------------------------------


def test_audit_flags_a_team_with_no_history(db):
    with store.connect(db) as conn:
        store.upsert_team(conn, LEAGUE, "GHO", "Ghost Town", "99", is_d1=None)
        conn.commit()
    out = _run(it.audit_data_integrity(LEAGUE, db=db))
    kinds = {f["kind"]: f for f in out["findings"]}
    assert "no_history" in kinds
    assert "nba:GHO" in kinds["no_history"]["teams"]
    assert out["ok"] is False


def test_audit_flags_stale_calibration(db):
    with store.connect(db) as conn:
        conn.execute(
            "INSERT INTO calibration (league,key,value,n_games,seasons,fitted_at) "
            "VALUES (?,?,?,?,?,?)",
            (LEAGUE, "sigma", 11.0, 100, "2027", "2026-11-01T00:00:00"),
        )
        conn.commit()
    out = _run(it.audit_data_integrity(LEAGUE, db=db))
    kinds = {f["kind"] for f in out["findings"]}
    assert "calibration_stale" in kinds


def test_audit_is_quiet_on_a_healthy_db(db):
    # The fixture's three teams all have history; nothing high/medium.
    out = _run(it.audit_data_integrity(LEAGUE, db=db))
    assert out["ok"] is True
    assert not [f for f in out["findings"] if f["severity"] in ("high", "medium")]


# --- alias link -------------------------------------------------------------


def test_link_alias_refuses_unknown_team(db):
    out = _run(it.link_team_alias(LEAGUE, "Bostonians", "barttorvik", "nba:NOPE", db=db))
    assert out["ok"] is False


def test_link_alias_records_and_reports_prior_holder(db):
    out = _run(it.link_team_alias(LEAGUE, "Boston C.", "barttorvik", HOME, db=db))
    assert out["ok"] is True and out["previously"] is None
    with store.connect(db, readonly=True) as conn:
        from thugsak import matching
        assert matching.resolve_team(conn, LEAGUE, "Boston C.", source="barttorvik") == HOME


# --- retention through the tool ---------------------------------------------


def test_run_retention_never_touches_games(db, session, monkeypatch):
    monkeypatch.setattr(it.cache, "trim", lambda: {"evicted": 0})
    _run(it.ingest_player_logs(LEAGUE, [HOME], db=db))
    with store.connect(db, readonly=True) as conn:
        before = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
    out = _run(it.run_retention(LEAGUE, keep=1, db=db))
    with store.connect(db, readonly=True) as conn:
        after = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
        dates = {r["date"] for r in store.player_games_for_team(conn, LEAGUE, HOME)}
    assert before == after == 3
    assert dates == {"2026-11-12"}
    assert out["removed"]["player_games"] == 2


# --- registry ---------------------------------------------------------------


def test_ingest_tools_are_registered_once_each():
    names = [t["name"] for t in tools.TOOL_SCHEMAS]
    for n in ("ingest_slate", "ingest_box_scores", "ingest_player_logs",
              "ingest_injuries", "ingest_depth_charts", "audit_data_integrity",
              "link_team_alias", "run_retention"):
        assert names.count(n) == 1, n


def test_registry_entries_have_the_shape_both_servers_need():
    for t in tools.TOOL_SCHEMAS:
        assert set(t) >= {"name", "description", "handler", "schema"}, t["name"]
        assert callable(t["handler"])
        assert t["schema"]["type"] == "object"
        assert "properties" in t["schema"]
