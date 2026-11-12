"""ESPN as a provider -- the reference implementation and the default.

This is a thin adapter, not a rewrite. `clients.espn.EspnClient` already
returns the dataclasses the model reads; the adapter exists so that the call
sites stop naming ESPN directly and start naming *a provider*. That is the
whole point of the seam: `tools.py` asking for `providers.get(name)` can be
pointed somewhere else by configuration, where `tools.py` constructing an
`EspnClient` could not.

Behaviour is identical to v1 by construction -- every method forwards. If this
adapter ever starts transforming data, the v1 and v2 models stop agreeing and
the lookback record carries over a discontinuity nobody can see.
"""

from __future__ import annotations

from typing import Any

from ..clients import espn as espn_mod
from .base import register


class EspnProvider:
    source_name = "espn"
    leagues = {"nfl", "nba", "ncaaf", "ncaamb"}

    def __init__(self, client: Any | None = None):
        self._client = client

    async def scoreboard(self, league_key: str, date: str | None = None) -> list[Any]:
        async with espn_mod.EspnClient(self._client) as ec:
            return await ec.scoreboard(league_key, date)

    async def teams(self, league_key: str) -> list[Any]:
        async with espn_mod.EspnClient(self._client) as ec:
            return await ec.teams(league_key)

    async def team_schedule(
        self, league_key: str, team_id: str, season: int | None = None
    ) -> list[Any]:
        async with espn_mod.EspnClient(self._client) as ec:
            return await ec.team_schedule(league_key, team_id, season)

    async def summary(self, league_key: str, event_id: str) -> dict[str, Any]:
        async with espn_mod.EspnClient(self._client) as ec:
            return await ec.summary(league_key, event_id)


register(EspnProvider())
