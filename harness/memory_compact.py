"""Weekly compaction: keep the inbox bounded so retrieval stays fresh.

Every other memory command adds. `remember` appends, `review` transitions a
status, `index` mirrors whatever is there. Nothing removes, so an inbox in
long-term use grows without bound -- and a growing inbox degrades exactly the
things the store exists to provide: the selector ranks over more candidates,
the context packet spends its budget on settled work, and the semantic index
carries entries nobody will ever need again.

Compaction is the only operation that removes entries from the store of
record, which is why the rules here are narrow and why the ordering matters.

**What may leave the inbox.** Only two things: entries in a closed status
(rejected/superseded/resolved -- work that is genuinely over), and duplicate
*proposals*. Nothing else. In particular an unreviewed proposal is never
discarded, because compaction is automated maintenance and the decision to
throw away an observation belongs to a human. Everything removed is written
to the week's archive first, so a removal is always recoverable.

**Promotion is a copy, not a move.** Accepted knowledge is written into
`durable.md` for human and Git review, but stays in the inbox. `memory_context`
reads durable knowledge *out of the inbox* -- it treats only `scope.md` and
`active.md` as prose -- so moving an accepted decision into `durable.md` would
delete it from retrieval and from the semantic index. That would make the
store smaller by making it worse.

**Write ordering is the crash-safety guarantee**, more than the snapshot is.
The inbox is the store of record and every other file is derived from it, so
the inbox is spliced *last*: archive, then durable, then active, then inbox. A
crash at any earlier point loses only derived work that the next run redoes.
Had the inbox been rewritten first, the same crash would lose entries.

Deliberately **not** implemented here, because each is its own roadmap item and
folding them in would repeat the plan-drift this increment was meant to end:
scope-drift detection (design step 6), cross-project relationship proposals
(step 9), contradiction detection (step 5, which exists nowhere yet), and the
optional AI summarization pass. Design step 4 (reconcile task and blocker
states) needs no work here -- `memory_review` and `memory_tasks` already
enforce those transitions at write time, so there is nothing left to reconcile.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from harness import memory_store


# Budgets for the rewritten `active.md`. Module constants rather than config:
# `init_project` does not yet write budget settings into config.json, and
# inventing a config system inside this increment would be scope creep.
ACTIVE_MAX_ENTRIES = 20
ACTIVE_MAX_CHARS = 4000

# Types whose accepted entries represent knowledge that outlives the work that
# produced it. Tasks and blockers are transient; relationships are rendered by
# the context packet as their own section and are not project-durable prose.
PROMOTABLE_TYPES: frozenset[str] = frozenset({"decision", "discovery", "lesson"})
WORK_TYPES: frozenset[str] = frozenset({"task", "blocker"})

LOCK_NAME = ".compact.lock"
LOCK_STALE_SECONDS = 900

_ENTRY_ID_RE = re.compile(r"mem-\d{4}-\d{2}-\d{2}-\d{3,}")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


class CompactLockError(RuntimeError):
    """Another compaction holds the project lock."""


@dataclass(frozen=True)
class CompactResult:
    """What one compaction pass did."""

    week: str
    archived: int = 0
    duplicates: int = 0
    promoted: int = 0
    carried: int = 0
    omitted: int = 0
    archive_path: Path = Path()
    index_stale: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.archived or self.promoted)


# --- pure helpers ---------------------------------------------------------


def week_stamp(now: datetime) -> str:
    """The ISO week label used for archive filenames, e.g. ``2026-W33``.

    Uses the ISO *year* from `isocalendar()`, not the calendar year: 2027-01-01
    falls in ISO week 53 of 2026, and filing it under 2027 would put the entry
    in a week that does not contain its date.
    """
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def normalize_summary(text: object) -> str:
    """Fold a summary to its comparison form for duplicate detection.

    Case, surrounding whitespace, internal whitespace runs, and punctuation are
    all discarded. Two observations of the same fact recorded minutes apart by
    different agents differ in exactly these ways and in nothing that matters.
    """
    lowered = str(text or "").lower()
    return _WHITESPACE_RE.sub(" ", _PUNCTUATION_RE.sub("", lowered)).strip()


def is_terminal(entry: Mapping) -> bool:
    """Whether `entry` represents work that is over and may be archived."""
    return entry.get("status") in memory_store.CLOSED_STATUSES


def is_promotable(entry: Mapping) -> bool:
    """Whether `entry` is confirmed knowledge worth writing into `durable.md`.

    Status alone is the gate. The trust model already guarantees that accepted
    implies evidence and that an inferred entry can never be accepted, so no
    separate confidence check is needed here.
    """
    return entry.get("status") == "accepted" and entry.get("type") in PROMOTABLE_TYPES


def find_duplicates(entries: Sequence[Mapping]) -> set[str]:
    """Ids of redundant *proposals*, keeping the earliest of each group.

    Only proposals are considered. Two accepted entries that read alike may
    cite different commits, and collapsing them would destroy the audit trail
    the trust model exists to preserve -- so accepted entries are never
    deduplicated, however similar their wording.

    Grouping is by (type, scope, normalized summary): identical wording as a
    task and as a decision are two different claims, not one repeated.
    """
    seen: dict[tuple, str] = {}
    dropped: set[str] = set()
    for entry in sorted(entries, key=lambda e: str(e.get("id") or "")):
        if entry.get("status") != "proposed":
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        key = (entry.get("type"), entry.get("scope"), normalize_summary(entry.get("summary")))
        if key in seen:
            dropped.add(entry_id)
        else:
            seen[key] = entry_id
    return dropped


def promoted_ids(durable_path: Path) -> set[str]:
    """Entry ids already present in `durable.md`.

    Promotion is idempotent by id rather than by content, so re-running
    compaction never appends the same knowledge twice, and a human who edits
    the wording of a promoted line does not cause it to be re-promoted.
    """
    try:
        text = Path(durable_path).read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(_ENTRY_ID_RE.findall(text))


def _entry_line(entry: Mapping) -> str:
    entry_id = entry.get("id", "?")
    kind = entry.get("type", "?")
    summary = str(entry.get("summary") or "").strip()
    return f"- `{entry_id}` {kind}: {summary}"


def render_archive(week: str, entries: Sequence[Mapping], now: datetime) -> str:
    """One compaction run's removals, as an appendable Markdown section."""
    lines = [f"## Compaction {memory_store.format_ts(now)}", ""]
    for entry in sorted(entries, key=lambda e: str(e.get("id") or "")):
        status = entry.get("status", "?")
        ts = entry.get("ts", "?")
        lines.append(
            f"- `{entry.get('id', '?')}` {entry.get('type', '?')}/{status} ({ts}): "
            f"{str(entry.get('summary') or '').strip()}"
        )
        source = entry.get("source")
        if isinstance(source, Mapping) and source:
            detail = ", ".join(f"{k}: {v}" for k, v in sorted(source.items()))
            lines.append(f"  - source: {detail}")
    lines.append("")
    return "\n".join(lines)


