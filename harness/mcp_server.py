"""Stdio MCP server exposing code-graph queries and project memory as 10
typed MCP tools.

Usage:
    python -m harness.mcp_server [--graph PATH]
    agent-harness-mcp [--graph PATH]

Requires the optional `mcp` Python SDK. From this checkout install it with:
    python -m pip install -e ".[mcp]"

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

    memory_context  -- budgeted context packet: scope, open work, knowledge
    memory_status   -- entry counts, freshness, what awaits review
    memory_remember -- record one PROPOSED entry for human review

Graph output is capped by `harness.graph_query.MAX_ROWS` (no separate
protocol-visible `limit` parameter -- callers get the same row cap the CLI
already enforces). Memory output is capped by the context packet's own
character budget.

There is deliberately no review/accept tool. Review is the human gate that
makes a proposal authoritative; an agent able to accept its own inferred
entries would defeat the memory design's central trust rule. `memory_remember`
therefore hardcodes `proposed`/`inferred` rather than taking them as
parameters.
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import NamedTuple

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

# The MCP server is deliberately long-lived, unlike a SessionStart hook. Keep
# only a few parsed graph/index pairs in-process and key each one by the
# resolved graph path plus its stat signature. Explicit --graph overrides are
# intentionally left uncached: they are also the test/integration override
# seam, and preserving their one-call-at-a-time behavior avoids surprising a
# caller that swaps an override target while a server process is alive.
_GRAPH_CACHE_LIMIT = 4


class _CachedGraph(NamedTuple):
    signature: tuple[int, int]
    graph: dict
    index: graph_query.GraphIndex
    symbol_index: object | None = None
    symbol_index_ready: bool = False


_GRAPH_CACHE: OrderedDict[Path, _CachedGraph] = OrderedDict()


def _discover_graph_path() -> Path:
    """Find the default graph with the same discovery/error wording as CLI."""
    return graph_query.find_graph_path()


def _stat_signature(graph_path: Path) -> tuple[Path, tuple[int, int]]:
    """Resolve a graph path and return the content-change cache signature."""
    resolved = graph_path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        _GRAPH_CACHE.pop(resolved, None)
        raise FileNotFoundError(f"graph.json not found at {resolved}") from exc
    return resolved, (stat.st_mtime_ns, stat.st_size)


def _cached_graph(
    include_symbol_index: bool = False,
) -> tuple[Path, dict, graph_query.GraphIndex, object | None]:
    """Load the discovered graph once per unchanged stat signature.

    Any failed stat/read/index attempt evicts its old entry. A later repaired
    graph therefore reloads instead of serving stale cached data.
    """
    try:
        discovered = _discover_graph_path()
    except SystemExit as exc:
        message = exc.code if isinstance(exc.code, str) else str(exc.code)
        raise FileNotFoundError(message) from exc

    resolved, signature = _stat_signature(discovered)
    cached = _GRAPH_CACHE.get(resolved)
    if cached is not None and cached.signature == signature:
        if include_symbol_index and not cached.symbol_index_ready:
            from harness import memory_graph

            cached = cached._replace(
                symbol_index=memory_graph.build_symbol_index(cached.graph),
                symbol_index_ready=True,
            )
            _GRAPH_CACHE[resolved] = cached
        _GRAPH_CACHE.move_to_end(resolved)
        return resolved, cached.graph, cached.index, cached.symbol_index

    try:
        graph = graph_query.load_graph(resolved)
        index = graph_query.build_index(graph)
    except SystemExit as exc:
        _GRAPH_CACHE.pop(resolved, None)
        message = exc.code if isinstance(exc.code, str) else str(exc.code)
        raise ValueError(message) from exc
    except json.JSONDecodeError as exc:
        _GRAPH_CACHE.pop(resolved, None)
        raise ValueError(f"graph.json malformed: invalid JSON ({exc})") from exc
    except UnicodeDecodeError as exc:
        _GRAPH_CACHE.pop(resolved, None)
        raise ValueError(f"graph.json unreadable at {resolved}: invalid UTF-8 ({exc})") from exc
    except OSError as exc:
        _GRAPH_CACHE.pop(resolved, None)
        raise ValueError(f"graph.json unreadable at {resolved}: {exc}") from exc

    symbol_index = None
    if include_symbol_index:
        from harness import memory_graph

        symbol_index = memory_graph.build_symbol_index(graph)
    _GRAPH_CACHE[resolved] = _CachedGraph(
        signature, graph, index, symbol_index, include_symbol_index
    )
    _GRAPH_CACHE.move_to_end(resolved)
    while len(_GRAPH_CACHE) > _GRAPH_CACHE_LIMIT:
        _GRAPH_CACHE.popitem(last=False)
    return resolved, graph, index, symbol_index


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
        memory = str(arguments.get("memory", "auto"))
        if _GRAPH_OVERRIDE is None:
            graph_path, loaded_graph, prebuilt_index, symbol_index = _cached_graph(
                memory in {"auto", "on"}
            )
            lines = graph_query.query(
                command,
                term,
                depth,
                graph_path,
                memory,
                loaded_graph=loaded_graph,
                prebuilt_index=prebuilt_index,
                symbol_index=symbol_index,
            )
        else:
            lines = graph_query.query(command, term, depth, _GRAPH_OVERRIDE, memory)
    except (ValueError, FileNotFoundError) as exc:
        return str(exc)
    return "\n".join(lines)


MEMORY_TOOL_DESCRIPTIONS: dict[str, str] = {
    "memory_context": (
        "Read this project's memory as a token-budgeted context packet: scope, "
        "open work, and the most relevant durable project and machine knowledge. "
        "Call this before starting work to recover what was already decided. "
        "Entries naming a symbol the code graph knows also carry that symbol's "
        "blast radius -- how many dependents it has and which files they are "
        "in -- so a past decision arrives with the code it actually touches."
    ),
    "memory_status": (
        "Summarize this project's memory: entry counts by status, how fresh it "
        "is, and what is awaiting human review."
    ),
    "memory_remember": (
        "Record one decision, discovery, task, blocker, or lesson as a PROPOSED "
        "entry for human review. Use a single concise, durable sentence. Never "
        "record credentials."
    ),
    "memory_confirm": (
        "Record a plan the human just approved IN THIS CONVERSATION, together "
        "with its steps as tracked action tasks. Pass their actual words as "
        "`confirmation` -- it is stored as the evidence of approval. Only call "
        "this after they have actually agreed; never to record your own "
        "intention."
    ),
    "memory_task_done": (
        "Mark one tracked action task complete. Supply evidence of completion "
        "via `note`, `commit`, or `file` -- a task cannot be closed without it."
    ),
    "memory_tasks": (
        "List open action tasks in priority order, plus the topics that work "
        "keeps stalling on (bottlenecks). Call this to decide what to pick up."
    ),
}


def dispatch_memory(tool_name: str, arguments: dict) -> str:
    """Run one memory tool call and return plain text.

    Kept separate from `dispatch` (which is `(term, depth)`-shaped for graph
    queries) and free of the `mcp` SDK, so tests exercise it without either
    optional dependency installed. Expected failures are returned as text
    rather than raised, matching `dispatch`: a bad call should read as a tool
    result, not crash the server for the rest of the session.
    """
    from harness import memory_context, memory_index, memory_store

    root = Path(arguments.get("project") or ".").expanduser()

    try:
        if tool_name == "memory_context":
            project_id = memory_store.project_id_for(root)
            # `auto` is right here and wrong in a hook: this server is long
            # lived, so a one-time chromadb import amortizes over the whole
            # session instead of being paid at every session start.
            try:
                selector, strategy, notes = memory_index.resolve_selector(
                    project_id, "auto"
                )
            except FileNotFoundError as exc:
                return str(exc)
            if arguments.get("cross_check") and strategy == "semantic":
                project_entries = list(
                    memory_store.iter_entries(memory_store.project_memory_dir(root))
                )
                machine_entries = list(
                    memory_store.iter_entries(memory_store.machine_memory_dir())
                )
                open_ids = {
                    entry.get("id") for entry in memory_context._open_work(project_entries)
                }
                candidate_groups = (
                    [
                        entry
                        for entry in project_entries
                        if memory_context._is_live(entry)
                        and entry.get("type") != "relationship"
                        and entry.get("id") not in open_ids
                    ],
                    [
                        entry
                        for entry in machine_entries
                        if memory_context._is_live(entry)
                        and entry.get("type") != "relationship"
                    ],
                    [
                        entry
                        for entry in project_entries + machine_entries
                        if entry.get("type") == "relationship"
                        and entry.get("status") == "accepted"
                    ],
                )
                try:
                    check_limit = int(
                        arguments.get("cross_check_limit")
                        or memory_context.CANDIDATE_LIMIT
                    )
                except (TypeError, ValueError):
                    return "cross_check_limit must be an integer"
                agreements = [
                    memory_index.top_k_agreement(
                        candidates,
                        arguments.get("query") or memory_context.active_objective(root),
                        selector,
                        check_limit,
                    )
                    for candidates in candidate_groups
                ]
                overlap = sum(int(agreement["overlap"]) for agreement in agreements)
                limit = sum(int(agreement["limit"]) for agreement in agreements)
                notes = [
                    *notes,
                    "selection agreement: "
                    f"{overlap}/{limit} shared top-K "
                    "between deterministic and semantic ranking",
                ]
            # Join memory to the code graph, so a recalled decision arrives
            # with the symbols it touches instead of leaving the agent to
            # guess. A silent no-op when this repo has no graph.
            from harness import memory_graph

            annotator, graph_notes = memory_graph.build_annotator(root)
            packet = memory_context.build_packet(
                root,
                query=arguments.get("query") or None,
                max_chars=int(arguments.get("max_chars") or 4000),
                selector=selector,
                strategy=strategy,
                notes=list(notes) + graph_notes,
                annotator=annotator,
            )
            return memory_context.render_markdown(packet)

        if tool_name == "memory_status":
            entries = list(
                memory_store.iter_entries(memory_store.project_memory_dir(root))
            )
            if not entries:
                return f"no memory recorded yet in {root}"
            by_status: dict[str, int] = {}
            for rec in entries:
                key = str(rec.get("status", "?"))
                by_status[key] = by_status.get(key, 0) + 1
            counts = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))
            return (
                f"project {memory_store.project_id_for(root)}: {len(entries)} "
                f"entries ({counts}); newest {entries[-1].get('ts', '?')}"
            )

        if tool_name == "memory_remember":
            summary = str(arguments.get("summary") or "").strip()
            if not summary:
                return "summary is required -- one concise, durable sentence"
            entry_type = str(arguments.get("type") or "")
            if entry_type not in memory_store.ENTRY_TYPES:
                return (
                    f"unknown type {entry_type!r} -- choose from "
                    f"{', '.join(memory_store.ENTRY_TYPES)}"
                )
            tags = [t.strip() for t in str(arguments.get("tags") or "").split(",")]
            entry: dict = {
                "type": entry_type,
                "scope": "project",
                "summary": summary,
                # Hardcoded, not caller-controlled. An agent recording its own
                # observation is exactly the "inferred" case, and the design's
                # central trust rule is that nothing automatic may make itself
                # authoritative. Promotion is `review`, which is deliberately
                # not exposed as a tool.
                "status": "proposed",
                "confidence": "inferred",
                "source": {"project": memory_store.project_id_for(root)},
            }
            if any(tags):
                entry["tags"] = [t for t in tags if t]
            memory_store.append_entries(
                [entry], directory=memory_store.project_memory_dir(root)
            )
            written = list(
                memory_store.iter_entries(memory_store.project_memory_dir(root))
            )[-1]
            return (
                f"recorded {written['id']} as proposed; it becomes authoritative "
                f"only after `agent-harness-memory review accept {written['id']}`"
            )
        if tool_name == "memory_confirm":
            from harness import memory_tasks

            steps = [
                s.strip()
                for s in str(arguments.get("tasks") or "").split("\n")
                if s.strip()
            ]
            tags = [
                t.strip()
                for t in str(arguments.get("tags") or "").split(",")
                if t.strip()
            ]
            outcome = memory_tasks.confirm(
                memory_store.project_memory_dir(root),
                summary=str(arguments.get("summary") or ""),
                confirmation=str(arguments.get("confirmation") or ""),
                tasks=steps,
                tags=tags,
                project_id=memory_store.project_id_for(root),
            )
            lines = [
                f"confirmed {outcome.decision['id']}: {outcome.decision['summary']}"
            ]
            lines += [f"  task {t['id']}: {t['summary']}" for t in outcome.tasks]
            lines.append(
                "Close each with memory_task_done once it is verifiably finished."
            )
            return "\n".join(lines)

        if tool_name == "memory_task_done":
            from harness import memory_tasks

            entry = memory_tasks.complete(
                memory_store.project_memory_dir(root),
                str(arguments.get("entry_id") or ""),
                note=arguments.get("note") or None,
                commit=arguments.get("commit") or None,
                file=arguments.get("file") or None,
            )
            return f"{entry['id']} -> resolved: {entry['summary']}"

        if tool_name == "memory_tasks":
            from harness import memory_tasks

            entries = list(
                memory_store.iter_entries(memory_store.project_memory_dir(root))
            )
            ranked = memory_tasks.prioritized(entries)
            stuck = memory_tasks.bottlenecks(entries)
            if not ranked:
                return "no open action tasks"
            lines = [f"{len(ranked)} open, in priority order:"]
            lines += [
                f"  {e['id']} [{e.get('type')}] {e.get('summary')}" for e in ranked
            ]
            if stuck:
                lines.append("\nbottlenecks:")
                lines += [f"  {b.describe()}" for b in stuck]
            return "\n".join(lines)

    except (ValueError, LookupError, OSError) as exc:
        return f"memory tool failed: {exc}"

    return (
        f"unknown tool '{tool_name}' -- choose from "
        f"{', '.join(MEMORY_TOOL_DESCRIPTIONS)}"
    )


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
    """Construct the FastMCP server and register all 10 tools.

    Imports the `mcp` SDK lazily -- only called from `main()`, never at
    module import time.

    Note what is absent: there is no `memory_review` / `memory_accept` tool.
    Review is the human gate that turns a proposal into authoritative
    knowledge, and handing an agent the ability to accept its own inferred
    entries would collapse the one trust boundary the memory design is built
    around. Promotion stays on the CLI, in front of a person.
    """
    from mcp.server.fastmcp import FastMCP

    mcp_app = FastMCP("agent-harness")

    def _make_graph_tool(tool_name: str):
        def _tool(term: str, depth: int = 1, memory: str = "auto") -> str:
            return dispatch(tool_name, {"term": term, "depth": depth, "memory": memory})

        _tool.__name__ = tool_name
        _tool.__doc__ = TOOL_DESCRIPTIONS[tool_name]
        return _tool

    for name in TOOL_TO_COMMAND:
        mcp_app.tool(name=name, description=TOOL_DESCRIPTIONS[name])(
            _make_graph_tool(name)
        )

    def memory_context(
        query: str = "",
        max_chars: int = 4000,
        project: str = "",
        cross_check: bool = False,
        cross_check_limit: int = 0,
    ) -> str:
        return dispatch_memory(
            "memory_context",
            {
                "query": query,
                "max_chars": max_chars,
                "project": project,
                "cross_check": cross_check,
                "cross_check_limit": cross_check_limit,
            },
        )

    def memory_status(project: str = "") -> str:
        return dispatch_memory("memory_status", {"project": project})

    def memory_remember(
        type: str, summary: str, tags: str = "", project: str = ""
    ) -> str:
        return dispatch_memory(
            "memory_remember",
            {"type": type, "summary": summary, "tags": tags, "project": project},
        )

    def memory_confirm(
        summary: str,
        confirmation: str,
        tasks: str = "",
        tags: str = "",
        project: str = "",
    ) -> str:
        return dispatch_memory(
            "memory_confirm",
            {
                "summary": summary,
                "confirmation": confirmation,
                "tasks": tasks,
                "tags": tags,
                "project": project,
            },
        )

    def memory_task_done(
        entry_id: str,
        note: str = "",
        commit: str = "",
        file: str = "",
        project: str = "",
    ) -> str:
        return dispatch_memory(
            "memory_task_done",
            {
                "entry_id": entry_id,
                "note": note,
                "commit": commit,
                "file": file,
                "project": project,
            },
        )

    def memory_tasks(project: str = "") -> str:
        return dispatch_memory("memory_tasks", {"project": project})

    for func in (
        memory_context,
        memory_status,
        memory_remember,
        memory_confirm,
        memory_task_done,
        memory_tasks,
    ):
        func.__doc__ = MEMORY_TOOL_DESCRIPTIONS[func.__name__]
        mcp_app.tool(
            name=func.__name__, description=MEMORY_TOOL_DESCRIPTIONS[func.__name__]
        )(func)

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
            'from this checkout install with: python -m pip install -e ".[mcp]"',
            file=sys.stderr,
        )
        return 1

    server = _build_server()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
