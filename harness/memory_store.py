"""Portable hierarchical memory: storage model and entry contract.

Markdown is the human-readable source of truth for accepted memory; this
module owns the compact append-oriented JSONL side (the proposal inbox) plus
the machine-scope project registry. Nothing here opens a network socket, and
nothing is ever summarized by an AI model in this layer.

Two storage scopes (see the design spec under
``docs/superpowers/specs/2026-07-22-portable-hierarchical-memory-design.md``):

    <project>/.agent-harness/        project-owned, committed with the repo
      scope.md                       purpose, boundaries, acceptance criteria
      memory/active.md               bounded weekly working context
      memory/durable.md              stable project knowledge
      memory/inbox.jsonl             proposed entries awaiting review
      memory/archive/YYYY-Www.md     immutable weekly records

    ~/.agent-harness/memory/         machine-owned, local, never committed
      registry.json                  stable project ids -> locations
      durable.md                     reusable cross-project knowledge
      inbox.jsonl                    machine-scope proposals

Entry contract (schema 1) -- one JSON object per line:

    schema      int, required, always ``1`` for this phase
    id          str, required, ``mem-YYYY-MM-DD-NNN``, unique per store
    ts          str, required, ISO 8601 UTC with a ``Z`` suffix
    type        str, required, one of ENTRY_TYPES
    scope       str, required, one of SCOPES
    status      str, required, one of STATUSES (default "proposed")
    confidence  str, required, one of CONFIDENCES (default "inferred")
    summary     str, required, one concise durable statement
    source      dict, optional -- {project, commit, file, manual}
    tags        list[str], optional

Trust rules enforced here rather than at the CLI, so every writer gets them:
an ``inferred`` entry can never be ``accepted`` (review is what promotes it),
and an ``accepted`` entry must carry a source or an explicit manual
confirmation. Unknown extra fields are preserved for forward compatibility;
readers skip records whose ``schema`` they do not understand and skip
malformed lines outright rather than raising.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

ENTRY_TYPES: tuple[str, ...] = (
    "decision",
    "discovery",
    "task",
    "blocker",
    "lesson",
    "relationship",
)

SCOPES: tuple[str, ...] = ("project", "machine")

STATUSES: tuple[str, ...] = ("proposed", "accepted", "superseded", "resolved")

CONFIDENCES: tuple[str, ...] = ("confirmed", "inferred")

MAX_SUMMARY_CHARS = 500

_ID_RE = re.compile(r"^mem-(\d{4}-\d{2}-\d{2})-(\d{3,})$")

# Patterns that indicate a probable credential. Deliberately shaped to match
# *assignments* and known key prefixes rather than the mere mention of a word
# like "password" -- ordinary prose about rotating an API key must survive.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"[\w-]*(?:password|passwd|secret|token|api[ _-]?key|access[ _-]?key"
        r"|private[ _-]?key)[\w-]*\s*[=:]\s*\S{6,}",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class InitResult:
    """Outcome of `init_project`: the stable id plus what was newly created."""

    project_id: str
    root: Path
    created: tuple[str, ...]


def machine_memory_dir() -> Path:
    """Machine-scope memory root, created lazily on first write."""
    return Path.home() / ".agent-harness" / "memory"


def project_memory_dir(project_root: Path) -> Path:
    """Project-scope memory root for `project_root`."""
    return Path(project_root) / ".agent-harness" / "memory"


def format_ts(dt: datetime) -> str:
    """Render a datetime as ISO 8601 UTC with a literal ``Z`` suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _reject_secrets(record: dict) -> None:
    """Raise if any string anywhere in the record looks like a credential."""

    def walk(node: object) -> None:
        if isinstance(node, str):
            if _find_secret(node):
                raise ValueError(
                    "it appears to contain a secret; memory stores facts "
                    "about work, never credential material"
                )
        elif isinstance(node, dict):
            for key, val in node.items():
                walk(key)
                walk(val)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(record)


def _has_source(source: dict | None) -> bool:
    """Whether `source` carries real evidence for an accepted entry.

    `project` deliberately does not count: it is attribution (which project
    the entry came from) and is attached automatically to every project-scope
    entry, so honouring it would make the accepted-needs-evidence rule
    unenforceable. Only a commit, a file, or an explicit human attestation
    counts as evidence.
    """
    if not source:
        return False
    if source.get("manual") is True:
        return True
    return any(source.get(key) for key in ("commit", "file"))


