"""SQLite persistence: schema, writes, and as-of-safe reads.

Retention model (see plan): the three bootstrap seasons exist only long enough
for `model.fit` to distil them into the `calibration` table, then they are
pruned. Steady state keeps the current season plus rolling last-7 detail.

Every read that feeds a prediction goes through a function taking a mandatory
`as_of` timestamp. Those functions filter to strictly-earlier completed games.
Lookahead leaks are the classic way a backtest lies, so the filter lives here
rather than being re-implemented per caller.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH
from .localtime import day_bounds_utc, local_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id      TEXT PRIMARY KEY,
    league       TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    display_name TEXT,
    espn_id      TEXT,
    -- 1 for Division I, 0 for a lower division kept only because a D1 team
    -- played them, NULL where the distinction does not apply (pro leagues).
    is_d1        INTEGER,
    UNIQUE(league, canonical)
);

-- Maps every name/abbreviation we encounter to a canonical team. Built during
-- the history import, while the source data is still present.
CREATE TABLE IF NOT EXISTS team_aliases (
    league  TEXT NOT NULL,
    alias   TEXT NOT NULL,
    source  TEXT NOT NULL,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    PRIMARY KEY (league, alias, source)
);

CREATE TABLE IF NOT EXISTS games (
    game_id       TEXT PRIMARY KEY,
    league        TEXT NOT NULL,
    season        INTEGER NOT NULL,
    season_type   TEXT DEFAULT 'regular',
    start_time    TEXT NOT NULL,
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    home_score    INTEGER,
    away_score    INTEGER,
    completed     INTEGER NOT NULL DEFAULT 0,
    neutral_site  INTEGER NOT NULL DEFAULT 0,
    close_spread  REAL,
    close_total   REAL,
    close_home_ml INTEGER,
    close_away_ml INTEGER,
    source        TEXT,
    -- The Central-Time calendar date a person would call this game's date.
    -- Distinct from start_time, which is stored in UTC: a 7pm CT tip-off is
    -- 01:00 UTC the *next* day, so filtering a slate by the UTC date of
    -- start_time hides most of an evening's games.
    slate_date    TEXT
);
CREATE INDEX IF NOT EXISTS ix_games_league_time ON games(league, start_time);
CREATE INDEX IF NOT EXISTS ix_games_season ON games(league, season);
-- Kalshi matching looks a game up by team pair once per market (~45 markets
-- a game); without this each lookup scanned the table, ~4s per NCAAF slate.
CREATE INDEX IF NOT EXISTS ix_games_teams ON games(league, home_team, away_team);

-- One row per team per game: the query surface for last-N windows.
CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id        TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
    team_id        TEXT NOT NULL,
    opponent_id    TEXT NOT NULL,
    league         TEXT NOT NULL,
    season         INTEGER NOT NULL,
    start_time     TEXT NOT NULL,
    is_home        INTEGER NOT NULL,
    neutral_site   INTEGER NOT NULL DEFAULT 0,
    points_for     INTEGER,
    points_against INTEGER,
    detail         TEXT,
    PRIMARY KEY (game_id, team_id)
);
CREATE INDEX IF NOT EXISTS ix_tgs_team_time ON team_game_stats(team_id, start_time);
CREATE INDEX IF NOT EXISTS ix_tgs_league_time ON team_game_stats(league, start_time);

CREATE TABLE IF NOT EXISTS ratings_pit (
    league   TEXT NOT NULL,
    as_of    TEXT NOT NULL,
    team_id  TEXT NOT NULL,
    rating   REAL NOT NULL,
    n_games  INTEGER NOT NULL,
    PRIMARY KEY (league, as_of, team_id)
);

-- The distilled bootstrap: survives pruning and is what the model uses.
CREATE TABLE IF NOT EXISTS calibration (
    league     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      REAL NOT NULL,
    n_games    INTEGER,
    seasons    TEXT,
    fitted_at  TEXT NOT NULL,
    PRIMARY KEY (league, key)
);

-- Never pruned: the running record of what the model said and when.
-- The permanent record. Everything else in this database is a cache that
-- `import_history.py` can rebuild, but this cannot be: it is what the model
-- actually said at the time, and no source will reissue that.
--
-- It is therefore deliberately **self-contained**. The final score is copied
-- in rather than joined from `games`, because `prune_seasons` deletes a
-- season's games once its calibration has been distilled -- including, at
-- rollover, the season you have just spent a year collecting predictions
-- for. Joining would leave a drawer full of forecasts with nothing to check
-- them against, exactly when the record finally got long enough to mean
-- something.
CREATE TABLE IF NOT EXISTS predictions (
    pred_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id          TEXT NOT NULL,
    league           TEXT NOT NULL,
    as_of            TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    pred_home_points REAL,
    pred_away_points REAL,
    pred_spread      REAL,
    pred_total       REAL,
    model_version    TEXT,
    payload          TEXT,
    -- Copied from the game once it finishes; see above.
    matchup          TEXT,
    slate_date       TEXT,
    -- Team ids, needed to settle a moneyline or spread bet against the
    -- score. Stored rather than joined for the same reason as everything
    -- else here: the games row will not always be there.
    home_team        TEXT,
    away_team        TEXT,
    actual_home      INTEGER,
    actual_away      INTEGER,
    settled_at       TEXT,
    -- The five bets exactly as recommended, with the prices and the money
    -- that were on screen. Kalshi serves a settled market's candles for now,
    -- but that is their retention policy to change, not ours to rely on.
    bets             TEXT,
    the_call         TEXT,
    kalshi_money_usd REAL
);
CREATE INDEX IF NOT EXISTS ix_pred_game ON predictions(game_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Player-level persistence. Before these tables, every player stat was
-- fetched live per request and discarded: the only durable trace was raw
-- padded JSON in the HTTP cache, keyed by URL hash and unqueryable by player.
-- These hold the rolling last-7 detail the plan describes. Team-level results
-- (`games`, `team_game_stats`) are NOT subject to the 7-game cap -- the fitted
-- blend weights put 0.80 on the full-season window and the ratings solve reads
-- every result this season, so capping those would silently change the model.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS players (
    athlete_id TEXT PRIMARY KEY,
    league     TEXT NOT NULL,
    team_id    TEXT NOT NULL,
    name       TEXT,
    position   TEXT,
    source     TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_players_team ON players(league, team_id);

-- One row per player per game, rolling last 7 per team.
CREATE TABLE IF NOT EXISTS player_games (
    athlete_id  TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    league      TEXT NOT NULL,
    team_id     TEXT NOT NULL,
    date        TEXT NOT NULL,
    opponent    TEXT,
    home_away   TEXT,
    result      TEXT,
    stats       TEXT,
    source      TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (athlete_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_pg_team_date ON player_games(league, team_id, date);

-- Position order per team, snapshotted with an as_of so a later run can tell
-- whether a backup was already the backup before kickoff.
CREATE TABLE IF NOT EXISTS depth_chart (
    league     TEXT NOT NULL,
    team_id    TEXT NOT NULL,
    position   TEXT NOT NULL,
    athlete_id TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    as_of      TEXT NOT NULL,
    PRIMARY KEY (league, team_id, position, rank, as_of)
);
CREATE INDEX IF NOT EXISTS ix_depth_team ON depth_chart(league, team_id, as_of);

-- Never pruned. ESPN's injuries endpoint is current-only with no history,
-- which is why backtests have had to run without the injury adjustment. A
-- snapshot every run is the one dataset that cannot be reconstructed later.
CREATE TABLE IF NOT EXISTS injury_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    league      TEXT NOT NULL,
    team_id     TEXT NOT NULL,
    athlete_id  TEXT NOT NULL,
    name        TEXT,
    position    TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    as_of       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inj_league_asof ON injury_snapshots(league, as_of);
CREATE INDEX IF NOT EXISTS ix_inj_athlete ON injury_snapshots(athlete_id, as_of);

-- The audit trail that makes autonomy reversible: one row per agent run,
-- including the no-op runs, so a quiet day reads as "checked, nothing moved"
-- rather than "did not run".
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    league      TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    signature   TEXT,
    summary     TEXT,
    writes      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_agent ON agent_runs(agent, started_at);

-- The model's training log (see migrations/postgres/0002_model_runs.sql).
CREATE TABLE IF NOT EXISTS model_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    league      TEXT NOT NULL,
    version     TEXT NOT NULL,
    trained_at  TEXT NOT NULL,
    promoted    INTEGER NOT NULL DEFAULT 0,
    model       TEXT,
    metrics     TEXT,
    changes     TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS ix_model_runs_league ON model_runs(league, trained_at);

-- Line movement (see migrations/postgres/0004_line_history.sql).
CREATE TABLE IF NOT EXISTS line_history (
    game_id     TEXT NOT NULL,
    sportsbook  TEXT NOT NULL,
    market      TEXT NOT NULL,
    side        TEXT NOT NULL,
    open_line   REAL,
    open_odds   REAL,
    open_at     TEXT NOT NULL,
    last_line   REAL,
    last_odds   REAL,
    last_at     TEXT NOT NULL,
    PRIMARY KEY (game_id, sportsbook, market, side)
);
CREATE INDEX IF NOT EXISTS ix_line_history_game ON line_history(game_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_postgres(conn: Any) -> bool:
    return isinstance(conn, db.PgConnection)


def day_gap_sql(conn: Any, a: str, b: str) -> str:
    """SQL for the absolute number of days between two YYYY-MM-DD expressions.

    The one construct with no common spelling: SQLite has `julianday`,
    Postgres subtracts dates. `a` and `b` are SQL expressions (a column, or a
    `?` placeholder), inserted verbatim -- never pass user input as either.
    """
    if is_postgres(conn):
        return f"abs(CAST({a} AS date) - CAST({b} AS date))"
    return f"abs(julianday({a}) - julianday({b}))"


def uses_postgres(path: Path | str = DB_PATH) -> bool:
    """True when this path means "the app's database" and that is Postgres.

    Only the default path is redirected. An explicit other path -- every test
    fixture, a scratch copy, a backup -- is always a SQLite file, so the test
    suite can never touch the production database by accident.
    """
    return Path(path) == Path(DB_PATH) and db.backend() == "postgres"


@contextmanager
def connect(path: Path | str = DB_PATH, *, readonly: bool = False):
    if uses_postgres(path):
        conn: Any = db.connect_postgres()
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A generous busy timeout: imports write in batches while making slow
        # HTTP calls, and a reader should wait rather than fail.
        conn = sqlite3.connect(path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` leaves an
# existing table alone, so a database built by an earlier version keeps the old
# shape and every query touching a new column fails. Each entry is applied only
# when absent, which keeps `init_db` safe to run repeatedly.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("teams", "is_d1", "INTEGER"),
    ("games", "slate_date", "TEXT"),
    ("predictions", "matchup", "TEXT"),
    ("predictions", "slate_date", "TEXT"),
    ("predictions", "home_team", "TEXT"),
    ("predictions", "away_team", "TEXT"),
    ("predictions", "actual_home", "INTEGER"),
    ("predictions", "actual_away", "INTEGER"),
    ("predictions", "settled_at", "TEXT"),
    ("predictions", "bets", "TEXT"),
    ("predictions", "the_call", "TEXT"),
    ("predictions", "kalshi_money_usd", "REAL"),
    # Efficiency model (model/efficiency.py); see migrations 0003.
    ("games", "possessions", "REAL"),
)


def _dedupe_predictions(conn: sqlite3.Connection) -> list[str]:
    """Collapse rows written before `save_prediction` became an upsert.

    Opening a game appended a row every time, so one fixture reached twenty
    identical rows. Any accuracy figure computed over the table would have
    weighted that game twenty times -- a silent bias toward whichever games
    happened to get looked at most.
    """
    dupes = conn.execute(
        """SELECT count(*) - count(DISTINCT
             game_id || '|' || as_of || '|' || COALESCE(model_version,'')
           ) AS n FROM predictions"""
    ).fetchone()
    if not dupes or not dupes["n"]:
        return []
    conn.execute(
        """DELETE FROM predictions WHERE pred_id NOT IN (
             SELECT max(pred_id) FROM predictions
             GROUP BY game_id, as_of, COALESCE(model_version,'')
           )"""
    )
    return [f"predictions deduped ({dupes['n']} repeat rows removed)"]


def _backfill_prediction_context(conn: sqlite3.Connection) -> list[str]:
    """Copy matchup, date and team ids onto predictions written before the
    record became self-contained.

    Worth doing once, now, while the games are still present: after the next
    `prune_seasons` those rows could never say who played.
    """
    rows = conn.execute(
        """SELECT p.pred_id, g.home_team, g.away_team, g.slate_date,
                  g.start_time
             FROM predictions p JOIN games g ON g.game_id = p.game_id
            WHERE p.home_team IS NULL OR p.matchup IS NULL"""
    ).fetchall()
    if not rows:
        return []
    updates = []
    for r in rows:
        updates.append((
            f"{team_label(conn, r['away_team'], short=True)} @ "
            f"{team_label(conn, r['home_team'], short=True)}",
            r["slate_date"] or str(r["start_time"] or "")[:10],
            r["home_team"], r["away_team"], r["pred_id"],
        ))
    conn.executemany(
        """UPDATE predictions
             SET matchup=COALESCE(matchup, ?), slate_date=COALESCE(slate_date, ?),
                 home_team=COALESCE(home_team, ?), away_team=COALESCE(away_team, ?)
           WHERE pred_id=?""",
        updates,
    )
    return [f"predictions backfilled with matchup/teams ({len(updates)} rows)"]


def _migrate(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"{table}.{column}")
    return applied


# Bumped when a backfill needs to run again.
SLATE_BACKFILL_VERSION = 1


def _backfill_slate_dates(conn: sqlite3.Connection) -> int:
    """Stamp the Central slate date onto rows stored before it was derived.

    Rows imported by earlier versions have `slate_date` NULL, and the slate
    query then falls back to a UTC window. That fallback is correct but
    costs an index-less scan, and leaves two different notions of a game's
    date in one table. Converting is a Python job rather than SQL because
    SQLite has no DST rule.
    """
    if get_meta(conn, "slate_backfill") == SLATE_BACKFILL_VERSION:
        return 0
    rows = conn.execute(
        "SELECT game_id, start_time FROM games "
        "WHERE slate_date IS NULL AND start_time IS NOT NULL"
    ).fetchall()
    updates = []
    for r in rows:
        try:
            updates.append((local_date(r["start_time"]), r["game_id"]))
        except (ValueError, TypeError):
            continue
    conn.executemany("UPDATE games SET slate_date=? WHERE game_id=?", updates)
    put_meta(conn, "slate_backfill", SLATE_BACKFILL_VERSION)
    return len(updates)


# Indexes over columns that arrived via a migration. They cannot sit in
# SCHEMA: on an existing database `executescript` runs before `_migrate` has
# added the column, and the whole script then fails.
POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_pred_settled "
    "ON predictions(league, settled_at)",
)


def init_db(path: Path | str = DB_PATH) -> list[str]:
    """Create the schema if absent, then apply any pending column additions."""
    if uses_postgres(path):
        return _init_postgres()
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        applied = _migrate(conn)
        for stmt in POST_MIGRATION_INDEXES:
            conn.execute(stmt)
        applied += _dedupe_predictions(conn)
        applied += _backfill_prediction_context(conn)
        n = _backfill_slate_dates(conn)
        if n:
            applied.append(f"games.slate_date backfilled ({n} rows, Central)")
        n = _clear_unfinished_scores(conn)
        if n:
            applied.append(f"team_game_stats: cleared placeholder scores on {n} unfinished rows")
        return applied


# Targets already brought up to date in this process. `init_db` is called at
# the top of nearly every tool, which on SQLite is a few local statements but
# on Postgres would be several network round trips per request. The SQLite
# schema script and its PRAGMA-based column migrations never run here: the
# Postgres schema is the versioned files in migrations/postgres.
_PG_READY: set[str] = set()


def _init_postgres() -> list[str]:
    key = db.target_key()
    if key in _PG_READY:
        return []
    with connect() as conn:
        applied = [f"migration {v}" for v in db.apply_migrations(conn)]
        applied += _dedupe_predictions(conn)
        applied += _backfill_prediction_context(conn)
        n = _backfill_slate_dates(conn)
        if n:
            applied.append(f"games.slate_date backfilled ({n} rows, Central)")
        n = _clear_unfinished_scores(conn)
        if n:
            applied.append(f"team_game_stats: cleared placeholder scores on {n} unfinished rows")
    _PG_READY.add(key)
    return applied


def _clear_unfinished_scores(conn: sqlite3.Connection) -> int:
    """Null the points on team rows whose game is not final.

    Before `_sync_team_rows` refused them, unplayed fixtures were written with
    ESPN's 0-0 placeholder. Idempotent: once clean, it matches nothing.
    """
    cur = conn.execute(
        """UPDATE team_game_stats SET points_for=NULL, points_against=NULL
           WHERE points_for IS NOT NULL AND game_id IN (
             SELECT game_id FROM games WHERE completed = 0
           )"""
    )
    return cur.rowcount


# --------------------------------------------------------------------------
# teams and aliases
# --------------------------------------------------------------------------

def team_key(league: str, canonical: str) -> str:
    return f"{league}:{canonical}"


def upsert_team(
    conn: sqlite3.Connection,
    league: str,
    canonical: str,
    display_name: str | None = None,
    espn_id: str | None = None,
    is_d1: bool | None = None,
) -> str:
    tid = team_key(league, canonical)
    conn.execute(
        """INSERT INTO teams
             (team_id, league, canonical, display_name, espn_id, is_d1)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(team_id) DO UPDATE SET
             display_name = COALESCE(excluded.display_name, teams.display_name),
             espn_id      = COALESCE(excluded.espn_id, teams.espn_id),
             is_d1        = COALESCE(excluded.is_d1, teams.is_d1)""",
        (
            tid, league, canonical, display_name, espn_id,
            None if is_d1 is None else int(is_d1),
        ),
    )
    # The teams cache (and the fuzzy-match cache keyed on it) must see this.
    _TEAMS_CACHE.pop((db_ident(conn), league), None)
    return tid


def d1_teams(conn: sqlite3.Connection, league: str) -> list[sqlite3.Row]:
    """A league's Division I teams, or all of them where the flag is unset."""
    return list(
        conn.execute(
            """SELECT * FROM teams
               WHERE league=? AND (is_d1 = 1 OR is_d1 IS NULL)
               ORDER BY canonical""",
            (league,),
        )
    )


