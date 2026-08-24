"""Local-only task workspaces for safe parallel agent implementation.

This module deliberately keeps transient implementation state outside the
repository.  A task records the exact commit it began from, the agent session
that owns it, its claimed repository-relative paths, and (when needed) an
isolated Git worktree.  Plans and state live under ``~/.agent-harness/work``;
neither enters ``git status`` or a branch diff.

The command does not rebase, merge, delete branches, or delete worktrees.
Those are integration decisions, not safe setup side effects.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from harness import memory_claims, memory_store

SCHEMA_VERSION = 1
TASK_PREFIX = "work"
ACTIVE = "active"
DONE = "done"
WORKTREE_MODES = ("auto", "always", "never")
LOCK_STALE_SECONDS = 300


class WorkError(ValueError):
    """An invalid or unsafe task-workspace transition."""


@dataclass(frozen=True)
class Repository:
    root: Path
    common_git_dir: Path
    head: str
    current_branch: str | None

    @property
    def id(self) -> str:
        # ``root`` is the current worktree and therefore differs per task.
        # The common Git directory identifies the repository shared by every
        # linked worktree, so state remains discoverable after an agent moves
        # into the external workspace returned by ``start``.
        repository_name = (
            self.common_git_dir.parent.name
            if self.common_git_dir.name.lower() == ".git"
            else self.common_git_dir.stem
        )
        slug = "".join(ch if ch.isalnum() else "-" for ch in repository_name.lower()).strip("-")
        digest = hashlib.sha256(str(self.common_git_dir).encode("utf-8")).hexdigest()[:10]
        return f"{slug or 'repository'}-{digest}"


@dataclass(frozen=True)
class Task:
    id: str
    summary: str
    agent: str
    session_id: str
    paths: tuple[str, ...]
    base_sha: str
    branch: str
    worktree: str
    plan_path: str
    status: str
    created_at: str
    finished_at: str | None = None


def _git(directory: Path, *args: str) -> str:
    """Run a fixed-shape Git command and return stdout, never through a shell."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkError(f"could not run git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise WorkError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def repository_for(directory: Path) -> Repository:
    """Resolve the Git repository and common Git directory for ``directory``."""
    start = Path(directory).expanduser().resolve()
    root = Path(_git(start, "rev-parse", "--show-toplevel")).resolve()
    common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    head = _git(root, "rev-parse", "HEAD")
    # ``symbolic-ref`` exits 1 on a detached HEAD, which is valid task input.
    try:
        branch_result = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkError(f"could not determine current branch: {exc}") from exc
    current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    return Repository(root=root, common_git_dir=common, head=head, current_branch=current_branch)


def state_dir(repo: Repository) -> Path:
    return Path.home() / ".agent-harness" / "work" / repo.id


def state_path(repo: Repository) -> Path:
    return state_dir(repo) / "tasks.json"


def _now(now: datetime | None = None) -> str:
    return memory_store.format_ts(now or datetime.now(timezone.utc))


