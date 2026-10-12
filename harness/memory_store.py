"""Portable hierarchical memory: storage model and entry contract.

Markdown is the human-readable source of truth for accepted memory; this
module owns the compact append-oriented JSONL side (the proposal inbox) plus
the machine-scope project registry. Nothing here opens a network socket, and
nothing is ever summarized by an AI model in this layer.

Two storage scopes (see the design spec under
``docs/superpowers/specs/2026-07-22-portable-hierarchical-memory-design.md``):

    <project>/.agent-harness/        project-owned, machine-local, never in git
      scope.md                       purpose, boundaries, acceptance criteria
      memory/active.md               bounded weekly working context
      memory/durable.md              stable project knowledge
      memory/inbox.jsonl             proposed entries awaiting review
      memory/archive/YYYY-Www.md     immutable weekly records

    ~/.agent-harness/memory/         machine-owned, local, never committed
      registry.json                  stable project ids -> locations
      durable.md                     reusable cross-project knowledge
      inbox.jsonl                    machine-scope proposals

Project storage sits beside the code but is excluded from git through the
repository's private ``.git/info/exclude`` (ADR 0004), so memory never shows
up in anyone's status, diff, or commit -- not even as a ``.gitignore`` change.
The machine registry, not git, is what links projects to each other.

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
import subprocess
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

STATUSES: tuple[str, ...] = (
    "proposed",
    "accepted",
    "rejected",
    "superseded",
    "resolved",
)

# Statuses that no longer represent live knowledge. Context selection skips
# these outright, and review refuses to act on an entry already in one.
CLOSED_STATUSES: frozenset[str] = frozenset({"rejected", "superseded", "resolved"})

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
class ExcludeResult:
    """Outcome of `ensure_git_exclude` (or `ensure_graph_git_exclude`) for
    one repository.

    `status` is one of:

        not-git   the project is not inside a git repository; nothing done
        tracked   git already tracks files there; nothing done (graph only)
        present   the exclusion was already there; nothing written
        added     the exclusion line was appended
        failed    the exclude file could not be read or written (see `note`)

    `path` is the exclude file consulted, or None for ``not-git`` and
    ``tracked``.
    """

    status: str
    path: Path | None = None
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.status == "added"


@dataclass(frozen=True)
class InitResult:
    """Outcome of `init_project`: the stable id plus what was newly created.

    `exclude` reports what happened to the repository's git exclusion; it is
    informational, never a failure -- see `ensure_git_exclude`.
    """

    project_id: str
    root: Path
    created: tuple[str, ...]
    exclude: ExcludeResult = ExcludeResult("not-git")


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


def read_lines(directory: Path) -> list[str]:
    """Every non-blank raw line of ``<directory>/inbox.jsonl``, verbatim.

    Deliberately does no parsing: a caller that intends to rewrite the file
    needs to see the lines `iter_entries` filters out (malformed JSON, a
    future schema version) so it can carry them across untouched instead of
    silently deleting them. An unreadable or missing file yields ``[]``.
    """
    path = Path(directory) / "inbox.jsonl"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def write_lines(lines: Iterable[str], directory: Path) -> int:
    """Atomically replace ``<directory>/inbox.jsonl`` with `lines`.

    Written to a temp sibling and `replace`d in one step -- the same
    temp-then-replace shape `save_registry` uses. A concurrent reader
    therefore sees either the entire old file or the entire new one, never a
    half-rewritten inbox, and a crash mid-write leaves the original intact.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "inbox.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    written = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line)
            fh.write("\n")
            written += 1
    tmp.replace(path)
    return written


def rewrite_entries(
    entries: Iterable[dict],
    directory: Path,
    now: datetime | None = None,
) -> int:
    """Validate `entries` and atomically replace the inbox with them.

    The append path (`append_entries`) is the normal way in; this is the
    narrow exception review needs, because a status transition edits a record
    that is already on disk. Every entry is validated *before* anything is
    written, so one bad record leaves the existing file untouched.

    Unlike `append_entries` this does NOT allocate ids: a rewrite operates on
    records that already have one, and inventing ids here would silently
    renumber the store. An entry without an `id` raises ValueError.

    Note that this drops any line `iter_entries` could not parse. Callers that
    must preserve unparseable lines should splice at the line level with
    `read_lines`/`write_lines` instead.
    """
    now = now or datetime.now(timezone.utc)
    normalized = [validate_entry(e, now=now) for e in entries]
    missing = [r for r in normalized if not r.get("id")]
    if missing:
        raise ValueError(
            f"rewrite_entries requires every entry to carry an id "
            f"({len(missing)} without one); use append_entries to add new records"
        )
    return write_lines(
        (json.dumps(rec, ensure_ascii=False) for rec in normalized), directory
    )


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


