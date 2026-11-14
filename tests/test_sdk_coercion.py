"""Two things the first unattended run taught, pinned.

1. The SDK tool boundary hands integers and booleans to handlers as strings.
   `thin_games` arrived as "4", `0 < n <= "4"` raised inside the audit, and
   the ingest agent reported a crash instead of findings. `coerce_args` casts
   by the declared schema type before the handler sees the value.

2. `allowed_tools` pre-approves but does not restrict. The ingest agent
   reached for Grep and ToolSearch (fine) and had considered editing the
   repository (not fine). The write builtins are disallowed outright, and the
   per-sport subagents are confined to the ingest tools by name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.agents import runner  # noqa: E402
from thugsak.mcp import sdk_server, tools  # noqa: E402

PROPS = {
    "league": {"type": "string"},
    "thin_games": {"type": "integer", "default": 2},
    "contracts": {"type": "integer"},
    "trim_cache": {"type": "boolean"},
    "team_ids": {"type": "array", "items": {"type": "string"}},
    "ratio": {"type": "number"},
}


def test_integer_strings_become_ints():
    out = sdk_server.coerce_args({"thin_games": "4", "contracts": "100"}, PROPS)
    assert out == {"thin_games": 4, "contracts": 100}
    assert all(isinstance(v, int) for v in out.values())


def test_already_typed_values_are_untouched():
    out = sdk_server.coerce_args({"thin_games": 3, "trim_cache": False, "ratio": 0.5}, PROPS)
    assert out == {"thin_games": 3, "trim_cache": False, "ratio": 0.5}


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("no", False), ("", False),
])
def test_boolean_strings(raw, expected):
    assert sdk_server.coerce_args({"trim_cache": raw}, PROPS)["trim_cache"] is expected


def test_array_given_as_json_string_is_parsed():
    out = sdk_server.coerce_args({"team_ids": '["nba:BOS", "nba:NYK"]'}, PROPS)
    assert out["team_ids"] == ["nba:BOS", "nba:NYK"]


def test_array_given_as_list_passes_through():
    out = sdk_server.coerce_args({"team_ids": ["nba:BOS"]}, PROPS)
    assert out["team_ids"] == ["nba:BOS"]


def test_number_string_becomes_float():
    assert sdk_server.coerce_args({"ratio": "0.25"}, PROPS)["ratio"] == 0.25


def test_unknown_keys_and_none_pass_through():
    out = sdk_server.coerce_args({"mystery": "x", "thin_games": None}, PROPS)
    assert out == {"mystery": "x", "thin_games": None}


def test_uncastable_value_is_left_for_the_handler_to_reject():
    # The handler's error names the argument; a cast error here would not.
    out = sdk_server.coerce_args({"thin_games": "four"}, PROPS)
    assert out["thin_games"] == "four"


def test_every_registered_tool_schema_is_coercible():
    # Smoke: coerce a string for every integer/boolean property of every tool
    # and make sure none raises and each lands typed.
    for spec in tools.TOOL_SCHEMAS:
        props = spec["schema"]["properties"]
        sample = {}
        for k, p in props.items():
            if p.get("type") == "integer":
                sample[k] = "7"
            elif p.get("type") == "boolean":
                sample[k] = "false"
        out = sdk_server.coerce_args(sample, props)
        for k, v in out.items():
            t = props[k]["type"]
            assert isinstance(v, int if t == "integer" else bool), (spec["name"], k, v)


# --- the write lockout --------------------------------------------------------


def test_write_builtins_are_disallowed():
    for name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
        assert name in runner.WRITE_TOOLS


def test_read_builtins_are_not_disallowed():
    # An agent that can read the source of a failing tool writes a better bug
    # report. That is a feature, and it did so in the first live run.
    for name in ("Read", "Grep", "Glob", "ToolSearch"):
        assert name not in runner.WRITE_TOOLS


def test_ingest_subagents_are_confined_to_ingest_tools():
    pytest.importorskip("claude_agent_sdk")
    subs = runner._ingest_subagents()
    assert set(subs) == {"ingest-nfl", "ingest-nba", "ingest-ncaaf", "ingest-ncaamb"}
    for name, a in subs.items():
        assert a.tools, name
        for t in a.tools:
            assert t.startswith("mcp__thugsak__"), (name, t)
            short = t.split("__")[-1]
            assert short.startswith("ingest_") or short in ("audit_data_integrity", "link_team_alias"), (name, t)
        # No subagent may run retention on its own; that is the daily tier's.
        assert "mcp__thugsak__run_retention" not in a.tools
