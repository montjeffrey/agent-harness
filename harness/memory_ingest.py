"""Seed a new project's memory from the documentation it already has.

An enrolled project used to start empty. Enrollment created the files, built
the graph, and left a store with nothing in it -- so the session hook had
nothing to inject, `scope.md` stayed an untouched template, and the two things
that depend on it stayed switched off. This module is what enrollment runs so
that a project arrives with a map instead of an empty room.

**Pointers, not copies.** The memory protocol is explicit that the repository
is not worth recording into memory: code structure, git history, and anything
a README or ADR already says are retrievable without it. So nothing here
ingests document *prose*. What it records is the map -- which document answers
which question, where it lives, and what state it was in when read. A pointer
earns its place because finding the right document is the expensive step; the
document itself was never missing.

**Why `scope.md` is the highest-value output.** Two features read it and both
are dark until it is filled in. `memory_context.active_objective` takes the
default retrieval query from it, and `memory_index.semantic_pointless_reason`
declines semantic ranking outright when there is no query to rank against.
An untouched scope file therefore pins every project on this machine to
deterministic selection, silently and permanently. Seeding the Purpose section
from the README is what turns that back on.

Only Purpose is seeded. Boundaries, exclusions, acceptance criteria and the
active objective are left as placeholders because they cannot be read off a
README without inventing them, and an invented boundary is worse than a blank
one -- it is a rule an agent will be held to that nobody agreed.

**Idempotency lives in the store of record.** Every entry written here carries
`source.digest`, the hash of the file it points at. A re-run reads the existing
entries, skips any document whose digest is already present, and proposes only
what changed. That keeps the decision in the one place that survives a clone:
the JSONL itself. A side manifest under `work/` would have been cheaper and
wrong, since that tier is gitignored per-machine scratch -- a teammate's first
enroll would re-propose every pointer the original author had already filed.

Stdlib only. No model, no network, no chromadb. Enrollment stays sub-second on
a cold cache, which is the only reason it is affordable to run unconditionally.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from harness import memory_store

# Bounds. A repository with a thousand markdown files is not a reason to
# propose a thousand entries -- the map stops being a map at that point, and
# the context packet's budget would be spent on pointers instead of knowledge.
MAX_DOCUMENTS = 40
MAX_FILE_BYTES = 1_000_000
MAX_EXCERPT_CHARS = 140
MAX_SECTIONS_LISTED = 4

# Digest length. Full sha256 is 64 hex characters and every one of them is
# carried on every line of the store forever. 16 is 64 bits of collision
# resistance against an adversary who is not present -- these are our own
# files, and the failure mode of a collision is one skipped re-proposal.
DIGEST_CHARS = 16

INGEST_TAG = "ingested"

# Documents worth pointing at, in the order a newcomer would want them. Order
# matters: it is the order entries are proposed in, and `MAX_DOCUMENTS` cuts
# from the end, so the most orienting documents survive a truncated scan.
ROOT_DOCUMENTS: tuple[str, ...] = (
    "README.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "ROADMAP.md",
    "CURRENT_STATE.md",
    "CHANGELOG.md",
)

# Where ADRs live, across the conventions in the wild. All are checked; a
# project using two of them at once gets both, deduplicated by resolved path.
ADR_DIRECTORIES: tuple[str, ...] = (
    "docs/adr",
    "docs/adrs",
    "docs/decisions",
    "doc/adr",
    "adr",
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_ADR_NUMBER_RE = re.compile(r"^(\d{3,4})[-_]")
# Strips a title's own "ADR 0001:" lead-in, which most ADR templates already
# write into their H1. Without this, the summary's "ADR 0001 records:" prefix
# and the title repeat the number and the word "ADR" back to back.
_ADR_TITLE_PREFIX_RE = re.compile(r"^ADR\s*\d{3,4}\s*[:.\-]?\s*", re.IGNORECASE)
_BADGE_RE = re.compile(r"^\[!\[.*\]\(.*\)\]?\(?.*\)?$")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WHITESPACE_RE = re.compile(r"\s+")

# The placeholder `init_project` writes into scope.md. Finding it verbatim is
# what proves a human has not written a Purpose yet. Matching anything looser
# risks overwriting someone's actual words, which this module must never do.
SCOPE_PURPOSE_PLACEHOLDER = "_What this project is for._"


@dataclass
class IngestResult:
    """What one ingest run did, in the terms an operator would ask about."""

    scanned: int = 0
    proposed: list[dict] = field(default_factory=list)
    unchanged: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    scope_seeded: bool = False
    scope_reason: str = ""

    @property
    def wrote_anything(self) -> bool:
        return bool(self.proposed) or self.scope_seeded

    def describe(self) -> str:
        bits = [f"{len(self.proposed)} proposed", f"{self.scanned} scanned"]
        if self.unchanged:
            bits.append(f"{self.unchanged} unchanged")
        if self.rejected:
            bits.append(f"{len(self.rejected)} skipped")
        if self.scope_seeded:
            bits.append("scope.md purpose seeded")
        return ", ".join(bits)


def digest_bytes(data: bytes) -> str:
    """Short content hash used to tell a re-read document from a changed one."""
    return hashlib.sha256(data).hexdigest()[:DIGEST_CHARS]


def _read_text(path: Path) -> str | None:
    """File text, or None if it is unreadable, empty, or implausibly large.

    Returns None rather than raising for every failure mode. Ingestion runs
    inside enrollment, and no single document is worth failing an enrollment.
    """
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _clean(text: str) -> str:
    """Fold markdown inline noise to the words a reader would actually keep."""
    without_links = _LINK_RE.sub(r"\1", text)
    stripped = without_links.replace("`", "").replace("*", "").replace("_", "")
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def document_title(path: Path, text: str) -> str:
    """The document's own H1, falling back to a readable form of its name."""
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 1:
            title = _clean(match.group(2))
            if title:
                return title
    return _clean(path.stem.replace("-", " ").replace("_", " ")) or path.name


