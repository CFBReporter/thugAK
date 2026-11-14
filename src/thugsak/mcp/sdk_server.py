"""In-process MCP server for the Streamlit app, via the Claude Agent SDK.

Same handlers as the stdio server -- both wrap `tools.TOOL_SCHEMAS` -- but
these run inside the Streamlit process, so a game-day session makes no extra
subprocess and shares the HTTP cache with the UI.

The SDK inherits the machine's existing Claude Code credentials, so no API key
is configured anywhere in this project.
"""

from __future__ import annotations

import json
from typing import Any

from . import tools

SERVER_NAME = "thugsak"


def build_sdk_server() -> Any:
    """Wrap each handler as an SDK tool and return an in-process MCP server."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    wrapped = []
    for spec in tools.TOOL_SCHEMAS:
        wrapped.append(_wrap(spec, tool))
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=wrapped)


def _wrap(spec: dict[str, Any], tool_decorator: Any) -> Any:
    """Adapt one tool spec to the SDK's decorator form.

    The SDK passes a single dict of arguments and expects content blocks back,
    whereas the handlers take keyword arguments and return plain data, so the
    translation happens here rather than in every handler.
    """
    handler = spec["handler"]
    properties = (spec["schema"].get("properties") or {})

    @tool_decorator(spec["name"], spec["description"], properties)
    async def _run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await handler(**coerce_args(args or {}, properties))
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}", "tool": spec["name"]}
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(tools._json(result), indent=2, default=str),
                }
            ]
        }

    return _run


def coerce_args(args: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    """Cast each argument to the type its schema declares.

    The SDK's `@tool` decorator receives the schema but, in practice, hands
    the handler integers and booleans as strings -- `thin_games` arrived as
    `"4"` and `0 < n <= "4"` raised TypeError inside the audit, which the
    ingest agent then reported as a crash in its first unattended run. The
    stdio adapter never had this problem because it synthesises a typed
    signature; this is the equivalent for the in-process path.

    Unknown keys pass through untouched so a handler's own validation still
    sees them. A value that cannot be cast is left as-is for the same reason:
    the handler's error names the argument, a cast error here would not.
    """
    out: dict[str, Any] = {}
    for k, v in args.items():
        spec = properties.get(k) or {}
        t = spec.get("type")
        if v is None or t is None:
            out[k] = v
            continue
        try:
            if t == "integer" and not isinstance(v, bool):
                out[k] = int(v) if not isinstance(v, int) else v
            elif t == "number":
                out[k] = float(v) if not isinstance(v, (int, float)) else v
            elif t == "boolean":
                if isinstance(v, str):
                    out[k] = v.strip().lower() in ("1", "true", "yes", "y", "on")
                else:
                    out[k] = bool(v)
            elif t == "array" and isinstance(v, str):
                parsed = json.loads(v)
                out[k] = parsed if isinstance(parsed, list) else [parsed]
            elif t == "object" and isinstance(v, str):
                out[k] = json.loads(v)
            else:
                out[k] = v
        except (TypeError, ValueError):
            out[k] = v
    return out


def allowed_tool_names() -> list[str]:
    """Fully-qualified names to pre-approve, so no permission prompt appears.

    The SDK namespaces in-process tools as ``mcp__<server>__<tool>``.
    """
    return [f"mcp__{SERVER_NAME}__{t['name']}" for t in tools.TOOL_SCHEMAS]
