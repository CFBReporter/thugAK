"""The hourly loop: window, lock, gate, agents, log.

`run_auto` is what the scheduled task calls. In order:

1. **Window.** 07:00-01:00 Central. Outside it, record `skipped-window` and
   exit. (Task Scheduler also only fires inside the window; this is the
   belt to its braces, and it is what `--force` overrides.)
2. **Lock.** One run at a time. A full NCAAMB ingest can exceed an hour and
   SQLite wants a single writer; a run that finds the lock held records
   `skipped-locked` and exits rather than queueing.
3. **Gate.** Compute the change signature (HTTP + SQL, no LLM) and diff it
   against the last run's. Nothing moved -> `no-op`, exit. Otherwise the
   diff names the agents to run.
4. **Agents.** Each is a Claude Agent SDK loop over the project's own MCP
   tools, pre-approved by name so an unattended run never blocks on a
   permission prompt, with a turn cap and a dollar cap so a confused run
   cannot spend the afternoon.
5. **Log.** One `agent_runs` row per run, including the quiet ones, so a
   silent day reads as "checked, nothing moved" rather than "did not run".

The daily tier -- retention, cache trim, the integrity audit -- runs inside
the first run after 07:00 Central, deterministically, without an LLM.

Every LLM call goes through `_run_llm`, which tests replace with a stub; the
rest of this module is then exercised end to end with no network and no model.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .. import store
from ..config import DATA_DIR, DB_PATH, LEAGUES, league as get_league
from ..localtime import now_local, today_local
from . import gate, ingest_tools, prompts

WINDOW_START_HOUR = 7        # first run at 07:00 Central
WINDOW_END_HOUR = 1          # last run at 01:00 Central; 02:00-06:00 are dead
LOCK_PATH = DATA_DIR / "agents.lock"
LOCK_STALE_SECONDS = 3 * 3600
DAILY_EVERY_SECONDS = 20 * 3600

DEFAULT_MAX_TURNS = {"ingest": 24, "market": 24, "analyst": 24}
# Sized on a nine-game NBA slate, where the market agent spent $1.07 on its
# way to a $1.00 cap. The first (one-game NFL) run had suggested half this.
DEFAULT_BUDGET_USD = {"ingest": 2.5, "market": 3.0, "analyst": 3.0}

# Which model each agent runs on. The data agents fetch and summarise through
# deterministic tools, so a fast model is the right one; the analyst's
# judgement is the only place a stronger model earns its cost, and even that
# defaults to Sonnet because the binding constraint turned out to be the
# account's usage quota, not per-run dollars: the first NBA cycle died on
# "You've hit your 5-hour limit". Override per agent with
# THUGSAK_MODEL_INGEST / THUGSAK_MODEL_MARKET / THUGSAK_MODEL_ANALYST.
SONNET = "claude-sonnet-5-5"
DEFAULT_MODELS = {"ingest": SONNET, "market": SONNET, "analyst": SONNET}


def model_for(agent: str) -> str:
    return os.environ.get(f"THUGSAK_MODEL_{agent.upper()}") or DEFAULT_MODELS[agent]


# When the account quota is hit, every retry until the window resets burns
# more quota and fails the same way. The cycle records `rate-limited`, sets a
# cooldown, and skips the LLM agents until it passes. The gate and the daily
# tier keep running; they cost nothing.
COOLDOWN_SECONDS = 60 * 60
COOLDOWN_KEY = "agents_cooldown_until"
RATE_LIMIT_MARKERS = ("hit your 5-hour limit", "rate limit", "rate_limit", "usage limit", "429")

# Builtin tools an unattended agent must never hold. Everything it is meant to
# do goes through the project's MCP tools, which write only to the database
# and only through tested code paths. Read-only builtins (Grep, Read, Glob,
# ToolSearch) are left available: an agent that can read the source of a tool
# that just failed writes a better bug report, and that is exactly what the
# first live run did.
WRITE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"]


# --------------------------------------------------------------------------
# window and lock
# --------------------------------------------------------------------------

def in_window(now: datetime | None = None) -> bool:
    """07:00 through 01:00 Central, inclusive of both hourly starts."""
    h = (now or now_local()).hour
    return h >= WINDOW_START_HOUR or h <= WINDOW_END_HOUR


class Locked(RuntimeError):
    pass


class RunLock:
    """A pidfile with stale detection. `O_EXCL` makes creation atomic."""

    def __init__(self, path: Path | str = LOCK_PATH, *, stale_after: float = LOCK_STALE_SECONDS):
        self.path = Path(path)
        self.stale_after = stale_after
        self._held = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._is_stale():
                self.path.unlink(missing_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise Locked(f"another run holds {self.path}")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"pid": os.getpid(), "started": time.time()}))
        self._held = True
        return self

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        return age > self.stale_after

    def __exit__(self, *exc: Any) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


# --------------------------------------------------------------------------
# the LLM call
# --------------------------------------------------------------------------

@dataclass
class LLMResult:
    text: str = ""
    cost_usd: float | None = None
    turns: int | None = None
    is_error: bool = False
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None
    # The account quota, not this run's budget: retrying is pointless until
    # the window resets, so the cycle sets a cooldown instead.
    rate_limited: bool = False


def _looks_rate_limited(text: str | None) -> bool:
    t = (text or "").lower()
    return any(m in t for m in RATE_LIMIT_MARKERS)


def _ingest_subagents() -> dict[str, Any]:
    """Four per-sport subagents, each confined to the ingest tools."""
    from claude_agent_sdk import AgentDefinition  # noqa: PLC0415

    from ..mcp.sdk_server import SERVER_NAME  # noqa: PLC0415

    ingest_tool_names = [
        f"mcp__{SERVER_NAME}__{n}" for n in (
            "ingest_slate", "ingest_box_scores", "ingest_player_logs",
            "ingest_injuries", "ingest_depth_charts", "audit_data_integrity",
            "link_team_alias",
        )
    ]
    out: dict[str, Any] = {}
    for key, lg in LEAGUES.items():
        out[f"ingest-{key}"] = AgentDefinition(
            description=f"Ingests {lg.name} data for today's slate and reports gaps.",
            prompt=prompts.INGEST_SUBAGENT.format(
                league=key, league_name=lg.name,
                facts=prompts.LEAGUE_FACTS.get(key, ""),
            ),
            tools=ingest_tool_names,
            model=model_for("ingest"),
            maxTurns=16,
        )
    return out


async def _run_llm(
    agent: str,
    prompt: str,
    *,
    system: str,
    max_turns: int,
    budget_usd: float,
    subagents: dict[str, Any] | None = None,
) -> LLMResult:
    """One Claude Agent SDK loop over the in-process MCP server.

    Tools are pre-approved by name and `permission_mode='dontAsk'` denies
    anything else outright rather than prompting -- an unattended run must
    never block on a question nobody is there to answer.
    """
    from claude_agent_sdk import (  # noqa: PLC0415
        AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock, query,
    )

    from ..mcp.sdk_server import (  # noqa: PLC0415
        SERVER_NAME, allowed_tool_names, build_sdk_server,
    )

    options = ClaudeAgentOptions(
        mcp_servers={SERVER_NAME: build_sdk_server()},
        allowed_tools=allowed_tool_names(),
        # `allowed_tools` pre-approves; it does not restrict. The first live
        # run showed the ingest agent reaching for Grep and ToolSearch (fine,
        # read-only) and ending its report with "I didn't edit t..." -- it had
        # considered editing the repository to fix a bug it found. An
        # unattended run must be able to *report* a fix, never make one.
        disallowed_tools=WRITE_TOOLS,
        system_prompt=system,
        model=model_for(agent),
        max_turns=max_turns,
        max_budget_usd=budget_usd,
        permission_mode="dontAsk",
        agents=subagents,
    )
    out = LLMResult()
    chunks: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content or []:
                    text = getattr(block, "text", None)
                    if text:
                        chunks.append(text)
                    if isinstance(block, ToolUseBlock):
                        out.tool_calls.append(block.name)
            elif isinstance(msg, ResultMessage):
                out.cost_usd = msg.total_cost_usd
                out.turns = msg.num_turns
                out.is_error = bool(msg.is_error)
                if msg.is_error and msg.result:
                    out.error = str(msg.result)
                if msg.result and not chunks:
                    chunks.append(str(msg.result))
    except Exception as e:  # the SDK raises on a missing/signed-out CLI
        out.is_error = True
        out.error = f"{type(e).__name__}: {e}"
    out.text = "".join(chunks).strip()
    out.rate_limited = out.is_error and (
        _looks_rate_limited(out.error) or _looks_rate_limited(out.text[-400:])
    )
    return out


# Injected so tests can replace the model with a stub.
LLM: Callable[..., Any] = _run_llm


# --------------------------------------------------------------------------
# the three agents
# --------------------------------------------------------------------------

async def run_ingest(leagues: list[str], date: str, *, db: str = str(DB_PATH),
                     max_turns: int | None = None, budget: float | None = None) -> LLMResult:
    prompt = (
        f"Date: {date} (Central). Leagues in scope: {', '.join(leagues)}.\n"
        f"Delegate each league to its `ingest-<league>` subagent, then combine "
        f"their reports into one. Database: {db}"
    )
    return await LLM(
        "ingest", prompt, system=prompts.INGEST_SYSTEM,
        max_turns=max_turns or DEFAULT_MAX_TURNS["ingest"],
        budget_usd=budget or DEFAULT_BUDGET_USD["ingest"],
        subagents=_ingest_subagents(),
    )


async def run_market(leagues: list[str], date: str, *, db: str = str(DB_PATH),
                     max_turns: int | None = None, budget: float | None = None) -> LLMResult:
    prompt = (
        f"Date: {date} (Central). Leagues: {', '.join(leagues)}.\n"
        f"Read the money on every game with Kalshi markets and report."
    )
    return await LLM(
        "market", prompt, system=prompts.MARKET_SYSTEM,
        max_turns=max_turns or DEFAULT_MAX_TURNS["market"],
        budget_usd=budget or DEFAULT_BUDGET_USD["market"],
    )


async def run_analyst(leagues: list[str], date: str, *, db: str = str(DB_PATH),
                      context: dict[str, str] | None = None,
                      max_turns: int | None = None, budget: float | None = None) -> LLMResult:
    ctx = context or {}
    prompt = (
        f"Date: {date} (Central). Leagues: {', '.join(leagues)}.\n\n"
        f"Data agent's report:\n{ctx.get('ingest') or '(not run this cycle)'}\n\n"
        f"Market agent's read:\n{ctx.get('market') or '(not run this cycle)'}\n\n"
        f"Now work the slate."
    )
    return await LLM(
        "analyst", prompt, system=prompts.analyst_system(),
        max_turns=max_turns or DEFAULT_MAX_TURNS["analyst"],
        budget_usd=budget or DEFAULT_BUDGET_USD["analyst"],
    )


AGENTS = {"ingest": run_ingest, "market": run_market, "analyst": run_analyst}


# --------------------------------------------------------------------------
# daily tier (deterministic)
# --------------------------------------------------------------------------

async def run_daily(leagues: list[str], *, db: str = str(DB_PATH)) -> dict[str, Any]:
    """Retention, cache trim, integrity audit. No LLM."""
    out: dict[str, Any] = {}
    for lg in leagues:
        out[lg] = {
            "retention": await ingest_tools.run_retention(lg, trim_cache=(lg == leagues[-1]), db=db),
            "audit": await ingest_tools.audit_data_integrity(lg, db=db),
        }
    return out


def _daily_due(conn) -> bool:
    row = store.last_agent_run(conn, "daily", statuses=("ok",))
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(row["started_at"])
    except (TypeError, ValueError):
        return True
    return (datetime.now(last.tzinfo) - last).total_seconds() > DAILY_EVERY_SECONDS


# --------------------------------------------------------------------------
# the hourly run
# --------------------------------------------------------------------------

async def run_auto(
    leagues: list[str] | None = None,
    date: str | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    db: str = str(DB_PATH),
    lock_path: Path | str = LOCK_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One scheduled cycle. Returns what it did, and writes the same to
    `agent_runs`."""
    leagues = leagues or list(LEAGUES)
    for lg in leagues:
        get_league(lg)
    date = date or today_local().strftime("%Y-%m-%d")
    store.init_db(db)
    summary: dict[str, Any] = {"date": date, "leagues": leagues}

    if not force and not in_window(now):
        with store.connect(db) as conn:
            rid = store.start_agent_run(conn, "cycle", ",".join(leagues))
            store.finish_agent_run(conn, rid, "skipped-window", summary=summary)
        return {"status": "skipped-window", **summary}

    try:
        lock = RunLock(lock_path)
        lock.__enter__()
    except Locked as e:
        with store.connect(db) as conn:
            rid = store.start_agent_run(conn, "cycle", ",".join(leagues))
            store.finish_agent_run(conn, rid, "skipped-locked", summary=summary, error=str(e))
        return {"status": "skipped-locked", **summary}

    try:
        return await _run_cycle(leagues, date, force=force, dry_run=dry_run, db=db, summary=summary)
    finally:
        lock.__exit__(None, None, None)