def render_active(entries: Sequence[Mapping], week: str, now: datetime) -> tuple[str, int, int]:
    """Rewrite `active.md` from live entries. Returns (text, carried, omitted).

    Budgeting is unit-atomic, matching the context packet: an entry is included
    whole or not at all, and the reader is told how many were dropped rather
    than being handed a silently truncated list. Sections use `##` headings
    because `memory_context` consumes this file through its Markdown section
    parser -- a rewrite that broke that parse would take the packet down.
    """
    work = sorted(
        (e for e in entries if e.get("type") in WORK_TYPES),
        key=lambda e: str(e.get("id") or ""),
    )
    knowledge = sorted(
        (e for e in entries if is_promotable(e)),
        key=lambda e: str(e.get("id") or ""),
    )

    header = f"# Active memory\n\nLast compacted: {memory_store.format_ts(now)} ({week})\n"
    candidates = [("work", e) for e in work] + [("knowledge", e) for e in knowledge]
    allowed = candidates[:ACTIVE_MAX_ENTRIES]

    def assemble(chosen: list[tuple[str, Mapping]], omitted: int) -> str:
        parts = [header]
        for label, heading in (("work", "## Open work"), ("knowledge", "## Recent decisions")):
            section = [_entry_line(e) for tag, e in chosen if tag == label]
            if section:
                parts.append("\n" + heading + "\n\n" + "\n".join(section) + "\n")
        if omitted:
            parts.append(
                f"\n_{omitted} further entries omitted by the active-memory budget._\n"
            )
        return "".join(parts)

    # Drop from the tail until the whole document fits. Recomputed each round
    # because the omission notice itself costs characters.
    chosen = list(allowed)
    while True:
        omitted = len(candidates) - len(chosen)
        text = assemble(chosen, omitted)
        if len(text) <= ACTIVE_MAX_CHARS or not chosen:
            return text, len(chosen), omitted
        chosen.pop()


# --- locking --------------------------------------------------------------


