"""The weekly retrain: build a candidate, test it, promote it only if it is better.

For each league:
  1. Snapshot the league's last REPLAY_SEASONS seasons into a local SQLite file.
  2. Score the ACTIVE model (current settings + its correction, or the engine
     alone) on the last HOLDOUT_DAYS of finished games, replayed as of kickoff.
  3. Build a CANDIDATE: refit the settings (home advantage, form-window
     weights, scoring level, uncertainty) on the snapshot, replay every game
     under them, and fit the correction layer on the games BEFORE the
     holdout window.
  4. Score the candidate on the same holdout games.
  5. Promote only if the candidate's combined miss (margin + total, mean
     absolute points) is at least PROMOTE_MIN_GAIN lower. On promotion the
     correction is refitted on all games, the new settings are written to
     `calibration`, and predictions start carrying the new version.
Every run -- promoted or not -- is written to `model_runs`, with the numbers
and a plain-language list of what changed, which is what the Model page shows.

One honest caveat: the refitted settings are fitted on all games in the
snapshot, holdout included. They are four or five league-wide constants
fitted on thousands of games, so three weeks of overlap barely moves them;
the correction layer, which has far more freedom, never sees the holdout.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from .. import store
from ..config import DB_PATH
from ..localtime import parse_ts
from . import correction, fit, replay

HOLDOUT_DAYS = 21
MIN_HOLDOUT_GAMES = 30
MIN_TRAIN_GAMES = 200
PROMOTE_MIN_GAIN = 0.005      # 0.5% lower combined miss
REPLAY_SEASONS = 3

SETTINGS = (
    ("hfa", "Home advantage", "pts"),
    ("mean_total", "League average total", "pts"),
    ("w_last3", "Weight on last 3 games", ""),
    ("w_last7", "Weight on last 7 games", ""),
    ("w_season", "Weight on season to date", ""),
    ("sigma_margin", "Margin uncertainty", "pts"),
    ("sigma_total", "Total uncertainty", "pts"),
)


def _score(m: dict[str, Any]) -> float:
    return float(m.get("mae_margin", 0)) + float(m.get("mae_total", 0))


def _weekly(rows: list[dict[str, Any]], models: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-week accuracy for the chart: each model plus the sportsbook line."""
    weeks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        d = parse_ts(r["start_time"]).date()
        weeks[(d - timedelta(days=d.weekday())).isoformat()].append(r)
    out = []
    for wk in sorted(weeks):
        rs = weeks[wk]
        entry: dict[str, Any] = {"week": wk, "games": len(rs)}
        for name, corr in models.items():
            m = correction.evaluate(corr, rs)
            entry[name] = {"mae_margin": m["mae_margin"], "mae_total": m["mae_total"]}
        b = correction.evaluate_book(rs)
        if b["n"]:
            entry["book"] = {"mae_margin": b["mae_margin"], "mae_total": b["mae_total"], "n": b["n"]}
        out.append(entry)
    return out


def describe_changes(old: dict[str, float], new: dict[str, float],
                     corr: "correction.Correction | None") -> dict[str, list[str]]:
    """What a person reading the log needs: settings that moved, and what the
    correction learned, in words. Generated from the numbers, not written."""
    changed = []
    for key, label, unit in SETTINGS:
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            continue
        if abs(b - a) >= (0.02 if not unit else 0.1):
            fmt = "{:.2f}" if not unit else "{:.1f}"
            changed.append(f"{label} {fmt.format(a)} -> {fmt.format(b)}{(' ' + unit) if unit else ''}")

    learned: list[str] = []
    if corr is None:
        return {"settings": changed, "learned": learned}
    ti, mi = corr.total.intercept, corr.margin.intercept
    if abs(ti) >= 0.5:
        learned.append(f"Totals were running {abs(ti):.1f} pts {'low' if ti > 0 else 'high'}; corrected")
    if abs(mi) >= 0.5:
        learned.append(f"Home margins were running {abs(mi):.1f} pts {'low' if mi > 0 else 'high'}; corrected")
    idx = {f: i for i, f in enumerate(corr.features)}

    def per_unit(target: correction.Target, f: str) -> float:
        if f not in idx:
            return 0.0
        return target.coef[idx[f]] / (corr.std[idx[f]] or 1.0)

    b2b_h = per_unit(corr.margin, "b2b_home")
    b2b_a = per_unit(corr.margin, "b2b_away")
    if abs(b2b_h) >= 0.5:
        learned.append(f"Home team on a back-to-back: margin {b2b_h:+.1f} pts")
    if abs(b2b_a) >= 0.5:
        learned.append(f"Away team on a back-to-back: home margin {b2b_a:+.1f} pts")
    rg = per_unit(corr.margin, "rating_gap")
    if abs(rg) >= 0.1:
        learned.append(
            f"When power ratings disagree with recent form, the margin moves {abs(rg):.0%} "
            f"{'toward the ratings' if rg > 0 else 'away from the ratings'}"
        )
    em, et = per_unit(corr.margin, "eff_margin_gap"), per_unit(corr.total, "eff_total_gap")
    if abs(em) >= 0.05 or abs(et) >= 0.05:
        learned.append(
            f"Efficiency model (KenPom-style): the score moves {em:.0%} of the way to its margin "
            f"and {et:.0%} of the way to its total"
        )
    es = per_unit(corr.total, "early_season")
    if abs(es) >= 1.0:
        learned.append(f"Early-season totals adjusted {es:+.1f} pts at the season's start")
    mw, tw = corr.margin.market_weight, corr.total.market_weight
    learned.append(
        f"Market input weight (sportsbook line, capped at {correction.MARKET_WEIGHT_CAP:.0%}): "
        f"margin {mw:.0%}, total {tw:.0%}"
    )
    return {"settings": changed, "learned": learned}


