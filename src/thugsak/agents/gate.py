"""The change gate, which is really a router.

Running three LLM loops nineteen times a day to mostly restate the previous
hour would cost real money and tell you nothing new. So each hourly run starts
here: a cheap pass -- HTTP and SQL only, no LLM -- computes a **change
signature** for the slate and compares it to the last run's. If nothing
crossed a threshold the run records a `no-op` and exits. If something did, the
diff says *which* agents need to think, so a price move does not trigger a
re-ingest and a new final does not re-read the money.

| Signal                                   | Agents re-run        |
|------------------------------------------|----------------------|
| new finals, injury-status deltas         | ingest, then analyst |
| price / volume / OI moves, new markets   | market, then analyst |
| integrity findings changed               | ingest               |

Everything here is deterministic given its inputs, which is what makes the
"two consecutive quiet runs produce a no-op" property testable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from datetime import datetime, timedelta

from .. import cache, matching, store
from ..config import DB_PATH, league as get_league
from . import ingest_tools

PRICE_THRESHOLD = 0.03     # winner-price move that counts, in dollars (3 cents)
VOLUME_PCT = 0.25          # relative volume / open-interest move that counts
VOLUME_FLOOR = 500.0       # ...but only once the market has at least this much


# --------------------------------------------------------------------------
# collecting the signature
# --------------------------------------------------------------------------

async def fetch_market_state(league: str, date: str, *, db: str = str(DB_PATH)) -> dict[str, Any]:
    """Per-game winner prices, market counts, volume and open interest.

    Goes through `tools._league_markets`, the one place Kalshi events are
    matched to stored games, so the gate and the UI agree on which market
    belongs to which game. Imported lazily to avoid a cycle with the registry.
    """
    from ..mcp import tools as t  # noqa: PLC0415

    out: dict[str, Any] = {"prices": {}, "markets": {}, "volume": {}, "oi": {}}
    store.init_db(db)
    async with cache.make_client() as client:
        with store.connect(db) as conn:
            rows = store.games_on_date(conn, league, date)
            by_game, unmatched = await t._league_markets(conn, client, league)
            # Save this hour's prices: a slate page opens on them instantly
            # and swaps in live ones when Kalshi answers.
            t.save_price_snapshot(conn, t._build_prices(rows, by_game, unmatched, league, date))
            conn.commit()
            for r in rows:
                gid = r["game_id"]
                markets = by_game.get(gid, [])
                if not markets:
                    continue
                winner = [m for m in markets if "GAME" in m.ticker]
                out["prices"][gid] = {
                    "home": t._winner_price(winner, r["home_team"]),
                    "away": t._winner_price(winner, r["away_team"]),
                }
                out["markets"][gid] = len(markets)
                out["volume"][gid] = float(sum(getattr(m, "volume", 0) or 0 for m in markets))
                out["oi"][gid] = float(sum(getattr(m, "open_interest", 0) or 0 for m in markets))
    return out


def db_state(conn, league: str, date: str) -> dict[str, Any]:
    """The database half of the signature: finals, injuries, audit kinds."""
    rows = store.games_on_date(conn, league, date)
    # Yesterday's finals count too: a late tip that ends after midnight is a
    # new final on the previous slate, and it changes tomorrow's form windows.
    prev = (datetime.strptime(date, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y-%m-%d")
    yrows = store.games_on_date(conn, league, prev)
    finals = sorted(r["game_id"] for r in (*rows, *yrows) if r["completed"])
    scheduled = sorted(r["game_id"] for r in rows if not r["completed"])
    injuries = store.latest_injury_status(conn, league)
    return {"finals": finals, "scheduled": scheduled, "injuries": injuries}


def build_signature(
    league: str, date: str, db_part: dict[str, Any], market_part: dict[str, Any],
    audit_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the comparable snapshot. `digest` is a stable hash of it so a
    log line can say "same as last hour" without diffing by hand."""
    # Audit findings reduce to their kinds plus the teams named, which is what
    # a change in them looks like. Informational findings are left out: the
    # thin-history list names ~75 teams early in a season and changes every
    # time one of them plays its third game, which would fire the gate on
    # news the new-finals signal already carries.
    def _teams(f: dict[str, Any]) -> str:
        t = f.get("teams")
        if isinstance(t, dict):
            t = list(t.keys())
        return ",".join(sorted(map(str, t or [])))

    audit = sorted(
        f"{f.get('kind')}:{_teams(f)}"
        for f in audit_findings
        if f.get("severity") != "info"
    )
    sig = {
        "league": league,
        "date": date,
        "finals": db_part["finals"],
        "scheduled": db_part["scheduled"],
        "injuries": db_part["injuries"],
        "prices": market_part.get("prices", {}),
        "markets": market_part.get("markets", {}),
        "volume": market_part.get("volume", {}),
        "oi": market_part.get("oi", {}),
        "audit": audit,
    }
    sig["digest"] = hashlib.sha256(
        json.dumps(sig, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return sig


async def refresh_scores(league: str, date: str, *, db: str = str(DB_PATH)) -> dict[str, int]:
    """Pull today's and yesterday's scoreboards so finals land every hour.

    Without this nothing refreshed a slate after its games ended unless a
    person browsed that date, so yesterday's games sat at ESPN's 0-0
    placeholder -- and the form windows read those as real games. Yesterday
    matters because a late West Coast tip finishes after midnight Central but
    belongs to the previous slate. One ESPN call per date, cached 5 minutes.
    """
    from ..mcp import tools as t  # noqa: PLC0415

    d = datetime.strptime(date, "%Y-%m-%d").date()
    out: dict[str, int] = {}
    async with cache.make_client() as client:
        with store.connect(db) as conn:
            async with t._provider().session(client) as ec:
                for day in (d - timedelta(days=1), d):
                    iso = day.strftime("%Y-%m-%d")
                    try:
                        live = await ec.scoreboard(league, date=iso)
                    except Exception:
                        out[iso] = -1
                        continue
                    # Same writer as the ingest path: only new or changed
                    # games are written, so a quiet hour costs no writes.
                    res = t.store_scoreboard(conn, league, iso, live)
                    # Pages no longer settle predictions; this hourly pass does.
                    store.settle_predictions(conn, league)
                    out[iso] = res["written"]
                    out[f"{iso}_unchanged"] = res["unchanged"]
                    conn.commit()   # before the next date's network call
    return out


async def compute_signature(league: str, date: str, *, db: str = str(DB_PATH)) -> dict[str, Any]:
    await refresh_scores(league, date, db=db)
    market = await fetch_market_state(league, date, db=db)
    audit = await ingest_tools.audit_data_integrity(league, db=db)
    with store.connect(db, readonly=True) as conn:
        part = db_state(conn, league, date)
    return build_signature(league, date, part, market, audit["findings"])


# --------------------------------------------------------------------------
# the diff, and the routing it implies
# --------------------------------------------------------------------------

def _moved(a: float | None, b: float | None, threshold: float) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(a - b) >= threshold


def _rel_moved(a: float, b: float, pct: float, floor: float) -> bool:
    if max(a, b) < floor:
        return False
    base = max(a, 1.0)
    return abs(a - b) / base >= pct


def diff_signatures(
    prev: dict[str, Any] | None,
    cur: dict[str, Any],
    *,
    price_threshold: float = PRICE_THRESHOLD,
    volume_pct: float = VOLUME_PCT,
    volume_floor: float = VOLUME_FLOOR,
) -> dict[str, Any]:
    """What changed, and which agents it means should run.

    No previous signature -- first run of a day, or after a failure -- counts
    as everything changed: better to think once unnecessarily than to sit on a
    slate nobody has looked at.
    """
    if prev is None:
        return {
            "first_run": True,
            "material": True,
            "agents": ["ingest", "market", "analyst"],
            "reasons": ["no previous signature"],
        }

    reasons: list[str] = []
    agents: list[str] = []

    def want(*names: str) -> None:
        for n in names:
            if n not in agents:
                agents.append(n)

    new_finals = sorted(set(cur["finals"]) - set(prev.get("finals", [])))
    if new_finals:
        reasons.append(f"{len(new_finals)} new final(s)")
        want("ingest", "analyst")

    pi, ci = prev.get("injuries", {}), cur.get("injuries", {})
    injury_deltas = {
        aid: {"from": pi.get(aid), "to": st}
        for aid, st in ci.items() if pi.get(aid) != st
    }
    cleared = sorted(set(pi) - set(ci))
    if injury_deltas or cleared:
        reasons.append(f"{len(injury_deltas)} injury status change(s), {len(cleared)} cleared")
        want("ingest", "analyst")

    pp, cp = prev.get("prices", {}), cur.get("prices", {})
    price_moves: dict[str, Any] = {}
    for gid, sides in cp.items():
        before = pp.get(gid, {})
        for side in ("home", "away"):
            if _moved(before.get(side), sides.get(side), price_threshold):
                price_moves.setdefault(gid, {})[side] = {
                    "from": before.get(side), "to": sides.get(side)
                }
    if price_moves:
        reasons.append(f"price moved >= {int(price_threshold * 100)}c on {len(price_moves)} game(s)")
        want("market", "analyst")

    new_markets = sorted(
        gid for gid, n in cur.get("markets", {}).items()
        if n > prev.get("markets", {}).get(gid, 0)
    )
    if new_markets:
        reasons.append(f"new markets listed on {len(new_markets)} game(s)")
        want("market", "analyst")

    vol_moves: list[str] = []
    for key in ("volume", "oi"):
        for gid, v in cur.get(key, {}).items():
            if _rel_moved(prev.get(key, {}).get(gid, 0.0), v, volume_pct, volume_floor):
                vol_moves.append(f"{key}:{gid}")
    if vol_moves:
        reasons.append(f"volume/OI moved >= {int(volume_pct * 100)}% on {len(vol_moves)} market group(s)")
        want("market", "analyst")

    audit_changed = sorted(cur.get("audit", [])) != sorted(prev.get("audit", []))
    if audit_changed:
        reasons.append("data-integrity findings changed")
        want("ingest")

    # Order is the dependency order: ingest before market before analyst.
    order = {"ingest": 0, "market": 1, "analyst": 2}
    agents.sort(key=order.get)

    return {
        "first_run": False,
        "material": bool(agents),
        "agents": agents,
        "reasons": reasons,
        "new_finals": new_finals,
        "injury_deltas": injury_deltas,
        "injuries_cleared": cleared,
        "price_moves": price_moves,
        "new_markets": new_markets,
        "volume_moves": vol_moves,
        "audit_changed": audit_changed,
    }


def last_signature(conn, league: str) -> dict[str, Any] | None:
    """The signature from the most recent run that computed one for this
    league. A failed run has none and must not shadow the last good one."""
    row = conn.execute(
        """SELECT signature FROM agent_runs
           WHERE agent='auto' AND league=? AND signature IS NOT NULL
             AND status IN ('ok', 'no-op', 'dry-run', 'skipped-cooldown', 'rate-limited')
           ORDER BY started_at DESC LIMIT 1""",
        (league,),
    ).fetchone()
    if not row or not row["signature"]:
        return None
    try:
        return json.loads(row["signature"])
    except (TypeError, ValueError):
        return None
