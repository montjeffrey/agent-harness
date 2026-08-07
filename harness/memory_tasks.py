"""Confirmed plans as tracked action tasks, with bottleneck detection.

This is the in-session approval path. Instead of an agent writing a
blueprint file and a human opening it to tick a box, the agent presents a
plan in chat, the human accepts or objects in chat, and `confirm()` records
that exchange as durable memory: one `decision` entry for the plan plus one
`task` entry per step, all `accepted`/`confirmed`.

What makes those entries authoritative is the human's own words, stored
verbatim on every entry as `source.confirmation`. That satisfies the entry
contract's accepted-needs-evidence rule with something better than a bare
`manual: true` flag -- it leaves an audit trail you can read back and
disagree with later. The trade is deliberate and worth naming: an agent
that called `confirm()` without actually being told to would be recording
an approval that never happened, and nothing here can detect that. The
stored text is what makes it reviewable after the fact.

Tasks are closed with `complete()`, which routes through the same
`memory_review.apply_review(..., "resolve")` path as any other resolution,
so completion evidence keeps one shape.

`bottlenecks()` is the reason tasks live in memory rather than in a todo
list. Open work is grouped by tag, and a tag scores as a bottleneck when
work piles up on it or sits unfinished -- blockers weigh heavier than
tasks, and age weighs heavier than count, because two things stuck for a
month is a worse signal than five things opened yesterday. The result feeds
the context packet's priorities section, so the next session opens already
knowing which topic keeps stalling.

Nothing here defines a new store or schema version. These are ordinary
schema-1 entries in `inbox.jsonl`, so they are reviewed, superseded,
archived, and semantically indexed by the machinery that already exists.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness import memory_store

# A tag needs at least this many open items before it can score as a
# bottleneck -- one open task is work in progress, not a pattern.
BOTTLENECK_MIN_OPEN = 2

# Open work older than this contributes its age to the bottleneck score.
BOTTLENECK_STALE_DAYS = 7

# A blocker counts this many times a task when scoring. Something declared
# blocking is a stronger signal of a stuck topic than an unstarted task.
BLOCKER_WEIGHT = 3

_ID_SEQ = re.compile(r"^mem-(\d{4}-\d{2}-\d{2})-(\d{3,})$")

OPEN_STATUSES: frozenset[str] = frozenset({"proposed", "accepted"})
WORK_TYPES: frozenset[str] = frozenset({"task", "blocker"})


@dataclass(frozen=True)
class ConfirmResult:
    """The decision entry recorded for a plan, plus its task entries."""

    decision: dict
    tasks: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Bottleneck:
    """One topic that work keeps piling up on."""

    tag: str
    open_count: int
    blocker_count: int
    oldest_days: int
    score: float

    def describe(self) -> str:
        parts = [f"{self.open_count} open"]
        if self.blocker_count:
            parts.append(f"{self.blocker_count} blocking")
        if self.oldest_days:
            parts.append(f"oldest {self.oldest_days}d")
        return f"{self.tag} ({', '.join(parts)})"


def _allocate_ids(directory: Path, count: int, now: datetime) -> list[str]:
    """Reserve `count` consecutive ids up front.

    The task entries reference the decision's id, so it must be known before
    the batch is written -- and the batch has to go through one
    `append_entries` call to keep its all-or-nothing guarantee. Writing the
    decision first and the steps second could leave a plan recorded with
    none of its steps.
    """
    seed = _ID_SEQ.match(memory_store.next_entry_id(directory, now=now))
    day = now.strftime("%Y-%m-%d")
    start = int(seed.group(2)) if seed else 1
    return [f"mem-{day}-{start + offset:03d}" for offset in range(count)]


def confirm(
    directory: Path,
    summary: str,
    confirmation: str,
    tasks: Sequence[str] = (),
    tags: Sequence[str] | None = None,
    project_id: str | None = None,
    now: datetime | None = None,
) -> ConfirmResult:
    """Record an in-session approval as a decision plus its action tasks.

    `confirmation` is what the human actually said. It is stored on every
    entry as the evidence for accepting it, so an empty one is refused --
    an approval with no record of the approving is exactly what this design
    will not create.
    """
    confirmation = (confirmation or "").strip()
    if not confirmation:
        raise ValueError(
            "confirmation text is required: record what the human actually "
            "said, so an accepted entry carries evidence of its approval"
        )
    if not (summary or "").strip():
        raise ValueError("summary is required -- one concise statement of the plan")

    now = now or datetime.now(timezone.utc)
    steps = [s.strip() for s in tasks if s and s.strip()]
    ids = _allocate_ids(directory, 1 + len(steps), now)
    tag_list = [t for t in (tags or []) if t]

    source: dict = {"manual": True, "confirmation": confirmation}
    if project_id:
        source["project"] = project_id

    decision: dict = {
        "id": ids[0],
        "type": "decision",
        "scope": "project",
        "status": "accepted",
        "confidence": "confirmed",
        "summary": summary.strip(),
        "source": dict(source),
    }
    if tag_list:
        decision["tags"] = tag_list

    task_entries: list[dict] = []
    for entry_id, step in zip(ids[1:], steps):
        rec: dict = {
            "id": entry_id,
            "type": "task",
            "scope": "project",
            "status": "accepted",
            "confidence": "confirmed",
            "summary": step,
            "source": dict(source),
            # Links the step back to the plan it came from, so completing a
            # step and superseding a plan stay connected in the archive.
            "plan": ids[0],
        }
        if tag_list:
            rec["tags"] = tag_list
        task_entries.append(rec)

    memory_store.append_entries([decision, *task_entries], directory=directory, now=now)
    return ConfirmResult(decision=decision, tasks=tuple(task_entries))


def complete(
    directory: Path,
    entry_id: str,
    note: str | None = None,
    commit: str | None = None,
    file: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Mark one action task done, keeping its completion evidence.

    A thin pass-through to review's `resolve`, so there is exactly one code
    path that closes work and therefore one shape of completion evidence in
    the archive.
    """
    from harness import memory_review

    result = memory_review.apply_review(
        directory, entry_id, "resolve", note=note, commit=commit, file=file, now=now
    )
    return result.entry