def register_project(project_root: Path, now: datetime | None = None) -> str:
    """Record `project_root` in the machine registry and return its id.

    Upsert semantics: `first_seen` is kept from any existing record so the
    registry preserves when a project was first met, while `last_seen` always
    advances to `now`. The write goes through `save_registry`, so it is
    atomic.
    """
    now = now or datetime.now(timezone.utc)
    resolved = Path(project_root).resolve()
    project_id = project_id_for(resolved)
    registry = load_registry()
    stamp = format_ts(now)
    existing = registry["projects"].get(project_id, {})
    registry["projects"][project_id] = {
        **existing,
        "path": str(resolved),
        "name": resolved.name,
        "first_seen": existing.get("first_seen", stamp),
        "last_seen": stamp,
    }
    save_registry(registry)
    return project_id


def touch_project(project_root: Path, now: datetime | None = None) -> bool:
    """Refresh `last_seen` for an already-registered project.

    Returns True when the registry was updated. Unlike `register_project`
    this never adds a record: touching is a side effect of ordinary commands
    that run in whatever directory the operator happens to be in, and
    enrolling every such directory would fill the registry with noise. Only
    `init`/`enroll` decide what belongs in it.

    Registry upkeep must never be the reason a read-only command fails, so a
    store that cannot be written (read-only home, vanished directory) is
    reported as "not touched" rather than raised.
    """
    project_id = project_id_for(project_root)
    registry = load_registry()
    record = registry["projects"].get(project_id)
    if not isinstance(record, dict):
        return False
    stamp = format_ts(now or datetime.now(timezone.utc))
    if record.get("last_seen") == stamp:
        return False
    record["last_seen"] = stamp
    try:
        save_registry(registry)
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class PruneResult:
    """Outcome of `prune_registry`.

    `removed` holds `(project_id, path, reason)` triples in registry order so
    a caller can report exactly what went and why; `kept` is what survived.
    """

    removed: tuple[tuple[str, str, str], ...]
    kept: tuple[str, ...]
    dry_run: bool

    @property
    def changed(self) -> bool:
        return bool(self.removed)


def prune_registry(
    ids: Iterable[str] = (), dry_run: bool = False
) -> PruneResult:
    """Drop registry records whose path is gone, plus the named `ids`.

    A record is stale when its recorded `path` no longer resolves to an
    existing directory -- the project was deleted, renamed, or lived on a
    drive that is not mounted. Named ids are removed regardless of whether
    their path still exists, which is how an operator retires a project they
    keep on disk but no longer want memory to reason about.

    Raises `LookupError` when a named id is not in the registry: silently
    succeeding on a typo would report "nothing to do" for a project the
    operator believes they just retired.
    """
    named = list(dict.fromkeys(ids))
    registry = load_registry()
    projects = registry["projects"]

    unknown = [pid for pid in named if pid not in projects]
    if unknown:
        raise LookupError(
            "not in the registry: " + ", ".join(sorted(unknown))
        )

    removed: list[tuple[str, str, str]] = []
    kept: list[str] = []
    for project_id, record in projects.items():
        path = str(record.get("path", "")) if isinstance(record, dict) else ""
        if project_id in named:
            removed.append((project_id, path, "named"))
        elif not path or not Path(path).is_dir():
            removed.append((project_id, path, "missing"))
        else:
            kept.append(project_id)

    if removed and not dry_run:
        registry["projects"] = {
            pid: rec for pid, rec in projects.items() if pid in set(kept)
        }
        save_registry(registry)

    return PruneResult(
        removed=tuple(removed), kept=tuple(kept), dry_run=dry_run
    )


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

    project_id = register_project(root, now=now)
    exclude = ensure_git_exclude(root)

    return InitResult(
        project_id=project_id, root=root, created=tuple(created), exclude=exclude
    )


# --- git exclusion ---------------------------------------------------------

# The directory name as it appears in git pattern files. No leading slash so
# it also covers a project enrolled from a subdirectory of a larger repo, and
# the trailing slash so only a directory of that name is matched.
EXCLUDE_LINE = ".agent-harness/"

_EXCLUDE_COMMENT = (
    "# agent-harness memory is machine-local, never shared through git (ADR 0004)"
)

# The code graph's output directory (`graph_location.GRAPH_RELATIVE_PATH`'s
# parent). Rebuilt per machine, so it is kept out of git the same private way.
GRAPH_EXCLUDE_LINE = "graphify-out/"

_GRAPH_EXCLUDE_COMMENT = (
    "# agent-harness code graph is rebuilt per machine, never shared through git"
)


