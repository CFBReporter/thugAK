"""TTL cache for HTTP responses, plus the shared async fetch helper.

Kept in its own SQLite file so it can be deleted without touching the game
history. Two properties worth noting:

- Completed games are immutable, so anything derived from them is cached with
  no expiry (`ttl=None`) rather than a long TTL.
- A stale entry is served when the network fails, because a slightly old price
  is more useful than a traceback in the middle of a game-day session. The
  staleness is reported back to the caller so the UI can say so.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import DATA_DIR

CACHE_PATH = DATA_DIR / "cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    body       TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    status     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cache_fetched ON http_cache(fetched_at);
"""

USER_AGENT = "thugsak/0.1 (local research tool)"
DEFAULT_TIMEOUT = 30.0


@dataclass
class Fetched:
    """A response plus where it came from, so callers can surface staleness."""

    data: Any
    from_cache: bool
    age_seconds: float
    stale: bool = False

    @property
    def fresh(self) -> bool:
        return not self.stale


@contextmanager
def _conn(path: Path | str = CACHE_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(_SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def _key(url: str, params: dict[str, Any] | None) -> str:
    raw = url + "?" + json.dumps(params or {}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def get(url: str, params: dict[str, Any] | None = None, ttl: float | None = None
        ) -> tuple[Any, float] | None:
    """Return (body, age_seconds) if cached and within ttl, else None."""
    k = _key(url, params)
    with _conn() as c:
        row = c.execute(
            "SELECT body, fetched_at FROM http_cache WHERE key=?", (k,)
        ).fetchone()
    if not row:
        return None
    age = time.time() - row["fetched_at"]
    if ttl is not None and age > ttl:
        return None
    return json.loads(row["body"]), age


def get_any_age(url: str, params: dict[str, Any] | None = None
                ) -> tuple[Any, float] | None:
    """Cached entry regardless of age -- the fallback when the network fails."""
    return get(url, params, ttl=None)


def put(url: str, params: dict[str, Any] | None, body: Any, status: int = 200) -> None:
    k = _key(url, params)
    with _conn() as c:
        c.execute(
            """INSERT INTO http_cache (key, url, body, fetched_at, status)
               VALUES (?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 body=excluded.body, fetched_at=excluded.fetched_at,
                 status=excluded.status""",
            (k, url, json.dumps(body), time.time(), status),
        )


# Kalshi rate-limits cursor pagination, and this project walks three market
# series per league, so 429s are routine rather than exceptional. Retried with
# exponential backoff, honouring Retry-After when the server sends it.
MAX_RETRIES = 4
BACKOFF_BASE = 1.5
RETRY_STATUS = {429, 500, 502, 503, 504}


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    ttl: float | None = 60,
    allow_stale: bool = True,
    max_retries: int = MAX_RETRIES,
) -> Fetched:
    """GET JSON through the cache, retrying throttles and transient errors.

    On a terminal failure, falls back to any cached copy and marks the result
    stale rather than raising: mid-session a slightly old price beats a
    traceback, and the staleness is reported so the UI can say so.
    """
    hit = get(url, params, ttl)
    if hit is not None:
        body, age = hit
        return Fetched(body, True, age)

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = await client.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            if r.status_code in RETRY_STATUS and attempt < max_retries - 1:
                delay = _retry_delay(r, attempt)
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1 and _is_retryable(e):
                await asyncio.sleep(BACKOFF_BASE**attempt)
                continue
            break
        else:
            put(url, params, body, 200)
            return Fetched(body, False, 0.0)

    if allow_stale:
        stale = get_any_age(url, params)
        if stale is not None:
            body, age = stale
            return Fetched(body, True, age, stale=True)
    raise last_exc if last_exc else RuntimeError(f"failed to fetch {url}")


def _is_retryable(e: Exception) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in RETRY_STATUS
    return isinstance(e, (httpx.TimeoutException, httpx.TransportError))


def _retry_delay(r: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when present, else exponential backoff."""
    header = r.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return BACKOFF_BASE**attempt


async def fetch_text(
    client: httpx.AsyncClient, url: str, *, ttl: float | None = 3600
) -> str:
    """GET a text/CSV body through the same cache (Barttorvik, nflverse)."""
    hit = get(url, None, ttl)
    if hit is not None:
        return hit[0]
    r = await client.get(url, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    text = r.text
    put(url, None, text, 200)
    return text


def make_client(**kw: Any) -> httpx.AsyncClient:
    """An AsyncClient with the settings every caller here wants.

    `follow_redirects` matters: several of these sources (GitHub release
    assets in particular) 302 to a CDN.
    """
    kw.setdefault("headers", {"User-Agent": USER_AGENT, "Accept": "application/json"})
    kw.setdefault("follow_redirects", True)
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    return httpx.AsyncClient(**kw)


def clear(older_than: float | None = None) -> int:
    with _conn() as c:
        if older_than is None:
            n = c.execute("SELECT COUNT(*) n FROM http_cache").fetchone()["n"]
            c.execute("DELETE FROM http_cache")
        else:
            cut = time.time() - older_than
            n = c.execute(
                "SELECT COUNT(*) n FROM http_cache WHERE fetched_at < ?", (cut,)
            ).fetchone()["n"]
            c.execute("DELETE FROM http_cache WHERE fetched_at < ?", (cut,))
    return int(n)


# Bulk history files are enormous relative to everything else: the parsed
# nflverse game table and three seasons of Barttorvik schedules run to
# hundreds of megabytes as JSON. Once imported they live in the game database,
# so keeping them cached buys nothing.
BULK_SIZE_BYTES = 2_000_000
DEFAULT_MAX_BYTES = 150_000_000


def trim(max_bytes: int = DEFAULT_MAX_BYTES, *, drop_bulk: bool = True) -> dict[str, Any]:
    """Keep the cache from growing without bound.

    Drops oversized bulk entries first, then evicts oldest-first until the
    cache is under `max_bytes`. Called at the end of an import, where those
    bulk files are created and immediately no longer needed.
    """
    freed_bulk = 0
    with _conn() as c:
        if drop_bulk:
            cur = c.execute(
                "DELETE FROM http_cache WHERE length(body) > ?", (BULK_SIZE_BYTES,)
            )
            freed_bulk = cur.rowcount

        total = c.execute(
            "SELECT COALESCE(SUM(length(body)), 0) s FROM http_cache"
        ).fetchone()["s"]
        evicted = 0
        if total > max_bytes:
            rows = c.execute(
                "SELECT key, length(body) n FROM http_cache ORDER BY fetched_at ASC"
            ).fetchall()
            for r in rows:
                if total <= max_bytes:
                    break
                c.execute("DELETE FROM http_cache WHERE key=?", (r["key"],))
                total -= r["n"]
                evicted += 1

    # Reclaim the file space, or the cache file stays large on disk.
    conn = sqlite3.connect(CACHE_PATH, timeout=10)
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return {
        "bulk_dropped": freed_bulk,
        "evicted": evicted,
        "size_bytes": CACHE_PATH.stat().st_size if CACHE_PATH.exists() else 0,
    }


def stats() -> dict[str, Any]:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n, MIN(fetched_at) oldest, MAX(fetched_at) newest "
            "FROM http_cache"
        ).fetchone()
    return {
        "entries": row["n"],
        "oldest_age_s": time.time() - row["oldest"] if row["oldest"] else None,
        "newest_age_s": time.time() - row["newest"] if row["newest"] else None,
        "size_bytes": CACHE_PATH.stat().st_size if CACHE_PATH.exists() else 0,
    }
