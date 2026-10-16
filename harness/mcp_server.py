"""Stdio MCP server exposing code-graph queries, project memory, and task
workspaces as 21 typed MCP tools.

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
    memory_search   -- focused top-k entries for one topic, optionally
                       across other registered projects on this machine
    memory_status   -- entry counts, freshness, what awaits review
    memory_remember -- record one PROPOSED entry for human review
    memory_confirm  -- record a plan the human just approved, plus its steps
    memory_task_done -- mark one tracked action task complete
    memory_tasks    -- open tasks in priority order, plus bottlenecks and claims
    memory_claim    -- claim an open task/blocker for an agent program
    memory_release  -- release a held claim
    memory_curate   -- verify document pointers and reduce duplicate proposals

    work_start       -- claim exact paths and prepare a task branch/worktree
    work_status      -- show active/finished task records for this repository
    work_diff        -- show a task's tracked diff from its immutable base
    work_finish      -- release a completed task's path claims

Graph output is capped by `harness.graph_query.MAX_ROWS` (no separate
protocol-visible `limit` parameter -- callers get the same row cap the CLI
already enforces). Memory output is capped by the context packet's own
character budget.

There is deliberately no general review/accept tool. An agent able to accept
its own inferred entries would defeat the memory design's central trust rule.
`memory_curate` is the narrow deterministic exception: it can verify a source
digest for an ingested document pointer, never an agent's conclusion.
`memory_remember` therefore hardcodes `proposed`/`inferred` rather than taking
them as parameters.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, NamedTuple

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


# `memory_graph.build_annotator` asks `graph_location.report_health` for the
# graph's freshness caveat, and that shells out to git twice (status, rev-parse).
# In this long-lived server those two subprocesses repeat on every single
# `memory_context` call for a verdict that rarely changes, so the check is
# lifted out of the annotator and memoized here, mirroring `_cached_graph`.
#
# The key is the graph file's stat signature (a rebuild invalidates it) plus a
# short TTL: a dirty -> clean working-tree transition never touches graph.json,
# so only elapsed time can notice it. Seconds, not minutes -- the point is to
# collapse a burst of calls, not to hold a verdict across a work session.
#
# Deliberately in mcp_server and not in graph_location: the CLI is short-lived
# and one-shot, and must keep asking git for the current truth every run.
_HEALTH_TTL_SECONDS = 5.0
_HEALTH_CACHE_LIMIT = 4


class _CachedHealth(NamedTuple):
    signature: tuple[int, int]
    checked_at: float
    health: Any


_HEALTH_CACHE: OrderedDict[Path, _CachedHealth] = OrderedDict()


def _cached_graph_health(graph_path: Path) -> Any:
    """`graph_location.report_health`, memoized per stat signature plus TTL.

    A failed stat evicts its entry and falls through to an uncached check, so
    a repaired or rebuilt graph is never answered from stale cached state.
    """
    from harness import graph_location

    resolved = Path(graph_path).resolve()
    try:
        stat = resolved.stat()
    except OSError:
        _HEALTH_CACHE.pop(resolved, None)
        return graph_location.report_health(resolved)

    signature = (stat.st_mtime_ns, stat.st_size)
    now = time.monotonic()
    cached = _HEALTH_CACHE.get(resolved)
    if (
        cached is not None
        and cached.signature == signature
        and now - cached.checked_at < _HEALTH_TTL_SECONDS
    ):
        _HEALTH_CACHE.move_to_end(resolved)
        return cached.health

    health = graph_location.report_health(resolved)
    _HEALTH_CACHE[resolved] = _CachedHealth(signature, now, health)
    _HEALTH_CACHE.move_to_end(resolved)
    while len(_HEALTH_CACHE) > _HEALTH_CACHE_LIMIT:
        _HEALTH_CACHE.popitem(last=False)
    return health


def _graph_health_caveat(project_root: Path) -> str | None:
    """The freshness note `build_annotator(check_health=True)` would have
    added, served from `_cached_graph_health` instead of two git subprocesses.

    Resolves the graph exactly as `build_annotator` does, so the note text and
    the repo it judges are unchanged -- only the caching differs. The caveat
    table is read from `memory_graph` rather than restated here: two copies of
    this wording would drift apart the first time one of them is edited.
    """
    from harness import graph_location, memory_graph

    path = graph_location.find_graph_in_repo(Path(project_root))
    if path is None:
        return None
    try:
        health = _cached_graph_health(path)
    except Exception:  # noqa: BLE001 -- health is advisory, never fatal
        health = graph_location.GraphHealth.UNKNOWN
    return memory_graph._HEALTH_CAVEATS.get(health)


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
        "Call this ONCE before starting work to recover what was already decided; "
        "for follow-up questions mid-task call memory_search instead, which is "
        "smaller and topic-focused. Pass `query` as the task you are about to do "
        "-- it is the ranking query; without it the scope file's active objective "
        "is used. Narrow the packet to a topic with `tags` (comma-separated, keep "
        "entries carrying any of them), `types` (decision, discovery, task, "
        "blocker, lesson, relationship), and `exclude` (tags or entry ids to "
        "drop); filters apply before ranking and the packet states how many "
        "entries they excluded. `max_chars` bounds the whole packet; anything "
        "omitted for budget is counted, never silently dropped. Entries naming a "
        "symbol the code graph knows also carry that symbol's blast radius -- how "
        "many dependents it has and which files they are in -- so a past "
        "decision arrives with the code it actually touches."
    ),
    "memory_search": (
        "Fetch the few memory entries most relevant to one topic: ids, type, "
        "status, and summary, ranked, with no scope prose, warnings, or graph "
        "annotation. Use this INSTEAD of memory_context for mid-task follow-up "
        "questions ('what was decided about X?', 'is there a lesson about Y?') "
        "and whenever a packet's '... N more omitted' line hides something you "
        "need -- it fetches detail on demand instead of re-reading everything. "
        "`query` is the topic in plain words (required). `limit` defaults to 8 "
        "and is capped at 25. `projects` chooses where to look: 'current' "
        "(default), 'all' (every project registered on this machine), 'related' "
        "(projects an accepted relationship entry links to this one), or a "
        "comma-separated list of registered project ids; every hit is labelled "
        "[project-id] or [machine], and a project that cannot be searched is "
        "reported in a note, never silently skipped. `tags`, `types`, and "
        "`exclude` narrow the candidates exactly as in memory_context. Results "
        "are proposals unless marked accepted; nothing here promotes them."
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
        "keeps stalling on (bottlenecks), and who (if anyone) has claimed each "
        "one. Call this to decide what to pick up."
    ),
    "memory_claim": (
        "Claim one open task or blocker for `owner` (claude, codex, or agy) so "
        "other agent sessions know not to pick it up too. Call this before "
        "starting work on something from memory_tasks. Fails if someone else "
        "already holds a live claim on it."
    ),
    "memory_release": (
        "Release your claim on a task or blocker, e.g. because it's done "
        "(prefer memory_task_done for that) or you're abandoning it so someone "
        "else can pick it up."
    ),
    "memory_curate": (
        "Run deterministic memory curation: auto-accept only ingested document "
        "pointers whose source file still matches its recorded digest, reject "
        "exact duplicate proposals, and show the bounded exception queue. "
        "This does not auto-accept agent conclusions."
    ),
}

WORK_TOOL_DESCRIPTIONS: dict[str, str] = {
    "work_start": (
        "Start an implementation task after its scope is agreed: claim exact "
        "repository-relative paths for this unique session, record an immutable "
        "base commit, create a branch, and create an external worktree when "
        "another session is active or the primary checkout is dirty. `paths` is "
        "one path per line. Refuses overlapping active path claims. On success "
        "the reply also carries a small memory packet ranked by this task's "
        "`summary` and optional `tags` (comma-separated topic words), so the "
        "task starts with the memory relevant to it rather than the whole "
        "store; call memory_search for anything it omits. The task still "
        "starts if that packet cannot be built."
    ),
    "work_status": (
        "List task branches, sessions, paths, private plans, and worktree paths "
        "recorded for this repository. Call before starting implementation."
    ),
    "work_diff": (
        "Show all tracked changes for one task against the exact commit it began "
        "from. Use before handoff; it is independent of later movement on main."
    ),
    "work_finish": (
        "Mark an implementation task complete and release its path claims. Does "
        "not merge, rebase, delete the branch, or delete its worktree."
    ),
}


def _resolve_project(arguments: dict) -> Path:
    """The project root a tool call targets, with its registry stamp refreshed.

    `memory_store.touch_project` only refreshes an already-registered
    project's `last_seen` -- it never enrolls, is a no-op within the same
    second, and swallows OSError -- so the stamp is a side effect that cannot
    fail a read-only call. A tool call is the clearest evidence a project is
    still alive; before this, `last_seen` only ever moved on enrol, which is
    what left the registry unable to tell a live project from an abandoned
    one.
    """
    from harness import memory_store

    root = Path(arguments.get("project") or ".").expanduser()
    try:
        memory_store.touch_project(root)
    except Exception:  # noqa: BLE001 -- registry upkeep must never fail a tool call
        pass
    return root


# The budget of the packet `work_start` attaches. Modest on purpose: it is
# the memory for ONE task, ranked by that task's own words, and anything it
# omits is one `memory_search` away. A full session packet is `memory_context`.
TASK_PACKET_MAX_CHARS = 1500


def _search_selector_resolver(current_project_id: str, query: str, client_factory: Any = None):
    """A `memory_context.SelectorResolver` that routes each search source to
    its own semantic index -- another project's index for its inbox, the one
    shared machine index for machine entries -- through the same `adaptive`
    gate `memory_context` uses, so a source too small to benefit from ranking
    is ranked deterministically without touching an index. `resolve_selector`
    never builds an index, and every fallback comes back as a note that the
    search prefixes with the source it concerns.
    """
    from harness import memory_context, memory_index

    def resolve(source: str, entry_count: int):
        if source == memory_context.MACHINE_SOURCE:
            return memory_index.resolve_selector(
                current_project_id,
                "adaptive",
                client_factory=client_factory,
                query=query,
                entry_count=entry_count,
                scopes=("machine",),
            )
        return memory_index.resolve_selector(
            source,
            "adaptive",
            client_factory=client_factory,
            query=query,
            entry_count=entry_count,
            scopes=("project",),
        )

    return resolve


def _task_packet(project: Path, summary: str, tags: str) -> str:
    """The memory a task starts with: a packet ranked by the task's own words.

    The task's summary and tags are the query, so what arrives is the memory
    relevant to THIS job rather than the store's general top entries. It
    deliberately skips the code-graph join (that is `memory_context`'s job
    and costs a graph load) and degrades to a one-line note on any failure:
    the task has already started by the time this runs, and a retrieval
    problem must never read as a failed start.
    """
    from harness import memory_context, memory_index, memory_store

    tag_words = " ".join(t.strip() for t in str(tags or "").split(",") if t.strip())
    query = " ".join(part for part in (summary.strip(), tag_words) if part)
    try:
        store = memory_store.project_memory_dir(project)
        if not (store / "inbox.jsonl").is_file():
            return f"memory: no project memory at {store}; nothing to attach"
        loaded = memory_context.load_entries(project)
        selector, strategy, notes = memory_index.resolve_selector(
            memory_store.project_id_for(project),
            "adaptive",
            query=query,
            entry_count=loaded.count,
        )
        packet = memory_context.build_packet(
            project,
            query=query,
            max_chars=TASK_PACKET_MAX_CHARS,
            selector=selector,
            strategy=strategy,
            notes=[
                *notes,
                "task-scoped packet ranked by this task's summary and tags, "
                "without the code-graph join; call memory_search for anything omitted",
            ],
            loaded=loaded,
        )
        return "memory:\n" + memory_context.render_markdown(packet)
    except Exception as exc:  # noqa: BLE001 -- the task already started; say why, never fail
        return (
            f"memory: task-scoped packet unavailable ({type(exc).__name__}: {exc}); "
            "the task started anyway -- call memory_context directly"
        )


def dispatch_memory(tool_name: str, arguments: dict) -> str:
    """Run one memory tool call and return plain text.

    Kept separate from `dispatch` (which is `(term, depth)`-shaped for graph
    queries) and free of the `mcp` SDK, so tests exercise it without either
    optional dependency installed. Expected failures are returned as text
    rather than raised, matching `dispatch`: a bad call should read as a tool
    result, not crash the server for the rest of the session.
    """
    from harness import memory_context, memory_curation, memory_index, memory_store

    root = _resolve_project(arguments)

    try:
        curation = memory_curation.curate(root) if tool_name == "memory_context" else None
        if tool_name in ("memory_context", "memory_search"):
            # Filters are parsed and applied BEFORE anything is counted or
            # ranked: the adaptive gate below must see the number of entries
            # that can actually be ranked, or a narrow filter over a large
            # store would pay for a model to reorder three entries.
            try:
                entry_filter = memory_context.parse_filter(
                    arguments.get("tags"), arguments.get("types"), arguments.get("exclude")
                )
            except ValueError as exc:
                return str(exc)
            loaded = memory_context.load_entries(root, entry_filter)

        if tool_name == "memory_search":
            return _memory_search(root, arguments, loaded)

        if tool_name == "memory_context":
            project_id = memory_store.project_id_for(root)
            # Resolve the query first: `adaptive` decides on the same string
            # the packet will actually rank by, and `build_packet` would
            # otherwise default it a second time further down.
            query = arguments.get("query") or memory_context.active_objective(root)
            # `cross_check` exists to compare deterministic against semantic
            # ranking, so it must not be given a strategy that may decline to
            # rank semantically -- the comparison would quietly become a no-op
            # and report nothing. Asking for it is asking for the attempt.
            #
            # Otherwise `adaptive`: this server is long lived, so the chromadb
            # import amortizes, but amortized waste is still waste. Ranking a
            # store with nothing to rank against costs an ONNX session and
            # returns the deterministic answer either way.
            wanted = "auto" if arguments.get("cross_check") else "adaptive"
            try:
                selector, strategy, notes = memory_index.resolve_selector(
                    project_id,
                    wanted,
                    query=query,
                    entry_count=loaded.count,
                )
            except FileNotFoundError as exc:
                return str(exc)
            if arguments.get("cross_check") and strategy == "semantic":
                project_entries = list(loaded.project)
                machine_entries = list(loaded.machine)
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

            # `check_health=False` plus `_graph_health_caveat`: the health
            # check is the same one `build_annotator` would run, but served
            # from this server's cache rather than two git subprocesses per
            # call. `build_annotator` only ever appends the caveat when it
            # returns a real annotator, so this reproduces its notes exactly.
            annotator, graph_notes = memory_graph.build_annotator(root, check_health=False)
            if annotator is not None:
                caveat = _graph_health_caveat(root)
                if caveat:
                    graph_notes = [*graph_notes, caveat]
            curation_note = curation.note() if curation else None
            packet = memory_context.build_packet(
                root,
                query=query,
                max_chars=int(arguments.get("max_chars") or 4000),
                selector=selector,
                strategy=strategy,
                notes=list(notes) + graph_notes + ([curation_note] if curation_note else []),
                annotator=annotator,
                loaded=loaded,
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
            from harness import memory_claims, memory_tasks

            entries = list(
                memory_store.iter_entries(memory_store.project_memory_dir(root))
            )
            ranked = memory_tasks.prioritized(entries)
            stuck = memory_tasks.bottlenecks(entries)
            if not ranked:
                return "no open action tasks"

            def _claim_tag(e: dict) -> str:
                c = e.get("claim")
                if not c:
                    return "unclaimed"
                if memory_claims.is_stale(c):
                    return f"stale claim by {c.get('owner')} (free to take)"
                return f"claimed by {c.get('owner')}"

            lines = [f"{len(ranked)} open, in priority order:"]
            lines += [
                f"  {e['id']} [{e.get('type')}] {e.get('summary')} -- {_claim_tag(e)}"
                for e in ranked
            ]
            if stuck:
                lines.append("\nbottlenecks:")
                lines += [f"  {b.describe()}" for b in stuck]
            return "\n".join(lines)

        if tool_name == "memory_claim":
            from harness import memory_claims

            owner = str(arguments.get("owner") or "")
            entry_id = str(arguments.get("entry_id") or "")
            result = memory_claims.claim(
                memory_store.project_memory_dir(root), entry_id, owner
            )
            text = f"{result.entry['id']} -> claimed by {owner}"
            if result.reclaimed_from:
                text += f" (took over a stale claim from {result.reclaimed_from})"
            return text

        if tool_name == "memory_release":
            from harness import memory_claims

            owner = str(arguments.get("owner") or "")
            entry_id = str(arguments.get("entry_id") or "")
            entry = memory_claims.release(
                memory_store.project_memory_dir(root), entry_id, owner
            )
            return f"{entry['id']} -> released"

        if tool_name == "memory_curate":
            return memory_curation.render(memory_curation.curate(root))

    except (ValueError, LookupError, OSError) as exc:
        return f"memory tool failed: {exc}"

    return (
        f"unknown tool '{tool_name}' -- choose from "
        f"{', '.join(MEMORY_TOOL_DESCRIPTIONS)}"
    )


def _memory_search(root: Path, arguments: dict, loaded: Any) -> str:
    """The `memory_search` tool body: resolve projects, gather sources, rank.

    `loaded` is the current project's store, already filtered. Other
    projects are read through their registry path with the same filter, one
    source each, and machine memory is one further source read exactly once.
    Every source is ranked to at most `limit` entries before the merge, so
    searching ten projects costs ten bounded rankings, never one unbounded
    union.
    """
    from harness import memory_context, memory_store

    query = str(arguments.get("query") or "").strip()
    if not query:
        return "query is required -- describe the topic you need memory about"
    try:
        limit = int(arguments.get("limit") or memory_context.SEARCH_DEFAULT_LIMIT)
        max_chars = int(arguments.get("max_chars") or memory_context.SEARCH_DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        return "limit and max_chars must be integers"

    current_id = memory_store.project_id_for(root)
    relationships = [
        e
        for e in (*loaded.project_all, *loaded.machine_all)
        if e.get("type") == "relationship" and e.get("status") == "accepted"
    ]
    scope = memory_context.resolve_search_projects(
        arguments.get("projects") or "current",
        root,
        memory_store.load_registry(),
        relationships=relationships,
    )

    sources: list[tuple[str, Any]] = [
        (current_id, loaded.project),
        (memory_context.MACHINE_SOURCE, loaded.machine),
    ]
    excluded = loaded.excluded
    for project_id, path in scope.projects[1:]:
        other = memory_context.load_entries(path, loaded.entry_filter, include_machine=False)
        sources.append((project_id, other.project))
        excluded += other.excluded

    notes = list(scope.notes)
    if not loaded.entry_filter.is_empty:
        plural = "y" if excluded == 1 else "ies"
        notes.append(
            f"filters applied ({loaded.entry_filter.describe()}); "
            f"{excluded} entr{plural} excluded before ranking"
        )
    result = memory_context.search(
        query,
        sources,
        limit=limit,
        max_chars=max_chars,
        resolver=_search_selector_resolver(current_id, query),
        notes=notes,
    )
    return memory_context.render_search_markdown(result)


def dispatch_work(tool_name: str, arguments: dict) -> str:
    """Run one local task-workspace action without requiring the MCP SDK."""
    from harness import work

    project = _resolve_project(arguments)
    try:
        if tool_name == "work_start":
            paths = [line.strip() for line in str(arguments.get("paths") or "").splitlines() if line.strip()]
            summary = str(arguments.get("summary") or "")
            task = work.start(
                project,
                summary=summary,
                session_id=str(arguments.get("session") or ""),
                paths=paths,
                agent=str(arguments.get("agent") or "codex"),
                worktree_mode=str(arguments.get("worktree") or "auto"),
            )
            started = (
                f"status: success\nsummary: started {task.id}\n"
                f"next_actions: work only in {task.worktree}; call work_diff before handoff\n"
                f"artifacts: {task.plan_path}\nbranch: {task.branch}\nbase: {task.base_sha}"
            )
            # Appended after the task record, never in place of it: the
            # first lines keep their shape for callers that parse them.
            return started + "\n\n" + _task_packet(project, summary, str(arguments.get("tags") or ""))
        if tool_name == "work_status":
            tasks = work.list_tasks(project, active_only=bool(arguments.get("active_only", False)))
            if not tasks:
                return "status: success\nsummary: no recorded tasks\nnext_actions: start a task before implementation\nartifacts:"
            lines = ["status: success", f"summary: {len(tasks)} recorded task(s)", "next_actions: inspect active path claims before editing", "artifacts:"]
            lines.extend(
                f"{task.id} [{task.status}] session={task.session_id} branch={task.branch} "
                f"paths={','.join(task.paths)} worktree={task.worktree} plan={task.plan_path}"
                for task in tasks
            )
            return "\n".join(lines)
        if tool_name == "work_diff":
            return work.diff(project, str(arguments.get("task_id") or ""))
        if tool_name == "work_finish":
            task = work.finish(
                project, str(arguments.get("task_id") or ""), str(arguments.get("session") or "")
            )
            return (
                f"status: success\nsummary: finished {task.id}; released path claims\n"
                "next_actions: hand off the branch for integration\n"
                f"artifacts: {task.plan_path}"
            )
    except (work.WorkError, OSError) as exc:
        return f"status: error\nsummary: {exc}\nnext_actions: correct the reported condition and retry\nartifacts:"
    return f"unknown tool '{tool_name}' -- choose from {', '.join(WORK_TOOL_DESCRIPTIONS)}"


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
    """Construct the FastMCP server and register all 21 tools.

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
        tags: str = "",
        types: str = "",
        exclude: str = "",
    ) -> str:
        return dispatch_memory(
            "memory_context",
            {
                "query": query,
                "max_chars": max_chars,
                "project": project,
                "cross_check": cross_check,
                "cross_check_limit": cross_check_limit,
                "tags": tags,
                "types": types,
                "exclude": exclude,
            },
        )

    def memory_search(
        query: str,
        limit: int = 8,
        projects: str = "current",
        tags: str = "",
        types: str = "",
        exclude: str = "",
        max_chars: int = 4000,
        project: str = "",
    ) -> str:
        return dispatch_memory(
            "memory_search",
            {
                "query": query,
                "limit": limit,
                "projects": projects,
                "tags": tags,
                "types": types,
                "exclude": exclude,
                "max_chars": max_chars,
                "project": project,
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

    def memory_claim(entry_id: str, owner: str, project: str = "") -> str:
        return dispatch_memory(
            "memory_claim", {"entry_id": entry_id, "owner": owner, "project": project}
        )

    def memory_release(entry_id: str, owner: str, project: str = "") -> str:
        return dispatch_memory(
            "memory_release", {"entry_id": entry_id, "owner": owner, "project": project}
        )

    def memory_curate(project: str = "") -> str:
        return dispatch_memory("memory_curate", {"project": project})

    for func in (
        memory_context,
        memory_search,
        memory_status,
        memory_remember,
        memory_confirm,
        memory_task_done,
        memory_tasks,
        memory_claim,
        memory_release,
        memory_curate,
    ):
        func.__doc__ = MEMORY_TOOL_DESCRIPTIONS[func.__name__]
        mcp_app.tool(
            name=func.__name__, description=MEMORY_TOOL_DESCRIPTIONS[func.__name__]
        )(func)

    def work_start(
        summary: str,
        session: str,
        paths: str,
        agent: str = "codex",
        worktree: str = "auto",
        project: str = "",
        tags: str = "",
    ) -> str:
        return dispatch_work(
            "work_start",
            {"summary": summary, "session": session, "paths": paths, "agent": agent,
             "worktree": worktree, "project": project, "tags": tags},
        )

    def work_status(project: str = "", active_only: bool = False) -> str:
        return dispatch_work("work_status", {"project": project, "active_only": active_only})

    def work_diff(task_id: str, project: str = "") -> str:
        return dispatch_work("work_diff", {"task_id": task_id, "project": project})

    def work_finish(task_id: str, session: str, project: str = "") -> str:
        return dispatch_work(
            "work_finish", {"task_id": task_id, "session": session, "project": project}
        )

    for func in (work_start, work_status, work_diff, work_finish):
        func.__doc__ = WORK_TOOL_DESCRIPTIONS[func.__name__]
        mcp_app.tool(name=func.__name__, description=WORK_TOOL_DESCRIPTIONS[func.__name__])(func)

    return mcp_app


def _cap_native_thread_pools() -> None:
    """Pin OpenBLAS to one thread before anything can import numpy.

    Measured on this Windows machine (2026-10-13): numpy's bundled OpenBLAS
    commits ~32 MB of private memory per thread at import and sizes its pool
    to the CPU count. On a 20-thread CPU that was 614 of the server's 697 MB,
    for a pool that never runs: this server calls no BLAS routine (embeddings
    go through onnxruntime's own pool, the vector index is hnswlib). Every
    client session spawns its own stdio server, so the waste multiplied --
    23 open sessions held ~16 GB of commit. Pinned, the warmed server is
    ~86 MB.

    `setdefault`, so an operator who sets the variable still wins. It must
    run before the first numpy import; OpenBLAS reads it once, at load.

    Fixed upstream in OpenMathLib/OpenBLAS#6125 (merged 2026-10-14, first
    release after 0.3.34): Windows workers now allocate their buffer on first
    use. Remove this pin once the numpy wheels bundle an OpenBLAS with that
    fix -- `numpy.show_config()` reports the bundled version.
    """
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")


def _warm_optional_native_imports() -> str | None:
    """Import the heavy optional native stack once, on the main thread.

    Measured on this Windows machine (2026-09-11), against the real server
    over the real stdio transport: importing `chromadb` lazily from inside a
    tool call NEVER completes -- >120 s, with the stack parked in numpy's
    `_core.multiarray` native module initialization while running on an anyio
    worker thread. The identical import costs 1.6 s in the short-lived
    SessionStart hook and 1.7 s in the CLI, so the cost is not the import; the
    cost is doing it off the main thread inside a running event loop.

    Doing it here, before `server.run()`, moved `memory_context` from a
    120 s timeout to 0.42 s, at a one-time startup cost of 0.92 s.

    Three properties this deliberately keeps:

    1. **Optional stays optional.** `chromadb` is an extra. A missing or
       broken install returns a reason and the server starts normally on the
       deterministic path, exactly as before.
    2. **It never fails startup.** A memory subsystem must not stop someone
       from working, so every exception is absorbed -- the same fail-open rule
       the SessionStart hook lives by.
    3. **It does not defeat the adaptive gate.** The gate's promise is that a
       call which cannot benefit from semantic ranking does no ranking work.
       That promise is about the *query*, not the import, and it is unchanged:
       `resolve_selector` still decides in arithmetic alone before touching an
       index. What changes is only WHERE the unavoidable import happens when
       the gate does say yes.

    Returns the failure reason, or None on success, so a caller can report it.
    """
    try:
        from harness import memory_index

        available, reason = memory_index.chromadb_status()
        return None if available else reason
    except Exception as exc:  # noqa: BLE001 -- see property 2 above
        return f"{type(exc).__name__}: {exc}"


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

    _cap_native_thread_pools()
    _warm_optional_native_imports()

    server = _build_server()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
