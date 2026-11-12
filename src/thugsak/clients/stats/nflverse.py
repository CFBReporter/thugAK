"""nflverse: NFL game history with closing lines, plus team-week EPA.

Two reasons this source is here rather than relying on ESPN for NFL:

1. ``games.csv.gz`` carries **historical closing lines** (``spread_line``,
   ``total_line``, ``home_moneyline``). That makes NFL the only league where a
   lookback can show predicted / actual / market side by side -- everywhere
   else there is no free historical line source.
2. ``stats_team_week_{year}.csv`` carries team EPA, which predicts future
   scoring better than points-per-game does.

One packaging quirk, hit during research: the plain ``.csv`` asset returned 404
while ``.csv.gz`` at the same path served fine. Asset URLs are therefore
resolved through the GitHub releases API rather than hardcoded, and the
gzipped variant is preferred (it is also 4x smaller).
"""

from __future__ import annotations

import csv
import gzip
import io
from typing import Any

import httpx

from ... import cache
from . import TeamRating, register

RELEASES_API = "https://api.github.com/repos/nflverse/nflverse-data/releases/tags"
DOWNLOAD = "https://github.com/nflverse/nflverse-data/releases/download"


async def asset_url(client: httpx.AsyncClient, tag: str, filename: str) -> str:
    """Resolve a release asset's download URL via the API.

    Hardcoding the path works until it doesn't; asking the API means a renamed
    or re-tagged asset surfaces as a clear error instead of a 404 body being
    parsed as CSV.
    """
    res = await cache.fetch_json(client, f"{RELEASES_API}/{tag}", ttl=21_600)
    for a in res.data.get("assets") or []:
        if a.get("name") == filename:
            return a.get("browser_download_url") or f"{DOWNLOAD}/{tag}/{filename}"
    raise FileNotFoundError(
        f"nflverse release {tag!r} has no asset {filename!r}; "
        f"available: {[a.get('name') for a in res.data.get('assets') or []]}"
    )


async def _fetch_csv_rows(
    client: httpx.AsyncClient, url: str, *, gz: bool
) -> list[dict[str, str]]:
    """Fetch a CSV (optionally gzipped) and return it as dict rows."""
    key = f"{url}#parsed"
    hit = cache.get(key, None, ttl=21_600)
    if hit is not None:
        return hit[0]

    r = await client.get(url, timeout=180.0, follow_redirects=True)
    r.raise_for_status()
    if gz:
        text = gzip.decompress(r.content).decode("utf-8", errors="replace")
    else:
        text = r.content.decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    cache.put(key, None, rows)
    return rows


def _i(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Nflverse:
    name = "nflverse"
    leagues = {"nfl"}

    async def games(
        self,
        client: httpx.AsyncClient,
        league: str = "nfl",
        seasons: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Completed NFL games, with closing lines where present.

        `spread_line` is nflverse's home-relative spread in the *favourite's*
        convention it documents: a positive value means the home team is
        favoured by that many points. It is negated here so the stored
        `close_spread` matches the sportsbook convention used elsewhere in
        this project (negative = home favoured).
        """
        url = await asset_url(client, "schedules", "games.csv.gz")
        rows = await _fetch_csv_rows(client, url, gz=True)
        out: list[dict[str, Any]] = []
        for r in rows:
            season = _i(r.get("season"))
            if season is None or (seasons and season not in seasons):
                continue
            hs, as_ = _i(r.get("home_score")), _i(r.get("away_score"))
            gameday, gametime = r.get("gameday") or "", r.get("gametime") or "00:00"
            if not gameday:
                continue
            spread = _f(r.get("spread_line"))
            out.append(
                {
                    "source": "nflverse",
                    "game_id": f"nfl:{r.get('game_id')}",
                    "season": season,
                    "season_type": "regular" if r.get("game_type") == "REG" else "post",
                    "start_time": f"{gameday}T{gametime}:00+00:00",
                    "home_abbrev": (r.get("home_team") or "").upper(),
                    "away_abbrev": (r.get("away_team") or "").upper(),
                    "home_score": hs,
                    "away_score": as_,
                    "completed": hs is not None and as_ is not None,
                    "neutral_site": 1 if (r.get("location") or "") == "Neutral" else 0,
                    "close_spread": -spread if spread is not None else None,
                    "close_total": _f(r.get("total_line")),
                    "close_home_ml": _i(r.get("home_moneyline")),
                    "close_away_ml": _i(r.get("away_moneyline")),
                    "week": _i(r.get("week")),
                    "roof": r.get("roof"),
                    "div_game": _i(r.get("div_game")),
                }
            )
        return out

    async def team_week_stats(
        self, client: httpx.AsyncClient, season: int
    ) -> list[dict[str, Any]]:
        """Team-week EPA and efficiency for one season."""
        url = await asset_url(client, "stats_team", f"stats_team_week_{season}.csv")
        rows = await _fetch_csv_rows(client, url, gz=False)
        keep = (
            "season", "week", "team", "opponent_team", "passing_epa", "rushing_epa",
            "passing_yards", "rushing_yards", "passing_tds", "rushing_tds",
            "passing_interceptions",
        )
        out = []
        for r in rows:
            row = {k: r.get(k) for k in keep if k in r}
            row["team"] = (row.get("team") or "").upper()
            out.append(row)
        return out

    async def team_ratings(
        self, client: httpx.AsyncClient, league: str, *, season: int | None = None
    ) -> dict[str, TeamRating]:
        """Season-to-date EPA per team, as a stats-registry rating.

        EPA is reported on its own scale (expected points added per game), not
        converted to a net-points rating -- combining it with a differently
        derived number would imply a precision that is not there. It is shown
        as supporting evidence, and `net` stays None.
        """
        if league not in self.leagues:
            return {}
        from ...config import league as get_league

        season = season or get_league("nfl").fpi_season
        try:
            rows = await self.team_week_stats(client, season)
        except (FileNotFoundError, httpx.HTTPError):
            return {}

        agg: dict[str, dict[str, float]] = {}
        for r in rows:
            t = r.get("team") or ""
            if not t:
                continue
            a = agg.setdefault(t, {"pass_epa": 0.0, "rush_epa": 0.0, "n": 0.0})
            a["pass_epa"] += _f(r.get("passing_epa")) or 0.0
            a["rush_epa"] += _f(r.get("rushing_epa")) or 0.0
            a["n"] += 1

        out: dict[str, TeamRating] = {}
        for t, a in agg.items():
            n = max(a["n"], 1.0)
            out[t.lower()] = TeamRating(
                source=self.name,
                name=t,
                net=None,
                extra={
                    "epa_per_game": (a["pass_epa"] + a["rush_epa"]) / n,
                    "pass_epa_per_game": a["pass_epa"] / n,
                    "rush_epa_per_game": a["rush_epa"] / n,
                    "games": int(n),
                    "season": season,
                },
            )
        return out


register(Nflverse())