def _load_tasks(repo: Repository) -> list[Task]:
    path = state_path(repo)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkError(f"task state is unreadable at {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
        raise WorkError(f"task state at {path} has an unsupported schema")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise WorkError(f"task state at {path} has no task list")
    try:
        return [Task(**{**item, "paths": tuple(item["paths"])}) for item in raw_tasks]
    except (KeyError, TypeError) as exc:
        raise WorkError(f"task state at {path} contains an invalid task") from exc


def _save_tasks(repo: Repository, tasks: list[Task]) -> None:
    directory = state_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = state_path(repo)
    payload = {"schema": SCHEMA_VERSION, "tasks": [asdict(task) for task in tasks]}
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@contextlib.contextmanager
def _state_lock(repo: Repository) -> Iterator[None]:
    """Serialize read-check-write task transitions for one repository.

    The lock is deliberately narrow and short-lived.  A stale lock from a
    killed setup process is recoverable after five minutes; an active lock is
    an explicit retry condition rather than an unsafe concurrent write.
    """
    directory = state_dir(repo)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "tasks.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0
        if age <= LOCK_STALE_SECONDS:
            raise WorkError("another task transition is in progress; retry in a moment")
        try:
            lock.unlink()
        except OSError as exc:
            raise WorkError(f"could not recover stale task lock at {lock}: {exc}") from exc
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def normalize_paths(paths: list[str]) -> tuple[str, ...]:
    """Validate exact repository-relative path claims without wildcard ambiguity."""
    normalized: list[str] = []
    for raw in paths:
        candidate = raw.replace("\\", "/").strip()
        path = PurePosixPath(candidate)
        if not candidate or path.is_absolute() or ".." in path.parts or any(ch in candidate for ch in "*?[]"):
            raise WorkError(f"invalid claimed path {raw!r}; use an exact repository-relative path")
        clean = str(path)
        if clean in (".", ""):
            raise WorkError("a task must claim a file or directory, not the repository root")
        if clean not in normalized:
            normalized.append(clean)
    if not normalized:
        raise WorkError("at least one --path is required before implementation begins")
    return tuple(normalized)


def paths_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Whether exact paths or directory claims overlap."""
    for left in first:
        for right in second:
            if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                return True
    return False


def _branch_name(agent: str, task_id: str) -> str:
    return f"{agent}/{task_id}"


def _worktree_path(repo: Repository, task_id: str) -> Path:
    return state_dir(repo) / "worktrees" / task_id


def _write_plan(task: Task) -> None:
    path = Path(task.plan_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    path.write_text(
        f"# {task.summary}\n\n"
        f"- Task: `{task.id}`\n"
        f"- Session: `{task.session_id}`\n"
        f"- Base: `{task.base_sha}`\n"
        f"- Branch: `{task.branch}`\n"
        f"- Claimed paths: {', '.join(f'`{path}`' for path in task.paths)}\n\n"
        "## Plan\n\n"
        "## Verification\n\n"
        "## Handoff\n",
        encoding="utf-8",
    )


def _working_tree_dirty(repo: Repository) -> bool:
    return bool(_git(repo.root, "status", "--porcelain=v1", "--untracked-files=all"))


def start(
    directory: Path,
    summary: str,
    session_id: str,
    paths: list[str],
    agent: str = "codex",
    worktree_mode: str = "auto",
    now: datetime | None = None,
) -> Task:
    """Start an isolated implementation task and return its complete record."""
    if agent not in memory_claims.KNOWN_AGENTS:
        raise WorkError(f"unknown agent {agent!r} (expected one of {memory_claims.KNOWN_AGENTS})")
    if worktree_mode not in WORKTREE_MODES:
        raise WorkError(f"unknown worktree mode {worktree_mode!r} (expected one of {WORKTREE_MODES})")
    if not summary.strip():
        raise WorkError("summary is required")
    if not session_id.strip():
        raise WorkError("session_id is required; use a unique chat/session identifier")

    repo = repository_for(directory)
    claimed_paths = normalize_paths(paths)
    task_now = now or datetime.now(timezone.utc)
    stamp = _now(task_now)
    task_id = f"{TASK_PREFIX}-{task_now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    branch = _branch_name(agent, task_id)

    with _state_lock(repo):
        tasks = _load_tasks(repo)
        conflicts = [task for task in tasks if task.status == ACTIVE and paths_overlap(claimed_paths, task.paths)]
        if conflicts:
            detail = ", ".join(f"{task.id} ({task.session_id}: {', '.join(task.paths)})" for task in conflicts)
            raise WorkError(f"claimed paths overlap active task(s): {detail}; choose non-overlapping paths or finish that task")

        concurrent = any(task.status == ACTIVE and task.session_id != session_id for task in tasks)
        must_isolate = concurrent or _working_tree_dirty(repo)
        use_worktree = worktree_mode == "always" or (worktree_mode == "auto" and must_isolate)
        if worktree_mode == "never" and must_isolate:
            reason = "another session is active" if concurrent else "the primary checkout has uncommitted changes"
            raise WorkError(f"cannot use the primary checkout: {reason}; retry with --worktree auto or always")

        if use_worktree:
            worktree = _worktree_path(repo, task_id)
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _git(repo.root, "worktree", "add", "-b", branch, str(worktree), repo.head)
        else:
            if repo.current_branch is None:
                raise WorkError("cannot switch the primary checkout from a detached HEAD; use --worktree always")
            _git(repo.root, "switch", "-c", branch, repo.head)
            worktree = repo.root

        task = Task(
            id=task_id,
            summary=summary.strip(),
            agent=agent,
            session_id=session_id.strip(),
            paths=claimed_paths,
            base_sha=repo.head,
            branch=branch,
            worktree=str(worktree),
            plan_path=str(state_dir(repo) / "plans" / f"{task_id}.md"),
            status=ACTIVE,
            created_at=stamp,
        )
        try:
            _write_plan(task)
            _save_tasks(repo, [*tasks, task])
        except Exception:
            # A failed local state write must not leave Git artifacts that
            # look coordinated when no task record exists.  These targets
            # were created by this invocation and have not been handed to an
            # agent yet, so this narrowly scoped rollback is safe.
            with contextlib.suppress(WorkError):
                if use_worktree:
                    _git(repo.root, "worktree", "remove", "--force", str(worktree))
                else:
                    _git(repo.root, "switch", repo.current_branch)
                _git(repo.root, "branch", "-D", branch)
            raise
    return task


def list_tasks(directory: Path, active_only: bool = False) -> list[Task]:
    repo = repository_for(directory)
    tasks = _load_tasks(repo)
    return [task for task in tasks if task.status == ACTIVE] if active_only else tasks


def find_task(directory: Path, task_id: str) -> tuple[Repository, Task]:
    repo = repository_for(directory)
    for task in _load_tasks(repo):
        if task.id == task_id:
            return repo, task
    raise WorkError(f"no task {task_id!r} is recorded for {repo.root}")


def diff(directory: Path, task_id: str) -> str:
    """Return the task's complete tracked diff against its immutable base."""
    _repo, task = find_task(directory, task_id)
    worktree = Path(task.worktree)
    if not worktree.is_dir():
        raise WorkError(f"task worktree no longer exists: {worktree}; do not delete it before handoff")
    return _git(worktree, "diff", task.base_sha, "--")


def finish(directory: Path, task_id: str, session_id: str, now: datetime | None = None) -> Task:
    """Mark an active task complete and release its path claims; retain Git artifacts."""
    if not session_id.strip():
        raise WorkError("session_id is required to finish a task")
    repo = repository_for(directory)
    with _state_lock(repo):
        tasks = _load_tasks(repo)
        for index, task in enumerate(tasks):
            if task.id != task_id:
                continue
            if task.status != ACTIVE:
                raise WorkError(f"{task_id} is already {task.status}")
            if task.session_id != session_id.strip():
                raise WorkError(f"{task_id} belongs to session {task.session_id!r}, not {session_id!r}")
            finished = Task(**{**asdict(task), "status": DONE, "finished_at": _now(now)})
            tasks[index] = finished
            _save_tasks(repo, tasks)
            return finished
    raise WorkError(f"no task {task_id!r} is recorded for {repo.root}")


def _render_task(task: Task) -> str:
    return (
        f"{task.id} [{task.status}]\n"
        f"  summary: {task.summary}\n"
        f"  session: {task.session_id} ({task.agent})\n"
        f"  branch: {task.branch}\n"
        f"  base: {task.base_sha}\n"
        f"  worktree: {task.worktree}\n"
        f"  plan: {task.plan_path}\n"
        f"  paths: {', '.join(task.paths)}"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start", help="claim paths and create a task branch/worktree")
    start_p.add_argument("--summary", required=True, help="one concise implementation objective")
    start_p.add_argument("--session", required=True, help="unique client chat/session identifier")
    start_p.add_argument("--path", action="append", default=[], help="exact repository-relative claimed path (repeatable)")
    start_p.add_argument("--agent", choices=memory_claims.KNOWN_AGENTS, default="codex")
    start_p.add_argument("--worktree", choices=WORKTREE_MODES, default="auto")
    start_p.add_argument("--project", default=".", help="repository or directory within it (default: current directory)")

    list_p = sub.add_parser("list", help="show recorded tasks for the repository")
    list_p.add_argument("--active", action="store_true", help="show only active tasks")
    list_p.add_argument("--project", default=".", help="repository or directory within it (default: current directory)")

    diff_p = sub.add_parser("diff", help="show complete tracked task diff against its recorded base")
    diff_p.add_argument("task_id")
    diff_p.add_argument("--project", default=".", help="repository or directory within it (default: current directory)")

    finish_p = sub.add_parser("finish", help="mark a task complete and release its path claims")
    finish_p.add_argument("task_id")
    finish_p.add_argument("--session", required=True, help="the session that started the task")
    finish_p.add_argument("--project", default=".", help="repository or directory within it (default: current directory)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    project = Path(args.project)
    try:
        if args.command == "start":
            task = start(project, args.summary, args.session, args.path, args.agent, args.worktree)
            print(f"status: success\nsummary: started {task.id}\nnext_actions: work only in {task.worktree}; run `agent-harness-work diff {task.id}` before handoff\nartifacts: {task.plan_path}\n{_render_task(task)}")
        elif args.command == "list":
            tasks = list_tasks(project, args.active)
            if not tasks:
                print("status: success\nsummary: no recorded tasks\nnext_actions: start a task before implementation\nartifacts:")
            else:
                print("status: success\nsummary: recorded tasks\nnext_actions: finish completed tasks to release path claims\nartifacts:\n" + "\n\n".join(_render_task(task) for task in tasks))
        elif args.command == "diff":
            print(diff(project, args.task_id))
        else:
            task = finish(project, args.task_id, args.session)
            print(f"status: success\nsummary: finished {task.id} and released path claims\nnext_actions: hand off the branch for integration\nartifacts: {task.plan_path}\n{_render_task(task)}")
    except WorkError as exc:
        print(f"status: error\nsummary: {exc}\nnext_actions: correct the reported condition and retry\nartifacts:", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
