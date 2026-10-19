"""One-shot "time for a fresh session" notice, at a threshold fitted per machine.

Why a threshold at all: every model call re-reads the whole cached context.
Replaying 66 real sessions (2026-10-19, list-price weights fitted exactly
from 490 benchmark calls: cache read 0.05x input, cache write 2x) put the
cheapest refresh point at about REFRESH_FACTOR x (fresh start + re-read),
with a flat optimum: a fresh session must first re-write its starting
context to cache and re-read what it needs, so refreshing earlier than that
costs more than it saves. A fixed 150k suits a lean ~40-50k start; a
machine whose sessions start at ~87k (plugins, tool lists) does best near
200-220k.

Why one-shot, and to the user only: the previous mechanism (a PreToolUse
hook re-advising /compact every 60k tokens and every 25 tool calls) put its
advice into the model's context, where the model could not act on it and
narrated it instead -- 1,207 notices in 36 sessions in a month. This module
is called from the Stop hook after the hand-off note is written and returns
at most one notice per session, which the hook prints as ``systemMessage``
(shown to the user, not added to the model's context).

Adaptive inputs:
- fresh start: the first-call context of recent sessions that started fresh
  on this machine (median of the last MAX_STARTS within MAX_AGE_DAYS),
  recorded as sessions run; DEFAULT_FRESH until MIN_STARTS exist.
- re-read: DEFAULT_REREAD tokens, override with AGENT_HARNESS_REFRESH_REREAD.
- AGENT_HARNESS_REFRESH_AT fixes the threshold in tokens; 0 or "off" disables.

State lives under ``~/.agent-harness/refresh/`` (one small file per session,
so parallel Stop hooks never share a read-modify-write). Nothing here opens
a socket or calls a model; every failure degrades to "no notice".
"""

from __future__ import annotations

import json
import os
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Best refresh point / (fresh start + re-read) in the replay: 1.8-2.1 whenever
# the re-read is above zero (e.g. 87k + 25k -> best 200k; 60k + 25k -> 150-200k;
# 30k + 50k -> 150k), with a flat optimum either side.
REFRESH_FACTOR = 2.0
MIN_THRESHOLD = 100_000
MAX_THRESHOLD = 400_000
DEFAULT_FRESH = 40_000
DEFAULT_REREAD = 25_000
MIN_STARTS = 3
MAX_STARTS = 50
MAX_AGE_DAYS = 30
TAIL_BYTES = 512 * 1024

THRESHOLD_ENV = "AGENT_HARNESS_REFRESH_AT"
REREAD_ENV = "AGENT_HARNESS_REFRESH_REREAD"


def state_dir() -> Path:
    return Path.home() / ".agent-harness" / "refresh"


# --------------------------------------------------------------- transcript


def _usage_context(rec: dict, own_session: str | None) -> int | None:
    """Context read by one main-loop model call, or None for any other line."""
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return None
    if own_session and rec.get("sessionId") not in (None, own_session):
        return None  # history a resumed session inherited from its parent
    msg = rec.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict) or msg.get("model") == "<synthetic>":
        return None
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total or None


