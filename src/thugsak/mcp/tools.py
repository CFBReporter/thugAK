"""The tool layer: one implementation, two MCP adapters.

`server.py` exposes these over stdio for Claude Code and Claude Desktop;
`sdk_server.py` wraps the same callables in-process for the Streamlit app. The
logic lives here once so the two surfaces cannot drift apart.

Every function returns plain JSON-serialisable data with units and provenance
spelled out, because the consumer is a language model reading numbers it
cannot otherwise interpret. In particular:

- Spreads are stated home-relative, with the sign convention named each time.
  Sportsbook convention (negative = home favoured) and margin convention
  (positive = home favoured) are trivially easy to confuse.
- Every price comparison is reported net of Kalshi fees, with the fee shown.
- Confidence and sample size travel with each figure, so a number resting on
  two games cannot be mistaken for one resting on two hundred.
- **Every time is Central.** Dates default to today in Central, and each
  timestamp is accompanied by a `*_local` field already rendered in Central
  with its zone named. The raw UTC value is kept beside it because that is
  what the as-of guard compares, but nothing in a write-up should quote it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

from .. import cache, config, matching, providers, store
from ..analysis import (
    devig,
    edge as edge_mod,
    fees,
    flow as flow_mod,
    lookback as lookback_mod,
    money as money_mod,
    verdict as verdict_mod,
)
from ..clients import espn as espn_mod
from ..clients.kalshi import KalshiClient
from ..clients.stats import sources_for
from ..config import DB_PATH, LEAGUES, league as get_league
from ..localtime import (
    fmt_datetime,
    fmt_time,
    local_date,
    now_local,
    today_local,
    tz_abbrev,
)
from ..model import (
    asof,
    availability as avail_mod,
    injuries as injuries_mod,
    predict as predict_mod,
    ratings as ratings_mod,
)

MODEL_NOTE = (
    "Spreads are home-relative: positive means the home team is favoured. "
    "Sportsbook lines are converted to this convention. All edges are net of "
    "Kalshi fees."
)


def _provider() -> Any:
    """The configured game-data provider.

    Resolved per call rather than at import, so THUGSAK_PROVIDER can be
    changed without restarting an interpreter that has already imported this
    module -- which matters for the tests and for a dev server.
    """
    return providers.get(config.DATA_PROVIDER)


def _json(obj: Any) -> Any:
    """Make dataclasses, Decimals and sqlite rows JSON-safe."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return _json(asdict(obj))
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json(v) for v in obj]
    if hasattr(obj, "keys"):  # sqlite3.Row
        return {k: _json(obj[k]) for k in obj.keys()}
    return obj


# --------------------------------------------------------------------------
# Kalshi market discovery
# --------------------------------------------------------------------------

def _attach_subjects(
    conn,
    league_key: str,
    markets: Sequence[Any],
    fallback: str | None = None,
    *,
    event_titles: Sequence[str] = (),
) -> None:
    """Tag each market with the team its rung refers to.

    The edge layer needs this to tell which side of the game a ladder strike
    belongs to; without it a spread rung cannot be assigned to a team and the
    per-team rows have nothing to choose from.
    """
    for m in markets:
        mr = matching.match_market(
            conn, m.ticker, title=getattr(m, "title", ""),
            rules_secondary=getattr(m, "rules_secondary", ""),
            league_hint=league_key,
            event_titles=event_titles,
        )
        setattr(m, "_subject_team", mr.subject_team or fallback)


def _winner_price(winner: Sequence[Any], team_id: str) -> float | None:
    """The yes ask on `team_id` winning, or None if that market is absent.

    Absent is a normal state -- Kalshi lists one side of some games only, and
    a settled market can have no ask at all -- so it returns None rather than
    raising, and the caller shows a dash.
    """
    for m in winner:
        if getattr(m, "_subject_team", None) == team_id:
            return float(m.yes_ask) if m.yes_ask else None
    return None


async def _league_markets(
    conn, client, league_key: str
) -> tuple[dict[str, list[Any]], list[dict[str, Any]]]:
    """Map stored game_id -> its Kalshi markets, plus a list of unmatched ones.

    All three series (winner, spread, total) are fetched and grouped by the
    shared middle segment of the ticker, which is what links a game's ladders
    together. Unmatched markets are returned rather than dropped: those are
    disproportionately the thin, illiquid markets where the largest
    mispricings live, so hiding them would defeat the purpose.
    """
    lg = get_league(league_key)
    by_game: dict[str, list[Any]] = {}
    unmatched: list[dict[str, Any]] = []

    async with KalshiClient(client) as kc:
        all_markets: list[Any] = []
        for series in (lg.kalshi_game, lg.kalshi_spread, lg.kalshi_total):
            try:
                all_markets += await kc.markets(series_ticker=series, status="open")
            except Exception as e:
                unmatched.append(
                    {"series": series, "error": f"{type(e).__name__}: {e}"}
                )

    # Resolve each event key once, using the winner markets (which name a team
    # outright) before falling back to spread markets.
    grouped = matching.group_by_event(all_markets)
    for event_key, markets in grouped.items():
        resolved: matching.MatchResult | None = None
        last_reason = ""
        ordered = sorted(
            markets,
            key=lambda m: 0 if "GAME" in m.ticker else 1,
        )
        # The two winner markets name one team each in their titles, which
        # for most college events is the only readable team name anywhere in
        # the payload. Passed as a set so the matcher can pair them up.
        event_titles = [
            m.title for m in ordered
            if "GAME" in m.ticker and getattr(m, "title", "")
        ]
        for m in ordered:
            r = matching.match_market(
                conn,
                m.ticker,
                title=m.title,
                rules_secondary=getattr(m, "rules_secondary", ""),
                league_hint=league_key,
                event_titles=event_titles,
            )
            if r.matched:
                resolved = r
                break
            last_reason = r.reason or last_reason
        if resolved is None or not resolved.game_id:
            unmatched.append(
                {
                    "event_key": event_key,
                    "tickers": [m.ticker for m in markets][:4],
                    "titles": [m.title for m in markets][:2],
                    # The per-market reason, not a generic one. "No candidate
                    # markets" hid the fact that every market had failed for
                    # the same nameable cause, which is what makes the long
                    # tail of unmatched events fixable rather than mysterious.
                    "reason": last_reason or "no candidate markets",
                }
            )
            continue

        _attach_subjects(
            conn, league_key, markets, resolved.subject_team,
            event_titles=event_titles,
        )
        by_game.setdefault(resolved.game_id, []).extend(markets)
    conn.commit()
    return by_game, unmatched


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def _event_key_date(event_key: str) -> str | None:
    """The calendar date encoded in a Kalshi event key ("26NOV11ORLUTA")."""
    from ..matching import parse_ticker

    p = parse_ticker(f"X-{event_key}")
    return p.date if p else None


