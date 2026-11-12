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
            result = await handler(**(args or {}))
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


def allowed_tool_names() -> list[str]:
    """Fully-qualified names to pre-approve, so no permission prompt appears.

    The SDK namespaces in-process tools as ``mcp__<server>__<tool>``.
    """
    return [f"mcp__{SERVER_NAME}__{t['name']}" for t in tools.TOOL_SCHEMAS]
