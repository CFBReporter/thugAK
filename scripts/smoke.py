#!/usr/bin/env python
"""Live endpoint health check.

    python scripts/smoke.py
    python scripts/smoke.py --league nfl

Every data source here is undocumented or positional, so a silent upstream
change is the likeliest way this project starts producing wrong numbers rather
than errors. This hits each endpoint, parses a sample, and checks the values
land in a plausible range -- so a schema change shows up as a failed check
instead of a quietly shifted rating.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import cache  # noqa: E402
from thugsak.clients import espn as espn_mod  # noqa: E402
from thugsak.clients.kalshi import KalshiClient  # noqa: E402
from thugsak.clients.stats import barttorvik, nflverse  # noqa: E402
from thugsak.config import LEAGUES, league as get_league  # noqa: E402

OK, WARN, FAIL = "ok  ", "warn", "FAIL"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


async def check_kalshi(client, league_key: str) -> None:
    lg = get_league(league_key)
    async with KalshiClient(client) as kc:
        for kind, series in (
            ("winner", lg.kalshi_game),
            ("spread", lg.kalshi_spread),
            ("total", lg.kalshi_total),
        ):
            try:
                markets = await kc.markets(series_ticker=series, status="open", limit=5)
            except Exception as e:
                record(FAIL, f"kalshi {kind} {series}", f"{type(e).__name__}: {e}")
                continue
            if not markets:
                record(WARN, f"kalshi {kind} {series}", "no open markets")
                continue
            # Prices are decimal-dollar strings, so every ask belongs in
            # [0,1]. Anything above 1 means the field moved or is being read
            # as cents -- that is the regression worth failing on.
            asks = [
                (m, float(m.yes_ask)) for m in markets if m.yes_ask is not None
            ]
            out_of_range = [t for m, a in asks if (a < 0 or a > 1) and (t := m.ticker)]
            # A single ask of exactly 1.00 is not a fault: a strike whose game
            # is already decided settles there, so demanding a tradeable price
            # on markets[0] made this check flap by time of day. It only
            # matters that *something* in the sample is still quotable.
            quotable = [m for m, a in asks if 0 < a < 1]
            if out_of_range:
                status, detail = FAIL, f"ask outside [0,1]: {out_of_range[:3]}"
            elif not asks:
                status, detail = WARN, f"{len(markets)} markets, none with an ask"
            elif not quotable:
                status, detail = (
                    WARN,
                    f"{len(markets)} markets, all settled at 0 or 1 "
                    "(late in the slate -- not a fault)",
                )
            else:
                m = quotable[0]
                status, detail = (
                    OK,
                    f"{len(markets)} markets, {len(quotable)} quotable, "
                    f"e.g. {m.ticker} ask={m.yes_ask}",
                )
            record(status, f"kalshi {kind} {series}", detail)

        if markets:
            t = markets[0].ticker
            try:
                trades = await kc.trades(t, limit=50)
                record(OK, f"kalshi trades", f"{len(trades)} on {t}")
            except Exception as e:
                record(FAIL, "kalshi trades", f"{type(e).__name__}: {e}")
            try:
                ob = await kc.orderbook(t)
                record(OK, "kalshi orderbook",
                       f"{len(ob['yes'])} yes / {len(ob['no'])} no levels")
            except Exception as e:
                record(FAIL, "kalshi orderbook", f"{type(e).__name__}: {e}")
            try:
                candles = await kc.candlesticks(
                    markets[0].ticker.split("-")[0], t, period_interval=60
                )
                # An empty list here is the documented silent-failure mode for
                # a bad period_interval, so it is worth distinguishing.
                record(
                    OK if candles else WARN,
                    "kalshi candlesticks",
                    f"{len(candles)} candles"
                    + ("" if candles else " (empty — thin market or bad interval)"),
                )
            except Exception as e:
                record(FAIL, "kalshi candlesticks", f"{type(e).__name__}: {e}")
            try:
                mult = await kc.fee_multiplier(lg.kalshi_game)
                record(OK, "kalshi fee_multiplier", f"{mult} for {lg.kalshi_game}")
            except Exception as e:
                record(FAIL, "kalshi fee_multiplier", f"{type(e).__name__}: {e}")


async def check_espn(client, league_key: str) -> None:
    lg = get_league(league_key)
    async with espn_mod.EspnClient(client) as ec:
        try:
            teams = await ec.teams(league_key)
            # `location` is the alias that makes cross-source matching work;
            # losing it would silently collapse the NCAA match rate.
            with_loc = sum(1 for t in teams if t.location)
            record(
                OK if teams and with_loc else FAIL,
                "espn teams",
                f"{len(teams)} teams, {with_loc} with a location field",
            )
        except Exception as e:
            record(FAIL, "espn teams", f"{type(e).__name__}: {e}")
            return

        try:
            games = await ec.scoreboard(league_key)
            with_line = sum(1 for g in games if g.best_line)
            record(OK, "espn scoreboard",
                   f"{len(games)} games, {with_line} with a book line")
        except Exception as e:
            record(FAIL, "espn scoreboard", f"{type(e).__name__}: {e}")
            games = []

        if games:
            g = games[0]
            try:
                s = await ec.summary(league_key, g.espn_id)
                inj = ec.parse_injuries(s)
                last = ec.parse_last_games(s)
                pred = ec.parse_predictor(s)
                record(
                    OK,
                    "espn summary",
                    f"injuries for {len(inj)} teams, lastFiveGames for "
                    f"{len(last)}, predictor={'yes' if pred else 'no'}",
                )
                # Chase one injured player through to their game log, which is
                # the whole basis of the injury adjustment.
                player = next(
                    (i for lst in inj.values() for i in lst if i.matters and i.athlete_id),
                    None,
                )
                if player:
                    log = await ec.player_gamelog(league_key, player.athlete_id)
                    record(
                        OK if log else WARN,
                        "espn player gamelog",
                        f"{player.name} ({player.status}): {len(log)} games logged",
                    )
                else:
                    record(WARN, "espn player gamelog", "no doubtful players to test")
            except Exception as e:
                record(FAIL, "espn summary", f"{type(e).__name__}: {e}")

            try:
                sched = await ec.team_schedule(league_key, g.home.espn_id)
                record(OK, "espn team schedule", f"{len(sched)} games")
            except Exception as e:
                record(FAIL, "espn team schedule", f"{type(e).__name__}: {e}")

            if not lg.has_depth_chart:
                record(
                    OK,
                    "espn depth chart",
                    "not published for this league (expected) — the injury "
                    "model reports replacement quality as unavailable",
                )
            else:
                try:
                    chart = await ec.depth_chart(league_key, g.home.espn_id)
                    # The site-API path returns 200 with an empty body, so an
                    # empty chart here is a real signal, not just thin data.
                    record(
                        OK if chart else WARN,
                        "espn depth chart",
                        f"{len(chart)} positions"
                        + ("" if chart else " (empty — wrong host? use core API)"),
                    )
                except Exception as e:
                    record(FAIL, "espn depth chart", f"{type(e).__name__}: {e}")

        try:
            power = await ec.power_index(league_key)
            expected_full = lg.power_complete
            note = f"{len(power)} teams rated"
            if not expected_full:
                note += " (partial by design — Barttorvik covers the rest)"
            record(OK if power else FAIL, "espn power index", note)
        except Exception as e:
            record(FAIL, "espn power index", f"{type(e).__name__}: {e}")


async def check_stats(client) -> None:
    try:
        probe = await barttorvik.probe(client)
        record(
            OK if probe["layout_ok"] else FAIL,
            "barttorvik team_results layout",
            f"{probe['teams']} teams, {probe['fields']} fields "
            f"(expected {probe['fields_expected']}), sample={probe['sample']}",
        )
    except Exception as e:
        record(FAIL, "barttorvik team_results", f"{type(e).__name__}: {e}")

    try:
        games = await barttorvik.Barttorvik().games(client, "ncaamb")
        # Guards the column-24-vs-column-4 trap: predictions are shrunk toward
        # the mean, so a suspiciously tight spread of totals means the parser
        # is reading forecasts instead of results.
        if games:
            totals = [g["home_score"] + g["away_score"] for g in games]
            mean = sum(totals) / len(totals)
            var = sum((t - mean) ** 2 for t in totals) / max(len(totals) - 1, 1)
            sd = var**0.5
            record(
                OK if sd > 14 else FAIL,
                "barttorvik results (not predictions)",
                f"{len(games)} played games, total SD {sd:.1f} "
                + ("" if sd > 14 else "— TOO TIGHT, likely reading the prediction column"),
            )
        else:
            record(WARN, "barttorvik results", "no played games yet this season")
    except Exception as e:
        record(FAIL, "barttorvik results", f"{type(e).__name__}: {e}")

    try:
        rows = await nflverse.Nflverse().games(client, "nfl", seasons={2026})
        with_lines = sum(1 for r in rows if r["close_spread"] is not None)
        record(
            OK if rows else FAIL,
            "nflverse games",
            f"{len(rows)} games, {with_lines} with closing lines",
        )
    except Exception as e:
        record(FAIL, "nflverse games", f"{type(e).__name__}: {e}")


async def main_async(args) -> int:
    keys = list(LEAGUES) if args.league == "all" else [args.league]
    async with cache.make_client() as client:
        for key in keys:
            print(f"\n=== {get_league(key).name} ===")
            await check_kalshi(client, key)
            await check_espn(client, key)
        print("\n=== stats sources ===")
        await check_stats(client)

    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    n_warn = sum(1 for s, _, _ in results if s == WARN)
    print(f"\n{len(results)} checks: {len(results) - n_fail - n_warn} ok, "
          f"{n_warn} warn, {n_fail} failed")
    if n_fail:
        print("\nFailures (an upstream change is the usual cause):")
        for s, name, detail in results:
            if s == FAIL:
                print(f"  {name}: {detail}")
    return 1 if n_fail else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--league", default="all", choices=["all", *LEAGUES])
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