async def _run_cycle(leagues, date, *, force, dry_run, db, summary) -> dict[str, Any]:
    with store.connect(db) as conn:
        # The cycle's own row is `cycle`, not `auto`: the per-league `auto`
        # rows below are what `gate.last_signature` reads, and for a
        # single-league run both would otherwise carry league='ncaamb' --
        # so the gate could pick up this row's nested {league: sig} blob,
        # find no top-level 'volume', and report every market as moved.
        rid = store.start_agent_run(conn, "cycle", ",".join(leagues))
        conn.commit()
    try:
        # Gate, per league; the union of routed agents runs once over all.
        sigs: dict[str, Any] = {}
        diffs: dict[str, Any] = {}
        agents: list[str] = []
        with store.connect(db, readonly=True) as conn:
            prevs = {lg: gate.last_signature(conn, lg) for lg in leagues}
        for lg in leagues:
            sig = await gate.compute_signature(lg, date, db=db)
            d = gate.diff_signatures(prevs[lg], sig)
            sigs[lg] = sig
            diffs[lg] = {k: v for k, v in d.items() if k != "injury_deltas" or v}
            for a in d["agents"]:
                if a not in agents:
                    agents.append(a)
        order = {"ingest": 0, "market": 1, "analyst": 2}
        agents.sort(key=order.get)
        summary["gate"] = {lg: {"digest": sigs[lg]["digest"], "reasons": diffs[lg]["reasons"]} for lg in leagues}
        signature_blob = json.dumps({lg: sigs[lg] for lg in leagues}, default=str)

        if force and not agents:
            agents = ["ingest", "market", "analyst"]
            summary["forced"] = True

        # Daily tier, deterministic, before any LLM.
        with store.connect(db, readonly=True) as conn:
            due = _daily_due(conn)
        if due and not dry_run:
            with store.connect(db) as conn:
                drid = store.start_agent_run(conn, "daily", ",".join(leagues))
                conn.commit()
            try:
                daily = await run_daily(leagues, db=db)
                with store.connect(db) as conn:
                    store.finish_agent_run(conn, drid, "ok", summary=daily)
                summary["daily"] = {lg: daily[lg]["retention"]["removed"] for lg in leagues}
            except Exception as e:
                with store.connect(db) as conn:
                    store.finish_agent_run(conn, drid, "failed", error=f"{type(e).__name__}: {e}")
                summary["daily_error"] = f"{type(e).__name__}: {e}"

        if not agents:
            summary["agents"] = []
            with store.connect(db) as conn:
                # Per-league signature rows so `last_signature` can find them.
                for lg in leagues:
                    lrid = store.start_agent_run(conn, "auto", lg)
                    store.finish_agent_run(conn, lrid, "no-op", signature=json.dumps(sigs[lg], default=str))
                store.finish_agent_run(conn, rid, "no-op", signature=signature_blob, summary=summary)
            return {"status": "no-op", "run_id": rid, **summary}

        summary["agents"] = agents

        # Quota cooldown: a previous cycle hit the account limit. Retrying
        # before the window resets burns more of it and fails the same way.
        with store.connect(db, readonly=True) as conn:
            until = store.get_meta(conn, COOLDOWN_KEY)
        if until and not force:
            try:
                active = datetime.fromisoformat(until) > datetime.now(timezone.utc)
            except (TypeError, ValueError):
                active = False
            if active:
                summary["cooldown_until"] = until
                with store.connect(db) as conn:
                    for lg in leagues:
                        lrid = store.start_agent_run(conn, "auto", lg)
                        store.finish_agent_run(conn, lrid, "skipped-cooldown",
                                               signature=json.dumps(sigs[lg], default=str))
                    store.finish_agent_run(conn, rid, "skipped-cooldown",
                                           signature=signature_blob, summary=summary)
                return {"status": "skipped-cooldown", "run_id": rid, **summary}

        if dry_run:
            with store.connect(db) as conn:
                for lg in leagues:
                    lrid = store.start_agent_run(conn, "auto", lg)
                    store.finish_agent_run(conn, lrid, "dry-run", signature=json.dumps(sigs[lg], default=str))
                store.finish_agent_run(conn, rid, "dry-run", signature=signature_blob, summary=summary)
            return {"status": "dry-run", "run_id": rid, "would_run": agents, **summary}

        # Agents, in dependency order, each logged as its own row.
        context: dict[str, str] = {}
        results: dict[str, Any] = {}
        for name in agents:
            with store.connect(db) as conn:
                arid = store.start_agent_run(conn, name, ",".join(leagues))
                conn.commit()
            fn = AGENTS[name]
            kwargs: dict[str, Any] = {"db": db}
            if name == "analyst":
                kwargs["context"] = context
            res: LLMResult = await fn(leagues, date, **kwargs)
            context[name] = res.text
            results[name] = {
                "cost_usd": res.cost_usd, "turns": res.turns,
                "tool_calls": len(res.tool_calls), "is_error": res.is_error,
                "error": res.error, "chars": len(res.text),
            }
            with store.connect(db) as conn:
                store.finish_agent_run(
                    conn, arid, "failed" if res.is_error else "ok",
                    summary={"report": res.text, **results[name]},
                    writes={"tool_calls": res.tool_calls},
                    error=res.error,
                )
            if res.rate_limited:
                # The account quota, not this run. Set the cooldown so the next
                # cycles skip the LLM agents rather than burning more of it,
                # and stop here -- later agents would hit the same wall.
                until = (datetime.now(timezone.utc) + timedelta(seconds=COOLDOWN_SECONDS)).isoformat(timespec="seconds")
                with store.connect(db) as conn:
                    store.put_meta(conn, COOLDOWN_KEY, until)
                summary["cooldown_until"] = until
                results[name]["rate_limited"] = True
                break
            if res.is_error and name == "ingest":
                # Downstream agents would reason over data the ingest did not
                # confirm; stop and let the next hour retry.
                break

        summary["results"] = results
        if any(r.get("rate_limited") for r in results.values()):
            status = "rate-limited"
        elif any(r["is_error"] for r in results.values()):
            status = "failed"
        else:
            status = "ok"
        with store.connect(db) as conn:
            for lg in leagues:
                lrid = store.start_agent_run(conn, "auto", lg)
                store.finish_agent_run(conn, lrid, status, signature=json.dumps(sigs[lg], default=str))
            store.finish_agent_run(conn, rid, status, signature=signature_blob, summary=summary)
        return {"status": status, "run_id": rid, **summary}

    except Exception as e:
        with store.connect(db) as conn:
            store.finish_agent_run(
                conn, rid, "failed", summary=summary,
                error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            )
        return {"status": "failed", "run_id": rid, "error": f"{type(e).__name__}: {e}", **summary}


