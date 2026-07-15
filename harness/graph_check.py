"""SessionStart hook: tell the agent when the code graph is missing, stale,
dirty, invalid, or of unverifiable git state.

Claude Code runs this at session start with hook JSON on stdin ({"cwd": ...}).
Anything printed to stdout is injected into the agent's context, so the agent
knows to build/refresh the graph itself -- with the operator's consent --
instead of silently working without one, or trusting a graph that is corrupt,
stale, or was built against a working tree whose git state we could not
verify.

Deliberately NEVER builds the graph itself: a full build can take tens of
minutes and needs an LLM API key, so it must be an explicit action. This hook
just closes the loop by making the gap visible every session.

Classification is delegated entirely to `harness.graph_location.report_health`
-- this module only turns a `GraphHealth` value into a human-readable
message. There is exactly one place that decides invalid/dirty/unknown/
stale/fresh; duplicating that branching here would let the two drift (as
happened before this module was refactored to call `report_health`).

Always exits 0 -- a broken graph check must never block a session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import graph_location
from harness.graph_location import GraphHealth
from harness.graph_refresh import _quote_for_display

HARNESS_ROOT = Path(__file__).resolve().parent.parent


def find_graph(start: Path) -> Path | None:
    """Repo-bound graph discovery. See `harness.graph_location.find_graph_in_repo`."""
    return graph_location.find_graph_in_repo(start)


def check(cwd: Path) -> str | None:
    """Return a context message for the agent, or None when all is well."""
    refresh = HARNESS_ROOT / "harness" / "graph_refresh.py"
    refresh_q = _quote_for_display(str(refresh))
    graph_path = find_graph(cwd)

    if graph_path is None:
        cwd_q = _quote_for_display(str(cwd))
        return (
            "[agent-harness] No code graph found for this project. Query "
            "tools (graph_query.py) will not work until one is built. "
            f"Build it with:\n  python {refresh_q} {cwd_q} --full\n"
            "(needs graphify installed and an LLM backend; ~minutes to tens "
            "of minutes on large repos -- confirm with the operator before "
            "running)."
        )

    repo_root = graph_path.parent.parent
    repo_root_q = _quote_for_display(str(repo_root))
    health = graph_location.report_health(graph_path, repo_root)

    if health is GraphHealth.INVALID:
        return (
            f"[agent-harness] Code graph at {graph_path} is INVALID (corrupt "
            "or truncated graph.json). Query results cannot be trusted. "
            f"Rebuild it with:\n  python {refresh_q} {repo_root_q} --full"
        )

    if health is GraphHealth.DIRTY:
        return (
            "[agent-harness] Working tree has uncommitted changes; the code "
            "graph may not reflect them. Relationship queries may be "
            f"outdated. Refresh with:\n  python {refresh_q} {repo_root_q}"
        )

    if health is GraphHealth.UNKNOWN:
        return (
            "[agent-harness] Could not verify the code graph's git state "
            "(git unavailable, not on PATH, or this isn't a git repo); "
            "freshness cannot be confirmed either way. If unsure, refresh "
            f"with:\n  python {refresh_q} {repo_root_q}"
        )

    if health is GraphHealth.STALE:
        metadata = graph_location.load_graph_metadata(graph_path) or {}
        built = metadata.get("built_at_commit") or "?"
        head = graph_location.git_head(repo_root) or "?"
        return (
            f"[agent-harness] Code graph is STALE: built at commit "
            f"{built[:12]}, HEAD is {head[:12]}. Relationship queries may be "
            f"outdated. Refresh with:\n  python {refresh_q} {repo_root_q}"
        )

    # FRESH or MISSING (MISSING can't actually happen here -- graph_path
    # already exists, having just been discovered above).
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        cwd = Path(payload.get("cwd") or Path.cwd())
    except (json.JSONDecodeError, OSError):
        cwd = Path.cwd()
    try:
        message = check(cwd)
    except Exception as exc:  # noqa: BLE001 -- never block a session start
        print(f"[agent-harness] graph check skipped ({exc})", file=sys.stderr)
        return 0
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