async def list_games(
    league: str, date: str | None = None, *, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """Games for a league on a Central-Time date (YYYY-MM-DD, default today).

    Past dates are allowed and are how the lookback works: completed games
    come back with their final score, and a prediction for them is still
    computed as of kickoff minus an hour.
    """
    get_league(league)
    # Central, not UTC. After 6pm CST the UTC date has already rolled over, so
    # a UTC default shows tomorrow's slate while the person is still looking
    # at tonight's games.
    date = date or today_local().strftime("%Y-%m-%d")
    store.init_db(db)

    async with cache.make_client() as client:
        with store.connect(db) as conn:
            # Refresh the slate from ESPN so the requested date exists locally.
            async with _provider().session(client) as ec:
                try:
                    live = await ec.scoreboard(league, date=date)
                except Exception as e:
                    live = []
                    note = f"ESPN scoreboard unavailable: {type(e).__name__}"
                else:
                    note = ""
            for g in live:
                home = matching.resolve_or_register(conn, league, g.home)
                away = matching.resolve_or_register(conn, league, g.away)
                if not home or not away or home == away:
                    continue
                line = g.best_line
                store.upsert_game(
                    conn,
                    {
                        "game_id": f"{league}:espn:{g.espn_id}",
                        "league": league,
                        "season": g.season or get_league(league).fpi_season,
                        "season_type": "regular",
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
                        # ESPN was asked for this date, so this is the slate
                        # the game belongs to regardless of its UTC start.
                        "slate_date": date,
                    },
                )
            conn.commit()

            # Fill in results for anything predicted earlier that has since
            # finished, so the record accrues as a side effect of browsing
            # rather than needing a job nobody remembers to run.
            store.settle_predictions(conn, league)
            # Commit before market matching: that step is slow and network
            # bound, and an open write here held the SQLite lock across it --
            # every other writer (the agent cycle, the next click) waited out
            # the 60s busy timeout and failed with "database is locked".
            conn.commit()
            rows = store.games_on_date(conn, league, date)
            by_game, unmatched = await _league_markets(conn, client, league)
            # Kalshi lists two weeks of events at once, so restrict the
            # unmatched report to the date in view -- otherwise next week's
            # fixtures read as failures.
            unmatched = [
                u for u in unmatched
                if not u.get("event_key") or _event_key_date(u["event_key"]) == date
            ]

            out = []
            for r in rows:
                markets = by_game.get(r["game_id"], [])
                winner = [m for m in markets if "GAME" in m.ticker]
                out.append(
                    {
                        "game_id": r["game_id"],
                        "start_time": r["start_time"],
                        "start_local": fmt_datetime(r["start_time"]),
                        "home": r["home_team"],
                        "away": r["away_team"],
                        "home_label": store.team_label(conn, r["home_team"], short=True),
                        "away_label": store.team_label(conn, r["away_team"], short=True),
                        "home_name": store.team_label(conn, r["home_team"]),
                        "away_name": store.team_label(conn, r["away_team"]),
                        "completed": bool(r["completed"]),
                        "score": (
                            f"{r['away_score']}-{r['home_score']}"
                            if r["home_score"] is not None else None
                        ),
                        "book_spread_home_relative": (
                            -r["close_spread"] if r["close_spread"] is not None else None
                        ),
                        "book_total": r["close_total"],
                        "kalshi_markets": len(markets),
                        "kalshi_winner_prices": {
                            m.ticker: float(m.yes_ask) if m.yes_ask else None
                            for m in winner
                        },
                        # Resolved by subject team rather than left for the
                        # caller to infer from the ticker. Kalshi's code and
                        # the stored team id disagree often enough that
                        # suffix-matching a ticker silently picks the wrong
                        # side: Kalshi writes WAS where the store says WSH, so
                        # "ends with -WSH" finds nothing and the home price
                        # reads as unavailable. `_subject_team` is already the
                        # canonical id, via the same alias table everything
                        # else uses.
                        "home_win_price": _winner_price(winner, r["home_team"]),
                        "away_win_price": _winner_price(winner, r["away_team"]),
                        "has_ladders": any("SPREAD" in m.ticker or "TOTAL" in m.ticker
                                           for m in markets),
                    }
                )
    return {
        "league": league,
        "date": date,
        "timezone": f"America/Chicago ({tz_abbrev()})",
        "games": out,
        "unmatched_kalshi_events": unmatched[:10],
        "unmatched_count": len(unmatched),
        "note": note or MODEL_NOTE,
    }


async def get_prediction(
    league: str,
    game_id: str,
    *,
    include_injuries: bool = True,
    db: str = str(DB_PATH),
) -> dict[str, Any]:
    """Predicted total, spread and win probability, with every input shown.

    Computed as of kickoff minus one hour using only earlier games, so the
    same code path serves tonight's slate and a lookback on a past game.
    """
    store.init_db(db)
    async with cache.make_client() as client:
        with store.connect(db) as conn:
            game = store.get_game(conn, game_id)
            if game is None:
                return {"error": f"unknown game_id {game_id!r}"}

            as_of = asof.as_of_for_kickoff(game["start_time"])
            rs = ratings_mod.solve_from_store(
                conn, league, as_of, season=game["season"], use_cache=False
            )

            impacts: dict[str, list[injuries_mod.InjuryImpact]] = {}
            injury_note = ""
            if include_injuries:
                try:
                    impacts, injury_note = await _injury_impacts(
                        conn, client, league, game, as_of, rs
                    )
                except Exception as e:
                    injury_note = f"injury analysis unavailable: {type(e).__name__}"

            line = await _market_line(conn, client, league, game)
            mkt_prob = None
            if line["home_ml"] is not None and line["away_ml"] is not None:
                fair = devig.fair_two_way(line["home_ml"], line["away_ml"])
                mkt_prob = fair[0] if fair else None

            pred = predict_mod.predict_game(
                conn,
                league,
                game,
                as_of=as_of,
                rs=rs,
                injury_impacts=impacts or None,
                market_spread=line["spread"],
                market_total=line["total"],
                market_home_prob=mkt_prob,
            )
            predict_mod.save(
                conn, pred,
                matchup=_matchup(conn, game),
                slate_date=game["slate_date"],
            )
            conn.commit()

            result = {
                "game_id": game_id,
                "league": league,
                "as_of": as_of,
                "as_of_local": fmt_datetime(as_of),
                "as_of_note": (
                    f"Computed from games before {fmt_datetime(as_of)} only "
                    "(kickoff minus 1 hour). The raw `as_of` field is UTC "
                    "because that is what the lookahead guard compares; quote "
                    "`as_of_local` instead."
                ),
                "start_time": game["start_time"],
                "start_local": fmt_datetime(game["start_time"]),
                "home": pred.home_team,
                "away": pred.away_team,
                "home_name": store.team_label(conn, pred.home_team),
                "away_name": store.team_label(conn, pred.away_team),
                "predicted_home_points": pred.home_points,
                "predicted_away_points": pred.away_points,
                "predicted_spread_home_relative": pred.spread,
                "predicted_total": pred.total,
                "home_win_probability": pred.home_win_prob,
                "sigma_margin": pred.sigma_margin,
                "sigma_total": pred.sigma_total,
                "calibrated_from_history": pred.calibrated,
                "blend_weights": pred.weights,
                "anchors": [
                    {
                        "name": a.name,
                        "spread_home_relative": a.spread,
                        "total": a.total,
                        "home_win_prob": a.home_win_prob,
                        "source": a.source,
                    }
                    for a in pred.anchors
                ],
                "home_form": pred.home_form,
                "away_form": pred.away_form,
                "injury_adjustment": {
                    "home_points": pred.injury_adj_home,
                    "away_points": pred.injury_adj_away,
                    "confidence": pred.injury_confidence,
                    "detail": pred.injury_detail,
                    "note": injury_note or (
                        "Injury effects are estimated from each player's own "
                        "recent production and the team's with/without split. "
                        "They cannot be validated by backtest, because no "
                        "historical injury snapshots exist."
                    ),
                },
                "availability": await _availability_block(
                    conn, client, league, game, as_of, impacts
                ),
                "warnings": pred.warnings,
                "summary": pred.summary(),
            }
            if game["home_score"] is not None:
                result["actual"] = predict_mod.grade(pred, game)
            return result


async def _injury_impacts(
    conn, client, league: str, game: Any, as_of: str, rs: Any
) -> tuple[dict[str, list[injuries_mod.InjuryImpact]], str]:
    """Build per-player injury assessments for both teams."""
    espn_id = game["game_id"].split(":")[-1]
    out: dict[str, list[injuries_mod.InjuryImpact]] = {}

    async with _provider().session(client) as ec:
        summary = await ec.summary(league, espn_id)
        by_abbrev = ec.parse_injuries(summary)
        if not by_abbrev:
            return {}, "ESPN reported no injuries for this game."

        from ..model import gamelog

        for abbrev, injuries in by_abbrev.items():
            team_id = matching.resolve_team(conn, league, abbrev)
            if not team_id:
                continue
            tf = gamelog.team_form(conn, team_id, as_of, rs=rs)
            baseline = gamelog.blended_points(
                tf, store.get_calibration(conn, league) or predict_mod.DEFAULT_WEIGHTS
            )
            assessed: list[injuries_mod.InjuryImpact] = []
            # Only players whose availability is in doubt; these feeds list
            # healthy players too, and those are not news.
            notable = [i for i in injuries if i.matters][:8]
            for inj in notable:
                if not inj.athlete_id:
                    continue
                try:
                    pgames = await ec.player_gamelog(league, inj.athlete_id)
                except Exception:
                    pgames = []
                backup_games: list[Any] | None = []
                backup_name = None
                if not get_league(league).has_depth_chart:
                    # No published depth chart for this league, so the
                    # starter-minus-backup signal genuinely cannot be computed.
                    # None tells `assess` to say so rather than implying the
                    # position has no backup.
                    backup_games = None
                else:
                    try:
                        bid = await ec.backup_for(
                            league, _espn_team_id(conn, league, team_id),
                            inj.position, inj.athlete_id,
                        )
                        if bid:
                            backup_games = await ec.player_gamelog(league, bid)
                            backup_name = bid
                    except Exception:
                        pass
                assessed.append(
                    injuries_mod.assess(
                        conn,
                        injury=inj,
                        team_id=team_id,
                        as_of=as_of,
                        player_games=pgames,
                        team_window_points=baseline,
                        backup_games=backup_games,
                        backup_name=backup_name,
                        ratings=rs,
                    )
                )
            if assessed:
                out[team_id] = assessed
    return out, ""


def _espn_team_id(conn, league: str, team_id: str) -> str:
    row = conn.execute(
        "SELECT espn_id FROM teams WHERE team_id=?", (team_id,)
    ).fetchone()
    return str(row["espn_id"]) if row and row["espn_id"] else ""



async def _availability_block(
    conn, client, league: str, game: Any, as_of: str, impacts: dict[str, Any]
) -> dict[str, Any]:
    """Who has actually stopped playing, for both teams.

    This is the only injury signal available for college: ESPN's NCAAF feed
    carried three players league-wide and its NCAAMB feed was empty, college
    game summaries have no injuries block, and the paid aggregators that do
    cover it are not free. Reading who appeared in recent box scores answers
    the question that matters -- availability -- without needing a report.

    For the NFL and NBA it runs as a cross-check: a player listed Questionable
    who has not actually played in three weeks is a different proposition from
    one who played last Sunday.
    """
    lg = get_league(league)
    reported: set[str] = {
        i.athlete_id for lst in (impacts or {}).values() for i in lst if i.athlete_id
    }
    out: dict[str, Any] = {
        "method": "appearance in recent box scores",
        "why": (
            "No usable injury feed exists for college: ESPN lists almost "
            "nothing and publishes no college depth charts. Absence is "
            "therefore measured rather than reported."
            if not lg.reliable_injuries
            else "Cross-check against the injury report."
        ),
        "is_primary_signal": not lg.reliable_injuries,
        "teams": {},
    }
    for side in ("home_team", "away_team"):
        team_id = game[side]
        espn_id = _espn_team_id(conn, league, team_id)
        try:
            rep = await avail_mod.team_availability(
                client, conn, league, team_id, espn_id, as_of
            )
        except Exception as e:
            out["teams"][team_id] = {"error": f"{type(e).__name__}: {e}"}
            continue
        merged = avail_mod.merge_with_injuries(rep, reported)
        merged["team_label"] = store.team_label(conn, team_id)
        merged["classification_note"] = (
            "kind=absent means the position's output fell with them; "
            "kind=replaced means someone absorbed the role so output held up; "
            "kind=stand_in means they were themselves covering for a starter "
            "who has since returned, which is not a loss at all."
        )
        merged["detail"] = [
            {
                "name": p.name,
                "position": p.position,
                "kind": p.kind,
                "confidence": p.confidence,
                "consecutive_missed": p.consecutive_missed,
                "usage_rank": p.usage_rank,
                "usage_share": round(p.usage_share, 3),
                "avg_usage": round(p.avg_usage, 1),
                "position_output_retained": p.position_retained,
            }
            for p in rep.absent
        ]
        merged["immaterial_absences_filtered"] = rep.filtered_out
        out["teams"][team_id] = merged
    return out


async def get_money_flow(
    ticker: str,
    *,
    lookback_hours: int = 48,
    halflife_hours: float = 12.0,
    db: str = str(DB_PATH),
) -> dict[str, Any]:
    """Where the big money is on one market, weighted by position size.

    Aggregates are notional-weighted (price x contracts), so a large position
    counts for more than a small one, and time-decayed so recent money
    dominates. Block trades are reported separately because they are
    negotiated off-book and carry different information.
    """
    parsed = matching.parse_ticker(ticker)
    series = parsed.series if parsed else ticker.split("-")[0]
    min_ts = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())

    async with cache.make_client() as client:
        async with KalshiClient(client) as kc:
            trades = await kc.trades(ticker, min_ts=min_ts)
            fa = flow_mod.analyze_trades(
                ticker, trades, halflife_hours=halflife_hours
            )
            try:
                candles = await kc.candlesticks(
                    series, ticker, period_interval=60, lookback_hours=lookback_hours
                )
                fa = flow_mod.analyze_open_interest(fa, candles)
            except Exception as e:
                fa.notes.append(f"candlestick history unavailable: {type(e).__name__}")
            try:
                book = await kc.orderbook(ticker)
                depth = flow_mod.depth_summary(book)
            except Exception:
                depth = {}

    return {
        "ticker": ticker,
        "window_hours": lookback_hours,
        "trades": fa.n_trades,
        "total_contracts": fa.total_contracts,
        "total_notional_usd": round(fa.total_notional, 2),
        "imbalance": round(fa.imbalance, 4),
        "imbalance_scale": "-1 = all NO aggression, +1 = all YES; notional-weighted, time-decayed",
        "imbalance_without_time_decay": round(fa.imbalance_raw, 4),
        "imbalance_by_trade_count": round(fa.count_imbalance, 4),
        "count_vs_size_note": (
            "If the notional imbalance is much larger than the trade-count "
            "imbalance, a few big positions are driving it rather than broad "
            "participation."
        ),
        "direction": fa.direction,
        "conviction": fa.conviction,
        "yes_notional_usd": round(fa.yes_notional, 2),
        "no_notional_usd": round(fa.no_notional, 2),
        "whale_threshold_contracts": round(fa.whale_threshold, 1),
        "whale_consensus_yes_share": (
            round(fa.whale_consensus, 4) if fa.whale_consensus is not None else None
        ),
        "whale_share_of_notional": (
            round(fa.whale_notional / fa.total_notional, 4)
            if fa.total_notional else None
        ),
        "largest_trades": [
            {
                "side": w.side,
                "contracts": w.contracts,
                "price": w.price,
                "notional_usd": round(w.notional, 2),
                "traded_at_local": (
                    fmt_datetime(w.created_time) if w.created_time else None
                ),
                "hours_ago": round(w.age_hours, 1),
                "is_block": w.is_block,
            }
            for w in fa.whales[:10]
        ],
        "block_trades": len(fa.blocks),
        "block_notional_usd": round(fa.block_notional, 2),
        "open_interest_reading": fa.oi_reading,
        "open_interest_strength": fa.oi_strength,
        "price_change": fa.price_change,
        "oi_change": fa.oi_change,
        "vwap": fa.vwap,
        "last_price": fa.last_price,
        "depth": _json(depth),
        "divergence": fa.divergence(fa.last_price),
        "notes": fa.notes,
        "summary": fa.summary(),
    }