async def run_one(agent: str, leagues: list[str] | None, date: str | None, *,
                  db: str = str(DB_PATH)) -> dict[str, Any]:
    """Run a single named agent now, bypassing window, lock and gate."""
    leagues = leagues or list(LEAGUES)
    date = date or today_local().strftime("%Y-%m-%d")
    store.init_db(db)
    if agent == "daily":
        with store.connect(db) as conn:
            rid = store.start_agent_run(conn, "daily", ",".join(leagues))
            conn.commit()
        out = await run_daily(leagues, db=db)
        with store.connect(db) as conn:
            store.finish_agent_run(conn, rid, "ok", summary=out)
        return {"status": "ok", "run_id": rid, "daily": out}
    if agent not in AGENTS:
        raise KeyError(f"unknown agent {agent!r}; known: {sorted(AGENTS)} + ['daily']")
    with store.connect(db) as conn:
        rid = store.start_agent_run(conn, agent, ",".join(leagues))
        conn.commit()
    res: LLMResult = await AGENTS[agent](leagues, date, db=db)
    with store.connect(db) as conn:
        store.finish_agent_run(
            conn, rid, "failed" if res.is_error else "ok",
            summary={"report": res.text, "cost_usd": res.cost_usd, "turns": res.turns},
            writes={"tool_calls": res.tool_calls}, error=res.error,
        )
    return {
        "status": "failed" if res.is_error else "ok", "run_id": rid,
        "report": res.text, "cost_usd": res.cost_usd, "turns": res.turns,
        "tool_calls": res.tool_calls, "error": res.error,
    }
