"""The league-wide injuries parse, pinned against both payload shapes.

ESPN's per-game `summary` puts the athlete id at `athlete.id`. The league-wide
`/injuries` endpoint omits that field entirely; the id appears only inside the
player-card URL in `links`. The injury model reads `summary`, so the mismatch
went unnoticed until the first unattended ingest produced zero snapshot rows
from 800 injuries -- every row was dropped for having no athlete id.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.clients import espn as E  # noqa: E402

LEAGUE_WIDE = {
    "timestamp": "2026-11-13T15:00Z",
    "injuries": [
        {
            "id": "22",
            "displayName": "Arizona Cardinals",
            "injuries": [
                {
                    "id": "647473",
                    "status": "Questionable",
                    "athlete": {
                        "displayName": "Hjalte Froholdt",
                        "position": {"abbreviation": "C"},
                        "links": [
                            {"rel": ["playercard"], "href": "https://www.espn.com/nfl/player/_/id/3886633/hjalte-froholdt"},
                            {"rel": ["stats"], "href": "https://www.espn.com/nfl/player/stats/_/id/3886633/hjalte-froholdt"},
                        ],
                    },
                    "details": {"type": "Concussion", "detail": "Concussion", "returnDate": "2026-11-15"},
                },
                {
                    "id": "647474",
                    "status": "Out",
                    "athlete": {"displayName": "No Links Guy", "position": {"abbreviation": "WR"}},
                    "details": {"type": "Knee"},
                },
            ],
        }
    ],
}

SUMMARY_SHAPE = {
    "injuries": [
        {
            "abbreviation": "BOS",
            "injuries": [
                {"status": "Out", "athlete": {"id": "4066", "displayName": "Jay T", "position": {"abbreviation": "F"}}},
            ],
        }
    ],
}


class _Client:
    def __init__(self, payload):
        self.payload = payload

    async def _get(self, url, params=None, ttl=None):
        return self.payload


def _injuries(payload, league):
    ec = E.EspnClient.__new__(E.EspnClient)
    ec._get = _Client(payload)._get  # type: ignore[attr-defined]
    return asyncio.run(E.EspnClient.injuries(ec, league))


def test_id_is_recovered_from_the_player_card_href():
    out = _injuries(LEAGUE_WIDE, "nfl")
    items = out["ARIZONA CARDINALS"]
    assert items[0].athlete_id == "3886633"
    assert items[0].name == "Hjalte Froholdt"
    assert items[0].position == "C"
    assert items[0].status == "Questionable"


def test_detail_carries_the_injury_and_return_date():
    out = _injuries(LEAGUE_WIDE, "nfl")
    assert out["ARIZONA CARDINALS"][0].detail == "Concussion (return 2026-11-15)"
    assert out["ARIZONA CARDINALS"][1].detail == "Knee"


def test_athlete_with_no_id_anywhere_yields_empty_not_a_crash():
    out = _injuries(LEAGUE_WIDE, "nfl")
    assert out["ARIZONA CARDINALS"][1].athlete_id == ""


def test_summary_shape_with_athlete_id_still_works():
    out = _injuries(SUMMARY_SHAPE, "nba")
    assert out["BOS"][0].athlete_id == "4066"


def test_href_parser_is_strict_about_the_id_segment():
    assert E._athlete_id({"links": [{"href": "https://x/player/_/id/12345/name"}]}) == "12345"
    assert E._athlete_id({"links": [{"href": "https://x/player/_/id/12345"}]}) == "12345"
    assert E._athlete_id({"links": [{"href": "https://x/team/_/name/ari"}]}) == ""
    assert E._athlete_id({"id": 77, "links": [{"href": "https://x/_/id/999/"}]}) == "77"
