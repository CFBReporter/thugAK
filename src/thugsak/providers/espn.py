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

    def session(self, client: Any | None = None) -> Any:
        """The v1 client itself.

        `EspnClient` is already an async context manager carrying exactly the
        methods the protocol names, so the seam costs nothing here: call sites
        that said `EspnClient(client)` now say `provider.session(client)` and
        get the identical object. That is why wiring the seam cannot change
        any number the model produces.
        """
        return espn_mod.EspnClient(client if client is not None else self._client)

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
