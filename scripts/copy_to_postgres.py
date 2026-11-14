"""Copy the SQLite game store into the configured Postgres database.

    python scripts/copy_to_postgres.py                       # dry run: counts only
    python scripts/copy_to_postgres.py --write               # copy into empty tables
    python scripts/copy_to_postgres.py --write --replace     # wipe Postgres first

Source: a consistent snapshot of the SQLite file (SQLite's backup API), taken
first, because the hourly agents may be writing to the live file while this
runs. Target: THUGSAK_DATABASE_URL (from the environment or .env).

Safety:
  * Without --write nothing is written anywhere.
  * If any target table already has rows, --replace is required, and then
    every app table is truncated together before the copy.
  * The whole copy is one transaction: it lands completely or not at all.
  * Afterwards every table's row count, and a checksum of its primary-key and
    numeric columns, is compared between source and target. A mismatch exits
    non-zero.

The HTTP cache (data/cache.db) is not copied: it is disposable and per-machine.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from thugsak import db, store  # noqa: E402
from thugsak.config import DB_PATH  # noqa: E402

# Parents before children (team_aliases -> teams, team_game_stats -> games).
TABLES = [
    "teams", "team_aliases", "games", "team_game_stats", "ratings_pit",
    "calibration", "predictions", "meta", "players", "player_games",
    "depth_chart", "injury_snapshots", "agent_runs",
]
IDENTITY = {"predictions": "pred_id", "injury_snapshots": "snapshot_id", "agent_runs": "run_id"}


def snapshot(src: Path) -> Path:
    """A point-in-time copy via the backup API: consistent even mid-write."""
    out = Path(tempfile.mkdtemp(prefix="thugsak_copy_")) / "snapshot.db"
    s = sqlite3.connect(src, timeout=60)
    d = sqlite3.connect(out)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return out


def pg_columns(raw, table: str) -> dict[str, str]:
    rows = raw.execute(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema = current_schema() AND table_name = %s
           ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def coerce(value, pg_type: str):
    """SQLite is loosely typed; Postgres is not. Make each value fit its column.

    A REAL that is a whole number can sit in an INTEGER column in SQLite
    (and a number in a TEXT one); COPY would reject both.
    """
    if value is None:
        return None
    if pg_type in ("integer", "bigint", "smallint"):
        return int(round(float(value)))
    if pg_type in ("double precision", "real", "numeric"):
        return float(value)
    if pg_type == "text":
        return value if isinstance(value, str) else str(value)
    return value


def checksum(rows, cols) -> str:
    """Order-independent digest of the rows, so source and target compare equal
    regardless of the order each database returns them in."""
    acc = 0
    for r in rows:
        h = hashlib.sha256(repr(tuple(_norm(v) for v in r)).encode()).digest()
        acc ^= int.from_bytes(h[:8], "big")
    return f"{acc:016x}"


def _norm(v):
    if isinstance(v, float):
        return round(v, 6)
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="Copy SQLite -> Postgres.")
    ap.add_argument("--source", default=str(DB_PATH))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--replace", action="store_true")
    args = ap.parse_args()

    if db.backend() != "postgres":
        print("THUGSAK_DATABASE_URL is not a Postgres URL; nothing to copy to.")
        return 2
    print(f"target: {db.redacted()}")

    t0 = time.perf_counter()
    snap = snapshot(Path(args.source))
    src = sqlite3.connect(snap)
    print(f"snapshot of {args.source} -> {snap} ({snap.stat().st_size/1e6:.1f} MB)")

    store.init_db()                      # migrations applied
    pg = db.connect_postgres()
    raw = pg.raw

    existing = {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    raw.rollback()
    nonempty = {t: n for t, n in existing.items() if n}
    print("source rows:", {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES})
    print("target rows:", existing)

    if not args.write:
        print("dry run -- re-run with --write to copy.")
        return 0
    if nonempty and not args.replace:
        print(f"target already has rows in {sorted(nonempty)}; pass --replace to wipe and recopy.")
        return 2

    try:
        if nonempty:
            raw.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        for t in TABLES:
            pcols = pg_columns(raw, t)
            scols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
            cols = [c for c in pcols if c in scols]
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
            types = [pcols[c] for c in cols]
            with raw.cursor().copy(f"COPY {t} ({', '.join(cols)}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row([coerce(v, ty) for v, ty in zip(r, types)])
            print(f"  {t:<17} {len(rows):>7} rows")
        for t, col in IDENTITY.items():
            raw.execute(
                f"SELECT setval(pg_get_serial_sequence('{t}', '{col}'), "
                f"COALESCE((SELECT MAX({col}) FROM {t}), 0) + 1, false)"
            )
        raw.commit()
    except Exception:
        raw.rollback()
        raise

    # Verify: counts and order-independent checksums of every column.
    bad = []
    for t in TABLES:
        pcols = pg_columns(raw, t)
        scols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        cols = [c for c in pcols if c in scols]
        types = [pcols[c] for c in cols]
        srows = [[coerce(v, ty) for v, ty in zip(r, types)]
                 for r in src.execute(f"SELECT {', '.join(cols)} FROM {t}")]
        prows = raw.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
        ok = len(srows) == len(prows) and checksum(srows, cols) == checksum(prows, cols)
        if not ok:
            bad.append(t)
        print(f"  verify {t:<17} {len(srows):>7} vs {len(prows):>7}  {'OK' if ok else 'MISMATCH'}")
    raw.rollback()
    pg.close()
    src.close()
    print(f"done in {time.perf_counter()-t0:.1f}s; target now {store.db_size_bytes()/1e6:.1f} MB")
    if bad:
        print("MISMATCH in:", bad)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
