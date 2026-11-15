"""Run the whole suite against Postgres with THUGSAK_TEST_BACKEND=postgres.

Default (unset): nothing here does anything; tests use their temporary SQLite
files exactly as before.

Postgres mode: every test gets its own freshly migrated schema in the
configured database, named `t_<random>`, and dropped afterwards. Tests keep
passing their tmp-file paths around; in this mode every path a test uses maps
to that test's schema. The real `public` tables are never read or written.

One physical connection per test is shared by every `store.connect` inside
it, because a fresh connection to a hosted pooler is ~0.5s and some tests
open dozens.

Tests that are about SQLite itself (opening raw `.db` files, the SQLite
column migrations) mark themselves `sqlite_only`.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PG_MODE = os.environ.get("THUGSAK_TEST_BACKEND", "").lower() == "postgres"


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: test exercises SQLite itself")
    config.addinivalue_line("markers", "no_pg_harness: test manages backends itself")


def pytest_collection_modifyitems(config, items):
    if not PG_MODE:
        return
    skip = pytest.mark.skip(reason="SQLite-specific; not run in Postgres mode")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _postgres_schema(request, monkeypatch):
    if not PG_MODE or "no_pg_harness" in request.keywords:
        yield
        return

    from thugsak import db, store

    if db.backend() != "postgres":
        pytest.skip("THUGSAK_TEST_BACKEND=postgres but no Postgres URL configured")

    schema = f"t_{uuid.uuid4().hex[:12]}"
    monkeypatch.setenv(db.SCHEMA_ENV_VAR, schema)
    monkeypatch.setattr(store, "uses_postgres", lambda path=None: True)

    real_connect = db.connect_postgres
    shared = real_connect(pooled=False)  # a pool per throwaway schema would leak connections

    class _Shared(db.PgConnection):
        def __init__(self, inner):
            self._conn = inner._conn
            self._pool = None

        def close(self):  # kept open for the rest of the test
            pass

    monkeypatch.setattr(db, "connect_postgres", lambda *a, **k: _Shared(shared))
    store._PG_READY.discard(db.target_key())
    try:
        yield
    finally:
        try:
            shared.raw.rollback()
        except Exception:
            pass
        shared.close()
        # Drop from a connection outside the test schema.
        os.environ.pop(db.SCHEMA_ENV_VAR, None)
        c = real_connect(pooled=False)
        try:
            c.raw.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            c.raw.commit()
        finally:
            c.close()
        store._PG_READY.discard(f"{db.database_url()}#{schema}")