def section_headings(text: str) -> list[str]:
    """Level-2 headings, in document order -- the document's table of contents."""
    found = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 2:
            heading = _clean(match.group(2))
            if heading:
                found.append(heading)
    return found


def first_paragraph(text: str) -> str:
    """The opening prose, skipping headings, badges, and blockquote notes.

    Badges are the reason this is not simply "the first non-blank line": a
    README's first three lines are frequently CI shields, which say nothing
    about the project and would make a useless pointer.
    """
    buffer: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if buffer:
                break
            continue
        if line.startswith("#") or line.startswith(">") or _BADGE_RE.match(line):
            if buffer:
                break
            continue
        if line.startswith(("- ", "* ", "|", "```")):
            if buffer:
                break
            continue
        buffer.append(line)
    return _clean(" ".join(buffer))


def _truncate(text: str, limit: int) -> str:
    """Cut to `limit` on a word boundary, marking that something was cut."""
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return clipped + "..."


def adr_status(text: str) -> str:
    """An ADR's declared status, if it declares one.

    Worth carrying into the summary because a superseded ADR and an accepted
    one are opposite instructions, and a pointer that omits which is which
    sends an agent to read a decision that was reversed.
    """
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        match = _HEADING_RE.match(line)
        if match and _clean(match.group(2)).lower() == "status":
            for follow in lines[index + 1 : index + 6]:
                value = _clean(follow)
                if value and not value.startswith("#"):
                    return value
            return ""
        if line.lower().startswith("status:"):
            return _clean(line.split(":", 1)[1])
    return ""


def discover(project_root: Path) -> list[tuple[Path, str]]:
    """Documents worth pointing at, as (path, kind) with kind in {adr, doc}.

    Deterministically ordered so two machines enrolling the same commit
    propose the same entries in the same order. Deduplicated by resolved path
    because the ADR directory conventions overlap.
    """
    root = Path(project_root)
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path, kind: str) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or not path.is_file():
            return
        seen.add(resolved)
        found.append((path, kind))

    for directory in ADR_DIRECTORIES:
        adr_dir = root / directory
        if adr_dir.is_dir():
            for path in sorted(adr_dir.glob("*.md")):
                if path.name.lower() not in {"readme.md", "index.md", "template.md"}:
                    add(path, "adr")

    for name in ROOT_DOCUMENTS:
        add(root / name, "doc")

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for path in sorted(docs_dir.glob("*.md")):
            add(path, "doc")

    return found[:MAX_DOCUMENTS]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return path.name


