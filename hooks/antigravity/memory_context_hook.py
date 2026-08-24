"""Inject one bounded project-memory packet into a new Antigravity turn.

Antigravity's ``PreInvocation`` event runs before *every* model call.  The
``invocationNum`` guard is therefore essential: only invocation zero receives
the packet; later invocations return an empty JSON object.  This keeps a
session's memory cost bounded rather than repeating the same context in every
agent loop iteration.

The hook follows Antigravity's JSON-on-stdin / JSON-on-stdout contract and is
fail-open.  A malformed hook input or unreadable project memory must never
prevent ordinary agent work.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from hooks.claude import memory_context_hook

DEFAULT_MAX_CHARS = 2000


def _project_root(workspace: Path) -> Path:
    """Return the nearest project/memory root above an Antigravity workspace."""
    try:
        current = workspace.resolve()
    except OSError:
        return workspace

    for candidate in (current, *current.parents):
        if (candidate / ".agent-harness").is_dir() or (candidate / ".git").exists():
            return candidate
    return current


def render(payload: object) -> dict:
    """Convert a PreInvocation payload into Antigravity hook output.

    The function is intentionally pure from Antigravity's perspective: it
    only reads the selected project's bounded memory and returns an ephemeral
    message when there is useful context to inject.
    """
    if not isinstance(payload, dict) or payload.get("invocationNum") != 0:
        return {}

    workspaces = payload.get("workspacePaths")
    if not isinstance(workspaces, list):
        return {}
    workspace = next((Path(path) for path in workspaces if isinstance(path, str)), None)
    if workspace is None:
        return {}

    # Keep the tighter Antigravity startup budget local to this process.  A
    # user-set common hook budget remains authoritative.
    os.environ.setdefault(
        memory_context_hook.MAX_CHARS_ENV, str(DEFAULT_MAX_CHARS)
    )
    packet = memory_context_hook.emit(_project_root(workspace))
    if not packet:
        return {}
    return {"injectSteps": [{"ephemeralMessage": packet}]}


def main() -> int:
    """Run the hook and always return valid JSON with a successful exit code."""
    try:
        payload = json.load(sys.stdin)
        output = render(payload)
    except Exception:  # noqa: BLE001 - a memory adapter must fail open
        output = {}
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