# --------------------------------------------------------------------------
# in-process caches for the name tables
# --------------------------------------------------------------------------
#
# One slate load resolves the same few hundred team names thousands of times
# (3,884 alias lookups for one NBA slate). Against a local SQLite file that
# is ~2s; against a hosted Postgres at ~84ms a round trip it was 8 minutes.
# So each league's alias and team tables are read once and answered from
# memory.
#
# Kept correct by: every write through this module updates or drops the
# cache; the three raw writes in `matching` call `invalidate_name_caches`;
# and entries expire after NAME_CACHE_TTL so a write from *another* process
# (the hourly agents beside the API) is picked up within that window. A name
# missing from a stale cache falls through to fuzzy matching, not to a wrong
# answer.

NAME_CACHE_TTL = 300.0

# matching._find_game memo; cleared by upsert_game. Short TTL as a backstop
# for game writes made by another process.
GAME_LOOKUP_CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}
# matching.preload_games: (loaded_at, lo, hi, {team pair: [rows]}) per league.
GAME_INDEX: dict[tuple[str, str], tuple[float, str, str, dict[Any, list[Any]]]] = {}
# Five minutes, like the name caches: page loads write no games, so between
# hourly agent runs nothing in-process changes these; the TTL bounds how long
# another process's write (the agents) can go unseen.
GAME_LOOKUP_TTL = 300.0
_ALIAS_CACHE: dict[tuple[str, str], tuple[float, dict[tuple[str, str], str], dict[str, str]]] = {}
_TEAMS_CACHE: dict[tuple[str, str], tuple[float, list[Any], int]] = {}