def context_tokens(transcript: Path | None, session_id: str | None = None) -> int | None:
    """Context of the latest main-loop call, read from the transcript's tail."""
    if transcript is None:
        return None
    try:
        with open(transcript, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # the first tail line may be cut mid-record
        if isinstance(rec, dict):
            ctx = _usage_context(rec, session_id)
            if ctx:
                return ctx
    return None


def first_call(transcript: Path | None, session_id: str | None) -> tuple[int | None, bool]:
    """(context of the first main-loop call, whether the session started fresh).

    A resumed or forked session's file opens with its parent's lines, tagged
    with the parent's session id; its first own call already carries that
    history, so it says nothing about a fresh start.
    """
    if transcript is None:
        return None, False
    resumed = False
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"sessionId"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                sid = rec.get("sessionId")
                if session_id and isinstance(sid, str) and sid != session_id:
                    resumed = True
                    continue
                ctx = _usage_context(rec, session_id)
                if ctx:
                    return ctx, not resumed
    except OSError:
        pass
    return None, False


# --------------------------------------------------------------- threshold


def _env_tokens(name: str) -> int | None:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in ("off", "false", "no"):
        return 0
    try:
        value = int(float(raw.rstrip("k")) * (1000 if raw.endswith("k") else 1))
    except ValueError:
        return None
    return max(0, value)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    tmp.replace(path)


def _safe_name(session_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80]


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fresh_starts(now: datetime | None = None) -> list[int]:
    """Recent fresh-start contexts on this machine, newest first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    found = []
    for path in (state_dir() / "starts").glob("*.json"):
        rec = _read_json(path)
        ts, ctx = _parse_ts(rec.get("ts")), rec.get("context")
        if ts and ts >= cutoff and isinstance(ctx, int) and ctx > 0:
            found.append((ts, ctx))
    found.sort(reverse=True)
    return [ctx for _, ctx in found[:MAX_STARTS]]


def threshold(now: datetime | None = None) -> tuple[int, str]:
    """(refresh threshold in tokens, one-line explanation). 0 means disabled."""
    fixed = _env_tokens(THRESHOLD_ENV)
    if fixed is not None:
        return fixed, f"set by {THRESHOLD_ENV}"
    starts = fresh_starts(now)
    fresh = int(statistics.median(starts)) if len(starts) >= MIN_STARTS else DEFAULT_FRESH
    reread = _env_tokens(REREAD_ENV)
    reread = DEFAULT_REREAD if reread is None else reread
    raw = REFRESH_FACTOR * (fresh + reread)
    value = int(min(MAX_THRESHOLD, max(MIN_THRESHOLD, round(raw / 10_000) * 10_000)))
    basis = (f"median fresh start {fresh // 1000}k over {len(starts)} sessions"
             if len(starts) >= MIN_STARTS else f"default fresh start {fresh // 1000}k")
    return value, f"{REFRESH_FACTOR} x ({basis} + {reread // 1000}k re-read)"


# --------------------------------------------------------------- the check


@dataclass
class Notice:
    context: int
    threshold: int
    basis: str
    handoff_saved: bool

    @property
    def message(self) -> str:
        note = ("Its hand-off note is saved; a new session in this project starts with it."
                if self.handoff_saved else
                "No hand-off note was saved for it (project not enrolled or nothing to hand off).")
        return (f"agent-harness: this session's context is {self.context // 1000}k tokens, past "
                f"this machine's refresh point of {self.threshold // 1000}k ({self.basis}). "
                f"{note} Every further call here re-reads the full context.")


def _prune(now: datetime) -> None:
    cutoff = (now - timedelta(days=MAX_AGE_DAYS)).timestamp()
    for sub in ("starts", "fired"):
        for path in (state_dir() / sub).glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def check(session_id: str, transcript: Path | None, handoff_saved: bool,
          now: datetime | None = None) -> Notice | None:
    """Record this session's fresh start once; return the notice at most once."""
    try:
        return _check(session_id, transcript, handoff_saved, now or datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 -- a notice must never break the Stop hook
        return None


def _check(session_id, transcript, handoff_saved, now) -> Notice | None:
    if not isinstance(session_id, str) or not session_id.strip() or transcript is None:
        return None
    name = _safe_name(session_id)
    start_path = state_dir() / "starts" / f"{name}.json"
    if not start_path.exists():
        ctx, fresh = first_call(transcript, session_id)
        if ctx:
            # Recorded for resumed sessions too, so the scan runs once; only
            # fresh starts carry a context that feeds the median.
            _write_json(start_path, {"context": ctx if fresh else 0,
                                     "ts": now.isoformat(timespec="seconds")})
            _prune(now)
    fired_path = state_dir() / "fired" / f"{name}.json"
    if fired_path.exists():
        return None
    limit, basis = threshold(now)
    if limit <= 0:
        return None
    ctx = context_tokens(transcript, session_id)
    if not ctx or ctx < limit:
        return None
    _write_json(fired_path, {"context": ctx, "threshold": limit,
                             "ts": now.isoformat(timespec="seconds")})
    return Notice(ctx, limit, basis, handoff_saved)
