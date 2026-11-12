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
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | str = DB_PATH, *, readonly: bool = False):
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
        return applied


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


def add_alias(
    conn: sqlite3.Connection, league: str, alias: str, source: str, team_id: str
) -> None:
    if not alias:
        return
    conn.execute(
        """INSERT INTO team_aliases (league, alias, source, team_id)
           VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
        (league, alias.strip().lower(), source, team_id),
    )


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
    if source:
        for src in (SCOPED_PREFIX + source, source):
            row = conn.execute(
                "SELECT team_id FROM team_aliases "
                "WHERE league=? AND alias=? AND source=?",
                (league, a, src),
            ).fetchone()
            if row:
                return row["team_id"]
    row = conn.execute(
        "SELECT team_id FROM team_aliases WHERE league=? AND alias=? "
        "AND source NOT LIKE ? LIMIT 1",
        (league, a, SCOPED_PREFIX + "%"),
    ).fetchone()
    return row["team_id"] if row else None


def all_teams(conn: sqlite3.Connection, league: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM teams WHERE league=? ORDER BY canonical", (league,)
        )
    )


def team_label(conn: sqlite3.Connection, team_id: str, *, short: bool = False) -> str:
    """Human-readable name for a canonical team id.

    Internal ids look like ``nfl:BAL``, which is right for keys and wrong for
    anything a person reads. Falls back to the bare suffix when the team is
    not in the table, so a label never comes back blank.
    """
    if not team_id:
        return ""
    row = conn.execute(
        "SELECT canonical, display_name FROM teams WHERE team_id=?", (team_id,)
    ).fetchone()
    if row is None:
        return team_id.split(":")[-1]
    if short:
        return row["canonical"] or team_id.split(":")[-1]
    return row["display_name"] or row["canonical"] or team_id.split(":")[-1]


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
    row = conn.execute(
        """SELECT game_id FROM games
           WHERE league=? AND home_team=? AND away_team=?
             AND (
                   substr(start_time,1,10) = ?
                   OR (
                        source IS NOT ?
                        AND abs(julianday(substr(start_time,1,10))
                                - julianday(?)) <= ?
                      )
                 )
           ORDER BY abs(julianday(substr(start_time,1,10)) - julianday(?))
           LIMIT 1""",
        (
            g["league"], g["home_team"], g["away_team"], date,
            source, date, tolerance_days, date,
        ),
    ).fetchone()
    return row["game_id"] if row else None


def upsert_game(conn: sqlite3.Connection, g: dict[str, Any]) -> None:
    """Insert or update one game, then refresh its two team_game_stats rows.

    Reuses an existing row for the same fixture when one is present, so a
    second source enriches the game rather than duplicating it.
    """
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
) -> None:
    """Fill in a game's sportsbook line without touching anything else.

    A whole-row upsert is the wrong tool for this: it would need every NOT
    NULL column restated, and a caller that only has odds does not have them.
    COALESCE keeps whatever is already recorded, so a line captured earlier
    (closer to the real close) is never overwritten by a later blank.
    """
    conn.execute(
        """UPDATE games SET
             close_spread  = COALESCE(close_spread, ?),
             close_total   = COALESCE(close_total, ?),
             close_home_ml = COALESCE(close_home_ml, ?),
             close_away_ml = COALESCE(close_away_ml, ?)
           WHERE game_id = ?""",
        (spread, total, home_ml, away_ml, game_id),
    )


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
    q = [
        """SELECT * FROM team_game_stats
           WHERE team_id=? AND start_time < ? AND points_for IS NOT NULL"""
    ]
    args: list[Any] = [team_id, as_of]
    if season is not None:
        q.append("AND season=?")
        args.append(season)
    if home is not None:
        q.append("AND is_home=?")
        args.append(1 if home else 0)
    q.append("ORDER BY start_time DESC")
    if limit:
        q.append("LIMIT ?")
        args.append(limit)
    return list(conn.execute(" ".join(q), args))


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
    return {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM calibration WHERE league=?", (league,)
        )
    }


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
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            p["game_id"], p["league"], p["as_of"], utcnow(),
            p.get("pred_home_points"), p.get("pred_away_points"),
            p.get("pred_spread"), p.get("pred_total"),
            p.get("model_version", "v1"),
            payload, p.get("matchup"), p.get("slate_date"),
            p.get("home_team"), p.get("away_team"),
        ),
    )
    return int(cur.lastrowid)


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
    """
    conn = sqlite3.connect(Path(path), timeout=5)
    try:
        conn.execute("VACUUM")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def db_size_bytes(path: Path | str = DB_PATH) -> int:
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
