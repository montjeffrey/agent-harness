"""PreToolUse hook: block direct `Read` of graphify-out/graph.json.

Claude Code runs this with the tool-call JSON on stdin (at minimum
`{"tool_input": {"file_path": "..."}}`). `graph.json` can be several MB --
reading it whole wastes a full context window -- so this hook exits 2 (deny)
and points the agent at `harness/graph_query.py` instead. Any other file
passes through untouched (exit 0).

Never raises: malformed stdin, missing keys, or any other unexpected input
must never crash a hook and block an unrelated tool call.
"""

from __future__ import annotations

import json
import posixpath
import sys

BLOCKED_DIR = "graphify-out"
BLOCKED_NAME = "graph.json"

GUIDANCE = (
    "Use: python <harness>/harness/graph_query.py ... "
    "(graph.json wastes a full context window)"
)


def is_blocked(file_path: str) -> bool:
    """True when `file_path` points at the harness-managed graph.json.

    This hook fails open by design (see `main`), so it deliberately
    over-normalizes rather than under-matches -- a false positive just sends
    the agent to `graph_query.py` instead, which is the intended tool anyway:

    - Case-insensitive (Windows/macOS filesystems are typically
      case-insensitive; `GRAPH.JSON` and `graphify-out` in any case match).
    - Backslashes normalized to forward slashes so POSIX- and Windows-style
      paths are both recognized.
    - `..` segments, doubled separators, and trailing space/dot (all of
      which real filesystems collapse or strip) are normalized away before
      comparing the final two path components -- so a `..` traversal or a
      doubled `//` can't sneak past the match.
    - Matches only a real `graphify-out/graph.json` path-segment pair, not
      lookalikes such as `graph.json.bak`, `subgraphify-out/graph.json`, or
      `my-graphify-out/graph.json` (segment-boundary exact match, not a raw
      string suffix).
    """
    normalized = file_path.replace("\\", "/").rstrip(" .")
    normalized = posixpath.normpath(normalized) if normalized else normalized
    segments = normalized.split("/")
    if len(segments) < 2:
        return False
    directory, name = segments[-2], segments[-1]
    return directory.casefold() == BLOCKED_DIR and name.casefold() == BLOCKED_NAME


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 -- a hook must never crash and block an unrelated tool call
        return 0

    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        return 0

    if is_blocked(file_path):
        print(GUIDANCE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
