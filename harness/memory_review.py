"""Review: the explicit gate that turns proposals into durable memory.

Nothing else in the harness may promote an entry. Collectors and
`remember` write `proposed` records; review is the only path from there to
`accepted`, and it always represents a deliberate human act -- which is why
accepting stamps `confidence: confirmed` and, absent a commit or file,
attaches `source.manual = True` as the operator's attestation.

Four transitions, all terminal except `accept`:

    accept      proposed          -> accepted    (+ confirmed, + evidence)
    reject      proposed          -> rejected
    resolve     proposed/accepted -> resolved    (task/blocker only, needs
                                                  resolution evidence)
    supersede   proposed/accepted -> superseded  (+ a new accepted entry)

`supersede` is how an accepted fact changes: the original is preserved with
a `superseded_by` pointer and a replacement entry is appended. Accepted
entries are never rewritten in place -- the history of what was believed and
when has to survive, or the record stops being evidence.

Writes go through a line-level splice (`memory_store.read_lines` /
`write_lines`) rather than a parse-and-regenerate pass, so a malformed or
future-schema line elsewhere in the inbox is carried across untouched
instead of being silently dropped by the review of an unrelated entry.

**Batches are one write, not N.** `apply_reviews` is what the `triage`
command runs: every decision is planned against the same snapshot of the
inbox and validated before a byte is written, then the whole batch lands in
one atomic splice. A rejected decision therefore rejects the batch, which is
the only behaviour that lets an unattended operator trust the exit code --
"2" means the inbox is exactly as it was, never "some of it went through".

The decisions file (`parse_decisions`) is deliberately a human format, one
line per entry in the same words the CLI uses (``accept mem-... --commit
abc``), because the person making these calls is editing it in a text editor
after `triage template` wrote it. The `skip` action is what an untouched
template line says: leave the entry proposed, decide later.

Triage never runs inside an agent. There is no MCP surface for any of this,
and there must not be: an agent that could accept its own inference would
make the `proposed` status meaningless.
"""

from __future__ import annotations

import json
import shlex
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from harness import memory_store

REVIEW_ACTIONS: tuple[str, ...] = ("accept", "reject", "resolve", "supersede")

# `skip` is a decision-file action only: it means "not now" and transitions
# nothing, so it is not a review action and `apply_review` refuses it.
DECISION_ACTIONS: tuple[str, ...] = (*REVIEW_ACTIONS, "skip")

# Only these types describe work that can be finished; a decision or a lesson
# is not something you "resolve".
RESOLVABLE_TYPES: frozenset[str] = frozenset({"task", "blocker"})

# Group keys the triage selectors understand. `tag:` and `type:` are exact
# set membership over the pending entries -- not the topic clusters `group_pending`
# renders, which assign each entry to one cluster and would make "accept the
# plan group" depend on how the other entries happened to be tagged.
SELECTOR_PREFIXES: tuple[str, ...] = ("tag:", "type:")
SELECT_ALL = "all"
UNTAGGED_KEY = "untagged"


@dataclass(frozen=True)
class ReviewResult:
    """What a review call did: the transitioned entry, plus the replacement
    entry when the action was `supersede` (None otherwise)."""

    action: str
    entry: dict
    created: dict | None = None


@dataclass(frozen=True)
class Decision:
    """One operator decision about one entry, as `triage apply` consumes it.

    `line` is the source line number in a decisions file (0 when the decision
    came from a command-line flag), so a rejection can point at what to fix.
    """

    entry_id: str
    action: str
    summary: str | None = None
    note: str | None = None
    commit: str | None = None
    file: str | None = None
    line: int = 0

    @property
    def is_skip(self) -> bool:
        return self.action == "skip"


@dataclass(frozen=True)
class Group:
    """One topic cluster of pending entries, as `triage list` shows it."""

    key: str
    entries: tuple[dict, ...]

    @property
    def size(self) -> int:
        return len(self.entries)

    @property
    def types(self) -> dict[str, int]:
        return dict(Counter(str(e.get("type") or "?") for e in self.entries))


def locate_entry(directory: Path, entry_id: str) -> tuple[list[str], int, dict]:
    """Find `entry_id` in the inbox, returning (raw lines, index, record).

    Raises LookupError when no schema-valid entry carries that id. Public
    because `memory_claims` needs the identical splice-in-place pattern --
    read lines, find the index, mutate a copy, write back -- rather than a
    second implementation of the same line-level lookup.
    """
    lines = memory_store.read_lines(directory)
    index = _find_line(lines, entry_id)
    if index is None:
        raise LookupError(f"no entry with id {entry_id!r} in this memory store")
    return lines, index, json.loads(lines[index])


