"""Keep the memory store maintained without anyone remembering to.

Three things decay in a memory store that is only ever added to: the inbox
grows (compaction fixes that), the semantic index falls behind the entries
it is supposed to rank (rebuilding fixes that), and the proposed backlog
piles up (triage fixes that). None of them fixes itself, and on a machine
that runs unattended agents nobody is watching for the moment they need
fixing. This module answers "what is owed right now" cheaply enough to ask
on every `status`, and runs the one part that is safe to run unattended.

**What runs and what only reports.** `maintain` runs compaction when it is
due -- compaction is deterministic, idempotent, archives before it removes,
and never discards an unreviewed proposal, so an unattended run can do no
harm a human would have to undo. Everything else is *reported* with the
exact command to run:

* the semantic index is never built here. `index` stays the one command
  that touches chromadb; this module only reads the build manifest it
  leaves behind, so `maintain` and `status` stay stdlib-only and can never
  trigger a model download.
* the review backlog is never touched here. Accepting is a human act
  (`triage`), and the count is surfaced so the human knows it is waiting.

**The index manifest** is the key to reporting drift without opening the
index. `build_indexes` writes ``build.json`` beside each Chroma store with
the content hash of every key it embedded. Comparing that to the live
store answers "how far behind is the index" with a dict lookup:
entries added, removed, or reworded since the build. Removed ones matter
as much as added -- `memory_index.build_index` drops departed ids only
when it is re-run, which is why compaction already reports that a removal
leaves the index serving archived entries.

The drift threshold (`AGENT_HARNESS_INDEX_DRIFT_ENTRIES`) follows the same
rule as `AGENT_HARNESS_ADAPTIVE_MIN_ENTRIES`: unset or unparseable means the
default, and it is read at call time so a hook can set it per session.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness import memory_compact, memory_index, memory_store

MANIFEST_NAME = "build.json"

# Changed entries (added + removed + reworded) since the last build before
# the index counts as stale. Ten is roughly one working session of capture:
# below that the missing entries still reach the packet -- the selector
# appends anything the index did not return, in deterministic order -- so
# the cost of waiting is ranking, not recall.
DRIFT_THRESHOLD = 10
DRIFT_THRESHOLD_ENV = "AGENT_HARNESS_INDEX_DRIFT_ENTRIES"

INDEX_COMMAND = "agent-harness-memory index"
TRIAGE_COMMAND = "agent-harness-memory triage"
MAINTAIN_COMMAND = "agent-harness-memory maintain"


def drift_threshold() -> int:
    """Changed entries since the last build that make an index stale."""
    try:
        return max(0, int(os.environ[DRIFT_THRESHOLD_ENV]))
    except (KeyError, ValueError):
        return DRIFT_THRESHOLD


# --- manifest ------------------------------------------------------------


def manifest_path(index_dir: Path) -> Path:
    return Path(index_dir) / MANIFEST_NAME


def write_manifest(index_dir: Path, entries: Sequence[dict], now: datetime | None = None) -> Path:
    """Record what `build_index` was just given, keyed the way the index is.

    Only entries the index would actually hold are recorded (an id and a
    non-empty document), so the manifest never reports drift for a record
    the index skips by design.
    """
    now = now or datetime.now(timezone.utc)
    hashes = {
        memory_index.index_key(e): memory_index.entry_content_hash(e)
        for e in entries
        if e.get("id") and memory_index.entry_document(e).strip()
    }
    path = manifest_path(index_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {"schema": 1, "built_at": memory_store.format_ts(now), "entries": hashes},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def read_manifest(index_dir: Path) -> tuple[str, dict[str, str]] | None:
    """(built_at, key -> content hash) from the manifest, or None if absent
    or unreadable. Unreadable counts as absent: a corrupt manifest must
    read as "unknown, rebuild" rather than wedge the report."""
    path = manifest_path(index_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return None
    entries = {str(k): str(v) for k, v in data["entries"].items()}
    return str(data.get("built_at") or ""), entries


# --- drift ---------------------------------------------------------------


@dataclass(frozen=True)
class IndexDrift:
    """How far one scope's index lags its store. All pure arithmetic."""

    scope: str
    built_at: str | None
    indexed: int
    added: int
    removed: int
    changed: int
    store_size: int

    @property
    def exists(self) -> bool:
        """Whether there is a build to compare against at all."""
        return self.built_at is not None

    @property
    def pending(self) -> int:
        return self.added + self.removed + self.changed

    @property
    def stale(self) -> bool:
        """Owed a rebuild: never built while the store has entries, or
        drifted past the threshold. Threshold 0 means any change counts."""
        if not self.exists:
            return self.store_size > 0
        return self.pending > 0 and self.pending >= drift_threshold()

    def describe(self) -> str:
        if not self.exists:
            if self.store_size == 0:
                return f"{self.scope}: nothing to index"
            return f"{self.scope}: no semantic index ({self.store_size} entries unranked)"
        parts = []
        if self.added:
            parts.append(f"{self.added} new")
        if self.removed:
            parts.append(f"{self.removed} removed")
        if self.changed:
            parts.append(f"{self.changed} reworded")
        if not parts:
            return f"{self.scope}: {self.indexed} indexed at {self.built_at}; up to date"
        return (
            f"{self.scope}: {self.indexed} indexed at {self.built_at}; "
            f"{', '.join(parts)} since (threshold {drift_threshold()})"
        )