def _lock_is_stale(path: Path, now: datetime) -> bool:
    """Whether an existing lock may be broken.

    An unreadable or malformed lock counts as stale: a corrupt file must not be
    able to wedge compaction permanently, and the failure mode of breaking one
    is a duplicate run that dedupe absorbs.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        held = datetime.fromisoformat(str(data["ts"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if held.tzinfo is None:
        return True
    return (now - held.astimezone(timezone.utc)).total_seconds() > LOCK_STALE_SECONDS


@contextmanager
def compact_lock(directory: Path, now: datetime | None = None) -> Iterator[Path]:
    """Hold an advisory project-scoped compaction lock.

    This excludes compaction from compaction, which is the case that can
    corrupt state: two passes archiving and splicing the same inbox. It does
    *not* make appenders honour it -- writer-side locking is a separate
    concern tracked on its own, and `append_entries` remains an atomic append
    that a compaction in flight tolerates.
    """
    now = now or datetime.now(timezone.utc)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOCK_NAME

    if path.exists():
        if not _lock_is_stale(path, now):
            raise CompactLockError(
                f"another compaction holds {path}; wait for it to finish or "
                f"remove the file if no compaction is running"
            )
        path.unlink(missing_ok=True)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:  # lost a race between the check and here
        raise CompactLockError(f"another compaction holds {path}") from exc

    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": memory_store.format_ts(now)}).encode("utf-8"))
    finally:
        os.close(fd)

    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# --- file writes ----------------------------------------------------------


def _write_archive(path: Path, week: str, entries: Sequence[Mapping], now: datetime) -> None:
    """Append this run's removals to the week's archive.

    Appends rather than replaces: `archive/YYYY-Www.md` is immutable history,
    and a second run in the same ISO week must not erase the first run's record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else f"# Archive {week}\n"
    body = render_archive(week, entries, now)
    path.write_text(existing.rstrip("\n") + "\n\n" + body, encoding="utf-8")


def _write_durable(path: Path, entries: Sequence[Mapping], now: datetime) -> None:
    """Append newly promoted knowledge, preserving everything already there."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# Durable project memory\n"
    lines = [f"## Promoted {memory_store.format_ts(now)}", ""]
    lines.extend(_entry_line(e) for e in sorted(entries, key=lambda e: str(e.get("id") or "")))
    lines.append("")
    path.write_text(existing.rstrip("\n") + "\n\n" + "\n".join(lines), encoding="utf-8")


def _write_active(directory: Path, text: str) -> None:
    (Path(directory) / "active.md").write_text(text, encoding="utf-8")


def _splice_inbox(directory: Path, raw_lines: Sequence[str], removed_ids: set[str]) -> None:
    """Drop `removed_ids` from the inbox at the *line* level.

    Line-level rather than `rewrite_entries` for two reasons: a line this
    schema version cannot parse may be a future schema rather than garbage and
    must be carried across untouched, and re-serializing every surviving record
    would rewrite entries this pass has no business modifying.
    """
    kept: list[str] = []
    for line in raw_lines:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            kept.append(line)
            continue
        if isinstance(record, dict) and str(record.get("id") or "") in removed_ids:
            continue
        kept.append(line)
    memory_store.write_lines(kept, directory)


def _snapshot(paths: Iterable[Path]) -> dict[Path, bytes | None]:
    """Capture file contents for rollback; `None` means the file did not exist."""
    snapshot: dict[Path, bytes | None] = {}
    for path in paths:
        try:
            snapshot[path] = path.read_bytes() if path.is_file() else None
        except OSError:
            snapshot[path] = None
    return snapshot


def _restore(snapshot: Mapping[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        except OSError:
            # Best effort: one unrestorable file must not mask the original
            # error, which is what the caller actually needs to see.
            continue


# --- the pass ------------------------------------------------------------


def compact(project_root: Path, now: datetime | None = None) -> CompactResult:
    """Run one deterministic compaction pass over `project_root`.

    Raises `CompactLockError` if another compaction holds the lock, and
    re-raises any write failure after restoring the derived files.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    directory = memory_store.project_memory_dir(root)
    week = week_stamp(now)
    archive_path = directory / "archive" / f"{week}.md"
    durable_path = directory / "durable.md"
    active_path = directory / "active.md"

    with compact_lock(directory, now=now):
        raw_lines = memory_store.read_lines(directory)
        entries = list(memory_store.iter_entries(directory))

        duplicates = find_duplicates(entries)
        removed_ids = {str(e.get("id") or "") for e in entries if is_terminal(e)} | duplicates
        removed_ids.discard("")
        removed = [e for e in entries if str(e.get("id") or "") in removed_ids]
        kept = [e for e in entries if str(e.get("id") or "") not in removed_ids]

        already = promoted_ids(durable_path)
        to_promote = [e for e in kept if is_promotable(e) and str(e.get("id") or "") not in already]

        snapshot = _snapshot((archive_path, durable_path, active_path, directory / "inbox.jsonl"))
        try:
            if removed:
                _write_archive(archive_path, week, removed, now)
            if to_promote:
                _write_durable(durable_path, to_promote, now)
            text, carried, omitted = render_active(kept, week, now)
            _write_active(directory, text)
            # Last, deliberately: see the module docstring on write ordering.
            if removed_ids:
                _splice_inbox(directory, raw_lines, removed_ids)
        except BaseException:
            _restore(snapshot)
            raise

    return CompactResult(
        week=week,
        archived=len(removed),
        duplicates=len(duplicates),
        promoted=len(to_promote),
        carried=carried,
        omitted=omitted,
        archive_path=archive_path,
        # `update_index` deletes ids that are gone, but only when it is re-run.
        # Until then the index keeps serving archived entries, so a removal has
        # to be reported as making the index stale.
        index_stale=bool(removed_ids),
    )