def _git_stdout(directory: Path, *args: str) -> str | None:
    """Run one fixed-shape git command; stdout on success, None otherwise.

    ``stdin=DEVNULL`` matters: `init_project` also runs under the stdio MCP
    server and the SessionStart hook, whose stdin is a live pipe. A git child
    that inherits it blocks until the timeout instead of returning at once.
    A missing git binary, a timeout, or a non-zero exit all read as "no
    answer" -- memory setup must never depend on git being present.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_common_dir(project_root: Path) -> Path | None:
    """The repository's common git directory for `project_root`, or None.

    Resolved through git rather than assumed to be ``<root>/.git`` because
    ``.git`` is a *file* in a linked worktree or a submodule, pointing at the
    real directory. ``info/exclude`` is read from the common directory, so a
    worktree's exclusion is written where every worktree of that repository
    will honour it. Returns None when `project_root` has no ``.git`` entry at
    all (the cheap common case) or git cannot resolve it.
    """
    root = Path(project_root)
    if not (root / ".git").exists():
        return None
    out = _git_stdout(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not out:
        return None
    return Path(out).resolve()


def _exclusion_present(text: str, line: str = EXCLUDE_LINE) -> bool:
    """Whether some non-comment line of a git pattern file already covers
    the directory `line` names, in any of the spellings git treats the same
    way (with or without leading and trailing slashes)."""
    name = line.strip().strip("/")
    for raw in text.splitlines():
        candidate = raw.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.lstrip("/").rstrip("/") == name:
            return True
    return False


def ensure_git_exclude(
    project_root: Path,
    line: str = EXCLUDE_LINE,
    comment: str = _EXCLUDE_COMMENT,
) -> ExcludeResult:
    """Make sure the repository around `project_root` ignores ``.agent-harness/``
    -- or, given `line` and `comment`, another directory the harness writes
    beside the project (enroll passes ``graphify-out/`` this way).

    The line goes into the repository's private ``.git/info/exclude`` and
    never into ``.gitignore``: the exclusion itself must not surface as a
    diff in anyone else's work, which a shared ``.gitignore`` edit would.
    Idempotent -- an existing line, however spelled, is left alone and the
    file is otherwise untouched. A project that is not in a git repository
    gets ``not-git`` and nothing on disk changes.

    Never raises. Memory storage must exist even when the exclude file cannot
    be written (read-only checkout, odd permissions); the failure is reported
    in the result for the caller to surface as a note.
    """
    git_dir = git_common_dir(project_root)
    if git_dir is None:
        return ExcludeResult("not-git")

    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if _exclusion_present(existing, line):
            return ExcludeResult("present", exclude)
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"{comment}\n{line}\n")
    except OSError as exc:
        return ExcludeResult("failed", exclude, f"could not update {exclude}: {exc}")
    return ExcludeResult("added", exclude)


def ensure_graph_git_exclude(project_root: Path) -> ExcludeResult:
    """List ``graphify-out/`` in the repository's ``.git/info/exclude``.

    The single path through which enroll and install keep the graph's build
    output out of git -- never through ``.gitignore``, for ADR 0004's reason.
    Returns ``tracked`` without writing when git already tracks anything
    under ``graphify-out/``: that project has chosen to commit its graph,
    and an exclude line would only hide newly added outputs from the people
    reviewing them. Graph health never depends on this line --
    `graph_location.git_is_dirty` ignores the directory either way.

    Never raises; see `ensure_git_exclude`.
    """
    if tracked_files(project_root, GRAPH_EXCLUDE_LINE.rstrip("/")):
        return ExcludeResult("tracked")
    return ensure_git_exclude(project_root, GRAPH_EXCLUDE_LINE, _GRAPH_EXCLUDE_COMMENT)


def tracked_memory_files(project_root: Path) -> tuple[str, ...]:
    """Paths under ``.agent-harness/`` that git currently tracks (or has
    staged), relative to `project_root`; empty when none or not a repo.

    An exclude line only keeps *untracked* files out of git. A directory
    that was committed before the exclusion existed stays tracked until
    someone runs ``git rm -r --cached .agent-harness`` -- a visible history
    change this module deliberately never performs on the caller's behalf.
    """
    return tracked_files(project_root, ".agent-harness")


def tracked_files(project_root: Path, relative_dir: str) -> tuple[str, ...]:
    """Paths under `relative_dir` (relative to `project_root`) that git
    currently tracks or has staged; empty when none or not a repo."""
    root = Path(project_root)
    if not (root / ".git").exists():
        return ()
    out = _git_stdout(root, "ls-files", "-z", "--", relative_dir)
    if not out:
        return ()
    return tuple(p for p in out.split("\0") if p)
