"""Pluggable stats-source registry.

Adding a site means writing one adapter module and registering it here --
nothing else in the project changes, and the MCP tools pick up the new fields
automatically.

An adapter implements:

    name     : short identifier, carried into output so provenance is visible
    leagues  : the league keys it covers
    async def team_ratings(client, league) -> dict[source_name, TeamRating]
    async def games(client, league, season) -> list[dict]   # optional

`team_ratings` keys are whatever the source calls its teams; `matching` maps
them onto canonical ids via the alias table, so an adapter never needs to know
about any other source's naming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TeamRating:
    """Normalised team strength from an external source.

    Offence/defence stay on the source's own scale -- it is not safe to pretend
    Barttorvik's points-per-100-possessions and ESPN's net-points FPI share a
    unit. `net` is the one cross-comparable field: points relative to an
    average opponent.
    """

    source: str
    name: str
    net: float | None = None
    offense: float | None = None
    defense: float | None = None
    tempo: float | None = None
    rank: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class StatsSource(Protocol):
    name: str
    leagues: set[str]

    async def team_ratings(self, client: Any, league: str) -> dict[str, TeamRating]: ...


_REGISTRY: dict[str, Any] = {}


def register(source: Any) -> Any:
    _REGISTRY[source.name] = source
    return source


def sources_for(league: str) -> list[Any]:
    return [s for s in _REGISTRY.values() if league in s.leagues]


def all_sources() -> dict[str, Any]:
    return dict(_REGISTRY)


def _autoregister() -> None:
    from . import barttorvik, nflverse  # noqa: F401


_autoregister()