async def get_edges(
    league: str,
    game_id: str,
    *,
    contracts: int = 100,
    db: str = str(DB_PATH),
) -> dict[str, Any]:
    """Score every Kalshi ladder strike for a game against the model.

    This is the actionable table: each spread and total strike is an
    independently priced binary, so one prediction produces an edge at every
    rung. Results are net of fees and sorted best-first, with implausible
    edges flagged rather than promoted.
    """
    store.init_db(db)
    async with cache.make_client() as client:
        with store.connect(db) as conn:
            game = store.get_game(conn, game_id)
            if game is None:
                return {"error": f"unknown game_id {game_id!r}"}

            by_game, _ = await _league_markets(conn, client, league)
            markets = by_game.get(game_id, [])
            if not markets:
                return {
                    "game_id": game_id,
                    "rungs": [],
                    "note": (
                        "No Kalshi markets matched this game. Either it is not "
                        "listed, or the team-name mapping failed -- check "
                        "unmatched_kalshi_events from list_games."
                    ),
                }

            as_of = asof.as_of_for_kickoff(game["start_time"])
            rs = ratings_mod.solve_from_store(
                conn, league, as_of, season=game["season"], use_cache=False
            )
            mkt_prob = None
            if game["close_home_ml"] is not None and game["close_away_ml"] is not None:
                fair = devig.fair_two_way(game["close_home_ml"], game["close_away_ml"])
                mkt_prob = fair[0] if fair else None
            pred = predict_mod.predict_game(
                conn, league, game, as_of=as_of, rs=rs,
                market_spread=game["close_spread"],
                market_total=game["close_total"],
                market_home_prob=mkt_prob,
            )
            if not pred.usable:
                return {
                    "game_id": game_id,
                    "rungs": [],
                    "warnings": pred.warnings,
                    "note": "No usable prediction, so no edges can be computed.",
                }

            lg = get_league(league)
            async with KalshiClient(client) as kc:
                mults: dict[str, Decimal] = {}
                for series in (lg.kalshi_game, lg.kalshi_spread, lg.kalshi_total):
                    try:
                        mults[series] = await kc.fee_multiplier(series)
                    except Exception:
                        mults[series] = Decimal(1)

            rungs: list[edge_mod.Rung] = []
            for m in markets:
                series = m.ticker.split("-")[0]
                r = edge_mod.evaluate_market(
                    pred, m,
                    fee_multiplier=mults.get(series, Decimal(1)),
                    contracts=contracts,
                )
                if r:
                    rungs.append(r)
            cal = store.get_calibration(conn, league)
            edge_mod.apply_skill_discount(rungs, cal)
            diag = edge_mod.diagnose_ladder(rungs, pred, cal)
            rungs.sort(
                key=lambda r: (
                    r.suspect,
                    -(r.adjusted_edge if r.adjusted_edge is not None
                      else (r.net_edge if r.net_edge is not None else -99)),
                )
            )
            arb = edge_mod.find_arbitrage(markets)

    return {
        "game_id": game_id,
        "league": league,
        "as_of": as_of,
        "as_of_local": fmt_datetime(as_of),
        "predicted_spread_home_relative": pred.spread,
        "predicted_total": pred.total,
        "position_size_contracts": contracts,
        "fee_multipliers": {k: float(v) for k, v in mults.items()},
        "model_skill": diag.get("skill"),
        "correlated_ladders": diag.get("one_sided"),
        "ladder_warnings": diag.get("warnings"),
        "edge_note": (
            "net_edge is the raw arithmetic; adjusted_edge scales it by how "
            "much the model is measurably better than predicting the league "
            "average. Rank on adjusted_edge. When many strikes on one ladder "
            "share a direction they are one position, not many."
        ),
        "rungs": [
            {
                "ticker": r.ticker,
                "kind": r.kind,
                "title": r.title,
                "strike": r.threshold,
                "subject_team": r.subject_team,
                "ask": float(r.ask) if r.ask is not None else None,
                "fee_per_contract": float(r.fee_per_contract) if r.fee_per_contract else None,
                "all_in_cost": float(r.all_in_cost) if r.all_in_cost else None,
                "model_probability": r.model_prob,
                "net_edge": r.net_edge,
                "adjusted_edge": r.adjusted_edge,
                "ev_per_100_contracts_usd": float(r.ev_per_100) if r.ev_per_100 else None,
                "volume": r.volume,
                "suspect": r.suspect,
                "notes": r.notes,
            }
            for r in rungs
        ],
        "best_bets": [r.ticker for r in edge_mod.best_bets(rungs)],
        "arbitrage": arb,
        "warnings": pred.warnings,
        "note": MODEL_NOTE,
    }




