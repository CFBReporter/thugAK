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


def test_init_db_refuses_to_run_sqlite_ddl_on_postgres(monkeypatch):
    monkeypatch.setenv(db.ENV_VAR, PG)
    with pytest.raises(NotImplementedError):
        store.init_db()


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
def test_live_bad_password_error_does_not_leak_it():
    bad = db.database_url().replace("@", "XXbad@", 1)
    with pytest.raises(RuntimeError) as exc:
        db.connect_postgres(bad, connect_timeout=10)
    pw = bad.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert pw not in str(exc.value)
