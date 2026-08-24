"""SessionStart hook: print this project's memory context packet.

Claude Code injects a SessionStart hook's stdout into the session as
context, so an agent starts already knowing the project's scope, its open
work, and what was previously decided -- without reading any history.

Three rules this hook lives by:

1. **Fail open, always.** Any error -- unreadable memory, a bug in the
   harness, an unexpected exception -- exits 0 with no output. A memory
   problem must never stop someone from working. That is why the body is
   wrapped in a bare `except Exception`, which is normally a smell and is
   correct here.

2. **Adaptive selection by default.** Every hook run is a fresh process, so
   an unconditional `auto` would import chromadb and build an ONNX session on
   *every* session start in *every* enrolled project -- seconds of latency
   for a packet keyword ranking usually fills well. `adaptive` keeps that
   cost only where it buys something: before touching chromadb it asks, in
   arithmetic alone, whether semantic ranking could change *this* packet.
   It cannot when there is no active objective to rank against (the semantic
   selector is defined to return deterministic order without a query), and it
   effectively cannot when the store is small enough that every section fits
   its budget. Both are the common case for a young project, which pays
   nothing. A project with a real objective and a real store gets semantic
   ranking without anyone opting in -- which is the point, because the
   opt-in flag was never going to be set by the people who most needed it.
   `AGENT_HARNESS_HOOK_STRATEGY` still forces `auto`, `semantic`, or
   `deterministic`, and `AGENT_HARNESS_ADAPTIVE_MIN_ENTRIES` moves the size
   threshold.

3. **Evidence-bounded curation.** Before reading memory, the hook accepts only
   document pointers whose recorded digest still matches the source file and
   rejects exact duplicates. Agent conclusions remain proposed. The pass is
   deterministic, local, and idempotent; it does not compact, index, or call a
   model.

4. **Smaller budget than the CLI default.** This text is prepended to every
   session whether or not it gets used, so it is charged against the
   context window unconditionally. `AGENT_HARNESS_HOOK_MAX_CHARS` overrides.

Never builds an index or opens a network socket. It writes only when a
verifiable pointer or exact duplicate needs a recorded review transition.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

HOOK_MAX_CHARS = 3500
STRATEGY_ENV = "AGENT_HARNESS_HOOK_STRATEGY"
MAX_CHARS_ENV = "AGENT_HARNESS_HOOK_MAX_CHARS"
GRAPH_ENV = "AGENT_HARNESS_HOOK_GRAPH"

# Auto-enrollment: create `.agent-harness/` on first sight of a project.
# Off unless a workspace root is configured, because this hook runs in every
# directory Claude Code is opened in -- including a colleague's repo you
# cloned to read for five minutes. Scaffolding committed files into someone
# else's project without being asked is not a good surprise, so the opt-in
# is a path you name.
WORKSPACE_ENV = "AGENT_HARNESS_WORKSPACE"
AUTO_ENROLL_ENV = "AGENT_HARNESS_AUTO_ENROLL"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def should_auto_enroll(project_root: Path) -> bool:
    """Whether to create memory storage for a project that has none.

    True only when `AGENT_HARNESS_AUTO_ENROLL` is truthy AND the project sits
    inside a directory named by `AGENT_HARNESS_WORKSPACE` (which may list
    several roots, `os.pathsep`-separated). Both conditions are required: the
    flag is the intent, the workspace is the blast radius. Without the
    workspace check, opening any cloned repository would silently add
    committed scaffolding to it.
    """
    if os.environ.get(AUTO_ENROLL_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return False

    roots = [r for r in os.environ.get(WORKSPACE_ENV, "").split(os.pathsep) if r.strip()]
    if not roots:
        return False

    try:
        resolved = project_root.resolve()
    except OSError:
        return False

    for root in roots:
        try:
            candidate = Path(root.strip()).expanduser().resolve()
        except OSError:
            continue
        if resolved == candidate or candidate in resolved.parents:
            return True
    return False


def _entry_count(memory_store: Any, project_root: Path) -> int:
    """How many entries both stores hold, for the adaptive size gate.

    This re-reads append-only JSONL that `build_packet` will read again a few
    lines later. The duplication is deliberate: the alternative is threading a
    count out of `build_packet` for one caller's benefit, and a stdlib pass
    over a small file is orders of magnitude cheaper than the ONNX session it
    decides whether to pay for.

    An unreadable store counts as zero, which sends the decision to
    deterministic. That is the correct direction to fail: rule 1 says a memory
    problem must never stop someone working, and the cheap path always
    produces a usable packet.
    """
    try:
        return sum(
            1
            for directory in (
                memory_store.project_memory_dir(project_root),
                memory_store.machine_memory_dir(),
            )
            for _ in memory_store.iter_entries(directory)
        )
    except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
        return 0


def _empty_packet_notice(project_root: Path, memory_store: Any) -> str:
    """One line distinguishing a wired-but-empty store from an absent one.

    An enrolled project whose packet has no units used to emit nothing at all,
    which is exactly what an *unenrolled* project emits. Two states that need
    opposite fixes -- one needs recording, the other needs enrolling -- looked
    identical from the operator's seat, and the absence of an error read as
    "no problem" rather than "no memory".

    The two empty cases are worth telling apart in the line itself. Zero
    entries means nothing was ever recorded. A non-zero count with an empty
    packet means entries exist but none survived selection, which is a budget
    or filter question, not a recording one.
    """
    try:
        count = sum(
            1
            for _ in memory_store.iter_entries(
                memory_store.project_memory_dir(project_root)
            )
        )
    except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
        count = 0

    if count == 0:
        return (
            "_agent-harness: memory is enrolled but empty (0 entries recorded). "
            "Nothing to inject. Record with `agent-harness-memory remember`; "
            "`agent-harness doctor` confirms the wiring._\n"
        )
    return (
        f"_agent-harness: {count} memory entries recorded, none selected into "
        "this packet -- check the budget with `agent-harness-memory status`._\n"
    )


def emit(project_root: Path) -> str:
    """Build the packet text for `project_root`, or "" if there is none.

    Returns empty for an unenrolled project so the hook stays silent rather
    than announcing itself in every repository on the machine.
    """
    try:
        from harness import memory_context, memory_curation, memory_index, memory_store
    except ImportError:
        # Source checkout: this file is <repo>/hooks/claude/, so the package
        # root is two levels up. An installed (pipx/wheel) harness takes the
        # branch above and never reaches here.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from harness import memory_context, memory_curation, memory_index, memory_store

    if not (project_root / ".agent-harness").is_dir():
        if not should_auto_enroll(project_root):
            return ""
        # Memory init only -- stdlib, milliseconds, no network. Building the
        # graph or the semantic index here would put a multi-second job in
        # front of every session start, the same trap that keeps `auto`
        # selection out of this hook. `agent-harness-enroll` does those.
        memory_store.init_project(project_root)

    # Curate before selection so a newly verified document pointer is visible
    # as accepted in this very packet. A curation failure must not suppress a
    # working session, consistent with the hook's fail-open contract.
    try:
        curation_note = memory_curation.curate(project_root).note()
    except Exception:  # noqa: BLE001 -- see the hook's fail-open rule
        curation_note = "memory curation skipped because its local check failed"

    strategy = os.environ.get(STRATEGY_ENV, "adaptive")
    project_id = memory_store.project_id_for(project_root)
    # Resolved once and passed to both the selector decision and the packet, so
    # the strategy is chosen against exactly the query the packet ranks by.
    # Letting `build_packet` default the query again could, if the two ever
    # diverged, pick semantic for a query that is not the one used.
    objective = memory_context.active_objective(project_root)
    try:
        selector, resolved, notes = memory_index.resolve_selector(
            project_id,
            strategy,
            query=objective,
            entry_count=_entry_count(memory_store, project_root),
        )
    except (FileNotFoundError, ValueError):
        selector = memory_context.deterministic_selector
        resolved = "deterministic"
        notes = []

    # The code-graph join, unlike semantic selection, is affordable here:
    # measured on a 6,300-node graph it is ~210ms to import and ~375ms to
    # parse and index, with annotation itself under a millisecond because
    # blast radii are memoized per symbol. No model, no chromadb, no network.
    # Set AGENT_HARNESS_HOOK_GRAPH=0 to turn it off.
    #
    # `check_health` is off deliberately. It costs two `git` calls -- ~880ms
    # on a large repo on network-backed storage, more than the rest of this
    # hook put together -- and buys nothing here, because `graph_check.py`
    # runs as a SessionStart hook alongside this one and already reports a
    # stale or dirty graph. The standalone CLI and MCP paths keep it on;
    # they have no such sibling.
    annotator = None
    if os.environ.get(GRAPH_ENV, "1").strip().lower() not in {"0", "false", "no"}:
        try:
            from harness import memory_graph

            annotator, graph_notes = memory_graph.build_annotator(
                project_root, check_health=False
            )
            notes = list(notes) + graph_notes
        except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
            annotator = None

    packet = memory_context.build_packet(
        project_root,
        query=objective,
        max_chars=_int_env(MAX_CHARS_ENV, HOOK_MAX_CHARS),
        selector=selector,
        strategy=resolved,
        notes=list(notes) + ([curation_note] if curation_note else []),
        annotator=annotator,
    )
    # An empty packet returned "" here for a while, on the theory that a
    # "nothing recorded yet" line is noise at session start. That traded a
    # little noise for a worse failure: silence is also what an unenrolled
    # project emits, so "not wired" and "wired but idle" became the same
    # observation. One line is cheaper than the debugging session that
    # ambiguity costs. Unenrolled projects still return "" above -- this hook
    # announces itself only where it has actually been installed.
    if not any(section.units for section in packet.sections):
        return _empty_packet_notice(project_root, memory_store)
    return memory_context.render_markdown(packet)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        root = Path(args[0]).expanduser() if args else Path.cwd()
        text = emit(root)
        if text:
            sys.stdout.write(text)
    except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