def index_drift(scope: str, entries: Sequence[dict], index_dir: Path) -> IndexDrift:
    """Compare the live `entries` of one scope with that scope's manifest."""
    live = {
        memory_index.index_key(e): memory_index.entry_content_hash(e)
        for e in entries
        if e.get("id") and memory_index.entry_document(e).strip()
    }
    manifest = read_manifest(index_dir)
    if manifest is None:
        return IndexDrift(scope, None, 0, 0, 0, 0, len(live))
    built_at, indexed = manifest
    added = sum(1 for key in live if key not in indexed)
    removed = sum(1 for key in indexed if key not in live)
    changed = sum(1 for key, digest in live.items() if key in indexed and indexed[key] != digest)
    return IndexDrift(scope, built_at, len(indexed), added, removed, changed, len(live))


def project_drift(project_root: Path) -> dict[str, IndexDrift]:
    """Drift for both scopes a project's context packet draws on.

    Reads the two inboxes and two manifests; never imports chromadb.
    """
    root = Path(project_root)
    project_id = memory_store.project_id_for(root)
    project_entries = list(memory_store.iter_entries(memory_store.project_memory_dir(root)))
    machine_entries = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    return {
        "project": index_drift("project", project_entries, memory_index.index_dir_for(project_id)),
        "machine": index_drift("machine", machine_entries, memory_index.machine_index_dir()),
    }


def any_stale(drift: Mapping[str, IndexDrift]) -> bool:
    return any(d.stale for d in drift.values())


# --- building ------------------------------------------------------------


@dataclass(frozen=True)
class BuildReport:
    """One scope's build: how many entries went in and what the index did."""

    scope: str
    index_dir: Path
    count: int
    stats: memory_index.IndexStats


