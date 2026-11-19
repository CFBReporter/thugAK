"""Run the hourly jobs (no LLM). What GitHub Actions calls every hour.

    python scripts/run_jobs.py                 # all leagues, today (Central)
    python scripts/run_jobs.py --league nba    # one league
    python scripts/run_jobs.py --daily         # force the once-a-day tier

Exit code 0 on ok, 1 on partial (some steps failed), 2 on failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from thugsak.config import LEAGUES  # noqa: E402

# Hard stop well inside the hour, so a hung network call can never block the
# next run (GitHub's own job timeout is the second line of defence).
WATCHDOG_SECONDS = int(os.environ.get("THUGSAK_JOBS_WATCHDOG_SECONDS", str(45 * 60)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--league", choices=list(LEAGUES), action="append")
    ap.add_argument("--date")
    ap.add_argument("--daily", action="store_true", help="force the once-a-day tier")
    args = ap.parse_args()

    from thugsak.agents import jobs  # noqa: PLC0415

    t = threading.Timer(WATCHDOG_SECONDS, lambda: (print("watchdog: exiting", flush=True), os._exit(3)))
    t.daemon = True
    t.start()

    out = asyncio.run(jobs.run_jobs(args.league, args.date, force_daily=args.daily))
    # Counts and timings only -- never the database URL (the repo is public,
    # and so are its Actions logs).
    print(json.dumps({k: v for k, v in out.items() if k != "error"}, indent=1, default=str))
    if out.get("error"):
        print("error:", out["error"])
    return {"ok": 0, "partial": 1}.get(out["status"], 2)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
