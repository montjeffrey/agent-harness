"""Claiming open work: which agent program owns a task right now.

The problem this solves: several agents (possibly on different machines,
definitely on different sessions) can see the same open task or blocker in
memory and pick it up at the same time. Nothing before this module stopped
two agents from doing the same work in parallel and colliding on the result.

A claim is an overlay on an existing task/blocker entry, not a new entry
type and not a status change -- claimed work is still `proposed`/`accepted`
and still shows up in `memory_tasks.open_work`/`prioritized`, deliberately:
an agent choosing what to pick up needs to see everything that is open,
including what someone else already has, so it can pick something else
instead of colliding with it.

    "claim": {"owner": "claude", "ts": "2026-08-24T06:10:00Z"}

`owner` identifies the *agent program*, not a person or a task -- one
identity per program, shared across however many sessions of it are
running. Restricted to `KNOWN_AGENTS` so a typo doesn't silently mint a new
identity that never matches anything in `claimed_by`. The three entries here
are this machine's actual programs today; extending the tuple is how a
future harness (a fourth CLI, a local model runner) joins the roster --
nothing else about the claim contract changes for that.

A claim carries no lease/heartbeat mechanism -- just the timestamp it was
taken. Liveness is judged at read time by `is_stale`, against
`CLAIM_STALE_HOURS`: a session that crashed or was killed without calling
`release()` should not permanently block a task, so its claim eventually
becomes free for anyone to take over.

Writes go through `memory_review.locate_entry` and `memory_store.write_lines`,
the same line-level splice `apply_review` uses, so a malformed or
future-schema line elsewhere in the inbox survives a claim/release untouched.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness import memory_review, memory_store, memory_tasks

# This machine's agent programs. `subscription` in the user's own words --
# which program's session is doing the claiming -- resolves to one of these.
# Claude Code -> claude, Codex/ChatGPT -> codex, Antigravity -> agy.
KNOWN_AGENTS: tuple[str, ...] = ("claude", "codex", "agy")

# A claim this old is presumed abandoned and is free for anyone to take over.
# Long enough that a task actually in progress is never bumped out from under
# its owner; short enough that a dead claim (crashed session, killed process)
# doesn't block the work for days waiting for a human to notice and clear it.
CLAIM_STALE_HOURS = 4


@dataclass(frozen=True)
class ClaimResult:
    """The claimed entry, plus the owner preempted if the prior claim was stale."""

    entry: dict
    reclaimed_from: str | None = None


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def is_stale(claim: dict, now: datetime | None = None) -> bool:
    """Whether `claim` is old enough to be presumed abandoned.

    An unparsable timestamp counts as stale rather than raising -- a claim
    record damaged by hand-editing should not be able to permanently lock a
    task, which is the same failure mode staleness exists to prevent.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _parse_ts(str(claim.get("ts") or ""))
    if stamp is None:
        return True
    return (now - stamp).total_seconds() > CLAIM_STALE_HOURS * 3600


def claim(
    directory: Path,
    entry_id: str,
    owner: str,
    now: datetime | None = None,
) -> ClaimResult:
    """Claim one open task or blocker for `owner`.

    Raises ValueError for an unknown owner, a non-task/blocker entry, a
    closed entry, or a live claim already held by a different owner. A stale
    claim is silently taken over -- the preempted owner is returned on the
    result so the caller can log what it displaced instead of that being
    invisible.
    """
    owner = (owner or "").strip()
    if owner not in KNOWN_AGENTS:
        raise ValueError(f"unknown owner {owner!r} (expected one of {KNOWN_AGENTS})")

    now = now or datetime.now(timezone.utc)
    lines, index, original = memory_review.locate_entry(directory, entry_id)

    if original.get("type") not in memory_tasks.WORK_TYPES:
        raise ValueError(
            f"{entry_id} is a {original.get('type')}; only "
            f"{sorted(memory_tasks.WORK_TYPES)} entries can be claimed"
        )
    if original.get("status") not in memory_tasks.OPEN_STATUSES:
        raise ValueError(f"{entry_id} is {original.get('status')}; only open work can be claimed")

    existing = original.get("claim")
    reclaimed_from: str | None = None
    if existing and existing.get("owner") != owner:
        if not is_stale(existing, now):
            raise ValueError(
                f"{entry_id} is already claimed by {existing.get('owner')!r} "
                f"(since {existing.get('ts')}); wait for it to go stale "
                f"(>{CLAIM_STALE_HOURS}h) or have that owner release it"
            )
        reclaimed_from = existing.get("owner")

    updated = dict(original)
    updated["claim"] = {"owner": owner, "ts": memory_store.format_ts(now)}
    updated = memory_store.validate_entry(updated, now=now)

    lines[index] = json.dumps(updated, ensure_ascii=False)
    memory_store.write_lines(lines, directory)
    return ClaimResult(entry=updated, reclaimed_from=reclaimed_from)


def release(
    directory: Path,
    entry_id: str,
    owner: str,
    now: datetime | None = None,
    force: bool = False,
) -> dict:
    """Release `owner`'s claim on `entry_id`.

    Raises ValueError if the entry carries no claim, or if it is held by a
    different owner and `force` is not set -- an agent should not be able to
    silently drop another agent's claim out from under it by accident.
    """
    now = now or datetime.now(timezone.utc)
    lines, index, original = memory_review.locate_entry(directory, entry_id)

    existing = original.get("claim")
    if not existing:
        raise ValueError(f"{entry_id} is not currently claimed")
    if existing.get("owner") != owner and not force:
        raise ValueError(
            f"{entry_id} is claimed by {existing.get('owner')!r}, not {owner!r}; "
            f"pass force=True to release someone else's claim"
        )

    updated = dict(original)
    del updated["claim"]
    updated = memory_store.validate_entry(updated, now=now)

    lines[index] = json.dumps(updated, ensure_ascii=False)
    memory_store.write_lines(lines, directory)
    return updated


def claimed_by(entries: Sequence[dict], owner: str, now: datetime | None = None) -> list[dict]:
    """Open work `owner` currently holds a live (non-stale) claim on."""
    now = now or datetime.now(timezone.utc)
    result = []
    for entry in memory_tasks.open_work(entries):
        c = entry.get("claim")
        if c and c.get("owner") == owner and not is_stale(c, now):
            result.append(entry)
    return result


def unclaimed(entries: Sequence[dict], now: datetime | None = None) -> list[dict]:
    """Open work with no live claim -- free for anyone to pick up."""
    now = now or datetime.now(timezone.utc)
    result = []
    for entry in memory_tasks.open_work(entries):
        c = entry.get("claim")
        if not c or is_stale(c, now):
            result.append(entry)
    return result
