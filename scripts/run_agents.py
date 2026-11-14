"""Run the agents. This is what Windows Task Scheduler calls, hourly.

    python scripts/run_agents.py                      # == --agent auto
    python scripts/run_agents.py --agent auto --dry-run
    python scripts/run_agents.py --agent auto --force
    python scripts/run_agents.py --agent ingest --league ncaamb
    python scripts/run_agents.py --agent daily

`auto` is the scheduled cycle: window check, lock, change gate, then whichever
agents the gate routes to, all logged to `agent_runs`. `--dry-run` computes
the gate and prints the routing without calling a model. `--force` bypasses
the window and runs all three agents even when nothing moved.

A single named agent (`ingest`, `market`, `analyst`, `daily`) runs now with no
window, lock or gate -- for a hand-triggered refresh or for debugging one
agent's behaviour.

Exit code is 0 for ok / no-op / skipped, 1 for failed, so the Task Scheduler
history shows failures as failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from thugsak.config import DB_PATH, LEAGUES  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--agent", default="auto",
        choices=["auto", "ingest", "market", "analyst", "daily"],
    )
    p.add_argument("--league", default="all", choices=["all", *LEAGUES])
    p.add_argument("--date", default=None, help="YYYY-MM-DD, Central (default today)")
    p.add_argument("--dry-run", action="store_true", help="gate + routing only, no model")
    p.add_argument("--force", action="store_true", help="ignore the window and the gate")
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args()

    from thugsak.agents import runner  # noqa: PLC0415  (after sys.path)

    leagues = list(LEAGUES) if args.league == "all" else [args.league]

    if args.agent == "auto":
        out = asyncio.run(runner.run_auto(
            leagues, args.date, force=args.force, dry_run=args.dry_run, db=args.db,
        ))
    else:
        out = asyncio.run(runner.run_one(args.agent, leagues, args.date, db=args.db))

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_human(out)
    return 1 if out.get("status") == "failed" else 0


def _print_human(out: dict) -> None:
    status = out.get("status")
    print(f"status: {status}   date: {out.get('date')}   leagues: {','.join(out.get('leagues', []))}")
    gate = out.get("gate") or {}
    for lg, g in gate.items():
        reasons = "; ".join(g.get("reasons") or []) or "nothing moved"
        print(f"  {lg:<7} {g.get('digest')}  {reasons}")
    if out.get("would_run"):
        print(f"would run: {', '.join(out['would_run'])}")
    if out.get("daily"):
        print(f"daily tier ran: {out['daily']}")
    results = out.get("results") or {}
    for name, r in results.items():
        flag = "FAILED" if r.get("is_error") else "ok"
        cost = f"${r['cost_usd']:.3f}" if r.get("cost_usd") is not None else "$?"
        print(f"  agent {name:<8} {flag:<6} turns={r.get('turns')} tools={r.get('tool_calls')} {cost}")
        if r.get("error"):
            print(f"     {r['error']}")
    if out.get("report"):
        print("\n" + out["report"])
    if out.get("error"):
        print(f"error: {out['error']}")


if __name__ == "__main__":
    raise SystemExit(main())
