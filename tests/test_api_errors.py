"""The bridge's translation of engine refusals into HTTP.

The bug these cover: the engine answers an unanswerable request by *returning*
a dict with an "error" key rather than raising, because its original caller was
Claude reading prose. Over HTTP that arrives as a 200 whose body has none of
the fields the detail view reads, so the browser rendered an empty page with
nothing on it saying why.

The first version of the promotion required ``len(result) == 1``, which caught
the bare ``{"error": ...}`` and missed the form the recommendation engine
actually returns -- ``{"game_id", "error", "warnings"}`` -- so the real case
sailed straight through. That is the regression worth pinning: not "an error
becomes an error", but "an error with explanatory baggage attached still
becomes an error, and keeps the baggage".

`asyncio.run` rather than pytest-asyncio, matching the rest of the suite --
the project does not depend on that plugin and these tests are not a reason to
add one.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from api import main as api_main


async def _ok(**_kw):
    return {"game_id": "x", "the_call": "Bet it", "bets": []}


async def _bare_error(**_kw):
    return {"error": "unknown game_id 'ncaamb:espn:000000'"}


async def _declined(**_kw):
    # The shape `get_recommendation` returns for a game it will not call.
    return {
        "game_id": "ncaamb:espn:401911471",
        "error": "no usable prediction",
        "warnings": [
            "Thin history (ncaamb:SJSU: 0 games, ncaamb:UCR: 1); the "
            "estimate leans on shrunk ratings.",
            "No usable scoring history for one or both teams.",
        ],
    }


def _status(fn) -> int:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_main._call(fn))
    return exc.value.status_code


def _detail(fn) -> str:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_main._call(fn))
    return str(exc.value.detail)


def test_a_normal_result_passes_through():
    assert asyncio.run(api_main._call(_ok)) == {
        "game_id": "x",
        "the_call": "Bet it",
        "bets": [],
    }


def test_bare_error_is_404():
    # Nothing but an error means the reference itself was no good.
    assert _status(_bare_error) == 404


def test_declined_prediction_is_not_a_200():
    # The whole point. A 200 here is what produced the blank screen.
    assert _status(_declined) != 200


def test_declined_prediction_is_422_not_404():
    # 404 would say the game does not exist, which is false and sends the
    # reader looking for a bad link. The game is real; the model declined.
    assert _status(_declined) == 422


def test_the_reasons_survive_the_translation():
    # The warnings are the only explanation the user gets, so losing them
    # would leave a message as unhelpful as the blank page it replaced.
    detail = _detail(_declined)
    assert "no usable prediction" in detail
    assert "Thin history" in detail
    assert "No usable scoring history" in detail


def test_promotion_does_not_depend_on_the_number_of_keys():
    """The actual regression guard.

    An error dict must be promoted however many other fields ride along, so
    that a future field added to the engine's refusal cannot quietly restore
    the 200.
    """

    async def fat_error(**_kw):
        return {
            "error": "no usable prediction",
            "game_id": "g",
            "warnings": ["because"],
            "league": "ncaamb",
            "as_of": "2026-11-12T19:00:00",
            "something_added_later": True,
        }

    assert _status(fat_error) == 422
