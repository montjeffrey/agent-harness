"""Stdio MCP server exposing the graph-query CLI as 7 typed MCP tools.

Usage:
    python -m harness.mcp_server [--graph PATH]
    agent-harness-mcp [--graph PATH]

Requires the optional `mcp` Python SDK:  pip install agent-harness[mcp]

Module-level code imports only the standard library plus
`harness.graph_query` -- `import harness.mcp_server` never requires the `mcp`
SDK to be installed. The SDK is imported lazily, inside `main()`, so
`python -m harness.mcp_server` fails with a clear, actionable message when
the optional dependency is absent instead of an opaque ImportError.

Tools (all thin wrappers around `harness.graph_query.query`):
    graph_find      -- search node labels/ids/files for a term
    graph_node      -- node details + direct edges
    graph_callers   -- who depends on this node (depth n, default 1)
    graph_callees   -- what this node depends on (depth n, default 1)
    graph_impact    -- full transitive blast radius (unbounded depth)
    graph_file      -- nodes defined in files matching a path fragment
    graph_community -- nodes in communities matching a name fragment

Output is capped by `harness.graph_query.MAX_ROWS` (no separate
protocol-visible `limit` parameter -- callers get the same row cap the CLI
already enforces).
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness import graph_query

# id -> graph_query command name. Tool names are MCP-facing and don't have
# to match the underlying CLI command 1:1 (e.g. "impact" already reads fine
# as both), but this mapping is the single place that reconciles them.
TOOL_TO_COMMAND: dict[str, str] = {
    "graph_find": "find",
    "graph_node": "node",
    "graph_callers": "callers",
    "graph_callees": "callees",
    "graph_impact": "impact",
    "graph_file": "file",
    "graph_community": "community",
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "graph_find": "Search node labels/ids/files for a term in the code graph.",
    "graph_node": "Show a node's details and its direct in/out edges.",
    "graph_callers": "List who depends on this node (traversal depth n, default 1).",
    "graph_callees": "List what this node depends on (traversal depth n, default 1).",
    "graph_impact": "Full transitive blast radius: every node that transitively depends on this one.",
    "graph_file": "List nodes defined in files matching a path fragment.",
    "graph_community": "List nodes in communities matching a name fragment.",
}

# --graph PATH override, set by main() before the server starts serving.
_GRAPH_OVERRIDE: Path | None = None


def dispatch(tool_name: str, arguments: dict) -> str:
    """Run one tool call and return its output as a single joined string.

    Never raises for expected failure modes -- `ValueError` (bad command, no
    match, ambiguous match, malformed graph) and `FileNotFoundError` (no
    graph.json discoverable) from `graph_query.query` are caught and their
    message returned as plain text, so a bad tool call surfaces as a normal
    tool result instead of crashing the server. Structured so tests can
    exercise the dispatch logic without the `mcp` SDK installed.
    """
    command = TOOL_TO_COMMAND.get(tool_name)
    if command is None:
        return f"unknown tool '{tool_name}' -- choose from {', '.join(TOOL_TO_COMMAND)}"

    term = arguments.get("term", "")
    depth = arguments.get("depth", 1)
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        return f"invalid depth {depth!r} -- must be an integer"

    try:
        lines = graph_query.query(command, term, depth, _GRAPH_OVERRIDE)
    except (ValueError, FileNotFoundError) as exc:
        return str(exc)
    return "\n".join(lines)


def _parse_argv(argv: list[str]) -> Path | None:
    """Pull an optional `--graph PATH` override out of argv. Anything else
    is ignored -- this server takes no other CLI flags."""
    graph_path: Path | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--graph" and i + 1 < len(argv):
            graph_path = Path(argv[i + 1])
            i += 2
            continue
        i += 1
    return graph_path


def _build_server():
    """Construct the FastMCP server and register all 7 tools.

    Imports the `mcp` SDK lazily -- only called from `main()`, never at
    module import time.
    """
    from mcp.server.fastmcp import FastMCP

    mcp_app = FastMCP("agent-harness")

    def _make_tool(tool_name: str):
        def _tool(term: str, depth: int = 1) -> str:
            return dispatch(tool_name, {"term": term, "depth": depth})

        _tool.__name__ = tool_name
        _tool.__doc__ = TOOL_DESCRIPTIONS[tool_name]
        return _tool

    for name in TOOL_TO_COMMAND:
        mcp_app.tool(name=name, description=TOOL_DESCRIPTIONS[name])(_make_tool(name))

    return mcp_app


def main(argv: list[str] | None = None) -> int:
    global _GRAPH_OVERRIDE

    args = sys.argv[1:] if argv is None else argv
    _GRAPH_OVERRIDE = _parse_argv(args)

    try:
        import mcp  # noqa: F401
    except ImportError:
        print(
            "harness.mcp_server requires the optional 'mcp' Python SDK.\n"
            "install with: pip install agent-harness[mcp]",
            file=sys.stderr,
        )
        return 1

    server = _build_server()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
