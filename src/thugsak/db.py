"""Which database the game store lives in, and a Postgres connection that
looks like the sqlite3 one the rest of the code already uses.

The switch is one setting, `THUGSAK_DATABASE_URL`:

- unset or empty  -> SQLite at `config.DB_PATH`, exactly as before
- a postgres URL  -> that database (Supabase in production)

It is read from the environment first, then from a `.env` file at the project
root, so the URL never has to be committed and the localhost folder (which has
no `.env`) keeps running on SQLite untouched.

`PgConnection` exists so the ~100 call sites written against sqlite3 do not all
change at once. It accepts sqlite's `?` placeholders and returns rows that can
be read by column name *or* position, like `sqlite3.Row`. What it does not do
is translate SQLite-only SQL (`julianday`, `rowid`, `INSERT OR REPLACE`, ...);
that is phase 3, statement by statement, where it can be tested.

The HTTP cache (`cache.py`) stays on local SQLite regardless: it is disposable,
per-machine, and large.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Iterable, Iterator, Sequence

from .config import PROJECT_ROOT

ENV_VAR = "THUGSAK_DATABASE_URL"
ENV_FILE = PROJECT_ROOT / ".env"
# Optional Postgres schema to work in instead of `public`. Production leaves
# it unset; the test harness sets one per test so tests never share tables
# and never touch the real data.
SCHEMA_ENV_VAR = "THUGSAK_PG_SCHEMA"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _read_env_file(path=None) -> dict[str, str]:
    """Minimal KEY=VALUE reader. No dependency for one line of config.

    `ENV_FILE` is looked up at call time, not bound as a default argument,
    so it can be redirected (tests, a deploy that keeps config elsewhere).
    """
    path = ENV_FILE if path is None else path
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def database_url() -> str:
    """The configured Postgres URL, or "" for SQLite.

    An environment variable wins over `.env`, including an explicitly empty
    one -- `THUGSAK_DATABASE_URL=` forces SQLite even where a `.env` exists.
    """
    if ENV_VAR in os.environ:
        return os.environ[ENV_VAR].strip()
    return _read_env_file().get(ENV_VAR, "").strip()


def backend() -> str:
    url = database_url()
    if not url:
        return "sqlite"
    if url.startswith(("postgres://", "postgresql://")):
        return "postgres"
    raise ValueError(
        f"{ENV_VAR} must be a postgres:// or postgresql:// URL, or empty for SQLite"
    )


def target_key() -> str:
    """Identifies the database a connection would reach -- URL plus schema --
    so per-process caches (e.g. "migrations already applied") are scoped to
    it. The schema part lets the test harness give each test its own."""
    return f"{database_url()}#{os.environ.get(SCHEMA_ENV_VAR, '')}"


def redacted(url: str | None = None) -> str:
    """The URL with its password masked, safe for logs and error messages."""
    import re

    u = database_url() if url is None else url
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1********@", u)


# --------------------------------------------------------------------------
# placeholder translation
# --------------------------------------------------------------------------

def translate_placeholders(sql: str) -> str:
    """sqlite `?` placeholders -> psycopg `%s`.

    Only outside string literals: `'what?'` is data, not a parameter. And every
    literal `%` is doubled, because once parameters are passed psycopg treats a
    bare `%` anywhere in the statement as the start of a placeholder -- a
    `LIKE '%jose%'` would otherwise raise.
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            # '' inside a literal is an escaped quote, not the end.
            if in_str and i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_str = not in_str
            out.append(c)
        elif c == "%":
            out.append("%%")
        elif c == "?" and not in_str:
            out.append("%s")
        else:
            out.append(c)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# a sqlite3-shaped Postgres connection
# --------------------------------------------------------------------------

class Row:
    """Readable by name or position, iterable over values, `dict()`-able --
    the parts of `sqlite3.Row` this codebase relies on."""

    __slots__ = ("_keys", "_values", "_index")

    def __init__(self, keys: Sequence[str], values: Sequence[Any]):
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._index = {k: i for i, k in enumerate(self._keys)}

    def keys(self) -> list[str]:
        return list(self._keys)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._index[key]]
        except KeyError:
            raise IndexError(f"No item with that key: {key!r}") from None

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._keys == other._keys and self._values == other._values
        return NotImplemented

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._keys, self._values))!r})"