def open_work(entries: Sequence[dict]) -> list[dict]:
    """Every unfinished task and blocker, oldest first.

    Oldest first because that is the order that surfaces what is stuck; the
    newest task is rarely the one worth worrying about.
    """
    live = [
        e
        for e in entries
        if e.get("type") in WORK_TYPES and e.get("status") in OPEN_STATUSES
    ]
    return sorted(live, key=lambda e: str(e.get("ts") or ""))


def _age_days(entry: dict, now: datetime) -> int:
    ts = str(entry.get("ts") or "")
    try:
        stamp = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return max((now - stamp).days, 0)


def bottlenecks(
    entries: Sequence[dict],
    now: datetime | None = None,
    limit: int = 5,
) -> list[Bottleneck]:
    """Topics that open work keeps accumulating on, worst first.

    Scored per tag as `open + BLOCKER_WEIGHT*blockers + staleness`, where
    staleness only counts age beyond BOTTLENECK_STALE_DAYS. A tag with a
    single open item is skipped: that is work in progress, and calling it a
    bottleneck would bury the real ones in noise.
    """
    now = now or datetime.now(timezone.utc)
    buckets: dict[str, list[dict]] = {}
    for entry in open_work(entries):
        for tag in entry.get("tags") or []:
            buckets.setdefault(str(tag), []).append(entry)

    found: list[Bottleneck] = []
    for tag, items in buckets.items():
        if len(items) < BOTTLENECK_MIN_OPEN:
            continue
        blockers = sum(1 for e in items if e.get("type") == "blocker")
        ages = [_age_days(e, now) for e in items]
        oldest = max(ages) if ages else 0
        staleness = sum(max(age - BOTTLENECK_STALE_DAYS, 0) for age in ages) / 7.0
        score = len(items) + BLOCKER_WEIGHT * blockers + staleness
        found.append(
            Bottleneck(
                tag=tag,
                open_count=len(items),
                blocker_count=blockers,
                oldest_days=oldest,
                score=round(score, 2),
            )
        )

    # Sort by score then tag, so identical scores never reorder between runs.
    found.sort(key=lambda b: (-b.score, b.tag))
    return found[:limit]


def prioritized(
    entries: Sequence[dict],
    now: datetime | None = None,
) -> list[dict]:
    """Open work ordered by the bottleneck weight of the tags it carries.

    Work on a stalling topic outranks work on a healthy one; ties fall back
    to blocking-ness, then age, then id. This is the ordering the context
    packet's priorities section uses, so "what should I pick up" has a
    deterministic answer.
    """
    now = now or datetime.now(timezone.utc)
    weights = {b.tag: b.score for b in bottlenecks(entries, now=now, limit=100)}

    def rank(entry: dict) -> tuple:
        worst = max(
            (weights.get(str(t), 0.0) for t in entry.get("tags") or []), default=0.0
        )
        blocking = 1 if entry.get("type") == "blocker" else 0
        return (
            -worst,
            -blocking,
            str(entry.get("ts") or ""),
            str(entry.get("id") or ""),
        )

    return sorted(open_work(entries), key=rank)
