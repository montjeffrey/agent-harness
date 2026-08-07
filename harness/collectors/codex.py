"""Codex CLI session-log collector.

Discovery (performed on a real Windows 11 dev machine before any parsing
code below was written)
------------------------------------------------------------------------
Searched: ``~/.codex/`` (Windows: ``%USERPROFILE%\\.codex``), including its
``sessions/``, ``.sandbox*``, and top-level files (`history.jsonl`,
`session_index.jsonl`). Codex CLI *is* installed on this machine and has
real session history, under::

    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl

Each file is JSONL. Two shapes matter here:

* The first line of every rollout file is `session_meta`, carrying the
  session id, working directory, and CLI version (REDACTED sample --
  `base_instructions`/system-prompt text and git remote details are
  environment-specific, stripped/summarized here)::

      {
        "timestamp": "2026-07-30T07:03:13.713Z",
        "type": "session_meta",
        "payload": {
          "session_id": "019fb1c3-...",
          "id": "019fb1d5-...",
          "cwd": "C:\\Users\\you\\...\\some-project",
          "timestamp": "2026-07-30T07:03:13.368Z",
          "cli_version": "0.146.0-alpha.3.1"
        }
      }

* Turn-level usage/rate-limit snapshots arrive as `event_msg` /
  `token_count` events::

      {
        "timestamp": "2026-07-30T07:03:20.283Z",
        "type": "event_msg",
        "payload": {
          "type": "token_count",
          "info": {
            "total_token_usage": {
              "input_tokens": 15984,
              "cached_input_tokens": 4096,
              "cache_write_input_tokens": 0,
              "output_tokens": 93,
              "reasoning_output_tokens": 75,
              "total_tokens": 16077
            },
            "last_token_usage": { "...": "same shape, this turn only" }
          },
          "rate_limits": {
            "limit_id": "codex",
            "primary": {
              "used_percent": 21.0,
              "window_minutes": 10080,
              "resets_at": 1785943430
            }
          }
        }
      }

This collector maps `last_token_usage` on each `token_count` event to a
`usage` record (per-turn deltas, not the cumulative `total_token_usage`,
so repeated turns don't double count), and `rate_limits.primary` to a
`rate_limit` record with `limit_kind="percent"`. Codex reports *used*
percent; this collector stores the complement (`100 - used_percent`) as
`limit_remaining` so the field means the same thing ("how much is left")
across all three agents -- a deliberate mapping decision, not a value
Codex writes directly. No per-request dollar cost is present anywhere in
these logs, so `cost_usd` is never populated here (see the top-level
telemetry docs: no price-estimation table is built for that gap).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from harness import telemetry_store


def _codex_sessions_dir() -> Path:
    return Path.home() / ".codex" / "sessions"


def _iter_session_files(root: Path):
    if not root.is_dir():
        return
    yield from sorted(root.rglob("*.jsonl"))


def _parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _after_since(ts: str, since: datetime | None) -> bool:
    if since is None:
        return True
    since_utc = since if since.tzinfo else since.replace(tzinfo=telemetry_store.timezone.utc)
    try:
        return telemetry_store.parse_ts(ts) >= since_utc
    except ValueError:
        return True


def collect(since: datetime | None = None) -> list[dict]:
    """Best-effort scan of local Codex CLI rollout session logs.

    Never raises: a missing `~/.codex/sessions` directory, an unreadable
    file, or a truncated/corrupt JSONL line all degrade to "skip it" --
    worst case this returns `[]`.
    """
    records: list[dict] = []
    try:
        root = _codex_sessions_dir()
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
    session_id: str | None = None
    project: str | None = None

    for line in text.splitlines():
        obj = _parse_line(line)
        if obj is None:
            continue
        obj_type = obj.get("type")

        if obj_type == "session_meta":
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            session_id = payload.get("session_id") or payload.get("id")
            project = payload.get("cwd")
            ts = payload.get("timestamp") or obj.get("timestamp")
            if isinstance(ts, str) and _after_since(ts, since):
                out.append({
                    "agent": "codex",
                    "event": "session_start",
                    "ts": ts,
                    "project": project,
                    "session_id": session_id,
                    "source": str(session_file),
                })
            continue

        if obj_type != "event_msg":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        ts = obj.get("timestamp")
        if not isinstance(ts, str) or not _after_since(ts, since):
            continue

        info = payload.get("info")
        if isinstance(info, dict):
            usage = info.get("last_token_usage")
            if isinstance(usage, dict):
                record: dict = {
                    "agent": "codex",
                    "event": "usage",
                    "ts": ts,
                    "project": project,
                    "session_id": session_id,
                    "source": str(session_file),
                }
                input_tokens = usage.get("input_tokens")
                if isinstance(input_tokens, (int, float)):
                    record["input_tokens"] = int(input_tokens)
                cached = usage.get("cached_input_tokens")
                if isinstance(cached, (int, float)):
                    # Codex's `input_tokens` already includes this cached
                    # subset (see the discovery notes above -- 15984 + 93 =
                    # 16077 only reconciles that way), so it is never added
                    # into input_tokens here. Recorded separately as
                    # informational-only; telemetry_store preserves unknown
                    # extra fields.
                    record["cached_input_tokens"] = int(cached)
                output_tokens = usage.get("output_tokens")
                if isinstance(output_tokens, (int, float)):
                    record["output_tokens"] = int(output_tokens)
                out.append(record)

        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            primary = rate_limits.get("primary")
            if isinstance(primary, dict):
                used_percent = primary.get("used_percent")
                if isinstance(used_percent, (int, float)):
                    out.append({
                        "agent": "codex",
                        "event": "rate_limit",
                        "ts": ts,
                        "project": project,
                        "session_id": session_id,
                        "limit_remaining": 100 - used_percent,
                        "limit_kind": "percent",
                        "source": str(session_file),
                    })

    return out