def db_ident(conn: Any) -> str:
    """Which database a connection reaches: SQLite file path, or URL + schema."""
    if is_postgres(conn):
        return db.target_key()
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""


def invalidate_name_caches(conn: Any | None = None, league: str | None = None) -> None:
    """Drop cached alias/team tables -- for one league of one database, or all."""
    if conn is None:
        _ALIAS_CACHE.clear()
        _TEAMS_CACHE.clear()
        return
    ident = db_ident(conn)
    for cache in (_ALIAS_CACHE, _TEAMS_CACHE):
        for key in [k for k in cache if k[0] == ident and (league is None or k[1] == league)]:
            cache.pop(key, None)


def _alias_table(conn: Any, league: str):
    import time as _time

    key = (db_ident(conn), league)
    hit = _ALIAS_CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < NAME_CACHE_TTL:
        return hit[1], hit[2]
    by_source: dict[tuple[str, str], str] = {}
    shared: dict[str, tuple[str, str]] = {}
    for r in conn.execute(
        "SELECT alias, source, team_id FROM team_aliases WHERE league=?", (league,)
    ).fetchall():
        alias, source, team_id = r[0], r[1], r[2]
        by_source[(alias, source)] = team_id
        if not source.startswith(SCOPED_PREFIX):
            # The unscoped fallback takes the alphabetically first source,
            # which is what the indexed `LIMIT 1` query returned in SQLite --
            # now deterministic on Postgres too.
            cur = shared.get(alias)
            if cur is None or source < cur[0]:
                shared[alias] = (source, team_id)
    flat = {a: t for a, (_s, t) in shared.items()}
    _ALIAS_CACHE[key] = (_time.monotonic(), by_source, flat)
    return by_source, flat


