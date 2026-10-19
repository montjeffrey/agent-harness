"""Stop / SessionEnd hook: write this session's hand-off note to memory.

Agents do not record memory on their own (harness-value benchmark,
2026-10-15/16: zero `memory_remember` calls in ~260 runs), so a later
session started cold. This hook writes the hand-off for them, from evidence:
the transcript's tool calls and final message, and the working-tree diff
since the SessionStart hook's snapshot. The rules -- proposed entries only,
no prompts, no secrets, no model call, one group per session -- live in
`harness.handoff`; this file only adapts Claude Code's hook payload.

Like the progress hook it never blocks: every path exits 0, and any error
inside the harness is swallowed. A `Stop` hook that exited 2 would hold the
session hostage.

Its only output: once per session, on `Stop`, when the context has passed
this machine's refresh point (`harness.context_refresh`), it prints
``{"systemMessage": ...}``. That notice is shown to the user and is not fed
to the model; nothing here uses `decision`, `reason` or `additionalContext`,
which would put text into the model's context.
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
    from harness import context_refresh, handoff

    return handoff, context_refresh


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
        transcript_path = Path(transcript) if isinstance(transcript, str) and transcript else None
        handoff, context_refresh = _import_harness()
        result = handoff.capture(
            root,
            session_id,
            transcript_path,
            # Newer Claude Code versions send the final reply with Stop; the
            # transcript may not have it flushed yet when the hook fires.
            last_message=payload.get("last_assistant_message"),
        )
        if payload.get("hook_event_name", "Stop") != "Stop":
            return 0  # SessionEnd: the session is over, nobody to tell
        saved = bool(result.proposed) or result.skipped == "unchanged since the last capture"
        notice = context_refresh.check(session_id, transcript_path, saved)
        if notice is not None:
            print(json.dumps({"systemMessage": notice.message}))
    except Exception:  # noqa: BLE001 -- never block the session from stopping
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
