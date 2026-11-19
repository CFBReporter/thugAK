"""Phase 1: the database switch and the sqlite3-shaped Postgres connection.

Most of this runs offline. The `live_` tests talk to the real Postgres only
when THUGSAK_DATABASE_URL resolves to one, and are read-only: they create
nothing in the database.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak import db, store  # noqa: E402
from thugsak.config import DB_PATH  # noqa: E402

pytestmark = pytest.mark.no_pg_harness  # tests the backend switch itself

PG = "postgresql://postgres.ref:s3cretPass@aws-1-us-east-1.pooler.supabase.com:5432/postgres"


# --- which backend --------------------------------------------------------------


def test_no_url_means_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv(db.ENV_VAR, raising=False)
    monkeypatch.setattr(db, "ENV_FILE", tmp_path / "missing.env")
    assert db.database_url() == ""
    assert db.backend() == "sqlite"
    assert store.uses_postgres() is False


def test_url_from_env_file(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"# comment\nOTHER=1\n{db.ENV_VAR}={PG}\n", encoding="utf-8")
    monkeypatch.delenv(db.ENV_VAR, raising=False)
    monkeypatch.setattr(db, "ENV_FILE", f)
    assert db.database_url() == PG
    assert db.backend() == "postgres"


def test_env_var_overrides_file_even_when_empty(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"{db.ENV_VAR}={PG}\n", encoding="utf-8")
    monkeypatch.setattr(db, "ENV_FILE", f)
    monkeypatch.setenv(db.ENV_VAR, "")
    assert db.backend() == "sqlite"


def test_quoted_value_in_env_file(monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text(f'{db.ENV_VAR}="{PG}"\n', encoding="utf-8")
    monkeypatch.delenv(db.ENV_VAR, raising=False)
    monkeypatch.setattr(db, "ENV_FILE", f)
    assert db.database_url() == PG


def test_non_postgres_url_is_rejected(monkeypatch):
    monkeypatch.setenv(db.ENV_VAR, "mysql://x@y/z")
    with pytest.raises(ValueError):
        db.backend()


def test_only_the_default_path_is_redirected(monkeypatch, tmp_path):
    # The guarantee that keeps the test suite off the production database.
    monkeypatch.setenv(db.ENV_VAR, PG)
    assert store.uses_postgres(DB_PATH) is True
    assert store.uses_postgres(str(DB_PATH)) is True
    assert store.uses_postgres(tmp_path / "t.db") is False


def test_init_db_on_postgres_uses_migrations_not_the_sqlite_script(monkeypatch):
    # The SQLite SCHEMA (AUTOINCREMENT, PRAGMA migrations) must never reach
    # Postgres; init_db routes to the versioned migrations instead.
    monkeypatch.setenv(db.ENV_VAR, PG)
    called = []
    monkeypatch.setattr(store, "_init_postgres", lambda: called.append(1) or ["ok"])
    assert store.init_db() == ["ok"] and called == [1]


def test_migration_files_are_ordered_and_numbered():
    files = db.migration_files()
    assert files, "no Postgres migrations found"
    assert [f.name for f in files] == sorted(f.name for f in files)
    assert files[0].name == "0001_initial.sql"


@pytest.mark.parametrize("index", range(len(db.migration_files())))
def test_every_migration_locks_its_tables_with_rls(index):
    import re as _re

    sql = _re.sub(r"--[^\n]*", "", db.migration_files()[index].read_text(encoding="utf-8"))
    created = set(__import__("re").findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    locked = set(__import__("re").findall(r"ALTER TABLE (\w+)\s+ENABLE ROW LEVEL SECURITY", sql))
    assert created == locked, created ^ locked
    assert "AUTOINCREMENT" not in sql and " REAL" not in sql.replace("DOUBLE PRECISION", "")


def test_redacted_hides_the_password():
    r = db.redacted(PG)
    assert "s3cretPass" not in r
    assert r.startswith("postgresql://postgres.ref:********@aws-1-")


# --- placeholder translation ------------------------------------------------------


@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM t WHERE a=? AND b=?", "SELECT * FROM t WHERE a=%s AND b=%s"),
    ("SELECT '?' , ?", "SELECT '?' , %s"),                      # ? in a literal is data
    ("SELECT 'it''s?' WHERE x=?", "SELECT 'it''s?' WHERE x=%s"),  # escaped quote
    ("WHERE name LIKE '%jose%' AND id=?", "WHERE name LIKE '%%jose%%' AND id=%s"),
    ("SELECT 1", "SELECT 1"),
])
def test_translate_placeholders(sql, expected):
    assert db.translate_placeholders(sql) == expected


# --- Row ----------------------------------------------------------------------------


def test_row_reads_like_sqlite_row():
    r = db.Row(["team_id", "n"], ("nba:BOS", 3))
    assert r["team_id"] == "nba:BOS" and r[1] == 3
    assert list(r) == ["nba:BOS", 3]
    assert dict(zip(r.keys(), r)) == {"team_id": "nba:BOS", "n": 3}
    assert len(r) == 2
    with pytest.raises(IndexError):
        r["missing"]


def test_row_supports_dict_conversion_the_way_callers_use_it():
    r = db.Row(["a", "b"], (1, 2))
    assert {k: r[k] for k in r.keys()} == {"a": 1, "b": 2}


# --- live, read-only, skipped without a configured Postgres -----------------------

live = pytest.mark.skipif(
    db.backend() != "postgres", reason="THUGSAK_DATABASE_URL not set to Postgres"
)


@live
def test_live_connects_and_translates_params():
    with store.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT ? AS a, ? AS b, '50%' AS pct, 'why?' AS q", (7, "x")
        ).fetchone()
    assert (row["a"], row["b"], row["pct"], row["q"]) == (7, "x", "50%", "why?")
    assert row[0] == 7


@live
def test_live_fetchall_and_rowcount():
    with store.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT g AS n FROM generate_series(1, ?) AS g", (3,)
        ).fetchall()
        cur = conn.execute("SELECT 1")
    assert [r["n"] for r in rows] == [1, 2, 3]
    assert cur.rowcount == 1


@live
def test_live_schema_is_migrated_and_locked():
    with store.connect(readonly=True) as conn:
        versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
        unlocked = conn.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname = current_schema() "
            "AND NOT rowsecurity"
        ).fetchone()["n"]
    assert "0001_initial" in versions
    assert unlocked == 0


@live
def test_live_bad_password_error_does_not_leak_it():
    bad = db.database_url().replace("@", "XXbad@", 1)
    with pytest.raises(RuntimeError) as exc:
        db.connect_postgres(bad, connect_timeout=10)
    pw = bad.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert pw not in str(exc.value)