def build_indexes(
    project_root: Path,
    client_factory: memory_index.ClientFactory | None = None,
    now: datetime | None = None,
) -> list[BuildReport]:
    """Build the project and machine indexes and record their manifests.

    This is the single path `index` and `enroll` share, so a build always
    leaves a manifest behind and drift can always be reported afterwards.
    Scopes with no entries are skipped, not emptied. The caller is expected
    to have checked `memory_index.chromadb_available()`; this function
    touches chromadb through `build_index` and must never be reached from a
    retrieval hot path.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    project_id = memory_store.project_id_for(root)
    reports: list[BuildReport] = []
    for scope, entries, index_dir in (
        ("project",
         list(memory_store.iter_entries(memory_store.project_memory_dir(root))),
         memory_index.index_dir_for(project_id)),
        ("machine",
         list(memory_store.iter_entries(memory_store.machine_memory_dir())),
         memory_index.machine_index_dir()),
    ):
        if not entries:
            continue
        stats = memory_index.build_index(entries, index_dir, client_factory=client_factory)
        write_manifest(index_dir, entries, now=now)
        reports.append(BuildReport(scope, index_dir, len(entries), stats))
    return reports


# --- the maintenance pass ------------------------------------------------


@dataclass(frozen=True)
class MaintenanceResult:
    """What one `maintain` call found and did."""

    due: memory_compact.DueStatus
    ran: bool
    compact: memory_compact.CompactResult | None
    drift: dict[str, IndexDrift]
    backlog: int
    forced: bool = False

    @property
    def index_stale(self) -> bool:
        """The index is owed a rebuild, by drift or because compaction just
        removed entries it still serves."""
        if self.compact is not None and self.compact.index_stale:
            return True
        return any_stale(self.drift)

    def advice(self) -> list[str]:
        """Exact commands for what this pass could not do itself."""
        lines: list[str] = []
        if self.index_stale:
            lines.append(f"run `{INDEX_COMMAND}` to bring the semantic index up to date")
        if self.backlog:
            lines.append(f"run `{TRIAGE_COMMAND}` to review {self.backlog} proposed entries")
        return lines


def maintain(
    project_root: Path,
    now: datetime | None = None,
    force: bool = False,
    check: bool = False,
) -> MaintenanceResult:
    """Answer "am I due?" and, unless `check`, compact if so.

    Idempotent: compaction stamps `active.md`, so a second call inside the
    interval reports "not due" and writes nothing. `force` runs compaction
    regardless of the stamp; `check` never writes. Drift and backlog are
    computed *after* compaction so the advice reflects the store as it now
    is. Raises `CompactLockError` like `compact` does.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    directory = memory_store.project_memory_dir(root)

    due = memory_compact.compaction_due(directory, now=now)
    result: memory_compact.CompactResult | None = None
    ran = False
    if not check and (due.due or force):
        result = memory_compact.compact(root, now=now)
        ran = True

    drift = project_drift(root)
    backlog = sum(1 for _ in memory_store.iter_entries(directory, status="proposed"))
    return MaintenanceResult(
        due=due, ran=ran, compact=result, drift=drift, backlog=backlog, forced=force
    )


def status_lines(project_root: Path, now: datetime | None = None) -> list[str]:
    """The maintenance summary `status` prints: due-ness, drift, backlog.

    Read-only and stdlib-only, so `status` stays cheap enough to run from
    anywhere -- including the SessionStart hook's neighbourhood.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    directory = memory_store.project_memory_dir(root)
    due = memory_compact.compaction_due(directory, now=now)
    drift = project_drift(root)
    backlog = sum(1 for _ in memory_store.iter_entries(directory, status="proposed"))

    lines = []
    marker = "DUE" if due.due else "ok"
    lines.append(f"  compaction: {marker} -- {due.reason}")
    if due.due:
        lines.append(f"              run `{MAINTAIN_COMMAND}`")
    for scope in ("project", "machine"):
        report = drift[scope]
        marker = "STALE" if report.stale else "ok"
        lines.append(f"  index:      {marker} -- {report.describe()}")
    if any_stale(drift):
        lines.append(f"              run `{INDEX_COMMAND}`")
    if backlog:
        lines.append(f"  backlog:    {backlog} proposed awaiting human review")
        lines.append(f"              run `{TRIAGE_COMMAND}`")
    else:
        lines.append("  backlog:    nothing awaiting review")
    return lines


def _describe_compact(result: memory_compact.CompactResult) -> list[str]:
    lines = [
        f"    archived: {result.archived} (of which duplicates: {result.duplicates})",
        f"    promoted: {result.promoted}",
        f"    active:   {result.carried} carried, {result.omitted} omitted",
    ]
    if result.archived:
        lines.append(f"    archive:  {result.archive_path}")
    return lines


def render(result: MaintenanceResult, project_root: Path) -> str:
    """Human-readable report for the `maintain` command."""
    lines = [f"maintenance: {Path(project_root)}"]
    verdict = "due" if result.due.due else "not due"
    if result.forced and not result.due.due:
        verdict = "not due, forced"
    lines.append(f"  compaction: {verdict} -- {result.due.reason}")
    if result.ran and result.compact is not None:
        lines.append(f"  compacted ({result.compact.week})")
        lines.extend(_describe_compact(result.compact))
        if not result.compact.changed:
            lines.append("    nothing to remove or promote; the stamp was refreshed")
    for scope in ("project", "machine"):
        report = result.drift[scope]
        lines.append(f"  index:      {'STALE' if report.stale else 'ok'} -- {report.describe()}")
    lines.append(
        f"  backlog:    {result.backlog} proposed awaiting human review"
        if result.backlog else "  backlog:    nothing awaiting review"
    )
    advice = result.advice()
    if advice:
        lines.append("")
        lines.append("next:")
        lines.extend(f"  {line}" for line in advice)
    return "\n".join(lines)