def _find_line(lines: Sequence[str], entry_id: str) -> int | None:
    """Index of the schema-valid line carrying `entry_id`, or None."""
    for index, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("schema") != memory_store.SCHEMA_VERSION:
            continue
        if rec.get("id") == entry_id:
            return index
    return None


def _with_evidence(
    source: dict | None,
    commit: str | None,
    file: str | None,
) -> dict:
    """Return a source dict that satisfies the accepted-needs-evidence rule.

    A supplied commit/file wins; otherwise the operator's act of accepting is
    itself the evidence and is recorded as `manual`. `project` attribution
    already on the entry is preserved either way.
    """
    result = dict(source or {})
    if commit:
        result["commit"] = commit
    if file:
        result["file"] = file
    if not any(result.get(key) for key in ("commit", "file")):
        result["manual"] = True
    return result


def _resolution(
    commit: str | None,
    file: str | None,
    note: str | None,
    stamp: str,
) -> dict:
    """The evidence block a resolved task/blocker keeps. Empty fields dropped."""
    block: dict = {"ts": stamp}
    if commit:
        block["commit"] = commit
    if file:
        block["file"] = file
    if note:
        block["note"] = note
    return block


def _plan_transition(
    original: dict,
    action: str,
    *,
    summary: str | None,
    note: str | None,
    commit: str | None,
    file: str | None,
    now: datetime,
    allocate_id: Callable[[], str],
) -> tuple[dict, dict | None]:
    """Compute the (updated, created) records for one transition.

    Pure with respect to the store: nothing is read or written here, which is
    what lets a batch plan every decision against one snapshot and validate
    them all before the single write. `allocate_id` is called only for
    `supersede`, and only once, so a batch can hand out consecutive ids.
    """
    if action not in REVIEW_ACTIONS:
        raise ValueError(
            f"unknown review action: {action!r} (expected one of {REVIEW_ACTIONS})"
        )

    stamp = memory_store.format_ts(now)
    entry_id = str(original.get("id") or "")
    status = original.get("status", "proposed")
    if status in memory_store.CLOSED_STATUSES:
        raise ValueError(
            f"{entry_id} is already {status}; a closed entry is history and is "
            f"not reviewed again"
        )

    updated = dict(original)
    updated["reviewed_at"] = stamp
    if note:
        updated["review_note"] = note
    created: dict | None = None

    if action == "accept":
        updated["status"] = "accepted"
        updated["confidence"] = "confirmed"
        updated["source"] = _with_evidence(original.get("source"), commit, file)
        if summary:
            updated["summary"] = summary

    elif action == "reject":
        updated["status"] = "rejected"

    elif action == "resolve":
        entry_type = original.get("type")
        if entry_type not in RESOLVABLE_TYPES:
            raise ValueError(
                f"only {sorted(RESOLVABLE_TYPES)} entries can be resolved; "
                f"{entry_id} is a {entry_type}"
            )
        if not (commit or file or note):
            raise ValueError(
                "resolving needs evidence: pass --commit, --file, or --note so "
                "the record shows how the work was verified"
            )
        updated["status"] = "resolved"
        updated["resolution"] = _resolution(commit, file, note, stamp)

    else:  # supersede
        if not summary:
            raise ValueError(
                "superseding needs --summary: the replacement statement is what "
                "the new entry records"
            )
        replacement_id = allocate_id()
        updated["status"] = "superseded"
        updated["superseded_by"] = replacement_id
        created = {
            "schema": memory_store.SCHEMA_VERSION,
            "id": replacement_id,
            "ts": stamp,
            "type": original.get("type"),
            "scope": original.get("scope"),
            "status": "accepted",
            "confidence": "confirmed",
            "summary": summary,
            "source": _with_evidence(original.get("source"), commit, file),
            "supersedes": entry_id,
        }
        tags = original.get("tags")
        if tags:
            created["tags"] = tags
        if note:
            created["review_note"] = note

    # Validate everything before anything is written: a rejected transition
    # must leave the inbox exactly as it was.
    updated = memory_store.validate_entry(updated, now=now)
    if created is not None:
        created = memory_store.validate_entry(created, now=now)
    return updated, created


def _id_allocator(directory: Path, now: datetime) -> Callable[[], str]:
    """Hand out consecutive unused ids for `directory` without re-reading it.

    `next_entry_id` scans the file, which a batch has not written yet, so two
    supersedes in one batch would otherwise both be given the same id.
    """
    seed = memory_store.next_entry_id(directory, now=now)
    day, _, seq = seed.rpartition("-")
    counter = int(seq)

    def allocate() -> str:
        nonlocal counter
        allocated = f"{day}-{counter:03d}"
        counter += 1
        return allocated

    return allocate


