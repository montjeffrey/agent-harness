"""Stop / SessionEnd hook: write this session's hand-off note to memory.

Agents do not record memory on their own (harness-value benchmark,
2026-10-15/16: zero `memory_remember` calls in ~260 runs), so a later
session started cold. This hook writes the hand-off for them, from evidence:
the transcript's tool calls and final message, and the working-tree diff
since the SessionStart hook's snapshot. The rules -- proposed entries only,
no prompts, no secrets, no model call, one group per session -- live in
`harness.handoff`; this file only adapts Claude Code's hook payload.

Like the progress hook it never blocks: every path exits 0 with no output,
and any error inside the harness is swallowed. A `Stop` hook that exited 2
would hold the session hostage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _import_harness():
    # The harness beside this file (<root>/hooks/claude/ -> <root>), never a
    # stale install found first on sys.path; see memory_context_hook._harness.
    root = str(Path(__file__).resolve().parents[2])
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    from harness import handoff

    return handoff


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        session_id = payload.get("session_id")
        if not isinstance(session_id, str):
            return 0
        cwd = payload.get("cwd")
        root = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
        transcript = payload.get("transcript_path")
        _import_harness().capture(
            root,
            session_id,
            Path(transcript) if isinstance(transcript, str) and transcript else None,
            # Newer Claude Code versions send the final reply with Stop; the
            # transcript may not have it flushed yet when the hook fires.
            last_message=payload.get("last_assistant_message"),
        )
    except Exception:  # noqa: BLE001 -- never block the session from stopping
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
