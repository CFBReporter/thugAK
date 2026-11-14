"""The in-flight bound on HTTP requests.

Before this, every fetch path was serial -- fine for one game view, hours for
an hourly agent run over a slate. The opposite failure, unbounded gather, turns
a burst of 300 requests into a burst of 429s. These pin the bound itself and
the two places it must NOT apply: a cache hit, and a backoff sleep.

All through a fake client. The real cache file is redirected to tmp so these
never touch data/cache.db.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import cache  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    orig = cache._conn
    target = tmp_path / "cache.db"
    monkeypatch.setattr(cache, "_conn", lambda path=None: orig(target))
    cache._limiters.clear()
    yield
    cache._limiters.clear()
    cache.set_concurrency(cache.MAX_CONCURRENCY)


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {"ok": True}
        self.text = text or "x"
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)

    def json(self):
        return self._body


class _Client:
    """Records how many requests are in flight at once."""

    def __init__(self, *, delay=0.02, statuses=None):
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0
        self.statuses = list(statuses or [])

    async def get(self, url, params=None, timeout=None, follow_redirects=None):
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            status = self.statuses.pop(0) if self.statuses else 200
            return _Resp(status)
        finally:
            self.in_flight -= 1


def _run(coro):
    return asyncio.run(coro)


def test_in_flight_never_exceeds_the_bound():
    cache.set_concurrency(3)
    client = _Client()

    async def go():
        await asyncio.gather(
            *(cache.fetch_json(client, f"http://x/{i}", ttl=0) for i in range(20))
        )

    _run(go())
    assert client.calls == 20
    assert client.peak == 3


def test_bound_is_actually_reached_not_just_respected():
    # A limiter that serialised everything would also "never exceed" the
    # bound. It has to let the full bound run at once.
    cache.set_concurrency(4)
    client = _Client(delay=0.05)

    async def go():
        await asyncio.gather(
            *(cache.fetch_json(client, f"http://y/{i}", ttl=0) for i in range(8))
        )

    _run(go())
    assert client.peak == 4


def test_cache_hit_takes_no_slot():
    cache.set_concurrency(1)
    client = _Client()

    async def go():
        # Prime one URL, then hammer it: every call after the first is a hit
        # and must not even reach the client.
        await cache.fetch_json(client, "http://z/a", ttl=3600)
        await asyncio.gather(
            *(cache.fetch_json(client, "http://z/a", ttl=3600) for _ in range(10))
        )

    _run(go())
    assert client.calls == 1


def test_backoff_sleep_releases_the_slot():
    """A throttled request must not pin a slot while it waits to retry.

    With a bound of 1, if the slot were held across the backoff sleep the
    second request could not start until the first had fully retried, and
    total wall time would be at least the backoff. Here the first request gets
    a 429 then succeeds; the second should run during the first's backoff.
    """
    cache.set_concurrency(1)
    client = _Client(delay=0.01, statuses=[429, 200, 200])

    async def go():
        t0 = asyncio.get_running_loop().time()
        await asyncio.gather(
            cache.fetch_json(client, "http://w/1", ttl=0),
            cache.fetch_json(client, "http://w/2", ttl=0),
        )
        return asyncio.get_running_loop().time() - t0

    elapsed = _run(go())
    # First attempt 429 -> backoff BACKOFF_BASE**0 = 1.0s. Both must finish in
    # about one backoff, not two serialised ones.
    assert client.calls == 3
    assert elapsed < 1.8


def test_fetch_text_is_bounded_too():
    cache.set_concurrency(2)
    client = _Client()

    async def go():
        await asyncio.gather(
            *(cache.fetch_text(client, f"http://t/{i}", ttl=0) for i in range(6))
        )

    _run(go())
    assert client.peak == 2


def test_set_concurrency_rejects_zero():
    with pytest.raises(ValueError):
        cache.set_concurrency(0)


def test_each_event_loop_gets_its_own_semaphore():
    # Streamlit runs each call on a fresh loop; a semaphore bound to a dead
    # loop would raise. Two separate asyncio.run calls must both work.
    cache.set_concurrency(2)
    client = _Client()

    async def go():
        await cache.fetch_json(client, "http://loop/a", ttl=0)

    _run(go())
    _run(go())
    assert client.calls == 2