def build_entry(path: Path, kind: str, root: Path, text: str) -> dict:
    """One pointer entry for one document. Pure -- no I/O, no clock."""
    relpath = _relative(path, root)
    title = document_title(path, text)
    digest = digest_bytes(text.encode("utf-8"))

    if kind == "adr":
        number = _ADR_NUMBER_RE.match(path.name)
        label = "ADR " + number.group(1) if number else "ADR"
        status = adr_status(text)
        clean_title = _ADR_TITLE_PREFIX_RE.sub("", title) or title
        summary = label + " records: " + clean_title
        if status:
            summary += " [status: " + status + "]"
        summary += " -- see " + relpath
        entry_type = "decision"
        tags = [INGEST_TAG, "adr"]
    else:
        headings = section_headings(text)[:MAX_SECTIONS_LISTED]
        summary = title + " (" + relpath + ")"
        excerpt = _truncate(first_paragraph(text), MAX_EXCERPT_CHARS)
        if excerpt:
            summary += " -- " + excerpt
        if headings:
            summary += " Sections: " + "; ".join(headings) + "."
        entry_type = "discovery"
        tags = [INGEST_TAG, "doc"]

    return {
        "type": entry_type,
        "scope": "project",
        "status": "proposed",
        "confidence": "inferred",
        "summary": _truncate(summary, memory_store.MAX_SUMMARY_CHARS - 1),
        "source": {"file": relpath, "digest": digest},
        "tags": tags,
    }


def existing_digests(entries) -> set[tuple[str, str]]:
    """(file, digest) pairs already pointed at, for skipping unchanged docs.

    Reads every entry rather than only ingested ones. A human who recorded a
    pointer by hand has said the same thing this module would say, and
    proposing it again would be noise they then have to review away.
    """
    pairs: set[tuple[str, str]] = set()
    for entry in entries:
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        file_name, digest = source.get("file"), source.get("digest")
        if isinstance(file_name, str) and isinstance(digest, str):
            pairs.add((file_name, digest))
    return pairs


def seed_scope_purpose(
    project_root: Path, text: str, *, dry_run: bool = False
) -> tuple[bool, str]:
    """Fill scope.md's Purpose placeholder from README prose.

    Returns (seeded, reason). Refuses -- and says why -- when there is no scope
    file, when the placeholder is gone (a human has written a Purpose, and
    overwriting it would be the single most destructive thing this module
    could do), or when the README yields nothing usable.
    """
    scope_path = Path(project_root) / ".agent-harness" / "scope.md"
    if not scope_path.is_file():
        return False, "no scope.md; run enroll first"

    try:
        current = scope_path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, "scope.md unreadable (" + str(exc) + ")"

    if SCOPE_PURPOSE_PLACEHOLDER not in current:
        return False, "purpose already written; left untouched"

    purpose = _truncate(first_paragraph(text), 400)
    if len(purpose) < 20:
        return False, "README has no usable opening prose"

    seeded = (
        purpose
        + "\n\n_Seeded from README.md at enrollment. Correct it if it is wrong --"
        " agents are held to this file._"
    )
    if dry_run:
        return True, purpose

    try:
        scope_path.write_text(
            current.replace(SCOPE_PURPOSE_PLACEHOLDER, seeded), encoding="utf-8"
        )
    except OSError as exc:
        return False, "scope.md not writable (" + str(exc) + ")"
    return True, purpose


def ingest(
    project_root: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    seed_scope: bool = True,
) -> IngestResult:
    """Propose pointer entries for `project_root`'s documentation.

    Every entry is validated one at a time before anything is written.
    `append_entries` validates the whole batch and refuses all of it if any
    single entry fails -- correct for a caller submitting a considered set,
    wrong here, where one README containing something that pattern-matches a
    credential would otherwise cost the project every other pointer.
    """
    root = Path(project_root)
    result = IngestResult()

    documents = discover(root)
    result.scanned = len(documents)

    directory = memory_store.project_memory_dir(root)
    try:
        known = existing_digests(memory_store.iter_entries(directory))
    except OSError:
        known = set()

    candidates: list[dict] = []
    for path, kind in documents:
        text = _read_text(path)
        if text is None:
            result.rejected.append((_relative(path, root), "unreadable or too large"))
            continue

        if kind == "doc" and path.name == "README.md" and seed_scope:
            seeded, reason = seed_scope_purpose(root, text, dry_run=dry_run)
            result.scope_seeded, result.scope_reason = seeded, reason

        entry = build_entry(path, kind, root, text)
        if (entry["source"]["file"], entry["source"]["digest"]) in known:
            result.unchanged += 1
            continue

        try:
            memory_store.validate_entry(entry, now=now)
        except ValueError as exc:
            result.rejected.append((entry["source"]["file"], str(exc)))
            continue
        candidates.append(entry)

    if candidates and not dry_run:
        memory_store.append_entries(candidates, directory=directory, now=now)
    result.proposed = candidates
    return result