class PgCursor:
    def __init__(self, cur: Any):
        self._cur = cur

    def _row(self, values: Any) -> Row | None:
        if values is None:
            return None
        names = [d.name for d in (self._cur.description or [])]
        return Row(names, values)

    def fetchone(self) -> Row | None:
        if self._cur.description is None:
            return None
        return self._row(self._cur.fetchone())

    def fetchall(self) -> list[Row]:
        if self._cur.description is None:
            return []
        names = [d.name for d in self._cur.description]
        return [Row(names, v) for v in self._cur.fetchall()]

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> None:
        # Postgres has no implicit row id. Inserts that need the new key use
        # RETURNING (phase 3); returning None here fails loudly at the caller
        # rather than handing back a wrong id.
        return None

    @property
    def description(self) -> Any:
        return self._cur.description


# --------------------------------------------------------------------------
# write notifications
# --------------------------------------------------------------------------
#
# The store keeps short-lived in-process caches of remote reads (a team's
# game history, calibration). Every write statement on a Postgres connection
# is reported here -- and again on commit -- so those caches drop the tables
# it touched. Hooked at the adapter, not in each store function, so raw SQL
# written anywhere (tests, scripts, the ingest tools) cannot leave a stale
# read behind in this process. Writes from other processes are bounded by the
# caches' TTL instead.

_WRITE_RE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE|COPY)\b", re.I)
_WRITE_LISTENERS: list[Callable[[str], None]] = []


def on_write(fn: Callable[[str], None]) -> None:
    """Register `fn(sql)` to run after any write statement or commit."""
    if fn not in _WRITE_LISTENERS:
        _WRITE_LISTENERS.append(fn)


def _notify(sql: str) -> None:
    for fn in _WRITE_LISTENERS:
        fn(sql)


class PgConnection:
    """Wraps a psycopg connection with the sqlite3 surface the store uses."""

    def __init__(self, conn: Any, pool: Any = None):
        self._conn = conn
        self._pool = pool
        self._writes: list[str] = []

    def _wrote(self, sql: str) -> None:
        if _WRITE_RE.match(sql):
            self.__dict__.setdefault("_writes", []).append(sql)
            _notify(sql)

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> PgCursor:
        self._wrote(sql)
        cur = self._conn.cursor()
        params = tuple(params) if params is not None else ()
        if params:
            cur.execute(translate_placeholders(sql), params)
        else:
            # No parameters means psycopg does no % processing, so the SQL
            # goes through exactly as written.
            cur.execute(sql)
        return PgCursor(cur)

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> PgCursor:
        self._wrote(sql)
        cur = self._conn.cursor()
        cur.executemany(translate_placeholders(sql), [tuple(p) for p in seq])
        return PgCursor(cur)

    def executescript(self, script: str) -> None:
        # psycopg runs a multi-statement string in one call when it carries
        # no parameters, which a schema script never does.
        self.__dict__.setdefault("_writes", []).append(script)
        _notify(script)
        self._conn.execute(script)

    def commit(self) -> None:
        self._conn.commit()
        # Again after commit: a read in another connection between the write
        # and the commit could have re-cached the old rows.
        writes, self._writes = self.__dict__.get("_writes", []), []
        for sql in writes:
            _notify(sql)

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._pool is not None:
            # Back to the pool for the next request. Read-only work leaves a
            # transaction open; end it here so the pool does not log a
            # "rolling back returned connection" line for every page view.
            try:
                if not self._conn.closed and self._conn.info.transaction_status != 0:
                    self._conn.rollback()
            except Exception:
                pass  # a broken connection; the pool discards it
            self._pool.putconn(self._conn)
        else:
            self._conn.close()

    @property
    def raw(self) -> Any:
        """The underlying psycopg connection, for the few places that need
        Postgres-native behaviour (COPY, RETURNING) in later phases."""
        return self._conn


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------

MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "postgres"


def migration_files(directory=None) -> list:
    """`NNNN_name.sql` files in apply order."""
    d = MIGRATIONS_DIR if directory is None else directory
    return sorted(p for p in d.glob("*.sql") if p.name[:4].isdigit())


def apply_migrations(conn: PgConnection, directory=None) -> list[str]:
    """Apply every migration not yet recorded, in order. Returns their names.

    Each file runs in its own transaction together with its bookkeeping row,
    so a failure part-way through a file leaves nothing behind and the next
    run retries the same file. Safe to call on every start-up.
    """
    from datetime import datetime, timezone

    raw = conn.raw
    raw.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version    TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )
    raw.execute("ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY")
    raw.commit()
    done = {r[0] for r in raw.execute("SELECT version FROM schema_migrations")}
    raw.commit()

    applied: list[str] = []
    for path in migration_files(directory):
        version = path.stem
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            raw.execute(sql)
            raw.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        applied.append(version)
    return applied


