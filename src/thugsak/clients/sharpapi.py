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


async def _odds_rows(client: Any, league: str, date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Main-line spread and total rows for one league and date (all pages)."""
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    offset = 0
    while offset <= MAX_OFFSET:
        r = await client.get(
            f"{BASE}/odds",
            params={
                "league": LEAGUES[league], "date": date, "market": "point_spread,total_points",
                "is_main_line": "true", "is_live": "false", "limit": PAGE, "offset": offset,
            },
            headers={"X-API-Key": api_key(), "Accept": "application/json"},
            timeout=30,
        )
        if r.status_code == 429:
            raise SharpApiError("rate limited (429)")
        if r.status_code >= 400:
            # The body may explain (bad filter, plan scope); never echo headers.
            raise SharpApiError(f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        page = body.get("data") or []
        rows.extend(page)
        meta = body.get("meta") or meta
        pag = body.get("pagination") or {}
        if not pag.get("has_more") and len(page) < PAGE:
            break
        offset += PAGE
    return rows, meta


def combine(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line per event: median home spread and median total across books."""
    events: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("is_live") or r.get("is_active") is False or r.get("line") is None:
            continue
        ev = events.setdefault(r.get("event_uuid") or r["event_id"], {
            "home_team": r["home_team"], "away_team": r["away_team"],
            "start_time": r.get("event_start_time"), "spreads": {}, "totals": {},
        })
        book = r.get("sportsbook", "?")
        mt = r.get("market_type", "")
        side = (r.get("selection_type") or "").lower()
        if mt == "point_spread" and (side == "home" or r.get("selection") == r["home_team"]):
            ev["spreads"][book] = float(r["line"])
        elif mt == "total_points" and (side == "over" or str(r.get("selection", "")).lower() == "over"):
            ev["totals"][book] = float(r["line"])
    out = []
    for ev in events.values():
        if not ev["spreads"] and not ev["totals"]:
            continue
        out.append({
            "home_team": ev["home_team"], "away_team": ev["away_team"], "start_time": ev["start_time"],
            "spread": statistics.median(ev["spreads"].values()) if ev["spreads"] else None,
            "total": statistics.median(ev["totals"].values()) if ev["totals"] else None,
            "books": sorted(set(ev["spreads"]) | set(ev["totals"])),
        })
    return out


async def lines(client: Any, league: str, date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Combined lines for a league's games on `date` (YYYY-MM-DD)."""
    if league not in LEAGUES:
        return [], {}
    rows, meta = await _odds_rows(client, league, date)
    return combine(rows), meta
