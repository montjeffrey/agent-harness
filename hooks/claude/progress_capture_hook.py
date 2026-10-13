"""Stop / SessionEnd hook: propose memory entries from work actually done.

Until this hook existed nothing wrote progress: commits landed, tasks were
finished, and memory learned about it only if an agent volunteered an entry.
This hook closes that gap from evidence rather than from narration. Every
run it looks at two things and proposes an entry for each that is new:

- **Commits** reachable from HEAD but not from the session's marker -- the
  HEAD recorded when the session started (by the SessionStart hook) or by
  the previous scan. One entry per commit: subject, branch, files, and the
  SHA as `source.commit`, so review can check the claim against git.
- **Finished tasks** owned by this session (`work_finish` recorded their
  branch, base, and final SHA): one entry per task with the commit count,
  the `base..final` range, the claimed paths, and up to five subjects.

Entries are written exactly the way `memory_remember` writes them:
`status: proposed`, `confidence: inferred`, project scope. Nothing here can
make itself authoritative; a human accepts or rejects in review. When
nothing is new the hook writes nothing at all.

Why `Stop` (and `SessionEnd` as a final sweep), not `PostToolUse`:

- `Stop` fires once per assistant turn, so the frequency is bounded by the
  conversation, not by tool calls, and the common case -- HEAD unchanged,
  no task finished -- costs one repository resolution (four `rev-parse`-class
  git calls, no `status`) and two small JSON reads.
- It fires after *every* turn, so progress survives a session that is later
  killed by a quota limit or a crash, which `SessionEnd` alone would miss
  (that is exactly how the previous attempt at this hook was lost).
- `PostToolUse` would need a matcher on `Bash` and on the MCP finish tool,
  would run on every shell command, and would still miss commits made by
  the user between turns. `SessionEnd` catches those last.
- A `Stop` hook can block the assistant from stopping by exiting 2. This one
  never does: every path exits 0, output goes nowhere, and any error inside
  the harness is swallowed. Capturing progress must never hold a session
  hostage.

No model, no socket, no index build. The only writes are one append to the
project's `inbox.jsonl` and the session marker under `~/.agent-harness/work/`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMMIT_LIMIT = 20
SUBJECTS_IN_TASK_ENTRY = 5
FILES_IN_COMMIT_ENTRY = 8
PROGRESS_TAG = "progress"


def _import_harness():
    # The harness beside this file (<root>/hooks/claude/ -> <root>), never a
    # stale install found first on sys.path; see memory_context_hook._harness.
    root = str(Path(__file__).resolve().parents[2])
    if sys.path[:1] != [root]:
        sys.path.insert(0, root)
    from harness import memory_store, work

    return memory_store, work


@dataclass
class Scan:
    """What one run found and wrote, for tests and for the docstring's claims."""

    proposed: list[dict] = field(default_factory=list)
    head: str | None = None
    skipped: str | None = None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _short(sha: str | None) -> str:
    return (sha or "")[:10]


def commit_entry(commit, branch: str | None, project_id: str, task_id: str | None = None) -> dict:
    """One proposed entry for one commit, bounded to the summary limit."""
    files = list(commit.files)
    shown = ", ".join(files[:FILES_IN_COMMIT_ENTRY])
    if len(files) > FILES_IN_COMMIT_ENTRY:
        shown += f", +{len(files) - FILES_IN_COMMIT_ENTRY} more"
    where = f" on {branch}" if branch else ""
    task = f" (task {task_id})" if task_id else ""
    # The subject is clipped on its own so a long one cannot push the file
    # list -- the part review actually checks against git -- off the end.
    summary = f"Committed {_short(commit.sha)}{where}{task}: {_clip(commit.subject, 200)}"
    if shown:
        summary += f" -- files: {shown}"
    tags = [PROGRESS_TAG, "commit"]
    if task_id:
        tags.append(task_id)
    return {
        "type": "discovery",
        "scope": "project",
        "status": "proposed",
        "confidence": "inferred",
        "summary": _clip(summary, 500),
        "source": {"project": project_id, "commit": commit.sha},
        "tags": tags,
    }


