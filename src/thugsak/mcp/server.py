"""stdio MCP server, for Claude Code and Claude Desktop.

A thin adapter: every tool's implementation lives in `tools.py`, which the
in-process Streamlit adapter also wraps, so the two surfaces cannot drift.

Written against **mcp 2.x**, where the high-level server is `MCPServer` (the
old `FastMCP`) and the v1 `@server.list_tools()` / `@server.call_tool()`
decorators on the low-level `Server` no longer exist. Tools are registered by
passing each handler to `add_tool`, which derives the schema from the
function's own signature.

Register in `.mcp.json`:

    {
      "mcpServers": {
        "thugsak": {
          "command": ".venv/Scripts/python.exe",
          "args": ["-m", "thugsak.mcp.server"],
          "env": {"PYTHONPATH": "src"}
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import tools

log = logging.getLogger("thugsak.mcp")

INSTRUCTIONS = """\
Read-only analysis of Kalshi sports markets (NFL, NBA, NCAAF, NCAAMB).

Start with list_games for a date, then get_prediction and get_edges for a
specific game_id, then get_money_flow on any market that looks mispriced.

Spreads are home-relative: positive means the home team is favoured. Every
edge is already net of Kalshi fees. Rank on `adjusted_edge`, not `net_edge` --
it scales the raw figure by how much the model is measurably better than
predicting the league average, and a league where it has no demonstrated skill
should produce no recommendations.

When many strikes on one ladder share a direction, they are one position, not
many; `ladder_warnings` says so explicitly.
"""


_JSON_TO_PY = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _make_handler(spec: dict[str, Any]):
    """Wrap a tool handler so failures return as data, not exceptions.

    A tool that raises aborts the model's turn; one that returns an error lets
    it adapt or report the problem.

    The wrapper is given an explicit signature built from the tool's JSON
    schema. `add_tool` introspects the function to publish its parameters, so
    a bare ``**kwargs`` wrapper would advertise a tool that takes no arguments
    -- it would register cleanly and then be uncallable.
    """
    import inspect

    handler = spec["handler"]
    schema = spec.get("schema") or {}
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    async def run(**kwargs: Any) -> str:
        # Drop unset optionals so each handler's own defaults apply.
        args = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = await handler(**args)
        except Exception as e:
            log.exception("tool %s failed", spec["name"])
            result = {
                "error": f"{type(e).__name__}: {e}",
                "tool": spec["name"],
                "arguments": args,
            }
        return json.dumps(tools._json(result), indent=2, default=str)

    params, annotations = [], {}
    for name, p in props.items():
        py = _JSON_TO_PY.get(p.get("type", "string"), str)
        has_default = name not in required
        default = p.get("default", None) if has_default else inspect.Parameter.empty
        annotations[name] = py if not has_default else (py | None)
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotations[name],
            )
        )
    # Required parameters first, so the synthesised signature is valid.
    params.sort(key=lambda prm: prm.default is not inspect.Parameter.empty)

    annotations["return"] = str
    run.__signature__ = inspect.Signature(params, return_annotation=str)
    run.__annotations__ = annotations
    run.__name__ = spec["name"]
    run.__doc__ = spec["description"]
    return run


def build_server() -> Any:
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="thugsak",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )
    for spec in tools.TOOL_SCHEMAS:
        server.add_tool(
            _make_handler(spec),
            name=spec["name"],
            description=spec["description"],
        )
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