def train_league(league: str, *, db: str = str(DB_PATH), dry_run: bool = False,
                 progress: bool = False) -> dict[str, Any]:
    store.init_db(db)   # applies pending migrations (model_runs)
    with store.connect(db) as conn:
        seasons = replay.recent_seasons(conn, league, REPLAY_SEASONS)
        if not seasons:
            return {"league": league, "status": "skipped", "note": "no finished games"}
        snap = replay.snapshot(conn, league, seasons=seasons)
        champion = store.active_model_run(conn, league)
        n_runs = conn.execute("SELECT count(*) AS n FROM model_runs WHERE league=?", (league,)).fetchone()["n"]

    with store.connect(snap) as sc:
        last = sc.execute(
            "SELECT max(start_time) AS t FROM games WHERE league=? AND completed=1", (league,)
        ).fetchone()["t"]
        old_cal = store.get_calibration(sc, league)
    cutoff = (parse_ts(last) - timedelta(days=HOLDOUT_DAYS)).isoformat()

    champ_corr = (
        correction.Correction.from_dict(champion["model"]["correction"])
        if champion and champion.get("model") and champion["model"].get("correction") else None
    )
    champ_version = champion["version"] if champion else "v1"
    if progress:
        print(f"  {league}: replaying holdout under the active model ({champ_version})", flush=True)
    champ_rows = replay.replay(snap, league, start=cutoff)

    if progress:
        print(f"  {league}: refitting settings on seasons {seasons}", flush=True)
    with store.connect(snap) as sc:
        new_cal = fit.fit_league(sc, league, seasons=seasons, verbose=False) or old_cal
        sc.commit()
    if progress:
        print(f"  {league}: replaying {seasons} under the candidate settings", flush=True)
    rows = replay.replay(snap, league, seasons=seasons, progress=progress)
    train = [r for r in rows if r["start_time"] < cutoff]
    hold = [r for r in rows if r["start_time"] >= cutoff]
    version = f"v{n_runs + 2}"

    if len(hold) < MIN_HOLDOUT_GAMES or len(train) < MIN_TRAIN_GAMES:
        note = (f"Not enough games to judge a new model: {len(hold)} in the last "
                f"{HOLDOUT_DAYS} days (need {MIN_HOLDOUT_GAMES}), {len(train)} before.")
        if not dry_run:
            with store.connect(db) as conn:
                store.save_model_run(conn, league, version, promoted=False, note=note,
                                     metrics={"holdout_games": len(hold), "train_games": len(train)})
                conn.commit()
        return {"league": league, "status": "skipped", "version": version, "note": note}

    # Does the correction layer earn its place this week? Decided on the
    # training games only (fit on the earlier 80%, judged on the latest 20%),
    # never on the holdout, so the holdout stays a fair final test.
    k = int(len(train) * 0.8)
    probe = correction.fit(train[:k], league=league, version=version)
    use_corr = _score(correction.evaluate(probe, train[k:], market=True)) < _score(
        correction.evaluate(None, train[k:]))
    cand = correction.fit(train, league=league, version=version) if use_corr else None
    m = {
        "holdout_days": HOLDOUT_DAYS,
        "holdout_from": cutoff[:10],
        "holdout_games": len(hold),
        "train_games": len(train),
        "seasons": seasons,
        "champion": {
            "version": champ_version,
            "model_only": correction.evaluate(champ_corr, champ_rows),
            "with_market": correction.evaluate(champ_corr, champ_rows, market=True),
        },
        "candidate": {
            "version": version,
            "uses_correction": use_corr,
            "model_only": correction.evaluate(cand, hold),
            "with_market": correction.evaluate(cand, hold, market=True),
        },
        "engine_alone": correction.evaluate(None, hold),
        "book": correction.evaluate_book(hold),
    }
    champ_score = _score(m["champion"]["with_market"])
    cand_score = _score(m["candidate"]["with_market"])
    promote = cand_score < champ_score * (1 - PROMOTE_MIN_GAIN)
    m["gain_pct"] = round(100 * (champ_score - cand_score) / champ_score, 2) if champ_score else 0.0

    final = (correction.fit(rows, league=league, version=version) if use_corr else None) if promote else cand
    m["weekly"] = _weekly(hold, {"champion": champ_corr, "candidate": cand})
    changes = describe_changes(old_cal, new_cal, final)
    if final is None:
        changes["learned"] = ["Correction layer not used: on recent training games it did not beat the refitted engine"]
    note = (
        f"{version} promoted: combined miss {champ_score:.2f} -> {cand_score:.2f} pts "
        f"on {len(hold)} held-out games"
        if promote else
        f"{version} kept out: {cand_score:.2f} vs active {champ_version} {champ_score:.2f} pts "
        f"on {len(hold)} held-out games -- not better by {PROMOTE_MIN_GAIN:.1%}"
    )
    if not dry_run:
        with store.connect(db) as conn:
            if promote:
                store.put_calibration(conn, league, new_cal, n_games=int(new_cal.get("n_basis", 0)) or None,
                                      seasons=seasons)
            store.save_model_run(
                conn, league, version, promoted=promote,
                model={"correction": final.to_dict() if final else None, "calibration": new_cal},
                metrics=m, changes=changes, note=note,
            )
            conn.commit()
    return {"league": league, "status": "promoted" if promote else "kept", "version": version,
            "note": note, "metrics": {k: v for k, v in m.items() if k != "weekly"}, "changes": changes}
