"""SharpAPI: current sportsbook lines (point spread and total) per game.

Used because ESPN publishes lines for featured games only -- about 1% of
stored NBA games had one. SharpAPI's free tier (12 requests/minute, two
sportsbooks chosen in its dashboard) covers the NFL, NBA, NCAAF and NCAAB;
its terms allow storing the data and training models on it. The key comes
from the SHARPAPI_KEY environment variable (a GitHub Actions secret) and is
sent as a header, never put in a URL, a log, or the HTTP cache.

Lines are combined across the books returned: the median of each book's main
line, so one book's outlier cannot set the stored number. Spreads are kept in
the sportsbook convention the `games` table uses: the HOME team's line,
negative when the home team is favoured.
"""

from __future__ import annotations

import os
import statistics
from typing import Any

class SharpApiError(RuntimeError):
    pass


BASE = "https://api.sharpapi.io/api/v1"
ENV_VAR = "SHARPAPI_KEY"
LEAGUES = {"nfl": "nfl", "nba": "nba", "ncaaf": "ncaaf", "ncaamb": "ncaab"}
PAGE = 200
MAX_OFFSET = 500   # the API refuses larger offsets on /odds


def api_key() -> str:
    key = os.environ.get(ENV_VAR, "").strip()
    if any(c.isspace() for c in key):
        # A pasted secret with two lines (key plus a label, or the key twice)
        # fails deep in the HTTP layer with an unreadable error. Say what is
        # wrong instead -- without ever printing the value.
        raise SharpApiError(
            f"{ENV_VAR} contains a line break or space; re-save the secret with only the key"
        )
    return key


def available() -> bool:
    return bool(os.environ.get(ENV_VAR, "").strip())


# Free tier: 12 requests a minute per key, shared by every call in a run.
MIN_INTERVAL_SECONDS = 5.2
MAX_PAGES = 25
_last_request = 0.0


async def _get(client: Any, params: dict[str, Any]) -> Any:
    """One paced request; on a 429, wait as told (once) and retry."""
    import asyncio
    import time

    global _last_request
    for attempt in (1, 2):
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()
        r = await client.get(
            f"{BASE}/odds", params=params,
            headers={"X-API-Key": api_key(), "Accept": "application/json"}, timeout=30,
        )
        if r.status_code == 429 and attempt == 1:
            try:
                retry = float(r.headers.get("retry-after", "15"))
            except ValueError:
                retry = 15.0
            await asyncio.sleep(min(max(retry, 5.0), 65.0))
            continue
        if r.status_code == 429:
            raise SharpApiError("rate limited (429)")
        if r.status_code >= 400:
            # The body may explain (bad filter, plan scope); never echo headers.
            raise SharpApiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    raise SharpApiError("rate limited (429)")


async def _odds_rows(client: Any, league: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Main-line spread and total rows for a league's upcoming games.

    No date filter: in practice `date` did not narrow the results (a whole
    week came back for each date asked), so one cursor-paged pass per league
    is both complete and the cheapest in requests.
    """
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    params: dict[str, Any] = {
        "league": LEAGUES[league], "market": "moneyline,point_spread,total_points",
        "is_main_line": "true", "is_live": "false", "limit": PAGE,
    }
    for _ in range(MAX_PAGES):
        body = await _get(client, params)
        rows.extend(body.get("data") or [])
        meta = body.get("meta") or meta
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
        params = {**params, "cursor": cursor}
    return rows, meta


def combine(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per event: median home spread and median total across books,
    plus every book's own quotes (`quotes`: book, market, side, line, odds)
    for line-movement tracking."""
    events: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("is_live") or r.get("is_active") is False:
            continue
        ev = events.setdefault(r.get("event_uuid") or r["event_id"], {
            "home_team": r["home_team"], "away_team": r["away_team"],
            "start_time": r.get("event_start_time"), "spreads": {}, "totals": {}, "quotes": [],
        })
        book = r.get("sportsbook", "?")
        mt = r.get("market_type", "")
        side = (r.get("selection_type") or "").lower()
        sel = str(r.get("selection", ""))
        odds = r.get("odds_american")
        odds = float(odds) if odds is not None else None
        if mt == "moneyline" and odds is not None:
            s = side if side in ("home", "away") else (
                "home" if sel == r["home_team"] else "away" if sel == r["away_team"] else "")
            if s:
                ev["quotes"].append((book, "moneyline", s, None, odds))
            continue
        if r.get("line") is None:
            continue
        if mt == "point_spread" and (side == "home" or sel == r["home_team"]):
            ev["spreads"][book] = float(r["line"])
            ev["quotes"].append((book, "spread", "home", float(r["line"]), odds))
        elif mt == "total_points" and (side == "over" or sel.lower() == "over"):
            ev["totals"][book] = float(r["line"])
            ev["quotes"].append((book, "total", "over", float(r["line"]), odds))
    out = []
    for ev in events.values():
        if not ev["spreads"] and not ev["totals"] and not ev["quotes"]:
            continue
        out.append({
            "home_team": ev["home_team"], "away_team": ev["away_team"], "start_time": ev["start_time"],
            "spread": statistics.median(ev["spreads"].values()) if ev["spreads"] else None,
            "total": statistics.median(ev["totals"].values()) if ev["totals"] else None,
            "books": sorted(set(ev["spreads"]) | set(ev["totals"])),
            "quotes": ev["quotes"],
        })
    return out


async def lines(client: Any, league: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Combined lines for a league's upcoming games."""
    if league not in LEAGUES:
        return [], {}
    rows, meta = await _odds_rows(client, league)
    return combine(rows), meta
