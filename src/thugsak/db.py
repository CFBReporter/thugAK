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
from typing import Any, Iterable, Iterator, Sequence

from .config import PROJECT_ROOT

ENV_VAR = "THUGSAK_DATABASE_URL"
ENV_FILE = PROJECT_ROOT / ".env"


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


class PgConnection:
    """Wraps a psycopg connection with the sqlite3 surface the store uses."""

    def __init__(self, conn: Any):
        self._conn = conn

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> PgCursor:
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
        cur = self._conn.cursor()
        cur.executemany(translate_placeholders(sql), [tuple(p) for p in seq])
        return PgCursor(cur)

    def executescript(self, script: str) -> None:
        # psycopg runs a multi-statement string in one call when it carries
        # no parameters, which a schema script never does.
        self._conn.execute(script)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def raw(self) -> Any:
        """The underlying psycopg connection, for the few places that need
        Postgres-native behaviour (COPY, RETURNING) in later phases."""
        return self._conn


def connect_postgres(url: str | None = None, *, connect_timeout: int = 15) -> PgConnection:
    import psycopg  # imported here so SQLite-only installs need no driver

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
    return PgConnection(conn)