def apply_review(
    directory: Path,
    entry_id: str,
    action: str,
    summary: str | None = None,
    note: str | None = None,
    commit: str | None = None,
    file: str | None = None,
    now: datetime | None = None,
) -> ReviewResult:
    """Apply `action` to `entry_id` in the store at `directory`.

    Raises LookupError if the entry does not exist, and ValueError for an
    unknown action, an entry already in a closed status, a missing
    `summary` on supersede, resolution without evidence, or any entry-
    contract violation the transition would introduce. Nothing is written
    unless the whole transition validates.
    """
    now = now or datetime.now(timezone.utc)
    lines, index, original = locate_entry(directory, entry_id)
    updated, created = _plan_transition(
        original, action,
        summary=summary, note=note, commit=commit, file=file,
        now=now, allocate_id=_id_allocator(directory, now),
    )

    lines[index] = json.dumps(updated, ensure_ascii=False)
    if created is not None:
        lines.append(json.dumps(created, ensure_ascii=False))
    memory_store.write_lines(lines, directory)

    return ReviewResult(action=action, entry=updated, created=created)


def apply_reviews(
    directory: Path,
    decisions: Sequence[Decision],
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[ReviewResult]:
    """Apply a batch of decisions to `directory` in one atomic write.

    Every decision is planned against the same snapshot of the inbox and
    validated first; only then is the inbox spliced, once. Any failure --
    an unknown id, a closed entry, a bad transition, the same id decided
    twice -- raises before the write, so a batch either lands whole or not
    at all. `skip` decisions are ignored. With `dry_run` the plan is built
    and returned but nothing is written.

    Raises LookupError for an unknown id and ValueError for everything else;
    both messages name the offending decision (and its file line when it
    came from a decisions file).
    """
    now = now or datetime.now(timezone.utc)
    lines = memory_store.read_lines(directory)
    allocate = _id_allocator(directory, now)

    seen: set[str] = set()
    results: list[ReviewResult] = []
    appended: list[str] = []
    for decision in decisions:
        if decision.is_skip:
            continue
        where = f" (line {decision.line})" if decision.line else ""
        if decision.entry_id in seen:
            raise ValueError(
                f"{decision.entry_id} is decided more than once{where}; a batch "
                f"may transition each entry only once"
            )
        seen.add(decision.entry_id)
        index = _find_line(lines, decision.entry_id)
        if index is None:
            raise LookupError(
                f"no entry with id {decision.entry_id!r} in this memory store{where}"
            )
        original = json.loads(lines[index])
        try:
            updated, created = _plan_transition(
                original, decision.action,
                summary=decision.summary, note=decision.note,
                commit=decision.commit, file=decision.file,
                now=now, allocate_id=allocate,
            )
        except ValueError as exc:
            raise ValueError(f"{decision.action} {decision.entry_id}{where}: {exc}") from exc
        lines[index] = json.dumps(updated, ensure_ascii=False)
        if created is not None:
            appended.append(json.dumps(created, ensure_ascii=False))
        results.append(ReviewResult(action=decision.action, entry=updated, created=created))

    if results and not dry_run:
        memory_store.write_lines(lines + appended, directory)
    return results


def pending_entries(directory: Path) -> list[dict]:
    """Every entry still awaiting review, oldest first."""
    return list(memory_store.iter_entries(directory, status="proposed"))


# --- decisions file --------------------------------------------------------

_DECISION_OPTIONS: dict[str, str] = {
    "--summary": "summary",
    "--note": "note",
    "--commit": "commit",
    "--file": "file",
}


def parse_decision_line(text: str, line: int = 0) -> Decision | None:
    """Parse one decisions-file line; None for a blank or comment line.

    Grammar, matching the `review` CLI so nothing new has to be learned::

        ACTION ENTRY_ID [--summary TEXT] [--note TEXT] [--commit SHA] [--file PATH]

    Quoting follows the shell (`shlex`), so a summary with spaces is written
    ``--summary "Now SQLite."``. Raises ValueError with the line number for
    anything else.
    """
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return None
    try:
        words = shlex.split(stripped)
    except ValueError as exc:
        raise ValueError(f"line {line}: {exc}") from exc
    if len(words) < 2:
        raise ValueError(
            f"line {line}: expected `ACTION ENTRY_ID [options]`, got {stripped!r}"
        )
    action, entry_id, *rest = words
    if action not in DECISION_ACTIONS:
        raise ValueError(
            f"line {line}: unknown action {action!r} (expected one of {DECISION_ACTIONS})"
        )
    fields: dict[str, str] = {}
    pending_option: str | None = None
    for word in rest:
        if pending_option is not None:
            fields[pending_option] = word
            pending_option = None
        elif word in _DECISION_OPTIONS:
            pending_option = _DECISION_OPTIONS[word]
        else:
            raise ValueError(
                f"line {line}: unexpected {word!r} (options are "
                f"{', '.join(_DECISION_OPTIONS)})"
            )
    if pending_option is not None:
        raise ValueError(f"line {line}: option --{pending_option} needs a value")
    return Decision(entry_id=entry_id, action=action, line=line, **fields)


def parse_decisions(lines: Iterable[str]) -> list[Decision]:
    """Parse a whole decisions file. Raises ValueError on the first bad line."""
    decisions: list[Decision] = []
    for number, text in enumerate(lines, start=1):
        decision = parse_decision_line(text, line=number)
        if decision is not None:
            decisions.append(decision)
    return decisions


def render_template(entries: Sequence[dict]) -> str:
    """A decisions file with every pending entry pre-filled as `skip`.

    The operator edits the verb; everything else is already there. The
    summary rides along as a comment so the file can be decided without the
    inbox open beside it.
    """
    lines = [
        "# agent-harness-memory triage decisions",
        "# Replace `skip` with accept | reject | resolve | supersede.",
        "# Options: --summary TEXT  --note TEXT  --commit SHA  --file PATH",
        "# resolve needs --note/--commit/--file; supersede needs --summary.",
        "",
    ]
    for group in group_pending(entries):
        lines.append(f"## {group.key} ({group.size})")
        for entry in group.entries:
            tags = ",".join(entry.get("tags") or [])
            lines.append(f"# [{entry.get('type')}] {str(entry.get('summary') or '').strip()}")
            if tags:
                lines.append(f"#   tags: {tags}")
            lines.append(f"skip {entry.get('id')}")
        lines.append("")
    return "\n".join(lines)


# --- selection and grouping ------------------------------------------------


def select_pending(entries: Sequence[dict], selector: str) -> list[dict]:
    """Pending entries matched by `selector`: an entry id, `tag:X`, `type:X`,
    or `all`. Raises ValueError for an unknown id or malformed selector.

    Selectors are exact set membership, never the topic clusters that
    `group_pending` draws for display -- see `SELECTOR_PREFIXES`.
    """
    pending = [e for e in entries if e.get("status") == "proposed"]
    if selector == SELECT_ALL:
        return pending
    if selector.startswith("tag:"):
        tag = selector[len("tag:"):]
        if tag == UNTAGGED_KEY:
            return [e for e in pending if not e.get("tags")]
        return [e for e in pending if tag in (e.get("tags") or [])]
    if selector.startswith("type:"):
        kind = selector[len("type:"):]
        if kind not in memory_store.ENTRY_TYPES:
            raise ValueError(
                f"unknown type in selector {selector!r} (expected one of "
                f"{memory_store.ENTRY_TYPES})"
            )
        return [e for e in pending if e.get("type") == kind]
    matched = [e for e in pending if e.get("id") == selector]
    if not matched:
        raise ValueError(
            f"{selector!r} is not a pending entry id, `tag:X`, `type:X`, or `all`"
        )
    return matched


def group_pending(entries: Sequence[dict], by: str = "topic") -> list[Group]:
    """Cluster pending entries for compact review.

    `by="topic"` assigns each entry to the *most common* of its tags among
    the pending set, so the backlog reads as its few big themes rather than
    as one line per entry; untagged entries fall back to their type. Each
    entry appears in exactly one group, which keeps the listing the same
    length as the backlog. `by="type"` groups purely by entry type.

    Groups are ordered largest first, then by key, and entries within a group
    by type then id, so the same backlog always renders the same way.
    """
    pending = [e for e in entries if e.get("status") == "proposed"]
    if by not in ("topic", "type"):
        raise ValueError(f"unknown grouping: {by!r} (expected 'topic' or 'type')")

    frequency: Counter[str] = Counter()
    if by == "topic":
        for entry in pending:
            frequency.update(t for t in (entry.get("tags") or []) if isinstance(t, str))

    buckets: dict[str, list[dict]] = {}
    for entry in pending:
        tags = [t for t in (entry.get("tags") or []) if isinstance(t, str)]
        if by == "topic" and tags:
            # Ties break alphabetically so the assignment is stable across runs.
            best = min(tags, key=lambda t: (-frequency[t], t))
            key = f"tag:{best}"
        else:
            key = f"type:{entry.get('type') or '?'}"
        buckets.setdefault(key, []).append(entry)

    groups = [
        Group(
            key=key,
            entries=tuple(sorted(items, key=lambda e: (str(e.get("type")), str(e.get("id"))))),
        )
        for key, items in buckets.items()
    ]
    groups.sort(key=lambda g: (-g.size, g.key))
    return groups