def validate_entry(entry: dict, now: datetime | None = None) -> dict:
    """Validate one entry against the schema-1 contract and return a
    normalized copy (never mutates the input).

    Fills in `schema`, `ts`, `status` ("proposed") and `confidence`
    ("inferred") when absent. Raises ValueError for an unknown enum value, a
    missing/blank/oversized summary, a probable secret, or a violation of the
    inferred/accepted trust rules. `id` is left to `append_entries`, which
    allocates it against the destination store.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"entry must be a dict, got {type(entry).__name__}")

    rec = dict(entry)
    now = now or datetime.now(timezone.utc)

    schema = rec.setdefault("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version: {schema!r}")

    entry_type = rec.get("type")
    if entry_type not in ENTRY_TYPES:
        raise ValueError(
            f"unknown or missing type: {entry_type!r} (expected one of {ENTRY_TYPES})"
        )

    scope = rec.get("scope")
    if scope not in SCOPES:
        raise ValueError(f"unknown or missing scope: {scope!r} (expected one of {SCOPES})")

    status = rec.setdefault("status", "proposed")
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r} (expected one of {STATUSES})")

    confidence = rec.setdefault("confidence", "inferred")
    if confidence not in CONFIDENCES:
        raise ValueError(
            f"unknown confidence: {confidence!r} (expected one of {CONFIDENCES})"
        )

    summary = rec.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary is required and must be a non-empty string")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ValueError(
            f"summary exceeds the {MAX_SUMMARY_CHARS}-character limit "
            f"({len(summary)}); record the detail in durable.md instead"
        )

    tags = rec.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError(f"tags must be a list of strings, got {tags!r}")

    source = rec.get("source")
    if source is not None and not isinstance(source, dict):
        raise ValueError(f"source must be a dict, got {type(source).__name__}")

    # Trust rules. Order matters: an inferred entry is refused before we ever
    # ask whether it has a source, so the diagnostic names the real problem.
    if status == "accepted":
        if confidence == "inferred":
            raise ValueError(
                "an inferred entry cannot be accepted directly; it stays "
                "proposed until review confirms it"
            )
        if not _has_source(source):
            raise ValueError(
                "an accepted entry requires a source (commit/file/project) or "
                "an explicit manual confirmation"
            )

    ts = rec.get("ts")
    if not ts:
        rec["ts"] = format_ts(now)
    elif not isinstance(ts, str):
        raise ValueError(f"ts must be a string, got {type(ts).__name__}")

    _reject_secrets(rec)
    return rec


def next_entry_id(directory: Path, now: datetime | None = None) -> str:
    """Return the next unused ``mem-YYYY-MM-DD-NNN`` id for `directory`.

    Scans existing entries for the same date and continues past the highest
    sequence number found, so ids stay unique within a store even when the
    inbox has been hand-edited.
    """
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    highest = 0
    for rec in iter_entries(directory):
        match = _ID_RE.match(str(rec.get("id", "")))
        if match and match.group(1) == day:
            highest = max(highest, int(match.group(2)))
    return f"mem-{day}-{highest + 1:03d}"


def append_entries(
    entries: Iterable[dict],
    directory: Path,
    now: datetime | None = None,
) -> int:
    """Validate and append `entries` as JSONL to ``<directory>/inbox.jsonl``.

    All entries are validated before anything is written -- a single invalid
    entry raises ValueError and nothing is appended, not even the directory.
    Entries without an `id` are assigned consecutive ids. Returns the number
    of entries written.
    """
    now = now or datetime.now(timezone.utc)
    normalized = [validate_entry(e, now=now) for e in entries]
    if not normalized:
        return 0

    day = now.strftime("%Y-%m-%d")
    seed = _ID_RE.match(next_entry_id(directory, now=now))
    counter = int(seed.group(2)) if seed else 1
    for rec in normalized:
        if not rec.get("id"):
            rec["id"] = f"mem-{day}-{counter:03d}"
            counter += 1

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "inbox.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for rec in normalized:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")
    return len(normalized)


def iter_entries(
    directory: Path,
    type: str | None = None,
    status: str | None = None,
    scope: str | None = None,
) -> Iterator[dict]:
    """Yield parsed, schema-valid entries from ``<directory>/inbox.jsonl`` in
    on-disk line order.

    Malformed lines (bad JSON, wrong type, unknown schema version) are
    silently skipped -- never raised -- so one corrupt line never blocks
    reading the rest of the store.
    """
    path = Path(directory) / "inbox.jsonl"
    if not path.is_file():
        return

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("schema") != SCHEMA_VERSION:
            continue
        if type is not None and rec.get("type") != type:
            continue
        if status is not None and rec.get("status") != status:
            continue
        if scope is not None and rec.get("scope") != scope:
            continue
        yield rec


# --- machine registry -----------------------------------------------------


def _registry_path() -> Path:
    return machine_memory_dir() / "registry.json"


def _restrict_permissions(path: Path) -> None:
    """Best-effort user-only permissions; a no-op where unsupported."""
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass


def load_registry() -> dict:
    """Return the machine project registry, or an empty one.

    A missing, unreadable, or corrupt registry yields an empty registry
    rather than raising -- memory must degrade to reduced functionality, not
    data loss.
    """
    path = _registry_path()
    empty: dict = {"schema": SCHEMA_VERSION, "projects": {}}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        return empty
    return data


def save_registry(registry: dict) -> None:
    """Write the registry atomically (temp file + replace)."""
    directory = machine_memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(directory)
    path = _registry_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def project_id_for(project_root: Path) -> str:
    """Return a stable, readable id for `project_root`.

    Derived from the directory name plus a short digest of the resolved path,
    so it survives repeated init and distinguishes same-named projects in
    different locations.
    """
    resolved = Path(project_root).resolve()
    slug = re.sub(r"[^a-z0-9]+", "-", resolved.name.lower()).strip("-") or "project"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


# --- project initialization ----------------------------------------------

_SCOPE_TEMPLATE = """# Project scope

