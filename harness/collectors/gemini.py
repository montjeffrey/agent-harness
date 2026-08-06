"""Gemini CLI / Antigravity session-log collector.

Discovery (performed on a real Windows 11 dev machine before any parsing
code below was written)
------------------------------------------------------------------------
Searched, in this order:

  - ``~/.gemini/`` (Windows: ``%USERPROFILE%\\.gemini``) -- present.
    Subdirectories found: ``antigravity/``, ``antigravity-browser-profile/``,
    ``antigravity-cli/``, ``config/``. No ``tmp/`` or ``sessions/``
    directory (the plain "Gemini CLI" convention of
    ``~/.gemini/tmp/<hash>/logs.json`` was NOT found on this machine --
    only the Antigravity IDE/CLI stack is installed here).
  - ``~/.config/gemini/`` -- does not exist.
  - ``%APPDATA%\\gemini`` -- does not exist.
  - ``%APPDATA%\\Antigravity`` / ``%LOCALAPPDATA%\\Antigravity`` -- exist,
    but contain IDE application state (window layout, browser allowlist,
    MCP config, protobuf user settings), not structured session/usage logs.

What WAS found, under ``~/.gemini/antigravity-cli/``:

  - ``history.jsonl`` -- one JSON object per submitted prompt::

        {"display": "<prompt text>", "timestamp": 1780273535554,
         "workspace": "C:\\Users\\you\\Desktop\\SomeProject",
         "conversationId": "00000000-0000-0000-0000-000000000000"}

    (REDACTED sample -- `display` is the literal prompt text the user
    typed, so this collector reads `timestamp` / `workspace` /
    `conversationId` only and never copies `display` into a telemetry
    record.) `timestamp` is milliseconds-since-epoch, not ISO 8601.

  - ``cli.log`` -- unstructured plain-text operational/debug log lines
    (HTTP trace IDs, gRPC stream lifecycle, auth errors). No structured
    JSON, no token-usage numbers, and no quota/rate-limit payload was
    found in it on this machine.

Net result: this collector emits a `session_start` record per distinct
`conversationId` (first occurrence, by file order) from `history.jsonl`.
No token usage, cost, or quota/rate-limit data source was found on this
machine, so `usage` and `rate_limit` records are NOT emitted by this
collector today -- `collect()` only ever returns `session_start` events (or
`[]`). The mapping this collector *would* use if a quota payload is ever
found (documented here so a future pass on a machine with richer Gemini
CLI quota output can finish this without re-discovering the shape) is:
quota/remaining-requests style output -> `event="rate_limit"` with
`limit_remaining` + `limit_kind` set to whichever of "requests" / "tokens"
/ "percent" the source reports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harness import telemetry_store


def _history_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "history.jsonl"


def _parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _epoch_ms_to_ts(value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return telemetry_store.format_ts(dt)


def _after_since(dt: datetime, since: datetime | None) -> bool:
    if since is None:
        return True
    since_utc = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    return dt >= since_utc


def collect(since: datetime | None = None) -> list[dict]:
    """Best-effort scan of the local Gemini CLI / Antigravity prompt log.

    Never raises: a missing `history.jsonl`, an unreadable file, or a
    truncated/corrupt JSONL line all degrade to "skip it" -- worst case
    this returns `[]`. See the module docstring for what this does (and
    does not yet) extract on this machine.
    """
    path = _history_path()
    try:
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    seen_conversations: set[str] = set()
    out: list[dict] = []
    for line in text.splitlines():
        obj = _parse_line(line)
        if obj is None:
            continue
        conversation_id = obj.get("conversationId")
        if not isinstance(conversation_id, str) or conversation_id in seen_conversations:
            continue
        ts_str = _epoch_ms_to_ts(obj.get("timestamp"))
        if ts_str is None:
            continue
        dt = telemetry_store.parse_ts(ts_str)
        if not _after_since(dt, since):
            continue
        seen_conversations.add(conversation_id)
        out.append({
            "agent": "gemini",
            "event": "session_start",
            "ts": ts_str,
            "project": obj.get("workspace"),
            "session_id": conversation_id,
            "source": str(path),
        })
    return out
