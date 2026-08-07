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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness import memory_store

REVIEW_ACTIONS: tuple[str, ...] = ("accept", "reject", "resolve", "supersede")

# Only these types describe work that can be finished; a decision or a lesson
# is not something you "resolve".
RESOLVABLE_TYPES: frozenset[str] = frozenset({"task", "blocker"})


@dataclass(frozen=True)
class ReviewResult:
    """What a review call did: the transitioned entry, plus the replacement
    entry when the action was `supersede` (None otherwise)."""

    action: str
    entry: dict
    created: dict | None = None


def _locate(directory: Path, entry_id: str) -> tuple[list[str], int, dict]:
    """Find `entry_id` in the inbox, returning (raw lines, index, record).

    Raises LookupError when no schema-valid entry carries that id.
    """
    lines = memory_store.read_lines(directory)
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
            return lines, index, rec
    raise LookupError(f"no entry with id {entry_id!r} in this memory store")


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
    if action not in REVIEW_ACTIONS:
        raise ValueError(
            f"unknown review action: {action!r} (expected one of {REVIEW_ACTIONS})"
        )

    now = now or datetime.now(timezone.utc)
    stamp = memory_store.format_ts(now)
    lines, index, original = _locate(directory, entry_id)

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
        replacement_id = memory_store.next_entry_id(directory, now=now)
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

    # Validate everything before touching the file: a rejected transition must
    # leave the inbox exactly as it was.
    updated = memory_store.validate_entry(updated, now=now)
    if created is not None:
        created = memory_store.validate_entry(created, now=now)

    lines[index] = json.dumps(updated, ensure_ascii=False)
    if created is not None:
        lines.append(json.dumps(created, ensure_ascii=False))
    memory_store.write_lines(lines, directory)

    return ReviewResult(action=action, entry=updated, created=created)


def pending_entries(directory: Path) -> list[dict]:
    """Every entry still awaiting review, oldest first."""
    return list(memory_store.iter_entries(directory, status="proposed"))
