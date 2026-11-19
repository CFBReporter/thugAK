"""Recovering from a network outage without a human.

On 2026-11-16 the internet dropped mid-cycle. The agent process hung for three
days (so every later hourly run was skipped) and the API's connection pool
never reconnected, answering 500 to every page until restarted by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import db, store  # noqa: E402


# --- a dead cycle's rows are closed --------------------------------------------------


@pytest.fixture()
def dbpath(tmp_path):
    p = tmp_path / "t.db"
    store.init_db(p)
    return p


def test_old_running_rows_are_abandoned_recent_ones_kept(dbpath):
    with store.connect(dbpath) as conn:
        old = store.start_agent_run(conn, "cycle", "nba")
        conn.execute("UPDATE agent_runs SET started_at='2026-11-16T15:00:00+00:00' WHERE run_id=?", (old,))
        fresh = store.start_agent_run(conn, "cycle", "nba")
        conn.commit()
        assert store.abandon_stale_runs(conn) == 1
        conn.commit()
        rows = {r["run_id"]: r for r in conn.execute("SELECT * FROM agent_runs")}
    assert rows[old]["status"] == "abandoned" and rows[old]["finished_at"]
    assert "network" in rows[old]["error"]
    assert rows[fresh]["status"] == "running"


# --- the pool is rebuilt when it cannot hand out a connection ------------------------


class _DeadPool:
    def __init__(self):
        self.closed = False

    def getconn(self):
        raise RuntimeError("PoolTimeout: couldn't get a connection after 15.00 sec")

    def close(self, timeout=None):
        self.closed = True


class _GoodPool:
    def getconn(self):
        return object()


@pytest.mark.no_pg_harness
def test_a_wedged_pool_is_replaced(monkeypatch):
    monkeypatch.setattr(db, "database_url", lambda: "postgresql://u:pw@h:5432/d")
    monkeypatch.delenv(db.SCHEMA_ENV_VAR, raising=False)
    dead, good = _DeadPool(), _GoodPool()
    key = "postgresql://u:pw@h:5432/d#"
    monkeypatch.setitem(db._POOLS, key, dead)

    def pool_for(target, schema):
        return db._POOLS.setdefault(f"{target}#{schema}", good)

    monkeypatch.setattr(db, "_pool_for", pool_for)
    conn = db.connect_postgres()
    assert conn._pool is good
    assert dead.closed and db._POOLS[key] is good
    db._POOLS.pop(key, None)


@pytest.mark.no_pg_harness
def test_a_pool_that_stays_down_fails_without_leaking_the_password(monkeypatch):
    monkeypatch.setattr(db, "database_url", lambda: "postgresql://u:secretpw@h:5432/d")
    monkeypatch.delenv(db.SCHEMA_ENV_VAR, raising=False)
    monkeypatch.setattr(db, "_pool_for", lambda target, schema: _DeadPool())
    with pytest.raises(RuntimeError) as e:
        db.connect_postgres()
    assert "secretpw" not in str(e.value) and "PoolTimeout" in str(e.value)
