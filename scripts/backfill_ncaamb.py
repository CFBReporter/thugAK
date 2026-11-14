"""Backfill NCAAMB games that the history import dropped.

Why this exists
---------------
``import_ncaamb`` resolves each Barttorvik team name against the alias table
and skips the row when either side is unknown. That is the right behaviour --
a game attributed to the wrong team is worse than a missing one -- but the
alias table was thinner when the import ran than it is now, so names the
resolver handles today were unresolved then. Those rows were skipped and
never revisited.

The cost was not spread evenly. Nine Division I teams came out with *zero*
stored games:

    AMCC  Texas A&M-Corpus Christi      MCN   McNeese
    APP   App State                     NICH  Nicholls
    GRAM  Grambling                     SELA  SE Louisiana
    HAW   Hawaii                        SJSU  San Jose State
                                        UTM   UT Martin

For those teams the model had no scoring history at all, so it declined to
predict any of their games -- correct, and useless. The data was always there
in Barttorvik's file; only the name lookup failed. Every one of the nine is a
name Barttorvik writes differently from ESPN ("McNeese St." against "McNeese
Cowboys", "Tennessee Martin" against "UT Martin Skyhawks").

What it does
------------
Re-reads each season's ``super_sked``, resolves both sides with the *current*
alias table, and upserts the games that resolve. Deliberately narrow:

  * The game_id is byte-identical to the one ``import_ncaamb`` builds, so this
    is idempotent and a later full import neither duplicates nor conflicts.
  * Rows whose opponent still does not resolve are counted and reported, not
    registered. Creating a team record from a Barttorvik name alone, with no
    ESPN id to key it on, risks duplicating a school ESPN later registers
    properly -- so that case is surfaced for a decision instead of guessed at.
  * Calibration is not refitted. The constants on file were fitted against a
    particular set of games, and changing the set does invalidate them -- but
    refitting moves every number the app shows, which is not a thing to do as
    a side effect of a backfill.

``--report-only`` is the default, so the effect can be read before anything
is written.

Usage
-----
    python scripts/backfill_ncaamb.py                 # report, write nothing
    python scripts/backfill_ncaamb.py --write         # apply
    python scripts/backfill_ncaamb.py --write --seasons 2027
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from thugsak import config, matching, store  # noqa: E402
from thugsak.clients.stats import barttorvik  # noqa: E402


async def run(seasons: list[int], *, write: bool) -> int:
    src = barttorvik.Barttorvik()
    added = updated = skipped = 0
    unresolved: dict[str, int] = {}

    with store.connect() as conn:
        async with httpx.AsyncClient(timeout=120) as client:
            for season in seasons:
                try:
                    rows = await src.games(client, "ncaamb", year=season)
                except Exception as exc:
                    print(f"  ! season {season}: {type(exc).__name__}: {exc}")
                    continue

                s_added = s_updated = s_skipped = 0
                for r in rows:
                    home = matching.resolve_team(conn, "ncaamb", r["home_name"])
                    away = matching.resolve_team(conn, "ncaamb", r["away_name"])
                    if not home:
                        unresolved[r["home_name"]] = (
                            unresolved.get(r["home_name"], 0) + 1
                        )
                    if not away:
                        unresolved[r["away_name"]] = (
                            unresolved.get(r["away_name"], 0) + 1
                        )
                    if not home or not away or home == away:
                        s_skipped += 1
                        continue
                    # Only completed games carry a result worth storing. A
                    # scheduled row has no score, and the live slate already
                    # covers upcoming fixtures.
                    if r["home_score"] is None or r["away_score"] is None:
                        continue

                    key = f"ncaamb:bt:{season}:{r['start_time'][:10]}:{home}:{away}"
                    exists = conn.execute(
                        "SELECT 1 FROM games WHERE game_id=?", (key,)
                    ).fetchone()
                    if exists:
                        s_updated += 1
                    else:
                        s_added += 1

                    if write:
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
                        # Record the spelling so the next import resolves it by
                        # exact lookup rather than by fuzzy match, which is what
                        # stops this gap reopening.
                        matching.link_alias(
                            conn, "ncaamb", r["home_name"], "barttorvik", home
                        )
                        matching.link_alias(
                            conn, "ncaamb", r["away_name"], "barttorvik", away
                        )
                        if (s_added + s_updated) % 500 == 0:
                            conn.commit()

                if write:
                    conn.commit()
                added += s_added
                updated += s_updated
                skipped += s_skipped
                print(
                    f"  season {season}: {len(rows)} rows -> "
                    f"{s_added} new, {s_updated} already stored, "
                    f"{s_skipped} unresolvable"
                )

        verb = "WROTE" if write else "WOULD WRITE"
        print()
        print(
            f"{verb}: {added} new games ({updated} already present, "
            f"{skipped} rows unresolvable)"
        )

        if unresolved:
            hot = sorted(unresolved.items(), key=lambda kv: -kv[1])
            many = [(n, c) for n, c in hot if c >= 10]
            print()
            print(f"{len(unresolved)} names still unresolved.")
            if many:
                print(
                    "  Appearing 10+ times, so likely Division I "
                    "and worth an alias:"
                )
                for n, c in many:
                    print(f"    {n!r:<34} {c:>4}")
            print(
                "  The rest are overwhelmingly non-D1 opponents, which have no"
            )
            print("  team record by design; their games stay dropped.")

        if not write:
            print()
            print("Nothing written. Re-run with --write to apply.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill dropped NCAAMB games.")
    ap.add_argument("--write", action="store_true", help="apply the changes")
    ap.add_argument(
        "--seasons",
        type=int,
        nargs="*",
        default=None,
        help="Barttorvik season years (default: the three the import uses)",
    )
    args = ap.parse_args()
    cur = config.BARTTORVIK_SEASON
    seasons = args.seasons if args.seasons else [cur - 2, cur - 1, cur]
    print(f"NCAAMB backfill -- seasons {seasons}")
    return asyncio.run(run(seasons, write=args.write))


if __name__ == "__main__":
    raise SystemExit(main())