def add_alias(
    conn: sqlite3.Connection, league: str, alias: str, source: str, team_id: str
) -> None:
    if not alias:
        return
    a = alias.strip().lower()
    by_source, flat = _alias_table(conn, league)
    if (a, source) in by_source:
        # Already stored (ON CONFLICT DO NOTHING would keep the old mapping
        # anyway) -- skip the write. This was 1,044 redundant inserts per slate.
        return
    conn.execute(
        """INSERT INTO team_aliases (league, alias, source, team_id)
           VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
        (league, a, source, team_id),
    )
    by_source[(a, source)] = team_id
    if not source.startswith(SCOPED_PREFIX):
        # Keep the cached fallback in step with the alphabetical-first rule.
        rivals = [s for (al, s) in by_source if al == a and not s.startswith(SCOPED_PREFIX)]
        flat[a] = by_source[(a, min(rivals))]


# Aliases registered under a source carrying this prefix are *private to that
# source*: visible when resolving on its behalf, invisible otherwise. It is
# what lets nflverse's "LA" mean the Rams during its import without handing
# the city to one of the two Los Angeles teams everywhere else.
SCOPED_PREFIX = "scoped:"


def resolve_alias(
    conn: sqlite3.Connection, league: str, alias: str, source: str | None = None
) -> str | None:
    """Look up a canonical team_id from any known name or abbreviation.

    A named `source` is preferred -- first its private mappings, then its
    ordinary ones -- before falling back to the shared table. The fallback
    excludes private mappings, or the scoping would be decorative: a lookup
    with no source would still find them.
    """
    if not alias:
        return None
    a = alias.strip().lower()
    by_source, flat = _alias_table(conn, league)
    if source:
        for src in (SCOPED_PREFIX + source, source):
            hit = by_source.get((a, src))
            if hit:
                return hit
    return flat.get(a)


def all_teams(conn: sqlite3.Connection, league: str) -> list[sqlite3.Row]:
    import time as _time

    key = (db_ident(conn), league)
    hit = _TEAMS_CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < NAME_CACHE_TTL:
        return hit[1]
    rows = list(
        conn.execute(
            "SELECT * FROM teams WHERE league=? ORDER BY canonical", (league,)
        )
    )
    global _TEAMS_LOADS
    _TEAMS_LOADS += 1
    _TEAMS_CACHE[key] = (_time.monotonic(), rows, _TEAMS_LOADS)
    return rows


_TEAMS_LOADS = 0


def teams_version(conn: Any, league: str) -> tuple[str, str, int]:
    """Changes every time the cached teams table for this league reloads."""
    all_teams(conn, league)
    ident = db_ident(conn)
    return (ident, league, _TEAMS_CACHE[(ident, league)][2])


def team_label(conn: sqlite3.Connection, team_id: str, *, short: bool = False) -> str:
    """Human-readable name for a canonical team id.

    Internal ids look like ``nfl:BAL``, which is right for keys and wrong for
    anything a person reads. Falls back to the bare suffix when the team is
    not in the table, so a label never comes back blank.
    """
    if not team_id:
        return ""
    # From the cached league table (team ids are "<league>:<code>"): a slate
    # renders two labels per game, which was a round trip each on Postgres.
    league = team_id.split(":", 1)[0]
    row = next((r for r in all_teams(conn, league) if r["team_id"] == team_id), None)
    if row is None:
        return team_id.split(":")[-1]
    if short:
        return row["canonical"] or team_id.split(":")[-1]
    return row["display_name"] or row["canonical"] or team_id.split(":")[-1]


def espn_id_for(conn: Any, team_id: str) -> str:
    """A team's ESPN id, from the cached league table ('' when unknown)."""
    league = team_id.split(":", 1)[0]
    row = next((r for r in all_teams(conn, league) if r["team_id"] == team_id), None)
    return str(row["espn_id"]) if row is not None and row["espn_id"] else ""


# --------------------------------------------------------------------------
# read caches for remote databases
# --------------------------------------------------------------------------
#
# On Postgres every query is a network round trip (~84 ms from a home PC to
# us-east-1), and one game view asked for the same team history 13 times and
# the calibration 5 times. These caches hold those reads for READ_CACHE_TTL.
# They are Postgres-only -- SQLite reads are local and sub-millisecond, so
# caching there would add risk for no gain -- and `db.on_write` clears them
# whenever any statement in this process writes the tables they mirror.

READ_CACHE_TTL = 300.0
_ACTIVE_MODEL_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_HISTORY_CACHE: dict[tuple[Any, ...], tuple[float, list[Any]]] = {}
_CALIBRATION_CACHE: dict[tuple[str, str], tuple[float, dict[str, float]]] = {}


def invalidate_read_caches() -> None:
    _HISTORY_CACHE.clear()
    _CALIBRATION_CACHE.clear()
    _ACTIVE_MODEL_CACHE.clear()


def _on_db_write(sql: str) -> None:
    s = sql.lower()
    if "team_game_stats" in s or "games" in s:
        _HISTORY_CACHE.clear()
        GAME_LOOKUP_CACHE.clear()
        GAME_INDEX.clear()
    if "calibration" in s:
        _CALIBRATION_CACHE.clear()
    if "model_runs" in s:
        _ACTIVE_MODEL_CACHE.clear()
    # The alias/teams caches are kept in step explicitly by add_alias,
    # upsert_team and invalidate_name_caches; clearing them on every alias
    # insert would reload the table once per newly registered name.


db.on_write(_on_db_write)


def _cached(cache: dict, key: tuple, load):
    import time as _time

    hit = cache.get(key)
    if hit and _time.monotonic() - hit[0] < READ_CACHE_TTL:
        return hit[1]
    value = load()
    cache[key] = (_time.monotonic(), value)
    return value


def team_labels(conn: sqlite3.Connection, league: str) -> dict[str, str]:
    """All of a league's id -> display-name mappings, for bulk rendering."""
    return {
        r["team_id"]: (r["display_name"] or r["canonical"])
        for r in all_teams(conn, league)
    }


# --------------------------------------------------------------------------
# games
# --------------------------------------------------------------------------

GAME_COLS = (
    "game_id", "league", "season", "season_type", "start_time",
    "home_team", "away_team", "home_score", "away_score", "completed",
    "neutral_site", "close_spread", "close_total", "close_home_ml",
    "close_away_ml", "source", "slate_date",
)


def find_existing_game(
    conn: sqlite3.Connection, g: dict[str, Any], *, tolerance_days: int = 1
) -> str | None:
    """Locate an already-stored row for the same real-world game.

    A game's identity has to be independent of which source supplied it: the
    history import keys NFL games by nflverse id while the live slate keys
    them by ESPN id, and without this the same fixture lands twice -- one row
    holding the book line, the other the history, and the slate showing
    whichever it happened to find.

    Matched on league and the team pair. An exact date match always counts;
    a one-day difference counts only when the other row came from a *different*
    source, because sources disagree on the date of a late kickoff (nflverse
    records the local date, ESPN the UTC one, and a Sunday night game falls on
    Monday in UTC).

    Restricting the tolerance to cross-source matches matters: two teams can
    legitimately meet on consecutive days, and a blanket one-day window would
    silently merge those into one game.
    """
    date = str(g.get("start_time") or "")[:10]
    if not date:
        return None
    source = g.get("source")
    gap = day_gap_sql(conn, "substr(start_time,1,10)", "?")
    row = conn.execute(
        f"""SELECT game_id FROM games
           WHERE league=? AND home_team=? AND away_team=?
             AND (
                   substr(start_time,1,10) = ?
                   OR (
                        source IS DISTINCT FROM ?
                        AND {gap} <= ?
                      )
                 )
           ORDER BY {gap}
           LIMIT 1""",
        (
            g["league"], g["home_team"], g["away_team"], date,
            source, date, tolerance_days, date,
        ),
    ).fetchone()
    return row["game_id"] if row else None


def game_unchanged(stored: Any, incoming: dict[str, Any]) -> bool:
    """Would writing `incoming` change nothing about the `stored` row?

    Mirrors `upsert_game`'s merge rule: a None in the incoming row never
    overwrites (COALESCE), so it is not a change. Anything else that differs
    -- a final score, `completed` flipping, a moved start, a new line -- is.
    No stored row means it is new.
    """
    if stored is None:
        return False
    keys = set(stored.keys())
    for col in GAME_COLS:
        if col in ("game_id", "source") or col not in keys:
            continue
        new = incoming.get(col)
        if new is None:
            continue
        old = stored[col]
        if isinstance(new, float) or isinstance(old, float):
            try:
                if abs(float(new) - float(old)) > 1e-9:
                    return False
            except (TypeError, ValueError):
                return False
        elif str(new) != str(old):
            return False
    return True


def upsert_game(conn: sqlite3.Connection, g: dict[str, Any]) -> None:
    """Insert or update one game, then refresh its two team_game_stats rows.

    Reuses an existing row for the same fixture when one is present, so a
    second source enriches the game rather than duplicating it.
    """
    # Any game write can change what a Kalshi-market lookup should find.
    GAME_LOOKUP_CACHE.clear()
    GAME_INDEX.clear()
    g = dict(g)
    # Derive the Central slate date when the caller has not stated one, so
    # every row carries the date a person would file the game under and the
    # slate query never has to fall back to a UTC window.
    if not g.get("slate_date") and g.get("start_time"):
        try:
            g["slate_date"] = local_date(g["start_time"])
        except (ValueError, TypeError):
            pass
    existing = find_existing_game(conn, g)
    if existing and existing != g["game_id"]:
        # Keep whichever id arrived first as canonical; this call's fields are
        # merged onto it by the COALESCE update below.
        g["game_id"] = existing
    vals = [g.get(c) for c in GAME_COLS]
    placeholders = ",".join("?" * len(GAME_COLS))
    updates = ",".join(
        f"{c}=COALESCE(excluded.{c}, games.{c})" for c in GAME_COLS if c != "game_id"
    )
    conn.execute(
        f"INSERT INTO games ({','.join(GAME_COLS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(game_id) DO UPDATE SET {updates}",
        vals,
    )
    _sync_team_rows(conn, g)


def upsert_games(conn: sqlite3.Connection, games: Iterable[dict[str, Any]]) -> int:
    n = 0
    for g in games:
        upsert_game(conn, g)
        n += 1
    return n


def _sync_team_rows(conn: sqlite3.Connection, g: dict[str, Any]) -> None:
    detail = g.get("detail") or {}
    # A score on an unfinished game is ESPN's 0-0 placeholder or a live
    # partial; neither is a result. Team rows get points only once final.
    if not g.get("completed"):
        g = {**g, "home_score": None, "away_score": None}
    sides = (
        ("home", g["home_team"], g["away_team"], g.get("home_score"), g.get("away_score")),
        ("away", g["away_team"], g["home_team"], g.get("away_score"), g.get("home_score")),
    )
    for side, team, opp, pf, pa in sides:
        d = detail.get(side)
        conn.execute(
            """INSERT INTO team_game_stats
                 (game_id, team_id, opponent_id, league, season, start_time,
                  is_home, neutral_site, points_for, points_against, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(game_id, team_id) DO UPDATE SET
                 points_for     = COALESCE(excluded.points_for, team_game_stats.points_for),
                 points_against = COALESCE(excluded.points_against, team_game_stats.points_against),
                 detail         = COALESCE(excluded.detail, team_game_stats.detail)""",
            (
                g["game_id"], team, opp, g["league"], g["season"], g["start_time"],
                1 if side == "home" else 0, int(g.get("neutral_site") or 0),
                pf, pa, json.dumps(d) if d else None,
            ),
        )


def update_market_line(
    conn: sqlite3.Connection,
    game_id: str,
    *,
    spread: float | None = None,
    total: float | None = None,
    home_ml: int | None = None,
    away_ml: int | None = None,
    overwrite: bool = False,
) -> None:
    """Fill in a game's sportsbook line without touching anything else.

    `overwrite=True` (the hourly line job, close to kickoff) lets a fresh
    line replace a stored one, so what is kept is near the closing line --
    the most accurate pre-game number, and the one the model compares with.
    A missing value never replaces a stored one either way.

    A whole-row upsert is the wrong tool for this: it would need every NOT
    NULL column restated, and a caller that only has odds does not have them.
    COALESCE keeps whatever is already recorded, so a line captured earlier
    (closer to the real close) is never overwritten by a later blank.
    """
    if overwrite:
        sql = """UPDATE games SET
             close_spread  = COALESCE(?, close_spread),
             close_total   = COALESCE(?, close_total),
             close_home_ml = COALESCE(?, close_home_ml),
             close_away_ml = COALESCE(?, close_away_ml)
           WHERE game_id = ?"""
    else:
        sql = """UPDATE games SET
             close_spread  = COALESCE(close_spread, ?),
             close_total   = COALESCE(close_total, ?),
             close_home_ml = COALESCE(close_home_ml, ?),
             close_away_ml = COALESCE(close_away_ml, ?)
           WHERE game_id = ?"""
    conn.execute(sql, (spread, total, home_ml, away_ml, game_id))


def count_games(conn: sqlite3.Connection, league: str | None = None) -> dict[int, int]:
    q = "SELECT season, COUNT(*) n FROM games WHERE completed=1"
    args: list[Any] = []
    if league:
        q += " AND league=?"
        args.append(league)
    q += " GROUP BY season ORDER BY season"
    return {r["season"]: r["n"] for r in conn.execute(q, args)}


# --------------------------------------------------------------------------
# as-of-safe reads
# --------------------------------------------------------------------------

class LookaheadError(RuntimeError):
    """Raised when a query would expose data at or after the as-of cutoff."""


def _check_asof(as_of: str) -> str:
    if not as_of or not isinstance(as_of, str):
        raise LookaheadError(f"as_of must be an ISO timestamp string, got {as_of!r}")
    return as_of


def games_before(
    conn: sqlite3.Connection,
    league: str,
    as_of: str,
    *,
    season: int | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Completed games that started strictly before `as_of`."""
    _check_asof(as_of)
    q = ["SELECT * FROM games WHERE league=? AND completed=1 AND start_time < ?"]
    args: list[Any] = [league, as_of]
    if season is not None:
        q.append("AND season=?")
        args.append(season)
    q.append("ORDER BY start_time DESC")
    if limit:
        q.append("LIMIT ?")
        args.append(limit)
    return list(conn.execute(" ".join(q), args))


def team_games_before(
    conn: sqlite3.Connection,
    team_id: str,
    as_of: str,
    *,
    limit: int | None = None,
    home: bool | None = None,
    season: int | None = None,
) -> list[sqlite3.Row]:
    """A team's completed games before `as_of`, most recent first.

    Backbone of the last-3 / last-7 windows and the home/away splits.
    `limit=7` therefore yields the last 7 games.

    `season` bounds the query to one season, which the season-to-date window
    needs. Without it the window silently widens to every season still in the
    database, so the same team would show 10 games after a prune and 43 before
    one -- making the model's behaviour depend on retention state rather than
    on the data.
    """
    _check_asof(as_of)
    # Joined to `games.completed`, not just "has a score". ESPN's scoreboard
    # reports 0-0 for a game that has not started, and a game from yesterday
    # stays 0-0 in the store until something refreshes that date. Filtered on
    # `points_for IS NOT NULL` alone, those phantom zeros counted as real
    # games: one in a team's last three dragged NBA predicted totals 20-30
    # points under the book across a whole slate (Nov 14, 2026).
    q = [
        """SELECT t.* FROM team_game_stats t
           JOIN games g ON g.game_id = t.game_id
           WHERE t.team_id=? AND t.start_time < ? AND t.points_for IS NOT NULL
             AND g.completed = 1"""
    ]
    args: list[Any] = [team_id, as_of]
    if season is not None:
        q.append("AND t.season=?")
        args.append(season)
    if home is not None:
        q.append("AND t.is_home=?")
        args.append(1 if home else 0)
    q.append("ORDER BY t.start_time DESC")
    if limit:
        q.append("LIMIT ?")
        args.append(limit)
    sql = " ".join(q)
    if not is_postgres(conn):
        return list(conn.execute(sql, args))
    rows = _cached(
        _HISTORY_CACHE, (db_ident(conn), sql, tuple(args)),
        lambda: list(conn.execute(sql, args)),
    )
    return list(rows)


def get_game(conn: sqlite3.Connection, game_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()


def current_season(conn: sqlite3.Connection, league: str) -> int | None:
    """The season the most recent stored game belongs to.

    Read from the data rather than from the calendar, because the leagues
    disagree about what a season is called -- the NBA labels the 2026-27
    season 2027 while the NFL labels its 2026 season 2026 -- and a wrong guess
    here silently empties every form window and rating solve.
    """
    row = conn.execute(
        "SELECT season FROM games WHERE league=? ORDER BY start_time DESC LIMIT 1",
        (league,),
    ).fetchone()
    return int(row["season"]) if row and row["season"] is not None else None


def games_on_date(
    conn: sqlite3.Connection, league: str, date_iso: str
) -> list[sqlite3.Row]:
    """All games on a Central-Time slate date, completed or not.

    `date_iso` is a Central calendar date. Rows are matched on `slate_date`
    where it is known, falling back to the UTC window that day occupies in
    Central -- computed in Python via the tz database, because SQLite cannot
    apply a DST rule and a fixed '-6 hours' would be an hour wrong for most
    of the football season.

    Getting this window wrong hides games rather than erroring: a 7pm CT
    tip-off is 01:00 UTC the *next* day, so matching the UTC date alone drops
    most of an evening's slate, which is how a 67-game college basketball
    night first appeared as 12 games.

    The comparison is against `substr(start_time,1,16)` because stored
    timestamps arrive in two shapes ('...T19:00Z' and '...T19:00:00+00:00')
    which do not sort against each other, while their first 16 characters do.

    Deliberately not as-of gated: this powers the schedule list, not a
    prediction.
    """
    lo, hi = day_bounds_utc(date_iso)
    return list(
        conn.execute(
            """SELECT * FROM games
               WHERE league=?
                 AND (
                      slate_date = ?
                      OR (slate_date IS NULL
                          AND substr(start_time,1,16) >= ?
                          AND substr(start_time,1,16) <  ?)
                 )
               ORDER BY start_time""",
            (league, date_iso, lo, hi),
        )
    )


# --------------------------------------------------------------------------
# ratings cache
# --------------------------------------------------------------------------

def put_ratings(
    conn: sqlite3.Connection,
    league: str,
    as_of: str,
    ratings: dict[str, tuple[float, int]],
) -> None:
    for team_id, (rating, n) in ratings.items():
        conn.execute(
            """INSERT INTO ratings_pit (league, as_of, team_id, rating, n_games)
               VALUES (?,?,?,?,?)
               ON CONFLICT(league, as_of, team_id) DO UPDATE SET
                 rating=excluded.rating, n_games=excluded.n_games""",
            (league, as_of, team_id, float(rating), int(n)),
        )


def get_ratings(
    conn: sqlite3.Connection, league: str, as_of: str
) -> dict[str, tuple[float, int]]:
    return {
        r["team_id"]: (r["rating"], r["n_games"])
        for r in conn.execute(
            "SELECT team_id, rating, n_games FROM ratings_pit WHERE league=? AND as_of=?",
            (league, as_of),
        )
    }


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------

def put_calibration(
    conn: sqlite3.Connection,
    league: str,
    values: dict[str, float],
    *,
    n_games: int | None = None,
    seasons: Sequence[int] | None = None,
) -> None:
    now = utcnow()
    seas = ",".join(str(s) for s in seasons) if seasons else None
    for k, v in values.items():
        conn.execute(
            """INSERT INTO calibration (league, key, value, n_games, seasons, fitted_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(league, key) DO UPDATE SET
                 value=excluded.value, n_games=excluded.n_games,
                 seasons=excluded.seasons, fitted_at=excluded.fitted_at""",
            (league, k, float(v), n_games, seas, now),
        )


def get_calibration(conn: sqlite3.Connection, league: str) -> dict[str, float]:
    def load() -> dict[str, float]:
        return {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM calibration WHERE league=?", (league,)
            )
        }

    if not is_postgres(conn):
        return load()
    return dict(_cached(_CALIBRATION_CACHE, (db_ident(conn), league), load))


def calibration_meta(conn: sqlite3.Connection, league: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT n_games, seasons, fitted_at FROM calibration WHERE league=? LIMIT 1",
        (league,),
    ).fetchone()
    return dict(row) if row else {}


# --------------------------------------------------------------------------
# predictions
# --------------------------------------------------------------------------

def save_prediction(conn: sqlite3.Connection, p: dict[str, Any]) -> int:
    """Record a prediction, replacing any earlier one for the same cutoff.

    Upserted rather than appended: opening a game used to write a new row
    every time, so one college fixture accumulated twenty identical rows and
    any accuracy figure computed from the table would have counted it twenty
    times. The key is (game_id, as_of, model_version) -- a genuinely
    different cutoff or a new model version is a different prediction and
    keeps its own row.

    `created_at` deliberately keeps the *first* time this prediction was
    made; re-opening a page is not a new forecast.
    """
    row = conn.execute(
        """SELECT pred_id FROM predictions
           WHERE game_id=? AND as_of=? AND COALESCE(model_version,'')=?""",
        (p["game_id"], p["as_of"], p.get("model_version", "v1")),
    ).fetchone()
    payload = (
        json.dumps(p.get("payload")) if p.get("payload") is not None else None
    )
    if row:
        conn.execute(
            """UPDATE predictions SET
                 pred_home_points=?, pred_away_points=?, pred_spread=?,
                 pred_total=?, payload=?,
                 matchup=COALESCE(?, matchup),
                 slate_date=COALESCE(?, slate_date),
                 home_team=COALESCE(?, home_team),
                 away_team=COALESCE(?, away_team)
               WHERE pred_id=?""",
            (
                p.get("pred_home_points"), p.get("pred_away_points"),
                p.get("pred_spread"), p.get("pred_total"), payload,
                p.get("matchup"), p.get("slate_date"),
                p.get("home_team"), p.get("away_team"), row["pred_id"],
            ),
        )
        return int(row["pred_id"])

    cur = conn.execute(
        """INSERT INTO predictions
             (game_id, league, as_of, created_at, pred_home_points,
              pred_away_points, pred_spread, pred_total, model_version,
              payload, matchup, slate_date, home_team, away_team)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           RETURNING pred_id""",
        (
            p["game_id"], p["league"], p["as_of"], utcnow(),
            p.get("pred_home_points"), p.get("pred_away_points"),
            p.get("pred_spread"), p.get("pred_total"),
            p.get("model_version", "v1"),
            payload, p.get("matchup"), p.get("slate_date"),
            p.get("home_team"), p.get("away_team"),
        ),
    )
    # RETURNING, not lastrowid: Postgres has no implicit row id.
    return int(cur.fetchone()[0])


def record_bets(
    conn: sqlite3.Connection,
    game_id: str,
    as_of: str,
    *,
    bets: Any,
    the_call: str | None = None,
    money_usd: float | None = None,
    model_version: str = "v1",
) -> None:
    """Attach the recommended bets to a stored prediction.

    Held because they cannot be recovered later: the five bets depend on the
    Kalshi asks and the money standing at the cutoff, and while Kalshi still
    serves a settled market's candles today, that is their retention policy
    rather than a guarantee. A price nobody kept is a bet nobody can grade.
    """
    conn.execute(
        """UPDATE predictions
             SET bets=?, the_call=COALESCE(?, the_call),
                 kalshi_money_usd=COALESCE(?, kalshi_money_usd)
           WHERE game_id=? AND as_of=? AND COALESCE(model_version,'')=?""",
        (
            json.dumps(bets), the_call, money_usd,
            game_id, as_of, model_version,
        ),
    )


def settle_predictions(
    conn: sqlite3.Connection, league: str | None = None
) -> int:
    """Copy final scores onto predictions whose games have finished.

    Run opportunistically whenever a slate is loaded, so the record fills in
    as a side effect of normal use rather than needing a cron job nobody
    remembers to set up.
    """
    q = """SELECT p.pred_id, g.home_score, g.away_score
             FROM predictions p JOIN games g ON g.game_id = p.game_id
            WHERE p.settled_at IS NULL
              AND g.completed = 1 AND g.home_score IS NOT NULL"""
    args: list[Any] = []
    if league:
        q += " AND p.league = ?"
        args.append(league)
    rows = conn.execute(q, args).fetchall()
    now = utcnow()
    conn.executemany(
        """UPDATE predictions
             SET actual_home=?, actual_away=?, settled_at=?
           WHERE pred_id=?""",
        [(r["home_score"], r["away_score"], now, r["pred_id"]) for r in rows],
    )
    return len(rows)


def settled_predictions(
    conn: sqlite3.Connection,
    league: str | None = None,
    *,
    since: str | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Graded predictions, newest first. Reads only the predictions table, so
    it keeps working after the games behind it have been pruned."""
    q = ["SELECT * FROM predictions WHERE settled_at IS NOT NULL"]
    args: list[Any] = []
    if league:
        q.append("AND league=?")
        args.append(league)
    if since:
        q.append("AND COALESCE(slate_date, substr(as_of,1,10)) >= ?")
        args.append(since)
    q.append("ORDER BY COALESCE(slate_date, as_of) DESC LIMIT ?")
    args.append(limit)
    return list(conn.execute(" ".join(q), args))


def predictions_for(conn: sqlite3.Connection, game_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM predictions WHERE game_id=? ORDER BY created_at DESC",
            (game_id,),
        )
    )


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------

def prune_seasons(
    conn: sqlite3.Connection, league: str, keep_seasons: Iterable[int]
) -> int:
    """Delete games outside `keep_seasons`. Calibration and predictions survive.

    Called after `model.fit` has distilled the bootstrap seasons. Safe because
    every history source is public and permanent, so import_history.py can
    rebuild on demand.
    """
    keep = [int(s) for s in keep_seasons]
    marks = ",".join("?" * len(keep)) if keep else "NULL"
    cur = conn.execute(
        f"DELETE FROM games WHERE league=? AND season NOT IN ({marks})",
        [league, *keep],
    )
    # team_game_stats cascades; cached ratings are now stale.
    conn.execute("DELETE FROM ratings_pit WHERE league=?", (league,))
    return cur.rowcount


def trim_to_rolling(conn: sqlite3.Connection, league: str, keep: int = 7) -> int:
    """Keep only each team's most recent `keep` completed games.

    Available for a minimal footprint, but normal steady state retains the
    whole current season, which opponent ratings and lookback both need.
    """
    rows = conn.execute(
        """SELECT game_id FROM games WHERE league=? AND completed=1
             AND game_id NOT IN (
               SELECT game_id FROM (
                 SELECT game_id,
                        ROW_NUMBER() OVER (
                          PARTITION BY team_id ORDER BY start_time DESC
                        ) rn
                 FROM team_game_stats WHERE league=?
               ) WHERE rn <= ?
             )""",
        (league, league, keep),
    ).fetchall()
    ids = [r["game_id"] for r in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        conn.execute(
            f"DELETE FROM games WHERE game_id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
    return len(ids)


def vacuum(path: Path | str = DB_PATH) -> bool:
    """Reclaim space after a prune. Best-effort.

    VACUUM needs an exclusive lock, so a reader elsewhere (a Streamlit session,
    another import) will block it. That is pure housekeeping, so a failure is
    reported and swallowed rather than discarding a completed import.

    On Postgres this is a no-op returning False: autovacuum reclaims space on
    its own, and a manual VACUUM cannot run inside the app's transactions.
    """
    if uses_postgres(path):
        return False
    conn = sqlite3.connect(Path(path), timeout=5)
    try:
        conn.execute("VACUUM")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def db_size_bytes(path: Path | str = DB_PATH) -> int:
    if uses_postgres(path):
        # The tables in the schema in use, not the whole cluster: on the
        # free plan the 500 MB limit is what this number is checked against.
        with connect(path, readonly=True) as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0)
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = current_schema() AND c.relkind = 'r'"""
            ).fetchone()
        return int(row[0])
    p = Path(path)
    return p.stat().st_size if p.exists() else 0


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

def put_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


# --------------------------------------------------------------------------
# players
# --------------------------------------------------------------------------

def upsert_player(
    conn: sqlite3.Connection,
    league: str,
    athlete_id: str,
    team_id: str,
    *,
    name: str = "",
    position: str = "",
    source: str = "espn",
) -> None:
    conn.execute(
        """INSERT INTO players (athlete_id, league, team_id, name, position, source, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(athlete_id) DO UPDATE SET
             team_id    = excluded.team_id,
             name       = COALESCE(NULLIF(excluded.name, ''), players.name),
             position   = COALESCE(NULLIF(excluded.position, ''), players.position),
             source     = excluded.source,
             updated_at = excluded.updated_at""",
        (athlete_id, league, team_id, name, position, source, utcnow()),
    )


def upsert_player_games(
    conn: sqlite3.Connection,
    league: str,
    athlete_id: str,
    team_id: str,
    games: Iterable[Any],
    *,
    source: str = "espn",
) -> int:
    """Store a player's game log. Each item is a `clients.espn.PlayerGame`
    or a dict with the same fields. Returns rows written."""
    now = utcnow()
    n = 0
    for g in games:
        get = g.get if isinstance(g, dict) else lambda k, _g=g: getattr(_g, k, None)
        event_id = str(get("event_id") or "")
        date = str(get("date") or "")[:10]
        if not event_id or not date:
            continue
        conn.execute(
            """INSERT INTO player_games
                 (athlete_id, event_id, league, team_id, date, opponent,
                  home_away, result, stats, source, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(athlete_id, event_id) DO UPDATE SET
                 team_id    = excluded.team_id,
                 stats      = COALESCE(excluded.stats, player_games.stats),
                 result     = COALESCE(excluded.result, player_games.result),
                 fetched_at = excluded.fetched_at""",
            (
                athlete_id, event_id, league, team_id, date,
                get("opponent"), get("home_away"), get("result"),
                json.dumps(get("stats") or {}), source, now,
            ),
        )
        n += 1
    return n


def player_games_for_team(
    conn: sqlite3.Connection, league: str, team_id: str, *, before: str | None = None
) -> list[sqlite3.Row]:
    """Stored player-game rows for a team, newest first.

    `before` is the as-of guard: pass the game's kickoff so a lookback cannot
    read a line that was logged after the game it is predicting.
    """
    if before:
        return conn.execute(
            """SELECT * FROM player_games WHERE league=? AND team_id=? AND date < ?
               ORDER BY date DESC""",
            (league, team_id, before[:10]),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM player_games WHERE league=? AND team_id=? ORDER BY date DESC",
        (league, team_id),
    ).fetchall()


def replace_depth_chart(
    conn: sqlite3.Connection,
    league: str,
    team_id: str,
    chart: dict[str, list[str]],
    *,
    as_of: str | None = None,
) -> int:
    """Write one depth-chart snapshot: {position: [athlete_id, ...] by rank}."""
    stamp = as_of or utcnow()
    n = 0
    for position, ids in chart.items():
        for rank, aid in enumerate(ids, start=1):
            conn.execute(
                """INSERT INTO depth_chart
                     (league, team_id, position, athlete_id, rank, as_of)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (league, team_id, position, rank, as_of)
                   DO UPDATE SET athlete_id = excluded.athlete_id""",
                (league, team_id, position, str(aid), rank, stamp),
            )
            n += 1
    return n


# --------------------------------------------------------------------------
# injury snapshots
# --------------------------------------------------------------------------

def snapshot_injuries(
    conn: sqlite3.Connection,
    league: str,
    rows: Iterable[tuple[str, Any]],
    *,
    as_of: str | None = None,
) -> int:
    """Append one snapshot. `rows` yields (team_id, Injury-like) pairs.

    Appends rather than replaces: the point of the table is the history.
    """
    stamp = as_of or utcnow()
    n = 0
    for team_id, inj in rows:
        get = inj.get if isinstance(inj, dict) else lambda k, _i=inj: getattr(_i, k, None)
        aid = str(get("athlete_id") or "")
        status = str(get("status") or "").strip()
        if not aid or not status:
            continue
        conn.execute(
            """INSERT INTO injury_snapshots
                 (league, team_id, athlete_id, name, position, status, detail, as_of)
               VALUES (?,?,?,?,?,?,?,?)""",
            (league, team_id, aid, get("name"), get("position"), status,
             get("detail"), stamp),
        )
        n += 1
    return n


def latest_injury_status(
    conn: sqlite3.Connection, league: str, *, as_of: str | None = None
) -> dict[str, str]:
    """{athlete_id: status} from the most recent snapshot at or before `as_of`.

    This is what the change gate diffs between runs, and what a future
    backtest can read to know who was listed Out before a past kickoff.
    """
    if as_of:
        row = conn.execute(
            "SELECT MAX(as_of) m FROM injury_snapshots WHERE league=? AND as_of<=?",
            (league, as_of),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(as_of) m FROM injury_snapshots WHERE league=?", (league,)
        ).fetchone()
    stamp = row["m"] if row else None
    if not stamp:
        return {}
    return {
        r["athlete_id"]: r["status"]
        for r in conn.execute(
            "SELECT athlete_id, status FROM injury_snapshots WHERE league=? AND as_of=?",
            (league, stamp),
        )
    }


def latest_injury_snapshot_time(conn: sqlite3.Connection, league: str) -> str | None:
    """`as_of` of the most recent snapshot, or None. Drives the daily heartbeat."""
    row = conn.execute(
        "SELECT MAX(as_of) m FROM injury_snapshots WHERE league=?", (league,)
    ).fetchone()
    return row["m"] if row and row["m"] else None


# --------------------------------------------------------------------------
# agent runs
# --------------------------------------------------------------------------

def start_agent_run(
    conn: sqlite3.Connection, agent: str, league: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO agent_runs (agent, league, started_at, status) VALUES (?,?,?,?) "
        "RETURNING run_id",
        (agent, league, utcnow(), "running"),
    )
    return int(cur.fetchone()[0])


def finish_agent_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    signature: str | None = None,
    summary: Any = None,
    writes: Any = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE agent_runs SET finished_at=?, status=?, signature=?,
                                 summary=?, writes=?, error=?
           WHERE run_id=?""",
        (
            utcnow(), status, signature,
            json.dumps(summary) if summary is not None else None,
            json.dumps(writes) if writes is not None else None,
            error, run_id,
        ),
    )


def abandon_stale_runs(conn: Any, *, older_than_seconds: int = 2 * 3600) -> int:
    """Close `running` rows left by a process that died or was killed.

    Without this a hung or force-exited cycle stays "running" forever in the
    log the user reads to see whether the agents are healthy.
    """
    from datetime import timedelta as _td

    cutoff = (datetime.now(timezone.utc) - _td(seconds=older_than_seconds)).isoformat(
        timespec="seconds"
    )
    cur = conn.execute(
        "UPDATE agent_runs SET status='abandoned', finished_at=?, "
        "error=COALESCE(error, 'process ended without finishing (hung, killed or lost network)') "
        "WHERE status='running' AND started_at < ?",
        (utcnow(), cutoff),
    )
    return cur.rowcount or 0


def last_agent_run(
    conn: sqlite3.Connection, agent: str, *, statuses: Sequence[str] | None = None
) -> sqlite3.Row | None:
    if statuses:
        marks = ",".join("?" * len(statuses))
        return conn.execute(
            f"SELECT * FROM agent_runs WHERE agent=? AND status IN ({marks}) "
            "ORDER BY started_at DESC LIMIT 1",
            (agent, *statuses),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM agent_runs WHERE agent=? ORDER BY started_at DESC LIMIT 1",
        (agent,),
    ).fetchone()


# --------------------------------------------------------------------------
# retention: the rolling-7 for DETAIL ONLY
# --------------------------------------------------------------------------

def trim_player_detail(
    conn: sqlite3.Connection, league: str, keep: int = 7
) -> dict[str, int]:
    """Apply the rolling last-`keep` rule to per-player and box-score detail.

    This is deliberately NOT `trim_to_rolling`, which deletes `games` rows.
    The 7-game cap applies to the heavy detail -- player game logs, depth-chart
    snapshots, and the box-score blob in `team_game_stats.detail` -- while the
    team-level results stay for the whole season. The fitted blend weights put
    0.80 on the season window and the ratings solve reads every result this
    season; trimming those would collapse "season" into "last 7" and quietly
    replace the dominant term of a calibrated model. So `games` and
    `team_game_stats` rows are never touched here: only the `detail` column is
    nulled, and only beyond each team's most recent `keep` completed games.

    Returns counts of what was removed, for the agent-run log.
    """
    out = {"player_games": 0, "depth_chart": 0, "detail_nulled": 0}

    # Rows are addressed by their primary keys, not SQLite's hidden `rowid`,
    # so the same statements run on Postgres.

    # player_games: each team's `keep` most recent distinct game dates survive.
    cur = conn.execute(
        """DELETE FROM player_games WHERE (athlete_id, event_id) IN (
             SELECT athlete_id, event_id FROM (
               SELECT athlete_id, event_id,
                      DENSE_RANK() OVER (
                        PARTITION BY team_id ORDER BY date DESC
                      ) rk
               FROM player_games WHERE league=?
             ) ranked WHERE rk > ?
           )""",
        (league, keep),
    )
    out["player_games"] = cur.rowcount

    # depth_chart: each team's `keep` most recent snapshots survive.
    cur = conn.execute(
        """DELETE FROM depth_chart WHERE league=? AND (team_id, as_of) IN (
             SELECT team_id, as_of FROM (
               SELECT team_id, as_of,
                      DENSE_RANK() OVER (
                        PARTITION BY team_id ORDER BY as_of DESC
                      ) rk
               FROM depth_chart WHERE league=?
             ) ranked WHERE rk > ?
           )""",
        (league, league, keep),
    )
    out["depth_chart"] = cur.rowcount

    # team_game_stats.detail: null beyond the window. The row itself stays --
    # points_for / points_against are the season-level record.
    cur = conn.execute(
        """UPDATE team_game_stats SET detail=NULL
           WHERE detail IS NOT NULL AND (game_id, team_id) IN (
             SELECT game_id, team_id FROM (
               SELECT t.game_id, t.team_id,
                      ROW_NUMBER() OVER (
                        PARTITION BY t.team_id ORDER BY t.start_time DESC
                      ) rn
               FROM team_game_stats t
               JOIN games g ON g.game_id = t.game_id
               WHERE t.league=? AND g.completed=1
             ) ranked WHERE rn > ?
           )""",
        (league, keep),
    )
    out["detail_nulled"] = cur.rowcount
    return out


# --------------------------------------------------------------------------
# model versions (the training log)
# --------------------------------------------------------------------------

def save_model_run(
    conn: Any, league: str, version: str, *, promoted: bool,
    model: Any = None, metrics: Any = None, changes: Any = None, note: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO model_runs (league, version, trained_at, promoted, model, metrics, changes, note)
           VALUES (?,?,?,?,?,?,?,?) RETURNING run_id""",
        (
            league, version, utcnow(), 1 if promoted else 0,
            json.dumps(model) if model is not None else None,
            json.dumps(metrics) if metrics is not None else None,
            json.dumps(changes) if changes is not None else None,
            note,
        ),
    )
    _ACTIVE_MODEL_CACHE.clear()
    return int(cur.fetchone()[0])


def _decode_run(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("model", "metrics", "changes"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    d["promoted"] = bool(d["promoted"])
    return d


def active_model_run(conn: Any, league: str) -> dict[str, Any] | None:
    """The league's live model: its latest promoted training run, or None
    (the engine alone, version v1). Cached; cleared on any model_runs write."""
    def load() -> dict[str, Any] | None:
        r = conn.execute(
            "SELECT * FROM model_runs WHERE league=? AND promoted=1 "
            "ORDER BY trained_at DESC, run_id DESC LIMIT 1",
            (league,),
        ).fetchone()
        return _decode_run(r) if r else None

    if not is_postgres(conn):
        return load()
    return _cached(_ACTIVE_MODEL_CACHE, (db_ident(conn), league), load)


def model_runs(conn: Any, league: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    """The training log, newest first (promoted and rejected)."""
    q = "SELECT * FROM model_runs"
    args: list[Any] = []
    if league:
        q += " WHERE league=?"
        args.append(league)
    q += " ORDER BY trained_at DESC, run_id DESC LIMIT ?"
    args.append(limit)
    return [_decode_run(r) for r in conn.execute(q, args).fetchall()]


def set_possessions(conn: Any, pairs: Sequence[tuple[str, float]]) -> int:
    """Record possession counts: (game_id, possessions) pairs."""
    if not pairs:
        return 0
    conn.executemany(
        "UPDATE games SET possessions=? WHERE game_id=?",
        [(float(p), gid) for gid, p in pairs],
    )
    return len(pairs)


# --------------------------------------------------------------------------
# line movement
# --------------------------------------------------------------------------

def record_lines(conn: Any, rows: Sequence[tuple[str, str, str, str, float | None, float | None]],
                 *, seen_at: str | None = None) -> int:
    """Record (game_id, sportsbook, market, side, line, odds) observations.

    The first observation of a row is kept as its opening; later ones only
    move `last_*`. Rows with neither a line nor odds are skipped.
    """
    at = seen_at or utcnow()
    vals = [(g, b, m, sd, ln, od, at, ln, od, at) for g, b, m, sd, ln, od in rows
            if ln is not None or od is not None]
    if not vals:
        return 0
    conn.executemany(
        """INSERT INTO line_history (game_id, sportsbook, market, side,
                                     open_line, open_odds, open_at, last_line, last_odds, last_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (game_id, sportsbook, market, side) DO UPDATE SET
             last_line = excluded.last_line,
             last_odds = excluded.last_odds,
             last_at   = excluded.last_at""",
        vals,
    )
    return len(vals)


def line_history(conn: Any, game_ids: Sequence[str]) -> list[Any]:
    if not game_ids:
        return []
    marks = ",".join("?" * len(game_ids))
    return conn.execute(
        f"SELECT * FROM line_history WHERE game_id IN ({marks})", list(game_ids)
    ).fetchall()