def task_entry(task, count: int | None, subjects: list[str], project_id: str) -> dict:
    """One proposed entry for one finished task, with its git evidence."""
    span = f"{_short(task.base_sha)}..{_short(task.final_sha)}" if task.final_sha else f"from {_short(task.base_sha)}"
    commits = f"{count} commit(s)" if count is not None else "commit count unknown"
    summary = (
        f"Finished task {task.id}: {task.summary} -- branch {task.branch}, "
        f"{commits} {span}, paths: {', '.join(task.paths)}"
    )
    if subjects:
        summary += " -- " + "; ".join(subjects[:SUBJECTS_IN_TASK_ENTRY])
    source: dict[str, Any] = {"project": project_id}
    if task.final_sha:
        source["commit"] = task.final_sha
    return {
        "type": "discovery",
        "scope": "project",
        "status": "proposed",
        "confidence": "inferred",
        "summary": _clip(summary, 500),
        "source": source,
        "tags": [PROGRESS_TAG, "task-finished", task.id],
    }


def _already_recorded(memory_store, memory_dir: Path) -> tuple[set[str], set[str]]:
    """Commit SHAs and task ids already proposed by this hook.

    The marker is the primary dedup; this is the backstop for a lost or
    hand-deleted marker, so a re-scan can never double-propose.
    """
    commits: set[str] = set()
    tasks: set[str] = set()
    for entry in memory_store.iter_entries(memory_dir):
        tags = entry.get("tags") or []
        if PROGRESS_TAG not in tags:
            continue
        source = entry.get("source") or {}
        if "task-finished" in tags:
            tasks.update(t for t in tags if t.startswith("work-"))
        elif source.get("commit"):
            commits.add(str(source["commit"]))
    return commits, tasks


def scan(project_root: Path, session_id: str, now=None) -> Scan:
    """Propose entries for everything new since the last scan. Never raises."""
    result = Scan()
    try:
        _scan(project_root, session_id, result, now)
    except Exception as exc:  # noqa: BLE001 -- see the module docstring: never block session end
        result.skipped = f"{type(exc).__name__}: {exc}"
    return result


def _scan(project_root: Path, session_id: str, result: Scan, now) -> None:
    memory_store, work = _import_harness()
    if not session_id.strip():
        result.skipped = "no session id"
        return
    memory_dir = memory_store.project_memory_dir(project_root)
    if not memory_dir.is_dir():
        result.skipped = "project is not enrolled"
        return

    repo = work.repository_for(project_root)
    result.head = repo.head
    project_id = memory_store.project_id_for(project_root)
    marker = work.load_session_marker(repo, session_id) or {}
    reported_tasks = {str(t) for t in marker.get("reported_tasks", []) if t}
    base = str(marker.get("head") or "")

    # Finished tasks first: their evidence is complete and self-contained.
    own_tasks = [t for t in work.recorded_tasks(repo) if t.session_id == session_id.strip()]
    done_tasks = [t for t in own_tasks if t.status == work.DONE and t.id not in reported_tasks]
    seen_commits, seen_tasks = _already_recorded(memory_store, memory_dir) if (done_tasks or base) else (set(), set())

    entries: list[dict] = []
    task_commit_shas: set[str] = set()
    for task in done_tasks:
        reported_tasks.add(task.id)
        if task.id in seen_tasks:
            continue
        count = work.commit_count(repo.root, task.base_sha, task.final_sha) if task.final_sha else None
        subjects: list[str] = []
        if task.final_sha:
            try:
                log = work.commit_log(repo.root, task.base_sha, task.final_sha, limit=COMMIT_LIMIT)
            except work.WorkError:
                log = []
            subjects = [c.subject for c in log]
            task_commit_shas.update(c.sha for c in log)
        entries.append(task_entry(task, count, subjects, project_id))

    # Then commits since the marker. No marker means no base: record HEAD
    # now so the next scan has one, and propose nothing for the past.
    if base and base != repo.head:
        task_id = {t.branch: t.id for t in own_tasks}.get(repo.current_branch or "")
        try:
            commits = work.commit_log(repo.root, base, repo.head, limit=COMMIT_LIMIT)
        except work.WorkError:
            commits = []  # the base is gone (rewritten history); start over from HEAD
        for commit in commits:
            if commit.sha in seen_commits or commit.sha in task_commit_shas:
                continue
            entries.append(commit_entry(commit, repo.current_branch, project_id, task_id))

    if entries:
        memory_store.append_entries(entries, directory=memory_dir, now=now)
        result.proposed = entries

    work.save_session_marker(
        repo,
        session_id,
        {
            **marker,
            "head": repo.head,
            "branch": repo.current_branch,
            "reported_tasks": sorted(reported_tasks),
            "last_scan": memory_store.format_ts(now or datetime.now(timezone.utc)),
        },
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        session_id = payload.get("session_id")
        if not isinstance(session_id, str):
            return 0
        cwd = payload.get("cwd")
        root = Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()
        scan(root, session_id)
    except Exception:  # noqa: BLE001 -- never block the session from stopping
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
