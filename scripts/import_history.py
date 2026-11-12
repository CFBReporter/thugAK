#!/usr/bin/env python
"""Bootstrap import: load three seasons, fit the model, then prune.

    python scripts/import_history.py --league all --seasons 3
    python scripts/import_history.py --league nfl --no-prune   # keep raw data

The three historical seasons exist only to be distilled into the `calibration`
table. Once fitted they are deleted, leaving the current season (which
opponent ratings and the lookback view both need) plus rolling form.

This is safe because every source is public and permanent, so re-running this
script rebuilds the history on demand:

    NFL      nflverse games.csv.gz        -- also the only free source of
                                             historical closing lines
    NBA      ESPN team schedules          -- 30 requests per season
    NCAAF    ESPN team schedules          -- 138 requests per season
    NCAAMB   Barttorvik super_sked.csv    -- every D1 game in one file,
                                             avoiding 365 requests per season
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import cache, matching, store  # noqa: E402
from thugsak.clients import espn as espn_mod  # noqa: E402
from thugsak.clients.stats import barttorvik, nflverse  # noqa: E402
from thugsak.config import DB_PATH, LEAGUES, league as get_league  # noqa: E402
from thugsak.model import fit as fit_mod  # noqa: E402


def seasons_for(league_key: str, n: int) -> list[int]:
    """The n most recent seasons, current one last.

    NBA and NCAAMB label a season by its *end* year, so the arithmetic is the
    same but the labels differ from the calendar year.
    """
    current = get_league(league_key).fpi_season
    if league_key == "ncaamb":
        from thugsak.config import BARTTORVIK_SEASON

        current = BARTTORVIK_SEASON
    return list(range(current - n + 1, current + 1))


async def seed_teams(conn, client, league_key: str) -> dict[str, str]:
    """Create canonical team records from ESPN and return abbrev -> team_id.

    Every team ESPN lists is registered, including non-Division-I schools, so
    that a D1 team's game against one still resolves both sides. Only the D1
    teams are flagged, and only they are iterated when pulling schedules --
    so the slate is D1, while a D1-versus-D2 game is kept in full.
    """
    async with espn_mod.EspnClient(client) as ec:
        teams = await ec.teams(league_key)
        d1 = await ec.d1_team_ids(league_key)

    out: dict[str, str] = {}
    n_d1 = 0
    for t in teams:
        # No group list means the league needs no filter, so everyone is "D1".
        is_d1 = (t.espn_id in d1) if d1 else None
        tid = matching.register_espn_team(conn, league_key, t, is_d1=is_d1)
        if t.abbrev:
            out[t.abbrev.upper()] = tid
        matching.link_alias(conn, league_key, t.espn_id, "espn_id", tid)
        if is_d1:
            n_d1 += 1
    if d1:
        print(f"  teams: {len(teams)} registered, {n_d1} Division I "
              f"(lower divisions kept only as opponents)")
    else:
        print(f"  teams: {len(out)} registered")
    return out


def _espn_game_to_row(g: Any, league_key: str, conn) -> dict[str, Any] | None:
    # Registers an unknown opponent as non-D1 rather than dropping the game:
    # a D1 team's fixture against a lower division is wanted, and the college
    # team lists only cover D1.
    home = matching.resolve_or_register(conn, league_key, g.home)
    away = matching.resolve_or_register(conn, league_key, g.away)
    if not home or not away or home == away:
        return None
    line = g.best_line
    return {
        "game_id": f"{league_key}:espn:{g.espn_id}",
        "league": league_key,
        "season": g.season or 0,
        "season_type": "regular" if (g.season_type or 2) == 2 else "other",
        "start_time": g.start_time,
        "home_team": home,
        "away_team": away,
        "home_score": g.home_score,
        "away_score": g.away_score,
        "completed": 1 if g.completed else 0,
        "neutral_site": 1 if g.neutral_site else 0,
        "close_spread": line.spread if line else None,
        "close_total": line.total if line else None,
        "close_home_ml": line.home_ml if line else None,
        "close_away_ml": line.away_ml if line else None,
        "source": "espn",
    }


async def import_espn_schedules(
    conn, client, league_key: str, seasons: Iterable[int]
) -> int:
    """Per-team season schedules. Used for NBA (30 teams) and NCAAF (138).

    Commits after every team rather than every season. NCAAF is 414 requests,
    and holding the write lock across that many slow HTTP calls blocks anything
    else touching the database -- including a Streamlit session or a smoke run,
    which is how this first failed.
    """
    async with espn_mod.EspnClient(client) as ec:
        all_teams = await ec.teams(league_key)
        d1 = await ec.d1_team_ids(league_key)
        # Iterate D1 only. Their opponents' games arrive through these
        # schedules anyway, so a D1-versus-FCS fixture is still captured
        # without walking 500 lower-division schedules to find it.
        teams = [t for t in all_teams if t.espn_id in d1] if d1 else all_teams
        if d1:
            print(f"    iterating {len(teams)} D1 schedules "
                  f"(of {len(all_teams)} teams)")
        n = 0
        seen: set[str] = set()
        for season in seasons:
            got = 0
            for t in teams:
                if not t.espn_id:
                    continue
                try:
                    games = await ec.team_schedule(
                        league_key, t.espn_id, season=season
                    )
                except Exception as e:  # one team failing must not kill the import
                    print(f"    ! {t.abbrev} {season}: {type(e).__name__}")
                    continue
                for g in games:
                    if g.espn_id in seen:
                        continue
                    row = _espn_game_to_row(g, league_key, conn)
                    if row and row["season"]:
                        store.upsert_game(conn, row)
                        seen.add(g.espn_id)
                        got += 1
                        n += 1
                conn.commit()
            print(f"    season {season}: {got} games")
    return n


async def import_nfl(conn, client, seasons: list[int]) -> int:
    """nflverse: one file, all seasons, with closing lines."""
    src = nflverse.Nflverse()
    matching.seed_source_codes(conn, src.name, "nfl")
    rows = await src.games(client, "nfl", seasons=set(seasons))
    n = 0
    skipped: dict[str, int] = {}
    for r in rows:
        home = matching.resolve_team(conn, "nfl", r["home_abbrev"], source=src.name)
        away = matching.resolve_team(conn, "nfl", r["away_abbrev"], source=src.name)
        if not home or not away:
            # Named, not swallowed. These used to vanish in silence, and
            # three seasons of Washington and Rams games went with them --
            # so the model had no history for either club and refused to
            # predict their games, with nothing on screen to say why.
            for abbrev, tid in ((r["home_abbrev"], home), (r["away_abbrev"], away)):
                if not tid:
                    skipped[abbrev] = skipped.get(abbrev, 0) + 1
            continue
        store.upsert_game(
            conn,
            {
                "game_id": r["game_id"],
                "league": "nfl",
                "season": r["season"],
                "season_type": r["season_type"],
                "start_time": r["start_time"],
                "home_team": home,
                "away_team": away,
                "home_score": r["home_score"],
                "away_score": r["away_score"],
                "completed": 1 if r["completed"] else 0,
                "neutral_site": r["neutral_site"],
                "close_spread": r["close_spread"],
                "close_total": r["close_total"],
                "close_home_ml": r["close_home_ml"],
                "close_away_ml": r["close_away_ml"],
                "source": "nflverse",
            },
        )
        n += 1
    conn.commit()
    with_lines = conn.execute(
        "SELECT COUNT(*) c FROM games WHERE league='nfl' AND close_spread IS NOT NULL"
    ).fetchone()["c"]
    print(f"    {n} games, {with_lines} with closing lines")
    if skipped:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items()))
        print(
            f"    WARNING: skipped games for unresolved team codes ({detail}). "
            f"Add them to matching.SOURCE_TEAM_CODES -- a club with no "
            f"history cannot be predicted at all."
        )
    return n


async def import_ncaamb(conn, client, seasons: list[int]) -> int:
    """Barttorvik super_sked: every D1 game per season in one file.

    Team names here differ from ESPN's ("Saint Mary's" vs "Saint Marys"), so
    unresolved names are counted and reported rather than silently dropped --
    a low match rate means the alias table needs work, and that is worth
    knowing before the ratings are trusted.
    """
    src = barttorvik.Barttorvik()
    total = 0
    for season in seasons:
        try:
            rows = await src.games(client, "ncaamb", year=season)
        except Exception as e:
            print(f"    ! season {season}: {type(e).__name__}: {e}")
            continue
        got, unresolved = 0, set()
        for r in rows:
            home = matching.resolve_team(conn, "ncaamb", r["home_name"])
            away = matching.resolve_team(conn, "ncaamb", r["away_name"])
            if not home:
                unresolved.add(r["home_name"])
            if not away:
                unresolved.add(r["away_name"])
            if not home or not away or home == away:
                continue
            key = f"ncaamb:bt:{season}:{r['start_time'][:10]}:{home}:{away}"
            store.upsert_game(
                conn,
                {
                    "game_id": key,
                    "league": "ncaamb",
                    "season": season,
                    "season_type": "regular",
                    "start_time": r["start_time"],
                    "home_team": home,
                    "away_team": away,
                    "home_score": r["home_score"],
                    "away_score": r["away_score"],
                    "completed": 1,
                    "neutral_site": 0,
                    "source": "barttorvik",
                },
            )
            got += 1
            total += 1
            # Learn the name so the next season resolves faster.
            matching.link_alias(conn, "ncaamb", r["home_name"], "barttorvik", home)
            matching.link_alias(conn, "ncaamb", r["away_name"], "barttorvik", away)
            # Commit in batches: NCAAMB is ~6,000 games per season, and one
            # transaction that large holds the write lock long enough to block
            # anything else touching the database.
            if got % 500 == 0:
                conn.commit()
        conn.commit()
        rate = got / len(rows) if rows else 0
        print(f"    season {season}: {got}/{len(rows)} games matched ({rate:.0%})")
        if unresolved:
            sample = sorted(unresolved)[:5]
            print(f"      {len(unresolved)} unresolved names, e.g. {sample}")
    return total


async def import_current_slate(conn, client, league_key: str) -> int:
    """Pull the current scoreboard so today's games exist before any fit."""
    async with espn_mod.EspnClient(client) as ec:
        try:
            games = await ec.scoreboard(league_key)
        except Exception as e:
            print(f"  ! current slate: {type(e).__name__}")
            return 0
    n = 0
    for g in games:
        row = _espn_game_to_row(g, league_key, conn)
        if row:
            if not row["season"]:
                row["season"] = get_league(league_key).fpi_season
            store.upsert_game(conn, row)
            n += 1
    conn.commit()
    return n


