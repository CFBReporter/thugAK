"""The provider seam.

These tests are about the *seam*, not about any provider's data. The valuable
property is that a blocked provider fails loudly and for a stated reason,
because the alternative -- a provider that returns nothing and lets the model
predict from an empty history -- is exactly the failure v1 hit when
`import_nfl` silently dropped Washington and the Rams. A provider that cannot
work must say so, not return [].
"""

from __future__ import annotations

import asyncio

import pytest

from thugsak import providers
from thugsak.providers import base, real


def test_espn_is_the_default_and_is_registered():
    assert "espn" in providers.available()
    assert providers.get("espn").source_name == "espn"


def test_unknown_provider_names_the_known_ones():
    # A typo in THUGSAK_PROVIDER should say what the options were, not raise a
    # bare KeyError that reads as a missing dict entry somewhere deeper.
    with pytest.raises(KeyError) as exc:
        providers.get("espnn")
    assert "espn" in str(exc.value)


def test_every_provider_covers_the_four_leagues():
    for name, p in providers.available().items():
        assert p.leagues == {"nfl", "nba", "ncaaf", "ncaamb"}, name


def test_every_provider_satisfies_the_protocol():
    for name, p in providers.available().items():
        assert isinstance(p, base.GameDataProvider), name


def test_providers_for_league_filters():
    assert providers.providers_for("nfl")
    assert providers.providers_for("cricket") == []


# --- the Real provider is blocked, and must stay loudly blocked -------------


def test_real_has_no_permission_on_file():
    # The switch that would enable it. If this ever flips, it must be because
    # written permission was obtained and recorded -- see
    # docs/real-data-access-request.md.
    assert real.PERMISSION_ON_FILE is False


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.scoreboard("nfl"),
        lambda p: p.teams("nfl"),
        lambda p: p.team_schedule("nfl", "1"),
        lambda p: p.summary("nfl", "1"),
    ],
)
def test_real_refuses_every_method(call):
    p = providers.get("real")
    with pytest.raises(providers.ProviderUnavailable):
        asyncio.run(call(p))


def test_real_explains_itself_rather_than_failing_blankly():
    p = providers.get("real")
    with pytest.raises(providers.ProviderUnavailable) as exc:
        asyncio.run(p.teams("nfl"))
    msg = str(exc.value)
    # The message has to carry the reason and the remedy, because it is what
    # the UI shows the user on a 503.
    assert "Terms of Use" in msg
    assert "written permission" in msg
    assert "espn" in msg


def test_provider_unavailable_is_not_a_network_error():
    # Distinct type on purpose: a network error is worth retrying and this is
    # not, so nothing should ever wrap it in a retry loop.
    assert issubclass(providers.ProviderUnavailable, RuntimeError)
    assert not issubclass(providers.ProviderUnavailable, OSError)
