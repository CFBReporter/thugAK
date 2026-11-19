"""Build the static data files for the GitHub Pages site.

GitHub Pages serves files, not a Python API, so this computes every response
the web app asks for and writes it as JSON under OUT/data/, at a path derived
from the request exactly as web/src/api.ts derives it in static mode:

    /api/<path>?league=L&date=D&game_id=G  ->  data/<path with / as ->/L/D/G.json

Built hourly by GitHub Actions right after the data jobs, so the site is at
most an hour behind. Read-only: nothing is recorded or written to the database.

    python scripts/build_site.py --out web/dist
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from thugsak import config, localtime, store  # noqa: E402
from thugsak.config import LEAGUES  # noqa: E402

DAYS_BACK = 7          # slates kept browsable on the site
DAYS_AHEAD = 3
DETAIL_DAYS = (-1, 0, 1)   # dates whose games get a full game view
CONCURRENCY = 4


def safe(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", part)


def data_path(out: Path, path: str, *, league: str | None = None, date: str | None = None,
              game_id: str | None = None) -> Path:
    """Must match staticUrl() in web/src/api.ts."""
    if path == "health":
        return out / "data" / "health.json"
    parts = [safe(league or "all")]
    if date:
        parts.append(safe(date))
    if game_id:
        parts.append(safe(game_id))
    return out / "data" / path.replace("/", "-") / ("/".join(parts) + ".json")


def write(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, default=str, separators=(",", ":")), encoding="utf-8")


def as_response(result: Any) -> Any:
    """Mirror api/main.py's error promotion: a dict with "error" becomes a
    status the web app treats as a failure, carried in the file itself."""
    if isinstance(result, dict) and "error" in result:
        reasons = result.get("warnings") or []
        detail = str(result["error"]) + (" -- " + " ".join(map(str, reasons)) if reasons else "")
        resolved = any(k in result for k in ("game_id", "warnings"))
        return {"__status": 422 if resolved else 404, "detail": detail}
    return result


async def build(out: Path) -> dict[str, int]:
    from thugsak.mcp import tools  # noqa: PLC0415
    from thugsak.model import report  # noqa: PLC0415

    counts = {"slates": 0, "games": 0, "edges": 0, "errors": 0}
    today = localtime.today_local()
    write(data_path(out, "health"), {
        "healthy": True,
        "provider": config.DATA_PROVIDER,
        "providers": [],
        "today": today.isoformat(),
        "timezone": localtime.tz_abbrev(),
        "built_at": store.utcnow(),
        "static": True,
    })

    sem = asyncio.Semaphore(CONCURRENCY)

    async def detail(league: str, g: dict[str, Any]) -> None:
        async with sem:
            try:
                rec = await tools.get_recommendation(league, g["game_id"], record=False)
            except Exception as e:
                rec = {"error": f"{type(e).__name__}: {e}"}
                counts["errors"] += 1
            write(data_path(out, "recommendation", league=league, game_id=g["game_id"]), as_response(rec))
            counts["games"] += 1
            if g.get("has_ladders") and not (isinstance(rec, dict) and "error" in rec):
                try:
                    edges = await tools.get_edges(league, g["game_id"])
                    write(data_path(out, "edges", league=league, game_id=g["game_id"]), as_response(edges))
                    counts["edges"] += 1
                except Exception:
                    counts["errors"] += 1

    for league in LEAGUES:
        for offset in range(-DAYS_BACK, DAYS_AHEAD + 1):
            d = (today + timedelta(days=offset)).isoformat()
            slate = await tools.list_games(league, d, prices="live")
            write(data_path(out, "games", league=league, date=d), slate)
            write(data_path(out, "prices", league=league, date=d), {
                "league": league, "date": d, "as_of": slate.get("prices_as_of"), "source": "live",
                "prices": {g["game_id"]: {k: g[k] for k in (
                    "kalshi_markets", "kalshi_winner_prices", "home_win_price",
                    "away_win_price", "has_ladders")} for g in slate["games"]},
                "unmatched_kalshi_events": slate.get("unmatched_kalshi_events", []),
                "unmatched_count": slate.get("unmatched_count", 0),
            })
            counts["slates"] += 1
            if offset in DETAIL_DAYS:
                await asyncio.gather(*(detail(league, g) for g in slate["games"]))

        write(data_path(out, "model", league=league), await report.overview(league))
        write(data_path(out, "model/log", league=league), await report.log(league))
        write(data_path(out, "model/record", league=league), await report.record(league))
        write(data_path(out, "model/accuracy", league=league), await report.accuracy(league))
    write(data_path(out, "model"), await report.overview(None))
    write(data_path(out, "model/log"), await report.log(None))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "web" / "dist"))
    args = ap.parse_args()
    t = time.time()
    counts = asyncio.run(build(Path(args.out)))
    print(json.dumps({**counts, "seconds": round(time.time() - t, 1)}))
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
