"""The game-data provider seam.

v1 hardwired ESPN as the source of schedules, teams, injuries and player game
logs. That is a different seam from `clients.stats`, which only supplies team
*ratings*: swapping ESPN means replacing the core game feed, not a rating
table, so it needs its own interface.

A provider supplies the facts the model treats as given -- who plays whom,
when, where, what the final score was, who is hurt. Everything downstream
(ratings, fit, predict, edge, verdict) consumes those facts and does not care
where they came from.

Deliberately NOT in this interface:

  * Kalshi markets      -- the thing we price against, never a stats source
  * team ratings        -- `clients.stats` already owns that registry
  * name normalisation  -- `matching` owns it, and owns it once, so a new
                           provider inherits the whole alias table for free

A provider declares `source_name`, which is written into the alias table
alongside every name it resolves. That is what lets two providers disagree
about what a team is called without corrupting each other: see the
`scoped:` prefix in `store.resolve_alias`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GameDataProvider(Protocol):
    """What the model needs from a stats source.

    The ESPN adapter is the reference implementation and the shapes it returns
    (`clients.espn.Game`, `.TeamRef`, `.Injury`, `.PlayerGame`) are the
    contract. A second provider converts into those dataclasses rather than
    inventing its own, because the model reads their fields by name.
    """

    source_name: str
    leagues: set[str]

    def session(self, client: Any | None = None) -> Any:
        """An async context manager exposing the methods below.

        Call sites open one session and make several calls inside it, because
        a prediction needs a scoreboard plus a schedule per team plus a
        summary, and reusing one HTTP connection across them matters. A
        provider that cannot be used raises here, at the point of acquisition,
        rather than once per method -- so a blocked provider fails before any
        work is done instead of halfway through a game's analysis.
        """
        ...

    async def scoreboard(self, league_key: str, date: str | None = None) -> list[Any]:
        """Games for a date. `date` is YYYYMMDD; None means the current slate."""
        ...

    async def teams(self, league_key: str) -> list[Any]:
        """Every team the source knows, for alias-table seeding."""
        ...

    async def team_schedule(self, league_key: str, team_id: str, season: int | None = None) -> list[Any]:
        """One team's game log -- the basis of the last-3 and last-7 windows."""
        ...

    async def summary(self, league_key: str, event_id: str) -> dict[str, Any]:
        """Per-game detail, including the injury block."""
        ...


_PROVIDERS: dict[str, Any] = {}


class ProviderUnavailable(RuntimeError):
    """Raised when a provider exists but may not legally or practically be used.

    Distinct from a network error on purpose. A network error is worth
    retrying; this is not, and the message is expected to say what would have
    to change for the provider to become usable.
    """


def register(provider: Any) -> Any:
    _PROVIDERS[provider.source_name] = provider
    return provider


def get(name: str) -> Any:
    try:
        return _PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS)) or "none registered"
        raise KeyError(f"unknown data provider {name!r}; known: {known}") from None


def available() -> dict[str, Any]:
    return dict(_PROVIDERS)


def providers_for(league: str) -> list[Any]:
    return [p for p in _PROVIDERS.values() if league in p.leagues]


def _autoregister() -> None:
    from . import espn, real  # noqa: F401


_autoregister()
