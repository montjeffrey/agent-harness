"""Deterministic, evidence-bounded curation of project memory proposals.

By default human review remains necessary for an agent's conclusions.  This
pass removes the mechanical work without pretending to solve that semantic
problem:

* pointer entries created by :mod:`harness.memory_ingest` are accepted only
  when their repository-relative file still hashes to the recorded digest;
* exact duplicate proposals are rejected with an audit note; and
* every other proposal remains pending with a reason.

That is the whole pass under the default ``review`` acceptance policy.  When
the operator has set the policy to ``auto`` (see :mod:`harness.memory_policy`)
and the store is one this machine trusts, the remaining agent-recorded
proposals are accepted too.  They are marked ``source.auto`` and keep their
recorded confidence, so an entry nobody reviewed is never presented as one a
person confirmed.  Two kinds stay pending even then: a document pointer whose
file changed, and a ``relationship`` entry, because accepting one widens which
other projects a search may read.

No model, network access, or summarisation is involved.  The pass is safe to
run before every context retrieval because it is idempotent after it handles
the small set of newly ingested or duplicate records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from harness import memory_compact, memory_ingest, memory_policy, memory_review, memory_store

MAX_EXCEPTIONS = 10

NEEDS_HUMAN_EVIDENCE = "requires human evidence"
RELATIONSHIP_NEEDS_REVIEW = "relationship entries require human review"
AUTO_ACCEPT_NOTE = "automatic curation: acceptance policy auto"
NOT_AGENT_NOTE = "not an agent-recorded note; requires human review"
AUTO_ACCEPT_REFUSED = "could not be accepted automatically; review it by hand"

# Tags of entries written by machinery rather than chosen by an agent: hand-off
# drafts, which are replaced every turn while still proposed, and progress
# pointers, whose summaries are commit subjects. Literals rather than imports
# so this module stays as light as the hot path needs; a test pins them to
# `handoff.HANDOFF_TAG` and the progress hook's `PROGRESS_TAG`.
DRAFT_TAGS: frozenset[str] = frozenset({"handoff", "progress"})

# The only `source` an agent's own note carries. Anything more -- a commit, a
# file, a quoted confirmation -- was put there by another writer, and each of
# those has a reason to wait for a person. An allow-list on purpose: a writer
# added later is excluded until someone decides otherwise.
_NOTE_SOURCE_KEYS: frozenset[str] = frozenset({"project"})


def is_agent_note(entry: dict) -> bool:
    """Whether `entry` is a note an agent recorded with `remember`, and so the
    one kind of proposal the `auto` policy accepts."""
    source = entry.get("source")
    if source is not None and not isinstance(source, dict):
        source = {}
    tags = entry.get("tags") or []
    return (
        entry.get("type") != "relationship"
        and set(source or {}) <= _NOTE_SOURCE_KEYS
        and DRAFT_TAGS.isdisjoint(t for t in tags if isinstance(t, str))
    )


@dataclass(frozen=True)
class CurationResult:
    """Observable outcome of one curation pass."""

    accepted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    pending: tuple[tuple[str, str], ...] = ()
    auto_accepted: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.accepted or self.rejected or self.auto_accepted)

    def note(self) -> str | None:
        """One bounded context-packet note, or ``None`` when fully settled."""
        pieces = []
        if self.accepted:
            pieces.append(f"auto-accepted {len(self.accepted)} verified document pointer(s)")
        if self.auto_accepted:
            pieces.append(
                f"auto-accepted {len(self.auto_accepted)} agent-recorded proposal(s) "
                "under the auto policy"
            )
        if self.rejected:
            pieces.append(f"rejected {len(self.rejected)} exact duplicate proposal(s)")
        if self.pending:
            pieces.append(f"{len(self.pending)} proposal(s) still need human evidence")
        return "memory curation: " + "; ".join(pieces) if pieces else None


def _source_path(root: Path, value: object) -> Path | None:
    """Return a safe in-repository source path, never following ``..`` out."""
    if not isinstance(value, str) or not value.strip():
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or str(relative) in {"", "."}:
        return None
    try:
        candidate = (Path(root).resolve() / Path(*relative.parts)).resolve()
        return candidate if candidate.is_relative_to(Path(root).resolve()) else None
    except OSError:
        return None


def pointer_reason(entry: dict, project_root: Path) -> str | None:
    """Return why an ingested pointer is not currently verifiable, else ``None``."""
    tags = entry.get("tags") or []
    source = entry.get("source")
    if memory_ingest.INGEST_TAG not in tags or not isinstance(source, dict):
        return NEEDS_HUMAN_EVIDENCE
    path = _source_path(project_root, source.get("file"))
    digest = source.get("digest")
    if path is None or not isinstance(digest, str):
        return "ingested pointer has invalid source metadata"
    try:
        if not path.is_file() or path.stat().st_size > memory_ingest.MAX_FILE_BYTES:
            return "source file is missing, unreadable, or too large"
        # Ingestion hashes decoded text after universal-newline translation.
        # Hash the same representation here; hashing raw bytes would mark a
        # Windows CRLF document as changed immediately after it was ingested.
        current = memory_ingest.digest_bytes(
            path.read_text(encoding="utf-8", errors="replace").encode("utf-8")
        )
    except OSError:
        return "source file is unreadable"
    if current != digest:
        return "source file changed since ingestion"
    return None


def curate(project_root: Path, now: datetime | None = None) -> CurationResult:
    """Apply deterministic curation rules to one project's pending entries.

    Raises only on a failed review write.  A malformed entry is already
    excluded by ``iter_entries``; an unverifiable source is an expected pending
    state rather than a failed curation pass.
    """
    root = Path(project_root)
    directory = memory_store.project_memory_dir(root)
    entries = list(memory_store.iter_entries(directory))
    proposed = [entry for entry in entries if entry.get("status") == "proposed"]
    seen: dict[tuple[object, object, str], str] = {}
    accepted: list[str] = []
    rejected: list[str] = []
    pending: list[tuple[str, str]] = []
    auto_accepted: list[str] = []

    # Asked at most once, and only when a proposal actually needs the answer:
    # the trust check shells out to git, and most passes have nothing pending.
    auto_allowed: bool | None = None

    def may_auto_accept() -> bool:
        nonlocal auto_allowed
        if auto_allowed is None:
            auto_allowed = (
                memory_policy.acceptance_policy() == memory_policy.AUTO
                and memory_store.is_trusted_store(root)
            )
        return auto_allowed

    for entry in proposed:
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        key = (
            entry.get("type"),
            entry.get("scope"),
            memory_compact.normalize_summary(entry.get("summary")),
        )
        first = seen.get(key)
        if first:
            memory_review.apply_review(
                directory,
                entry_id,
                "reject",
                note=f"automatic curation: exact duplicate of {first}",
                now=now,
            )
            rejected.append(entry_id)
            continue
        seen[key] = entry_id

        reason = pointer_reason(entry, root)
        if reason is None:
            source = entry.get("source") or {}
            memory_review.apply_review(
                directory,
                entry_id,
                "accept",
                file=str(source["file"]),
                now=now,
            )
            accepted.append(entry_id)
        elif reason == NEEDS_HUMAN_EVIDENCE and may_auto_accept():
            if entry.get("type") == "relationship":
                pending.append((entry_id, RELATIONSHIP_NEEDS_REVIEW))
                continue
            if not is_agent_note(entry):
                pending.append((entry_id, NOT_AGENT_NOTE))
                continue
            try:
                memory_review.apply_review(
                    directory,
                    entry_id,
                    "accept",
                    note=AUTO_ACCEPT_NOTE,
                    now=now,
                    auto=True,
                )
            except (LookupError, ValueError):
                # Reviewed, removed, or rewritten by someone else since the
                # snapshot above, or refused by the store's own validation.
                # Either way it is reported, not dropped.
                pending.append((entry_id, AUTO_ACCEPT_REFUSED))
                continue
            auto_accepted.append(entry_id)
        else:
            pending.append((entry_id, reason))

    return CurationResult(
        tuple(accepted), tuple(rejected), tuple(pending), tuple(auto_accepted)
    )


def render(result: CurationResult) -> str:
    """Render a deterministic, bounded CLI/MCP observation."""
    lines = ["status: success", f"summary: {result.note() or 'nothing to curate'}"]
    if result.pending:
        lines.append("next_actions: review only the remaining exception queue")
    else:
        lines.append("next_actions: no manual memory review is currently required")
    artifacts: list[str] = []
    if result.accepted:
        artifacts.append("accepted=" + ",".join(result.accepted))
    if result.auto_accepted:
        artifacts.append("auto_accepted=" + ",".join(result.auto_accepted))
    if result.rejected:
        artifacts.append("rejected=" + ",".join(result.rejected))
    lines.append("artifacts: " + " ".join(artifacts))
    for entry_id, reason in result.pending[:MAX_EXCEPTIONS]:
        lines.append(f"pending: {entry_id} — {reason}")
    if len(result.pending) > MAX_EXCEPTIONS:
        lines.append(f"pending: {len(result.pending) - MAX_EXCEPTIONS} further proposal(s) omitted")
    return "\n".join(lines)
