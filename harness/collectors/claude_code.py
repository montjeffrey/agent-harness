"""Claude Code session-log collector.

Discovery (performed on a real Windows 11 dev machine before any parsing
code below was written)
------------------------------------------------------------------------
Claude Code writes one JSONL file per session under::

    ~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl

(Windows: ``%USERPROFILE%\\.claude\\projects\\...``). The sanitized-cwd
directory name is the project's absolute path with path separators and
colons replaced by ``-`` (e.g. ``C:\\Users\\me\\proj`` becomes
``C--Users-me-proj``); the real cwd is also carried inside each record, so
this collector uses the in-record `cwd`/`sessionId` fields rather than
parsing the directory name.

Each line is a JSON object. The two shapes this collector cares about:

* Assistant turns carry a token-usage block::

      {
        "type": "assistant",
        "message": {
          "model": "claude-fable-5",
          "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 62924,
            "cache_read_input_tokens": 0,
            "output_tokens": 648
          }
        },
        "sessionId": "9ddf80e4-...",
        "cwd": "C:\\Users\\you\\Desktop\\Coding\\agent-harness\\agent-harness",
        "timestamp": "2026-08-06T04:44:12.928Z",
        "version": "2.1.222"
      }

  (REDACTED sample -- `message.content` and the thinking-block `signature`
  are model output, stripped here; nothing else in this shape is
  identifying beyond a local file path and a session UUID.)

* The very first line(s) of a file are session/mode metadata without a
  `usage` block, e.g.::

      {"type":"mode","mode":"normal","sessionId":"9ddf80e4-..."}
      {"type":"permission-mode","permissionMode":"acceptEdits","sessionId":"9ddf80e4-..."}

  These don't carry a timestamp or cwd reliably, so this collector treats
  the first line in a file that *does* have both a timestamp and a
  `sessionId` as the anchor for a single `session_start` record per file,
  and each `assistant` message with a `message.usage` block as one `usage`
  record. Claude Code writes a single API message as one line per content
  block (e.g. a `thinking` line then a `tool_use` line), and every one of
  those lines repeats the message's full `usage` under the same
  `message.id`, so usage is counted once per `message.id` -- counting lines
  over-reported tokens by roughly 1.8x input / 2.4x output (2026-10-12
  benchmark, docs/benchmarks/). Lines without an id are counted each.
  `input_tokens` on the emitted record is the sum of
  `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`
  from the source (all three represent tokens consumed as input for that
  turn under Claude's prompt-caching model) -- this is a deliberate
  aggregation choice, not a field Claude Code writes directly.

Nothing here reads prompt/response text (`message.content`) into a
telemetry record -- only token counts, model name, session id, project
path, and timestamps.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from harness import telemetry_store


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _iter_session_files(root: Path):
    if not root.is_dir():
        return
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        yield from sorted(project_dir.glob("*.jsonl"))


def _parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def collect(since: datetime | None = None) -> list[dict]:
    """Best-effort scan of local Claude Code session logs.

    Never raises: a missing `~/.claude/projects` directory, an unreadable
    file, or a truncated/corrupt JSONL line all degrade to "skip it" --
    worst case this returns `[]`.
    """
    records: list[dict] = []
    try:
        root = _claude_projects_dir()
        for session_file in _iter_session_files(root):
            records.extend(_collect_from_file(session_file, since))
    except OSError:
        return []
    return records


def _collect_from_file(session_file: Path, since: datetime | None) -> list[dict]:
    try:
        text = session_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out: list[dict] = []
    session_start_emitted = False
    seen_message_ids: set[str] = set()

    for line in text.splitlines():
        obj = _parse_line(line)
        if obj is None:
            continue

        ts = obj.get("timestamp")
        session_id = obj.get("sessionId") or obj.get("session_id")
        cwd = obj.get("cwd")

        if not session_start_emitted and isinstance(ts, str) and session_id:
            if _after_since(ts, since):
                out.append({
                    "agent": "claude-code",
                    "event": "session_start",
                    "ts": ts,
                    "project": cwd,
                    "session_id": session_id,
                    "source": str(session_file),
                })
            session_start_emitted = True

        if obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        # One API message is written as one line per content block, each
        # repeating the full usage; count it once. Marked seen before the
        # `since` check so a message an earlier collect already counted is not
        # counted again from a later line.
        message_id = message.get("id")
        if isinstance(message_id, str):
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
        if not isinstance(ts, str) or not _after_since(ts, since):
            continue

        input_tokens = 0
        found_any = False
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                input_tokens += value
                found_any = True
        output_tokens = usage.get("output_tokens")

        record: dict = {
            "agent": "claude-code",
            "event": "usage",
            "ts": ts,
            "project": cwd,
            "session_id": session_id,
            "source": str(session_file),
        }
        if found_any:
            record["input_tokens"] = int(input_tokens)
        if isinstance(output_tokens, (int, float)):
            record["output_tokens"] = int(output_tokens)
        model = message.get("model")
        if isinstance(model, str):
            record["model"] = model
        out.append(record)

    return out


def _after_since(ts: str, since: datetime | None) -> bool:
    if since is None:
        return True
    try:
        return telemetry_store.parse_ts(ts) >= (
            since if since.tzinfo else since.replace(tzinfo=telemetry_store.timezone.utc)
        )
    except ValueError:
        return True
