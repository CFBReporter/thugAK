"""Weekly retrain: test a candidate model per league, promote only if better.

    python scripts/train_model.py                 # all leagues
    python scripts/train_model.py --league nba    # one league
    python scripts/train_model.py --dry-run       # evaluate, write nothing

See src/thugsak/model/train.py. Prints numbers only, never the database URL
(the repo and its Actions logs are public).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from thugsak.config import LEAGUES  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--league", choices=list(LEAGUES), action="append")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from thugsak.model import train  # noqa: PLC0415

    failed = 0
    for lg in args.league or list(LEAGUES):
        print(f"== {lg}", flush=True)
        try:
            out = train.train_league(lg, dry_run=args.dry_run, progress=True)
        except Exception as e:  # one league failing must not stop the rest
            failed += 1
            print(f"  FAILED: {type(e).__name__}: {e}", flush=True)
            continue
        print(json.dumps(out, indent=1, default=str), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
