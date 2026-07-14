"""SessionStart hook: tell the agent when the code graph is missing or stale.

Claude Code runs this at session start with hook JSON on stdin ({"cwd": ...}).
Anything printed to stdout is injected into the agent's context, so the agent
knows to build/refresh the graph itself -- with the operator's consent --
instead of silently working without one.

Deliberately NEVER builds the graph itself: a full build can take tens of
minutes and needs an LLM API key, so it must be an explicit action. This hook
just closes the loop by making the gap visible every session.

Always exits 0 -- a broken graph check must never block a session.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent
GRAPH_RELATIVE_PATH = Path("graphify-out") / "graph.json"

# Cheap staleness probe: regex the raw text instead of json.load-ing a
# multi-MB file inside a session-start hook.
BUILT_AT_RE = re.compile(r'"built_at_commit"\s*:\s*"([0-9a-f]{7,40})"')


def find_graph(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        graph_path = candidate / GRAPH_RELATIVE_PATH
        if graph_path.exists():
            return graph_path
    return None


def git_head(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def built_at_commit(graph_path: Path) -> str | None:
    try:
        text = graph_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = BUILT_AT_RE.search(text)
    return match.group(1) if match else None


def check(cwd: Path) -> str | None:
    """Return a context message for the agent, or None when all is well."""
    refresh = HARNESS_ROOT / "harness" / "graph_refresh.py"
    graph_path = find_graph(cwd)

    if graph_path is None:
        return (
            "[agent-harness] No code graph found for this project. Query tools "
            "(graph_query.py) will not work until one is built. Build it with:\n"
            f'  python "{refresh}" "{cwd}" --full\n'
            "(needs graphify installed and an LLM backend; ~minutes to tens of "
            "minutes on large repos -- confirm with the operator before running)."
        )

    repo_root = graph_path.parent.parent
    head = git_head(repo_root)
    built = built_at_commit(graph_path)
    if head and built and not head.startswith(built) and not built.startswith(head):
        return (
            f"[agent-harness] Code graph is STALE: built at commit {built[:12]}, "
            f"HEAD is {head[:12]}. Relationship queries may be outdated. "
            f'Refresh with:\n  python "{refresh}" "{repo_root}"'
        )
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