async def _market_line(
    conn, client, league: str, game: Any
) -> dict[str, Any]:
    """The sportsbook line, falling back to the per-game endpoint.

    ESPN embeds odds in the scoreboard for featured games only -- 9 of 67
    college basketball games on a typical night. The `summary` endpoint's
    `pickcenter` carries DraftKings for essentially all of them, so without
    this fallback most games lose their independent market check and the model
    is compared against nothing but Kalshi.

    That check is what catches a bad prediction: on one game the model had
    Marquette by 10 while the book had Michigan by 2.5, a 12-point
    disagreement that is only visible once the line is present.
    """
    out = {
        "spread": game["close_spread"],
        "total": game["close_total"],
        "home_ml": game["close_home_ml"],
        "away_ml": game["close_away_ml"],
        "source": "scoreboard",
    }
    if out["spread"] is not None and out["total"] is not None:
        return out

    espn_id = str(game["game_id"]).split(":")[-1]
    if not espn_id.isdigit():
        out["source"] = "unavailable (no ESPN event id for this game)"
        return out
    try:
        async with _provider().session(client) as ec:
            summary = await ec.summary(league, espn_id)
            lines = ec.parse_pickcenter(summary)
    except Exception as e:
        out["source"] = f"unavailable ({type(e).__name__})"
        return out

    best = next((ln for ln in lines if ln.home_ml is not None), None) or (
        lines[0] if lines else None
    )
    if best is None:
        out["source"] = "no sportsbook line published for this game"
        return out

    if out["spread"] is None:
        out["spread"] = best.spread
    if out["total"] is None:
        out["total"] = best.total
    if out["home_ml"] is None:
        out["home_ml"] = best.home_ml
    if out["away_ml"] is None:
        out["away_ml"] = best.away_ml
    out["source"] = f"{best.provider} (per-game pickcenter)"

    # Persist so a later lookback sees the line that was available now.
    store.update_market_line(
        conn,
        game["game_id"],
        spread=out["spread"],
        total=out["total"],
        home_ml=out["home_ml"],
        away_ml=out["away_ml"],
    )
    return out


