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
          "id": "msg_01...",
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
  the first line in a session that *does* have both a timestamp and a
  `sessionId` as the anchor for a single `session_start` record, and every
  `assistant` line with a `message.usage` block as a `usage` record.
  `input_tokens` on the emitted record is the sum of `input_tokens` +
  `cache_creation_input_tokens` + `cache_read_input_tokens` from the source
  (all three represent tokens consumed as input for that turn under
  Claude's prompt-caching model) -- this is a deliberate aggregation
  choice, not a field Claude Code writes directly. Schema 2 keeps that sum
  and also records the two cache parts on their own (`cache_write_tokens`,
  `cache_read_tokens`), because the split is what drives cost.

* One API response is often written as several lines, one per content
  block, each repeating the same `message.id` and the same `usage` block.
  Usage is therefore counted once per `message.id` (the first line wins),
  while `tool_use` blocks are counted on every line and credited to that
  id's record -- the tool call usually sits on a later line than the one
  whose usage was kept. `<synthetic>` placeholder messages are not API
  responses and produce no record.

* Subagent turns live beside the main transcript, under
  ``<session-uuid>/subagents/**/agent-*.jsonl``. They are read as part of
  the same session (one dedup set across main and subagent files) and
  their records carry `subagent: true`, so main-loop turns stay countable.

* A resumed session's transcript opens with a copy of its parent's
  history, those lines still tagged with the parent's `sessionId` (seen on
  a real resume chain: three files, each re-carrying 127-195 of an earlier
  session's API messages). The file stem names the session; lines tagged
  with another id are skipped, so resumed work is not billed twice.

Nothing here reads prompt/response text (`message.content`) into a
telemetry record -- only token counts, tool *names* (to count calls),
model name, session id, project path, and timestamps.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from harness import telemetry_store

HARNESS_TOOL_PREFIX = "mcp__agent-harness__"


def _claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _io_path(path: Path) -> str:
    """`path` in a form `open` accepts even past Windows' 260-char limit.

    Subagent transcripts nest a sanitized cwd, a session uuid and a
    workflow id, and routinely cross MAX_PATH; without the extended-length
    prefix those files fail to open and their turns silently vanish.
    """
    text = str(path)
    if os.name == "nt" and len(text) >= 250 and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + str(path.absolute())
    return text


def _iter_sessions(root: Path):
    """Yield ``(session_key, main_file | None, [subagent files])`` per session."""
    if not root.is_dir():
        return
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        mains = {p.stem: p for p in project_dir.glob("*.jsonl")}
        sub_roots = {p.name for p in project_dir.iterdir() if (p / "subagents").is_dir()}
        for key in sorted(set(mains) | sub_roots):
            subs = (
                sorted((project_dir / key / "subagents").rglob("agent-*.jsonl"))
                if key in sub_roots else []
            )
            yield key, mains.get(key), subs


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
        for key, main, subs in _iter_sessions(root):
            records.extend(_collect_session(key, main, subs, since))
    except OSError:
        return []
    return records


def _modified_before(path: Path, since: datetime | None) -> bool:
    """True when nothing in `path` can be at or after `since`.

    A file's lines are all written no later than its mtime, so a file last
    touched before the watermark holds no new records. Skipping it is what
    keeps a daily incremental `collect` from re-reading every transcript.
    """
    if since is None:
        return False
    try:
        mtime = os.stat(_io_path(path)).st_mtime
    except OSError:
        return False
    since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    return mtime < since_utc.timestamp()


def _tool_counts(message: dict) -> tuple[int, dict[str, int]]:
    """``(tool_use blocks, {harness short name: count})`` on one line."""
    total = 0
    harness: dict[str, int] = {}
    content = message.get("content")
    if not isinstance(content, list):
        return 0, harness
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        total += 1
        name = block.get("name")
        if isinstance(name, str) and name.startswith(HARNESS_TOOL_PREFIX):
            short = name[len(HARNESS_TOOL_PREFIX):]
            harness[short] = harness.get(short, 0) + 1
    return total, harness


def _credit_tools(record: dict, total: int, harness: dict[str, int]) -> None:
    record["tool_calls"] += total
    if harness:
        record["harness_tool_calls"] += sum(harness.values())
        bucket = record.setdefault("harness_tools", {})
        for name, count in harness.items():
            bucket[name] = bucket.get(name, 0) + count


def _own_session_id(stem: str, objs: list[dict]) -> str | None:
    """The id of the session a main transcript belongs to.

    Claude Code names the file after its session, so the stem wins whenever
    any line carries it; a file whose lines all name another id (or a
    hand-made fixture) falls back to the first id it carries.
    """
    first: str | None = None
    for obj in objs:
        value = obj.get("sessionId") or obj.get("session_id")
        if not isinstance(value, str):
            continue
        if value == stem:
            return stem
        if first is None:
            first = value
    return first


def _collect_session(
    key: str, main: Path | None, subs: list[Path], since: datetime | None
) -> list[dict]:
    out: list[dict] = []
    # message.id -> its record, or None when the id produced no record (seen
    # before `since`, so an earlier run already has it; or `<synthetic>`).
    # Later lines of such an id must neither emit a duplicate nor be
    # credited anywhere.
    by_id: dict[str, dict | None] = {}
    # Tool counts seen on a line that came before its id's usage line.
    pending: dict[str, tuple[int, dict[str, int]]] = {}
    session_id: str | None = None
    session_start_emitted = False

    files = ([(main, False)] if main is not None else []) + [(f, True) for f in subs]
    for path, is_sub in files:
        if _modified_before(path, since):
            continue
        try:
            with open(_io_path(path), encoding="utf-8", errors="replace") as fh:
                # Iterate the file, not str.splitlines(): JSON.stringify leaves
                # U+2028 unescaped inside strings, and records end at "\n" only.
                objs = [obj for obj in map(_parse_line, fh) if obj is not None]
        except OSError:
            continue
        if not is_sub:
            session_id = _own_session_id(key, objs)

        cwd: str | None = None
        for obj in objs:
            ts = obj.get("timestamp")
            if isinstance(obj.get("cwd"), str):
                cwd = obj["cwd"]
            line_session = obj.get("sessionId") or obj.get("session_id")
            if (
                not is_sub
                and session_id is not None
                and isinstance(line_session, str)
                and line_session != session_id
            ):
                # Inherited history: a resumed or forked session's file opens
                # with a copy of its parent's lines, still tagged with the
                # parent's id. Those turns belong to (and are counted with)
                # the parent; counting them here would bill them twice.
                continue

            # A subagent is part of its parent's session, never one of its own.
            if not is_sub and not session_start_emitted and isinstance(ts, str) and line_session:
                if _after_since(ts, since):
                    out.append({
                        "agent": "claude-code",
                        "event": "session_start",
                        "ts": ts,
                        "project": cwd,
                        "session_id": session_id or key,
                        "source": str(path),
                    })
                session_start_emitted = True

            if obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            tools, harness = _tool_counts(message)
            mid = message.get("id")
            mid = mid if isinstance(mid, str) and mid else None

            if mid is not None and mid in by_id:
                record = by_id[mid]
                if record is not None:
                    _credit_tools(record, tools, harness)
                continue

            usage = message.get("usage")
            if not isinstance(usage, dict):
                if mid is not None and tools:
                    old_total, old_harness = pending.get(mid, (0, {}))
                    merged = dict(old_harness)
                    for name, count in harness.items():
                        merged[name] = merged.get(name, 0) + count
                    pending[mid] = (old_total + tools, merged)
                continue
            if (
                message.get("model") == "<synthetic>"
                or not isinstance(ts, str)
                or not _after_since(ts, since)
            ):
                if mid is not None:
                    by_id[mid] = None
                    pending.pop(mid, None)
                continue

            subagent_id = None
            if is_sub:
                agent_id = obj.get("agentId")
                subagent_id = (
                    agent_id if isinstance(agent_id, str) and agent_id
                    else path.stem.removeprefix("agent-")
                )
            record = _usage_record(
                ts, cwd, session_id or key, path, usage, message, subagent_id)
            _credit_tools(record, tools, harness)
            if mid is not None:
                by_id[mid] = record
                if mid in pending:
                    _credit_tools(record, *pending.pop(mid))
            out.append(record)

    return out


def _usage_record(
    ts: str,
    cwd: str | None,
    session_id: str,
    path: Path,
    usage: dict,
    message: dict,
    subagent_id: str | None,
) -> dict:
    parts: dict[str, int] = {}
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parts[key] = int(value)

    record: dict = {
        "agent": "claude-code",
        "event": "usage",
        "ts": ts,
        "project": cwd,
        "session_id": session_id,
        "source": str(path),
    }
    if parts:
        record["input_tokens"] = sum(parts.values())
        record["cache_write_tokens"] = parts.get("cache_creation_input_tokens", 0)
        record["cache_read_tokens"] = parts.get("cache_read_input_tokens", 0)
    output_tokens = usage.get("output_tokens")
    if isinstance(output_tokens, (int, float)) and not isinstance(output_tokens, bool):
        record["output_tokens"] = int(output_tokens)
    model = message.get("model")
    if isinstance(model, str):
        record["model"] = model
    record["tool_calls"] = 0
    record["harness_tool_calls"] = 0
    if subagent_id is not None:
        record["subagent"] = True
        record["subagent_id"] = subagent_id
    return record


def _after_since(ts: str, since: datetime | None) -> bool:
    if since is None:
        return True
    try:
        return telemetry_store.parse_ts(ts) >= (
            since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        )
    except ValueError:
        return True
