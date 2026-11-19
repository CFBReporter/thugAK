"""What the Model page and the game view show about the model.

Four reads, all from stored data (no engine run):
  overview(league)   the active version, when it was trained, its held-out
                     accuracy, and its live record over the last 4 weeks
  log(league)        every training run, promoted or kept out, with what changed
  record(league)     graded predictions: predicted vs final, the miss, version
  accuracy(league)   weekly accuracy of the live record, with the sportsbook
                     line on the same games as the yardstick
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import store
from ..config import DB_PATH, LEAGUES
from ..localtime import parse_ts


def _payload(r: Any) -> dict[str, Any]:
    try:
        return json.loads(r["payload"]) if r["payload"] else {}
    except (TypeError, ValueError):
        return {}


def _graded_rows(conn: Any, league: str | None, since: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
    """Settled predictions with the numbers the page needs, newest first."""
    out = []
    for r in store.settled_predictions(conn, league, since=since, limit=limit):
        if r["pred_home_points"] is None or r["actual_home"] is None:
            continue
        ph, pa = float(r["pred_home_points"]), float(r["pred_away_points"])
        ah, aa = float(r["actual_home"]), float(r["actual_away"])
        mkt = next((a for a in _payload(r).get("anchors", []) if a.get("name") == "market"), None)
        pm, pt, am, at = ph - pa, ph + pa, ah - aa, ah + aa
        out.append({
            "game_id": r["game_id"],
            "league": r["league"],
            "date": r["slate_date"] or (r["as_of"] or "")[:10],
            "matchup": r["matchup"],
            "version": r["model_version"] or "v1",
            "predicted": [round(pa), round(ph)],
            "final": [int(aa), int(ah)],
            "miss_margin": round(abs(pm - am), 1),
            "miss_total": round(abs(pt - at), 1),
            "miss_points": round((abs(ph - ah) + abs(pa - aa)) / 2, 1),
            "right_winner": (am != 0 and (pm > 0) == (am > 0)),
            "decided": am != 0,
            "book_miss_margin": round(abs(mkt["spread"] - am), 1) if mkt and mkt.get("spread") is not None else None,
            "book_miss_total": round(abs(mkt["total"] - at), 1) if mkt and mkt.get("total") is not None else None,
        })
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    n = len(rows)
    decided = [r for r in rows if r["decided"]]
    bm = [r["book_miss_margin"] for r in rows if r["book_miss_margin"] is not None]
    bt = [r["book_miss_total"] for r in rows if r["book_miss_total"] is not None]
    return {
        "n": n,
        "mae_margin": round(sum(r["miss_margin"] for r in rows) / n, 2),
        "mae_total": round(sum(r["miss_total"] for r in rows) / n, 2),
        "mae_points": round(sum(r["miss_points"] for r in rows) / n, 2),
        "winner_pct": round(sum(r["right_winner"] for r in decided) / len(decided), 3) if decided else None,
        "book_mae_margin": round(sum(bm) / len(bm), 2) if bm else None,
        "book_mae_total": round(sum(bt) / len(bt), 2) if bt else None,
        "book_n": len(bm),
    }


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def overview_conn(conn: Any, league: str) -> dict[str, Any]:
    run = store.active_model_run(conn, league)
    cal = store.calibration_meta(conn, league)
    m = (run or {}).get("metrics") or {}
    held = (m.get("candidate") or {}).get("with_market") if run else None
    return {
        "league": league,
        "version": run["version"] if run else "v1",
        "live_since": run["trained_at"] if run else None,
        "trained_on_games": (m.get("train_games", 0) + m.get("holdout_games", 0)) if run else None,
        "uses_correction": bool(run and run.get("model") and run["model"].get("correction")),
        "holdout": held,
        "holdout_book": m.get("book") if run else None,
        "previous": (m.get("champion") or {}).get("with_market") if run else None,
        "previous_version": (m.get("champion") or {}).get("version") if run else None,
        "live_4_weeks": _summary(_graded_rows(conn, league, since=_since(28))),
        "live_all": _summary(_graded_rows(conn, league)),
        "calibration_fitted_at": cal.get("fitted_at"),
    }


def badge_conn(conn: Any, league: str) -> dict[str, Any]:
    """The one line under a predicted score: version, when, recent average miss."""
    o = overview_conn(conn, league)
    recent = o["live_4_weeks"] if o["live_4_weeks"].get("n", 0) >= 10 else (o["holdout"] or {})
    return {
        "version": o["version"],
        "live_since": o["live_since"],
        "avg_miss_margin": recent.get("mae_margin"),
        "avg_miss_total": recent.get("mae_total"),
        "avg_miss_basis": ("live, last 4 weeks" if o["live_4_weeks"].get("n", 0) >= 10
                           else "replayed recent games" if o["holdout"] else None),
    }


def log_conn(conn: Any, league: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    out = []
    for r in store.model_runs(conn, league, limit=limit):
        m = r.get("metrics") or {}
        out.append({
            "league": r["league"],
            "version": r["version"],
            "trained_at": r["trained_at"],
            "promoted": r["promoted"],
            "note": r.get("note"),
            "changes": r.get("changes") or {"settings": [], "learned": []},
            "holdout_games": m.get("holdout_games"),
            "holdout_from": m.get("holdout_from"),
            "candidate": (m.get("candidate") or {}).get("with_market"),
            "champion": (m.get("champion") or {}).get("with_market"),
            "champion_version": (m.get("champion") or {}).get("version"),
            "book": m.get("book"),
            "gain_pct": m.get("gain_pct"),
        })
    return out


def accuracy_conn(conn: Any, league: str) -> dict[str, Any]:
    """Weekly live accuracy, plus the latest training run's replayed weeks."""
    weeks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in _graded_rows(conn, league):
        try:
            d = parse_ts(r["date"] + "T12:00:00+00:00").date()
        except ValueError:
            continue
        weeks[(d - timedelta(days=d.weekday())).isoformat()].append(r)
    live = [{"week": wk, **_summary(rs)} for wk, rs in sorted(weeks.items())]
    runs = store.model_runs(conn, league, limit=1)
    replayed = ((runs[0].get("metrics") or {}).get("weekly") if runs else None) or []
    return {"league": league, "live": live, "replayed": replayed}


# --------------------------------------------------------------------------
# async entry points for the API
# --------------------------------------------------------------------------

async def overview(league: str | None = None, *, db: str = str(DB_PATH)) -> dict[str, Any]:
    store.init_db(db)
    with store.connect(db, readonly=True) as conn:
        leagues = [league] if league else list(LEAGUES)
        return {"leagues": [overview_conn(conn, lg) for lg in leagues]}


async def log(league: str | None = None, *, db: str = str(DB_PATH)) -> dict[str, Any]:
    store.init_db(db)
    with store.connect(db, readonly=True) as conn:
        return {"runs": log_conn(conn, league)}


async def record(league: str | None = None, *, limit: int = 300, db: str = str(DB_PATH)) -> dict[str, Any]:
    store.init_db(db)
    with store.connect(db, readonly=True) as conn:
        rows = _graded_rows(conn, league, limit=limit)
        return {"games": rows, "summary": _summary(rows)}


async def accuracy(league: str, *, db: str = str(DB_PATH)) -> dict[str, Any]:
    store.init_db(db)
    with store.connect(db, readonly=True) as conn:
        return accuracy_conn(conn, league)