async def _flows_for(
    client: Any, tickers: Sequence[str], *, lookback_hours: int = 48
) -> dict[str, dict[str, Any]]:
    """Money-flow summaries for a handful of named markets.

    Called with the five markets the recommendation actually landed on rather
    than all sixty on the game, which keeps this to five requests. Failures
    are swallowed per market: flow is corroboration, and losing it should
    soften a recommendation, never break the page.
    """
    if not tickers:
        return {}
    min_ts = int(
        (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp()
    )
    out: dict[str, dict[str, Any]] = {}
    async with KalshiClient(client) as kc:
        async def one(t: str) -> None:
            try:
                trades = await kc.trades(t, min_ts=min_ts)
            except Exception:
                return
            fa = flow_mod.analyze_trades(t, trades)
            out[t] = {
                "imbalance": fa.imbalance,
                "whale_consensus_yes_share": fa.whale_consensus,
                "total_notional_usd": fa.total_notional,
            }

        await asyncio.gather(*(one(t) for t in tickers))
    return out


async def _settled_markets_for_game(
    conn, client: Any, league_key: str, game_id: str
) -> list[Any]:
    """A finished game's Kalshi markets, which are no longer `open`.

    Fetched in two steps on purpose. Pulling a whole settled ladder series
    means 6,000 markets and seven seconds per series, growing all season; the
    winner series is a few hundred, so it is used to find this game's event
    key and the ladders are then fetched by event ticker -- 30 markets in a
    fifth of a second.
    """
    lg = get_league(league_key)
    async with KalshiClient(client) as kc:
        try:
            winners = await kc.markets(
                series_ticker=lg.kalshi_game, status="settled", max_pages=30
            )
        except Exception:
            return []

        # Which event is this game? Resolved with the same matcher the live
        # path uses, so a settled game cannot match by different rules.
        event_key = None
        for m in winners:
            res = matching.match_market(
                conn, m.ticker,
                title=getattr(m, "title", ""),
                rules_secondary=getattr(m, "rules_secondary", ""),
                league_hint=league_key,
            )
            if res.game_id == game_id:
                event_key = res.parsed.event_key
                break
        if event_key is None:
            return []

        out = [m for m in winners if f"-{event_key}-" in m.ticker]
        for series in (lg.kalshi_spread, lg.kalshi_total):
            try:
                out += await kc.markets(
                    event_ticker=f"{series}-{event_key}",
                    status="settled", max_pages=5,
                )
            except Exception:
                continue
    _attach_subjects(conn, league_key, out)
    return out


async def _snapshots_at(
    client: Any, league: str, tickers: Sequence[str], as_of: str
) -> dict[str, Any]:
    """Each market's quotes and money as they stood at `as_of`.

    Hourly candlesticks carry a historical `yes_ask`, so a reconstruction can
    be charged the price a buyer would actually have paid rather than the
    last trade -- the difference is the spread, which on these markets is
    most of a thin edge.
    """
    if not tickers:
        return {}
    # Enough to cover a market's life for a game listed a few days out;
    # Kalshi caps the response at 96 hourly candles regardless.
    hours = max(
        1,
        int(
            (
                datetime.now(timezone.utc) - asof.parse_ts(as_of)
            ).total_seconds() // 3600
        )
        + 96,
    )
    out: dict[str, Any] = {}

    async with KalshiClient(client) as kc:
        async def one(t: str) -> None:
            series = t.split("-")[0]
            try:
                candles = await kc.candlesticks(
                    series, t, period_interval=60, lookback_hours=hours
                )
            except Exception:
                return
            out[t] = lookback_mod.snapshot_from_candles(t, candles, as_of)

        await asyncio.gather(*(one(t) for t in tickers))
    return out


async def get_recommendation(
    league: str,
    game_id: str,
    *,
    contracts: int = 100,
    db: str = str(DB_PATH),
) -> dict[str, Any]:
    """The answer: a predicted score, and five bets with a yes or no on each.

    Returns a predicted final score with what it rests on, then exactly five
    rows in a fixed order -- moneyline for each team, spread for each team,
    and the total as over or under -- each carrying one Kalshi market, its
    price, the dollars traded on it, and BET / SMALL / NO.

    **Prefer this over get_edges for any "what should I bet" question.** A
    sorted list of 60 strikes is data; this is a decision, and it collapses
    the one-disagreement-many-strikes trap into a single position per slot.

    How much money stands behind a price is an input, not a footnote: the
    required edge rises with the dollars, because a heavily traded price is
    much more likely to be right than a $200 one.

    **A completed game switches into lookback mode automatically.** The
    prediction is computed as of kickoff minus an hour and the Kalshi prices
    are rewound to the quotes standing at that moment, so the output is what
    this tool would have said before the game -- with the final score and a
    won/lost grade on each recommended bet beside it. The injury adjustment
    is switched off there, because ESPN publishes no historical injury
    snapshots and today's report is simply wrong data for last week's game.
    """
    store.init_db(db)
    async with cache.make_client() as client:
        with store.connect(db) as conn:
            game = store.get_game(conn, game_id)
            if game is None:
                return {"error": f"unknown game_id {game_id!r}"}

            as_of = asof.as_of_for_kickoff(game["start_time"])
            rs = ratings_mod.solve_from_store(
                conn, league, as_of, season=game["season"], use_cache=False
            )

            # A finished game is graded, not forecast. Everything below runs
            # as of kickoff minus an hour either way; what changes is that
            # the injury feed is withheld and the prices are rewound.
            finished = bool(game["completed"]) and game["home_score"] is not None

            impacts: dict[str, Any] = {}
            if get_league(league).reliable_injuries and not finished:
                try:
                    impacts, _ = await _injury_impacts(
                        conn, client, league, game, as_of, rs
                    )
                except Exception:
                    impacts = {}
            avail = (
                {} if finished
                else await _availability_block(
                    conn, client, league, game, as_of, impacts
                )
            )

            line = await _market_line(conn, client, league, game)
            mkt_prob = None
            if line["home_ml"] is not None and line["away_ml"] is not None:
                fair = devig.fair_two_way(line["home_ml"], line["away_ml"])
                mkt_prob = fair[0] if fair else None

            pred = predict_mod.predict_game(
                conn, league, game, as_of=as_of, rs=rs,
                injury_impacts=impacts or None,
                market_spread=line["spread"],
                market_total=line["total"],
                market_home_prob=mkt_prob,
            )
            if not pred.usable:
                return {
                    "game_id": game_id,
                    "error": "no usable prediction",
                    "warnings": pred.warnings,
                }
            predict_mod.save(
                conn, pred,
                matchup=_matchup(conn, game),
                slate_date=game["slate_date"],
            )

            by_game, _ = await _league_markets(conn, client, league)
            markets = by_game.get(game_id, [])
            if finished and not markets:
                # Settled markets are not returned by the `open` query the
                # live path uses, so a completed game would otherwise show
                # no markets at all and grade nothing.
                markets = await _settled_markets_for_game(
                    conn, client, league, game_id
                )
            lg = get_league(league)
            mults: dict[str, Decimal] = {}
            async with KalshiClient(client) as kc:
                for series in (lg.kalshi_game, lg.kalshi_spread, lg.kalshi_total):
                    try:
                        mults[series] = await kc.fee_multiplier(series)
                    except Exception:
                        mults[series] = Decimal(1)

            # In lookback mode every market is rewound to the quotes and the
            # money that stood an hour before tip-off. Scoring then runs
            # through the same `evaluate_market` as a live game: a second
            # implementation would be free to drift, and drift here would
            # look exactly like skill.
            snaps: dict[str, Any] = {}
            if finished and markets:
                snaps = await _snapshots_at(
                    client, league, [m.ticker for m in markets], as_of
                )

            rungs = []
            for m in markets:
                mult = mults.get(m.ticker.split("-")[0], Decimal(1))
                scored = m
                if finished:
                    snap = snaps.get(m.ticker)
                    if snap is None or not snap.usable:
                        continue
                    scored = lookback_mod.market_as_of(m, snap)
                # Both halves of a total market: Kalshi lists only "Over X",
                # so the under is the NO side and would otherwise be
                # unbettable through this tool.
                sides = ("yes", "no") if "TOTAL" in m.ticker else ("yes",)
                for side in sides:
                    r = edge_mod.evaluate_market(
                        pred, scored, side=side,
                        fee_multiplier=mult, contracts=contracts,
                    )
                    if r:
                        rungs.append(r)

            cal = store.get_calibration(conn, league)
            edge_mod.apply_skill_discount(rungs, cal)
            labels = store.team_labels(conn, league)
            gm = money_mod.game_money(rungs)
            outlook = verdict_mod.build_outlook(
                pred, calibration=cal, team_labels=labels, availability=avail,
            )

            # Two passes. The first picks the five markets; the second re-runs
            # with money flow for exactly those, so the direction of the money
            # can sharpen a recommendation without fetching trades for all 60
            # markets on the game.
            bets = verdict_mod.build_bets(
                pred, rungs, calibration=cal, team_labels=labels,
            )
            # The live tape is not fetched for a finished game: it is
            # dominated by in-game trading that nobody could have seen an
            # hour before tip-off, and folding it in would be the one piece
            # of lookahead this whole module exists to prevent.
            if not finished:
                flows = await _flows_for(client, verdict_mod.chosen_tickers(bets))
                if flows:
                    bets = verdict_mod.build_bets(
                        pred, rungs, calibration=cal, team_labels=labels,
                        flows=flows,
                    )

            # Keep the bets with the prediction. They cannot be rebuilt from
            # anything we control: they depend on the Kalshi asks and the
            # money that stood at the cutoff.
            store.record_bets(
                conn, game_id, as_of,
                bets=[
                    {
                        "slot": b.slot, "recommendation": b.recommendation,
                        "bet": b.bet, "ticker": b.ticker, "price": b.price,
                        "all_in_cost": b.all_in_cost, "edge": b.edge,
                        "required_edge": b.required_edge,
                        "dollars_traded": round(b.dollars_traded, 2),
                        "kind": b.kind, "side": b.side,
                        "threshold": b.threshold,
                        "subject_team": b.subject_team,
                    }
                    for b in bets
                ],
                the_call=verdict_mod.the_call(bets),
                money_usd=round(gm.total_traded, 2),
                model_version=predict_mod.MODEL_VERSION,
            )
            store.settle_predictions(conn, league)

            actual = None
            if finished:
                actual = lookback_mod.Actual(
                    home_team=game["home_team"], away_team=game["away_team"],
                    home_label=store.team_label(conn, game["home_team"], short=True),
                    away_label=store.team_label(conn, game["away_team"], short=True),
                    home_score=int(game["home_score"]),
                    away_score=int(game["away_score"]),
                )
            conn.commit()

    return {
        "game_id": game_id,
        "league": league,
        "matchup": (
            f"{store_label(league, pred.away_team)} @ "
            f"{store_label(league, pred.home_team)}"
        ),
        "as_of": as_of,
        "as_of_local": fmt_datetime(as_of),
        "start_local": fmt_datetime(game["start_time"]),

        # 1. the predicted outcome
        "predicted_score": outlook.score_line,
        "predicted_result": outlook.verdict_line,
        "predicted_points": {
            outlook.away_label: outlook.away_points,
            outlook.home_label: outlook.home_points,
        },
        # The same two numbers, keyed by side rather than by name. The dict
        # above is keyed by the model's own display label ("Washington
        # Commanders"), which is not the abbreviation a caller holds from the
        # slate ("WSH"), so indexing it by that abbreviation silently yields
        # nothing. Side is unambiguous and needs no name to agree.
        "predicted_away_points": outlook.away_points,
        "predicted_home_points": outlook.home_points,
        "predicted_winner": outlook.winner,
        "win_probability": outlook.win_prob,
        "predicted_margin": outlook.margin,
        "predicted_total": outlook.total,
        "confidence_in_prediction": outlook.confidence,
        "based_on": outlook.based_on,
        "versus_the_sportsbook": outlook.market_check or None,
        "prediction_cautions": outlook.cautions,

        # 2. the call -- the single thing to do, before any table
        "the_call": verdict_mod.the_call(bets),
        "best_bet": (
            {
                "label": _best.label,
                "bet": _best.bet,
                "kalshi_ticker": _best.ticker,
                "price": _best.price,
                "all_in_cost_after_fees": _best.all_in_cost,
                "edge_after_fees": _best.edge,
                "edge_required_at_this_money": _best.required_edge,
                "stake": verdict_mod.STAKE[_best.recommendation],
                "why": _best.why,
            }
            if (_best := verdict_mod.best_bet(bets)) else None
        ),

        # 3. the five bets, ranked with the actionable ones first
        "headline": verdict_mod.summarise(bets),
        "bets": [
            {
                "slot": b.slot,
                "label": b.label,
                "recommendation": b.recommendation,
                "bet": b.bet if b.actionable else None,
                "why": b.why,
                "kalshi_ticker": b.ticker,
                "price": b.price,
                "all_in_cost_after_fees": b.all_in_cost,
                "model_probability": b.model_prob,
                "sportsbook_probability": b.market_prob,
                "edge_after_fees": b.edge,
                "edge_required_at_this_money": b.required_edge,
                "ev_per_100_contracts_usd": b.ev_per_100,
                "dollars_traded_on_this_market": round(b.dollars_traded, 2),
                "money_note": b.money_note,
                "money_flow": b.flow_note or None,
                "cautions": b.cautions,
                **(
                    lookback_mod.grade_bet(b, actual) if actual else {}
                ),
            }
            for b in bets
        ],

        # 4. the money, which drives the thresholds above
        "kalshi_money": {
            "total_traded_usd": round(gm.total_traded, 2),
            "open_positions_usd": round(gm.total_at_risk, 2),
            "by_bet_type_usd": {k: round(v, 2) for k, v in gm.by_type.items()},
            "markets_counted": gm.markets_counted,
            "busiest_market": gm.busiest_title or None,
            "busiest_market_usd": round(gm.busiest_dollars, 2),
            "tier": gm.tier,
            "summary": gm.summary(),
            "note": (
                "Dollars are contracts traded times price, so they are an "
                "estimate of order of magnitude, not an exact figure. They "
                "set how much edge a bet needs: against deep money the "
                "market is probably right, so the bar rises. Direction of "
                "flow is reported per bet and is corroboration only -- large "
                "money is often wrong."
            ),
        },

        "sportsbook_line_source": line["source"],
        "market_census": verdict_mod.market_census(rungs),
        "census_note": (
            "listed = strikes Kalshi has for this game; quotable = those with "
            "a real price below a dollar; credible = those in the probability "
            "band where the model has resolution. A NO with credible=0 means "
            "there was nothing sensible to bet, not that data was missing."
        ),
        "model_skill": {
            "spread_and_moneyline": cal.get("skill_margin"),
            "total": cal.get("skill_total"),
            "note": (
                "Share of outcome variance the model explains versus simply "
                "predicting the league average, fitted on three seasons. "
                "Edges are scaled by this."
            ),
        },
        "warnings": pred.warnings,
        "scope": (
            "Five bets only: moneyline each way, spread each way, and the "
            "total as over or under. No other market type is evaluated."
        ),

        # 5. lookback: present only for a completed game
        "mode": "lookback" if actual else "pre-game",
        **(
            {
                "result": lookback_mod.grade_prediction(outlook, actual),
                "bets_graded": lookback_mod.summarise_grades(
                    bets, [lookback_mod.grade_bet(b, actual) for b in bets]
                ),
                "lookback_note": (
                    "This is what the tool would have said one hour before "
                    "tip-off. Team history is cut off there by the as-of "
                    "guard, and the Kalshi prices are the asks that were "
                    "actually standing at that moment, taken from hourly "
                    "candles -- not the last trade, so the edge is what you "
                    "could really have bought."
                ),
                "lookback_caveats": [
                    "The injury adjustment is switched off. ESPN publishes "
                    "current availability only, with no historical "
                    "snapshots, so applying today's report to a past game "
                    "would be wrong data rather than a reconstruction. It is "
                    "the one part of the model a lookback cannot test.",
                    "Money flow is not read either: the live tape is "
                    "dominated by in-game trading nobody could have seen "
                    "beforehand. Pre-game dollars come from the candles.",
                    "One game proves nothing in either direction. With a "
                    "typical error of 13 points on a margin, a 20-point miss "
                    "is routine and a close hit is partly luck.",
                ],
            }
            if actual else {}
        ),
    }


async def get_track_record(
    league: str | None = None,
    *,
    since: str | None = None,
    db: str = str(DB_PATH),
) -> dict[str, Any]:
    """How the model has actually done on games it predicted and you opened.

    Built from the `predictions` table, which stores each forecast with the
    final score and the bets as they were recommended. It therefore covers
    **only games whose page was opened before or after they were played** --
    this is a record of real use, not a backtest over every fixture, and a
    run of games you happened to look at is not a random sample.

    Reads nothing but that table, so it keeps working once the games behind
    it have been pruned at season rollover.
    """
    store.init_db(db)
    with store.connect(db) as conn:
        store.settle_predictions(conn, league)
        conn.commit()
        rows = store.settled_predictions(conn, league, since=since)

    if not rows:
        return {
            "league": league or "all",
            "games": 0,
            "note": (
                "Nothing graded yet. A game is added to the record when its "
                "page is opened, and settled once it finishes."
            ),
        }

    margin_errs: list[float] = []
    total_errs: list[float] = []
    winners_right = 0
    winners_called = 0
    placed = 0
    won = 0
    pnl = 0.0
    detail: list[dict[str, Any]] = []

    for r in rows:
        ah, aa = r["actual_home"], r["actual_away"]
        act_margin, act_total = ah - aa, ah + aa
        line: dict[str, Any] = {
            "game_id": r["game_id"],
            "matchup": r["matchup"],
            "date": r["slate_date"] or (r["as_of"] or "")[:10],
            "predicted": (
                f"{r['pred_away_points']:.0f}-{r['pred_home_points']:.0f}"
                if r["pred_home_points"] is not None else None
            ),
            "actual": f"{aa}-{ah}",
        }
        if r["pred_spread"] is not None:
            err = r["pred_spread"] - act_margin
            margin_errs.append(abs(err))
            line["margin_error"] = round(err, 1)
            if act_margin != 0:
                winners_called += 1
                if (r["pred_spread"] > 0) == (act_margin > 0):
                    winners_right += 1
                    line["picked_winner"] = True
                else:
                    line["picked_winner"] = False
        if r["pred_total"] is not None:
            terr = r["pred_total"] - act_total
            total_errs.append(abs(terr))
            line["total_error"] = round(terr, 1)

        graded = []
        for b in json.loads(r["bets"] or "[]"):
            if b.get("recommendation") not in ("BET", "SMALL"):
                continue
            g = lookback_mod.grade_bet(
                _StoredBet(b),
                lookback_mod.Actual(
                    home_team=r["home_team"] or "", 
                    away_team=r["away_team"] or "",
                    home_label="home", away_label="away",
                    home_score=ah, away_score=aa,
                ),
            )
            if g.get("outcome") not in ("WON", "LOST"):
                continue
            placed += 1
            won += g["outcome"] == "WON"
            pnl += g.get("profit_per_100_contracts_usd") or 0.0
            graded.append(
                {"bet": b.get("bet"), "outcome": g["outcome"],
                 "profit_per_100": g.get("profit_per_100_contracts_usd")}
            )
        if graded:
            line["bets"] = graded
        detail.append(line)

    return {
        "league": league or "all",
        "games": len(rows),
        "picked_the_winner": (
            f"{winners_right}/{winners_called}" if winners_called else None
        ),
        "mean_absolute_margin_error": (
            round(sum(margin_errs) / len(margin_errs), 2) if margin_errs else None
        ),
        "mean_absolute_total_error": (
            round(sum(total_errs) / len(total_errs), 2) if total_errs else None
        ),
        "bets_placed": placed,
        "bets_won": won,
        "profit_per_100_contracts_usd": round(pnl, 2),
        "games_detail": detail[:100],
        "note": (
            "Covers only games whose page was opened, so it is a record of "
            "use rather than a backtest -- the games you chose to look at "
            "are not a random sample. Fewer than a few dozen settled games "
            "cannot separate skill from luck in either direction, and a "
            "model with a 13-point typical margin error misses by 20 "
            "routinely. Compare against `model_skill`, which is measured on "
            "three full seasons."
        ),
    }


class _StoredBet:
    """Adapts a stored bet dict to what `lookback.grade_bet` reads."""

    def __init__(self, d: dict[str, Any]):
        self.kind = d.get("kind")
        self.side = d.get("side") or "yes"
        self.threshold = d.get("threshold")
        self.subject_team = d.get("subject_team")
        self.recommendation = d.get("recommendation") or "NO"
        self.all_in_cost = d.get("all_in_cost")
        self.bet = d.get("bet") or ""

    @property
    def actionable(self) -> bool:
        return self.recommendation in ("BET", "SMALL")


def _matchup(conn, game) -> str:
    """"DEN @ MEM", stored with the prediction so the record stays readable
    once the games table has been pruned."""
    return (
        f"{store.team_label(conn, game['away_team'], short=True)} @ "
        f"{store.team_label(conn, game['home_team'], short=True)}"
    )


def store_label(league: str, team_id: str) -> str:
    """Readable team name, used in the matchup string."""
    with store.connect(DB_PATH, readonly=True) as conn:
        return store.team_label(conn, team_id, short=True)


async def get_team_stats(
    league: str, team: str, *, db: str = str(DB_PATH)
) -> dict[str, Any]:
    """Form windows, home/away splits and external ratings for one team."""
    store.init_db(db)
    from ..model import gamelog

    async with cache.make_client() as client:
        with store.connect(db) as conn:
            team_id = matching.resolve_team(conn, league, team)
            if not team_id:
                return {"error": f"could not resolve team {team!r} in {league}"}
            now = asof.now_iso()
            season = store.current_season(conn, league)
            rs = ratings_mod.solve_from_store(
                conn, league, now, season=season, use_cache=False
            )
            tf = gamelog.team_form(conn, team_id, now, rs=rs, season=season)

            external: dict[str, Any] = {}
            for src in sources_for(league):
                try:
                    ratings = await src.team_ratings(client, league)
                except Exception as e:
                    external[src.name] = {"error": type(e).__name__}
                    continue
                row = conn.execute(
                    "SELECT display_name, canonical FROM teams WHERE team_id=?",
                    (team_id,),
                ).fetchone()
                for key in filter(None, [
                    matching._norm(row["display_name"] or "") if row else None,
                    (row["canonical"] or "").lower() if row else None,
                ]):
                    if key in ratings:
                        external[src.name] = _json(ratings[key])
                        break

            espn_power: dict[str, Any] = {}
            try:
                async with _provider().session(client) as ec:
                    power = await ec.power_index(league)
                eid = _espn_team_id(conn, league, team_id)
                if eid in power:
                    espn_power = _json(power[eid])
            except Exception as e:
                espn_power = {"error": type(e).__name__}

    return {
        "team": team_id,
        "league": league,
        "as_of": now,
        "as_of_local": fmt_datetime(now),
        "form": gamelog.describe(tf),
        "our_rating": {
            "net_points_vs_average": rs.rating(team_id),
            "games_in_solve": rs.games_seen(team_id),
            "sparse": rs.is_sparse(team_id),
            "note": (
                "Computed from games before the as-of instant. Used by the "
                "model; published ratings below are cross-checks only, because "
                "their historical form is an end-of-season snapshot."
            ),
        },
        "espn_power_index": espn_power,
        "external_sources": external,
    }


async def get_price_history(
    ticker: str, *, hours: int = 72, interval: int = 60
) -> dict[str, Any]:
    """Price, volume and open-interest history for one market.

    `interval` must be 1, 60 or 1440 minutes -- any other value makes the API
    return an empty list rather than an error.
    """
    parsed = matching.parse_ticker(ticker)
    series = parsed.series if parsed else ticker.split("-")[0]
    async with cache.make_client() as client:
        async with KalshiClient(client) as kc:
            candles = await kc.candlesticks(
                series, ticker, period_interval=interval, lookback_hours=hours
            )
    return {
        "ticker": ticker,
        "interval_minutes": interval,
        "candles": [
            {
                "end_ts": c.end_ts,
                # Kalshi returns epoch seconds, which no reader can interpret.
                "end_local": fmt_datetime(c.end_ts) if c.end_ts else None,
                "close": c.close,
                "mean": c.mean,
                "high": c.high,
                "low": c.low,
                "volume": c.volume,
                "open_interest": c.open_interest,
            }
            for c in candles
        ],
        "n": len(candles),
    }


# Schema definitions shared by both MCP adapters.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_recommendation",
        "description": (
            "THE MAIN TOOL. One verdict each for moneyline, spread (to win "
            "by) and point total: the model's number, what it is based on in "
            "words, the market's number, and whether to bet -- naming a "
            "single Kalshi market and price. Use this to answer 'what should "
            "I bet on this game'."
        ),
        "handler": get_recommendation,
        "schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "enum": list(LEAGUES)},
                "game_id": {"type": "string"},
                "contracts": {"type": "integer", "default": 100},
            },
            "required": ["league", "game_id"],
        },
    },
    {
        "name": "list_games",
        "description": (
            "List games for a league on a date (default today). Past dates "
            "work and are how lookback is done. Shows Kalshi market counts "
            "and the sportsbook line where available."
        ),
        "handler": list_games,
        "schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "enum": list(LEAGUES)},
                "date": {
                    "type": "string",
                    "description": (
                        "YYYY-MM-DD, Central Time. Defaults to today in "
                        "Central."
                    ),
                },
            },
            "required": ["league"],
        },
    },
    {
        "name": "get_prediction",
        "description": (
            "Predicted total points, predicted spread and win probability for "
            "one game, with the form windows, injury assessment and comparison "
            "anchors that produced it."
        ),
        "handler": get_prediction,
        "schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "enum": list(LEAGUES)},
                "game_id": {"type": "string"},
                "include_injuries": {"type": "boolean", "default": True},
            },
            "required": ["league", "game_id"],
        },
    },
    {
        "name": "get_edges",
        "description": (
            "Score every Kalshi spread and total strike for a game against the "
            "model, net of fees, best edge first. Flags implausible edges as "
            "suspect rather than ranking them first."
        ),
        "handler": get_edges,
        "schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "enum": list(LEAGUES)},
                "game_id": {"type": "string"},
                "contracts": {"type": "integer", "default": 100},
            },
            "required": ["league", "game_id"],
        },
    },
    {
        "name": "get_money_flow",
        "description": (
            "Where the big money is on one Kalshi market: notional-weighted "
            "taker imbalance, the largest trades, block trades, and the "
            "open-interest reading."
        ),
        "handler": get_money_flow,
        "schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "lookback_hours": {"type": "integer", "default": 48},
                "halflife_hours": {"type": "number", "default": 12.0},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_team_stats",
        "description": (
            "Form windows (last 3, last 7, season), home/away splits, our "
            "point-in-time rating and external ratings for one team."
        ),
        "handler": get_team_stats,
        "schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "enum": list(LEAGUES)},
                "team": {"type": "string", "description": "abbreviation or name"},
            },
            "required": ["league", "team"],
        },
    },
    {
        "name": "get_track_record",
        "description": (
            "How the model has actually done on games you looked at: "
            "winner accuracy, mean margin and total error, and the win/loss "
            "and profit of every bet it recommended. A record of real use, "
            "not a backtest over all fixtures."
        ),
        "handler": get_track_record,
        "schema": {
            "type": "object",
            "properties": {
                "league": {"type": "string", "enum": list(LEAGUES)},
                "since": {
                    "type": "string",
                    "description": "YYYY-MM-DD, Central. Earliest slate date.",
                },
            },
        },
    },
    {
        "name": "get_price_history",
        "description": "Price, volume and open-interest candles for one market.",
        "handler": get_price_history,
        "schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "hours": {"type": "integer", "default": 72},
                "interval": {"type": "integer", "enum": [1, 60, 1440], "default": 60},
            },
            "required": ["ticker"],
        },
    },
]

# Agent 1's deterministic tools live in their own module and register here,
# so `TOOL_SCHEMAS` stays the single list both MCP adapters iterate. The import
# sits at the bottom because `ingest_tools` reaches back into this module
# (lazily, inside `ingest_slate`) for `list_games`; a top-of-file import would
# be a cycle.
from ..agents.ingest_tools import INGEST_TOOL_SCHEMAS  # noqa: E402

TOOL_SCHEMAS.extend(INGEST_TOOL_SCHEMAS)
