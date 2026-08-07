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

2. **Deterministic selection by default.** Every hook run is a fresh
   process, so `auto` would mean importing chromadb and loading an ONNX
   model on *every* session start in *every* enrolled project -- seconds of
   latency for a packet keyword ranking already fills well. Semantic
   selection belongs in the long-lived MCP server, where the import is paid
   once. Set `AGENT_HARNESS_HOOK_STRATEGY=auto` to opt in anyway.

3. **Smaller budget than the CLI default.** This text is prepended to every
   session whether or not it gets used, so it is charged against the
   context window unconditionally. `AGENT_HARNESS_HOOK_MAX_CHARS` overrides.

Never builds an index, never writes, never opens a network socket.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOOK_MAX_CHARS = 3500
STRATEGY_ENV = "AGENT_HARNESS_HOOK_STRATEGY"
MAX_CHARS_ENV = "AGENT_HARNESS_HOOK_MAX_CHARS"

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


def emit(project_root: Path) -> str:
    """Build the packet text for `project_root`, or "" if there is none.

    Returns empty for an unenrolled project so the hook stays silent rather
    than announcing itself in every repository on the machine.
    """
    try:
        from harness import memory_context, memory_index, memory_store
    except ImportError:
        # Source checkout: this file is <repo>/hooks/claude/, so the package
        # root is two levels up. An installed (pipx/wheel) harness takes the
        # branch above and never reaches here.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from harness import memory_context, memory_index, memory_store

    if not (project_root / ".agent-harness").is_dir():
        if not should_auto_enroll(project_root):
            return ""
        # Memory init only -- stdlib, milliseconds, no network. Building the
        # graph or the semantic index here would put a multi-second job in
        # front of every session start, the same trap that keeps `auto`
        # selection out of this hook. `agent-harness-enroll` does those.
        memory_store.init_project(project_root)

    strategy = os.environ.get(STRATEGY_ENV, "deterministic")
    project_id = memory_store.project_id_for(project_root)
    try:
        selector, resolved, notes = memory_index.resolve_selector(project_id, strategy)
    except (FileNotFoundError, ValueError):
        selector = memory_context.deterministic_selector
        resolved = "deterministic"
        notes = []

    packet = memory_context.build_packet(
        project_root,
        max_chars=_int_env(MAX_CHARS_ENV, HOOK_MAX_CHARS),
        selector=selector,
        strategy=resolved,
        notes=notes,
    )
    # A packet with no sections -- only a "nothing recorded yet" warning --
    # is noise at session start. Stay quiet until there is something to say.
    if not any(section.units for section in packet.sections):
        return ""
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
