"""Real Sports (realapp.com) as a provider -- blocked pending permission.

This module is deliberately not a working adapter. What follows is what was
found when the source was surveyed on 2026-11-12, so that the next person to
pick this up does not have to repeat it.

WHAT REAL IS
------------
Real Sports App, by Real Vault, Inc (Chicago). A consumer scores/stats/polls/
predictions app -- iOS, Android, and an SEO-oriented marketing site at
www.realapp.com. It covers every league this project needs (NFL, NBA, NCAAF
and NCAAMB) plus MLB, NHL, WNBA, soccer, tennis and golf, so the *coverage*
would be a straight improvement on the current mix of ESPN + Barttorvik +
nflverse.

WHY IT IS NOT WIRED UP
----------------------
1. There is no public API. The app's backend is `mobileweb.realapp.com`:
   `GET /` returns {"success": true} and `GET /health` returns
   {"healthy": true, "realVaultEnvironment": "MOBILEWEBAPI"}. Every other
   path tried returned HTTP 400 {"error":"Bad Request","message":"Invalid
   request"} -- a 400 rather than a 404, i.e. a private endpoint rejecting an
   unauthenticated or unsigned request, not a missing route. There is no
   /openapi.json, no /developers page, no documented plan.

2. The Terms of Use forbid exactly this use. Real Vault, Inc's terms
   (https://docs.realapp.link/policies/terms-of-use, last updated
   2023-10-01) prohibit, in their own words:

     * "You will not access the Product through automated or non-human
       means, whether through a bot, script, or otherwise."
     * "Systematically retrieve data or other content from the Product to
       create or compile, directly or indirectly, a collection, compilation,
       database, or directory without written permission from us."
     * "Engage in any automated use of the system, such as using scripts to
       send comments or messages, or using any data mining, robots, or
       similar data gathering and extraction tools."
     * "use, launch, develop, or distribute any automated system, including
       without limitation, any spider, robot, cheat utility, scraper, or
       offline reader that accesses the Product"

   Note that www.realapp.com/robots.txt is permissive ("Disallow:" with no
   path). That is not a licence -- it governs search-engine crawling of the
   marketing site, and the Terms govern the Product. Where they conflict, the
   Terms are the agreement.

   This project already treats terms as binding: Sports-Reference was
   excluded from v1 for the same reason while Barttorvik was adopted in its
   place. Treating Real differently would be inconsistent, not bolder.

HOW IT BECOMES USABLE
---------------------
The prohibitions above are conditioned on "without written permission from
us", so permission is a real route rather than a formality. Ask Real Vault for
data access -- support@real.vg, or the contact on realsports.io. A startup
that is hiring engineers and publishes an llms.txt for its own docs may well
be receptive to a non-commercial, personal-use request. See
`docs/real-data-access-request.md` for a drafted ask.

If permission arrives, implement this class against whatever they grant --
most likely a keyed API rather than the page scrape this module refuses to
be. The interface to satisfy is `providers.base.GameDataProvider`; the ESPN
adapter alongside is the reference. Convert into the `clients.espn`
dataclasses rather than inventing new shapes, and set `source_name = "real"`
so `matching` scopes the alias table correctly and Real's team spellings
cannot contaminate ESPN's.
"""

from __future__ import annotations

from typing import Any

from .base import ProviderUnavailable, register

#: Flipped to True only once written permission from Real Vault, Inc is in
#: hand AND the methods below are implemented against what was granted. It is
#: not a "disable the safety check" switch -- with the methods unimplemented,
#: setting it True only changes which exception you get.
PERMISSION_ON_FILE = False

_BLOCKED = (
    "The Real Sports provider is not enabled. Real Vault, Inc's Terms of Use "
    "prohibit automated access and systematic data retrieval without their "
    "written permission, and Real publishes no public API "
    "(mobileweb.realapp.com rejects unauthenticated requests with HTTP 400). "
    "See src/thugsak/providers/real.py for the survey and "
    "docs/real-data-access-request.md for a drafted permission request. "
    "The app runs on the 'espn' provider meanwhile."
)


class RealProvider:
    source_name = "real"
    leagues = {"nfl", "nba", "ncaaf", "ncaamb"}

    #: Leagues Real covers that this project does not model yet. Recorded
    #: because it is the upside of the swap, and it is easy to forget.
    additional_coverage = {"mlb", "nhl", "wnba", "soccer", "tennis", "golf"}

    def _blocked(self) -> ProviderUnavailable:
        return ProviderUnavailable(_BLOCKED)

    def session(self, client: Any | None = None) -> Any:
        # Fails here, before any request is attempted, so a misconfigured
        # provider is reported as itself rather than as a prediction that
        # mysteriously has no data behind it.
        raise self._blocked()

    async def scoreboard(self, league_key: str, date: str | None = None) -> list[Any]:
        raise self._blocked()

    async def teams(self, league_key: str) -> list[Any]:
        raise self._blocked()

    async def team_schedule(
        self, league_key: str, team_id: str, season: int | None = None
    ) -> list[Any]:
        raise self._blocked()

    async def summary(self, league_key: str, event_id: str) -> dict[str, Any]:
        raise self._blocked()


register(RealProvider())
