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

* Subagent transcripts (Task/Agent tool runs, workflow agents) are separate
  files beside the session::

      <sanitized-cwd>/<session-uuid>/subagents/agent-<id>.jsonl
      <sanitized-cwd>/<session-uuid>/subagents/workflows/<run>/agent-<id>.jsonl

  Their lines have the same shape, carry the *parent's* `sessionId`, and add
  `agentId` / `isSidechain`. They are real spend for that session, so their
  usage is collected too, tagged with `subagent_id`; they never emit a
  `session_start`. A message id is counted once across the session file and
  all of its subagent transcripts.

Nothing here reads prompt/response text (`message.content`) into a
telemetry record -- only token counts, model name, session id, project
path, and timestamps.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from harness import telemetry_store


MAX_PATH = 260


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _iter_sessions(root: Path):
    """Yield `(session_file, subagent_files)` per session id.

    `session_file` is `<project>/<id>.jsonl`, or None when only the
    subagent directory survives. `subagent_files` are the transcripts under
    `<project>/<id>/subagents/` at any depth (workflow runs nest one level
    further), matched as `agent-*.jsonl` so sibling `.meta.json` files and
    workflow `journal.jsonl` files are not mistaken for transcripts.
    """
    if not root.is_dir():
        return
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        sessions = {p.stem: p for p in project_dir.glob("*.jsonl")}
        with_subagents = {p.name for p in project_dir.iterdir() if (p / "subagents").is_dir()}
        for session_id in sorted(set(sessions) | with_subagents):
            subagents = (
                sorted((project_dir / session_id / "subagents").rglob("agent-*.jsonl"))
                if session_id in with_subagents else []
            )
            yield sessions.get(session_id), subagents


def _io_path(path: Path) -> str:
    """The path to open, with the extended-length prefix where Windows needs it.

    A deep project cwd plus `<id>/subagents/workflows/<run>/agent-<id>.jsonl`
    passes MAX_PATH, and without the LongPathsEnabled policy a plain open of
    such a path raises -- which this collector would silently treat as an
    unreadable file. Listing those directories still works; only opening fails.
    """
    text = str(path)
    if os.name == "nt" and len(text) >= MAX_PATH and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + str(path.absolute())
    return text


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
        for session_file, subagent_files in _iter_sessions(root):
            # One seen-set per session: a message id can appear both in the
            # session file and in one of its subagent transcripts.
            seen_message_ids: set[str] = set()
            if session_file is not None:
                records.extend(_collect_from_file(session_file, since, seen_message_ids))
            for subagent_file in subagent_files:
                records.extend(_collect_from_file(
                    subagent_file, since, seen_message_ids, subagent=True))
    except OSError:
        return []
    return records


def _collect_from_file(
    session_file: Path,
    since: datetime | None,
    seen_message_ids: set[str],
    subagent: bool = False,
) -> list[dict]:
    try:
        with open(_io_path(session_file), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return []

    out: list[dict] = []
    # A subagent transcript carries its parent's sessionId; it is part of
    # that session, not a new one, so it never emits session_start.
    session_start_emitted = subagent

    # Split on "\n" only: str.splitlines() also breaks on U+2028, \x1c and
    # friends, which JSON.stringify leaves unescaped inside strings -- that cut
    # real records in two and silently dropped them. _parse_line strips "\r".
    for line in text.split("\n"):
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
        if subagent:
            agent_id = obj.get("agentId")
            record["subagent_id"] = (
                agent_id if isinstance(agent_id, str)
                else session_file.stem.removeprefix("agent-")
            )
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