> Read by `agent-harness-memory` to detect scope drift. Keep it short and
> concrete; every line here is something an agent will be held to.

## Purpose

_What this project is for._

## Boundaries

_What is in scope._

## Exclusions

_What is explicitly out of scope._

## Acceptance criteria

_How we know a change is done and safe._

## Active objective

_The one thing currently being worked on._
"""

_ACTIVE_TEMPLATE = """# Active memory

> Bounded working context, rewritten by `agent-harness-memory compact`.
> Hand edits are preserved until the next compaction.

## Current phase

_Not set yet._

## Open work

_None recorded._

## Blockers

_None recorded._

## Recent accepted decisions

_None yet._
"""

_DURABLE_TEMPLATE = """# Durable project memory

> Stable knowledge promoted from weekly compaction: architecture,
> conventions, confirmed decisions, milestones, recurring lessons, and
> unresolved risks. Entries are superseded, never silently rewritten.
"""


def init_project(project_root: Path, now: datetime | None = None) -> InitResult:
    """Create the project memory layout and register the project.

    Idempotent: existing files are never overwritten, so re-running after
    hand-editing `scope.md` preserves the edits. Returns the stable project
    id and the list of paths newly created.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    base = root / ".agent-harness"
    memory = base / "memory"
    archive = memory / "archive"

    created: list[str] = []
    for directory in (base, memory, archive):
        if not directory.exists():
            created.append(directory.relative_to(root).as_posix())
        directory.mkdir(parents=True, exist_ok=True)

    for path, template in (
        (base / "scope.md", _SCOPE_TEMPLATE),
        (memory / "active.md", _ACTIVE_TEMPLATE),
        (memory / "durable.md", _DURABLE_TEMPLATE),
    ):
        if not path.exists():
            path.write_text(template, encoding="utf-8")
            created.append(path.relative_to(root).as_posix())

    project_id = project_id_for(root)
    registry = load_registry()
    stamp = format_ts(now)
    existing = registry["projects"].get(project_id, {})
    registry["projects"][project_id] = {
        "path": str(Path(root).resolve()),
        "name": Path(root).resolve().name,
        "first_seen": existing.get("first_seen", stamp),
        "last_seen": stamp,
    }
    save_registry(registry)

    return InitResult(project_id=project_id, root=root, created=tuple(created))