async def run_league(
    conn, client, league_key: str, n_seasons: int, *, prune: bool
) -> None:
    seasons = seasons_for(league_key, n_seasons)
    print(f"\n=== {get_league(league_key).name} (seasons {seasons}) ===")

    await seed_teams(conn, client, league_key)

    if league_key == "nfl":
        await import_nfl(conn, client, seasons)
    elif league_key == "ncaamb":
        await import_ncaamb(conn, client, seasons)
    else:
        await import_espn_schedules(conn, client, league_key, seasons)

    live = await import_current_slate(conn, client, league_key)
    print(f"  current slate: {live} games")

    counts = store.count_games(conn, league_key)
    print(f"  completed games by season: {counts}")
    if not counts:
        print("  nothing to fit -- skipping calibration")
        return

    print("  fitting calibration...")
    fitted = fit_mod.fit_league(conn, league_key, seasons=seasons)
    conn.commit()

    if prune and fitted:
        keep = [seasons[-1]]
        removed = store.prune_seasons(conn, league_key, keep)
        conn.commit()
        print(f"  pruned {removed} games from historical seasons; keeping {keep}")
        print(f"  remaining: {store.count_games(conn, league_key)}")
    elif not prune:
        print("  --no-prune: historical seasons retained")


async def main_async(args: argparse.Namespace) -> int:
    store.init_db(args.db)
    keys = list(LEAGUES) if args.league == "all" else [args.league]

    async with cache.make_client() as client:
        with store.connect(args.db) as conn:
            for key in keys:
                try:
                    await run_league(
                        conn, client, key, args.seasons, prune=not args.no_prune
                    )
                except Exception as e:
                    print(f"  ! {key} failed: {type(e).__name__}: {e}")
                    if args.strict:
                        raise

            print("\n=== calibration summary ===")
            for key in keys:
                cal = store.get_calibration(conn, key)
                if not cal:
                    print(f"  {key}: not fitted")
                    continue
                print(
                    f"  {key}: hfa {cal.get('hfa', 0):+.2f} | "
                    f"sigma_margin {cal.get('sigma_margin', 0):.2f} | "
                    f"sigma_total {cal.get('sigma_total', 0):.2f} | "
                    f"weights {cal.get('w_last3', 0):.2f}/"
                    f"{cal.get('w_last7', 0):.2f}/{cal.get('w_season', 0):.2f} | "
                    f"n={int(cal.get('n_basis', 0))}"
                )

    if not store.vacuum(args.db):
        print("\n(skipped VACUUM -- database busy; harmless, space is reclaimed "
              "on the next run)")

    # The bulk history files are now in the game database, so their cached
    # copies are dead weight -- they alone run to hundreds of megabytes.
    if not args.keep_cache:
        trimmed = cache.trim()
        print(
            f"cache: dropped {trimmed['bulk_dropped']} bulk entries, "
            f"evicted {trimmed['evicted']}, now "
            f"{trimmed['size_bytes'] / 1e6:.1f} MB"
        )

    size = store.db_size_bytes(args.db) / 1e6
    print(f"database: {size:.1f} MB at {args.db}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--league", default="all", choices=["all", *LEAGUES])
    p.add_argument("--seasons", type=int, default=3)
    p.add_argument(
        "--no-prune",
        action="store_true",
        help="keep the historical seasons after fitting (uses more disk, "
             "lets you re-fit without re-downloading)",
    )
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument(
        "--keep-cache",
        action="store_true",
        help="do not trim the HTTP cache afterwards (keeps the bulk "
             "history files for a faster re-run, at a few hundred MB)",
    )
    p.add_argument("--strict", action="store_true", help="abort on first failure")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