# --------------------------------------------------------------------------
# connection pool
# --------------------------------------------------------------------------
#
# Opening a connection to the hosted pooler costs ~0.5-0.9s (TLS + auth over
# the network), and one page load opens several. The pool keeps a few open
# per database+schema and hands them out; each is health-checked on checkout
# because a pooler can drop idle connections.

POOL_MAX = int(os.environ.get("THUGSAK_PG_POOL_MAX", "5"))

import atexit as _atexit

_atexit.register(lambda: close_pools())
_POOLS: dict[str, Any] = {}


def _pool_for(target: str, schema: str) -> Any:
    from psycopg_pool import ConnectionPool

    key = f"{target}#{schema}"
    pool = _POOLS.get(key)
    if pool is not None:
        return pool

    def configure(conn: Any) -> None:
        if schema:
            from psycopg import sql as pgsql

            conn.execute(pgsql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(pgsql.Identifier(schema)))
            conn.execute(pgsql.SQL("SET search_path TO {}").format(pgsql.Identifier(schema)))
            conn.commit()

    def reconnect_failed(failed: Any) -> None:
        # The pool gave up re-opening a dropped connection (network or DNS
        # outage longer than reconnect_timeout). psycopg_pool does not retry
        # after that, so forget this pool; the next request builds a new one.
        # Without this the API answered 500 for three days after one outage.
        if _POOLS.get(key) is failed:
            _POOLS.pop(key, None)

    pool = ConnectionPool(
        target,
        min_size=1,
        max_size=POOL_MAX,
        kwargs={"prepare_threshold": None, "connect_timeout": 15},
        configure=configure,
        check=ConnectionPool.check_connection,
        max_idle=300,
        timeout=15,
        reconnect_timeout=60,
        reconnect_failed=reconnect_failed,
        name="thugsak",
        open=True,
    )
    _POOLS[key] = pool
    return pool


def _discard_pool(key: str) -> None:
    pool = _POOLS.pop(key, None)
    if pool is not None:
        try:
            pool.close(timeout=0)
        except Exception:
            pass


def close_pools() -> None:
    for p in list(_POOLS.values()):
        try:
            p.close()
        except Exception:
            pass
    _POOLS.clear()


def connect_postgres(
    url: str | None = None, *, connect_timeout: int = 15, pooled: bool | None = None
) -> PgConnection:
    """A connection to the configured database.

    Pooled by default when connecting to the configured URL; an explicit `url`
    (a one-off, such as a credentials check) gets a dedicated connection.
    """
    import psycopg  # imported here so SQLite-only installs need no driver

    if pooled is None:
        pooled = url is None
    if pooled:
        target = database_url()
        if not target:
            raise RuntimeError(f"{ENV_VAR} is not set")
        schema = os.environ.get(SCHEMA_ENV_VAR, "").strip()
        exc: Exception | None = None
        # Two attempts: if the pool cannot hand out a connection (every slot
        # lost to an outage that has since ended), a fresh pool usually can.
        for _attempt in range(2):
            try:
                pool = _pool_for(target, schema)
                return PgConnection(pool.getconn(), pool)
            except Exception as e:
                exc = e
                _discard_pool(f"{target}#{schema}")
        if exc is not None:
            raise RuntimeError(
                f"could not get a connection to {redacted(target)}: "
                f"{type(exc).__name__}: {str(exc).replace(target, redacted(target))}"
            ) from None

    target = url or database_url()
    if not target:
        raise RuntimeError(f"{ENV_VAR} is not set")
    try:
        conn = psycopg.connect(
            target,
            connect_timeout=connect_timeout,
            # Supabase's pooler can sit in transaction mode, which does not
            # support server-side prepared statements.
            prepare_threshold=None,
        )
    except psycopg.OperationalError as exc:
        # The driver's message can include the URL; never let a password
        # reach a log or a traceback.
        raise RuntimeError(f"could not connect to {redacted(target)}: "
                           f"{str(exc).replace(target, redacted(target))}") from None
    schema = os.environ.get(SCHEMA_ENV_VAR, "").strip()
    if schema:
        from psycopg import sql as pgsql

        conn.execute(pgsql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(pgsql.Identifier(schema)))
        conn.execute(pgsql.SQL("SET search_path TO {}").format(pgsql.Identifier(schema)))
        conn.commit()
    return PgConnection(conn)
