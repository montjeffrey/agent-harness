"""PreToolUse hook: refuse to edit a path another session's task has claimed.

Claude Code runs this before `Edit`, `MultiEdit`, `Write`, and `NotebookEdit`
with the tool-call JSON on stdin (`session_id`, `cwd`, `tool_input`). Path
claims recorded by `work_start` (ADR 0002) were cooperative until now: an
agent that never called `work_start` could edit a claimed file and corrupt
another task's work. This hook makes the claim binding for Claude Code
sessions: it exits 2 (deny) with guidance on stderr when, and only when,

    an ACTIVE task
    owned by a DIFFERENT session
    claims a path that overlaps the file about to be written.

Everything else allows (exit 0, no output): the session's own claims, tasks
already finished, a file outside any repository, a repository with no task
state, malformed input, and any error inside the harness. Denying on an error
would turn a memory-store hiccup into a blocked edit, which is a worse
failure than one unguarded write.

Session identity is the `session_id` Claude Code sends; the SessionStart
packet prints it so an agent can hand exactly that id to `work_start`. A task
started under any other id looks foreign, which is the safe direction: the
deny message says which id this session has and how to proceed. A file
inside a task's own private worktree is always that task's, whatever id the
tool call carries, because the worktree is the isolation boundary.

Cost: one repository resolution (four `git rev-parse`-class calls, no
`status`) and one JSON read of the task state. Nothing is written.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

GUARDED_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
PATH_KEYS = ("file_path", "notebook_path")


@dataclass(frozen=True)
class Verdict:
    """The guard's decision, kept separate from I/O so it is testable."""

    allow: bool
    reason: str = ""


ALLOW = Verdict(allow=True)


def _import_work():
    try:
        from harness import work
    except ImportError:
        # Source checkout: <repo>/hooks/claude/ -> package root two levels up.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from harness import work
    return work


def target_path(payload: dict) -> str | None:
    """The file the tool call will write, or None when there is none."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def resolve_target(raw: str, cwd: str | None) -> Path:
    """Absolute, normalized target. A relative path is anchored at `cwd`."""
    candidate = Path(raw.strip().rstrip(" ."))
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate if cwd else Path.cwd() / candidate
    return candidate.resolve()


def _existing_ancestor(path: Path) -> Path:
    """`Write` may create a file in a directory that does not exist yet;
    git needs a directory that does. Walk up to the first one."""
    current = path.parent
    while not current.is_dir():
        if current.parent == current:
            return current
        current = current.parent
    return current


def decide(payload: dict, work=None) -> Verdict:
    """Compute the verdict for one tool-call payload. Never raises."""
    try:
        return _decide(payload, work or _import_work())
    except Exception:  # noqa: BLE001 -- a harness error must never block an edit
        return ALLOW


def _decide(payload: dict, work) -> Verdict:
    if not isinstance(payload, dict):
        return ALLOW
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name not in GUARDED_TOOLS:
        return ALLOW
    raw = target_path(payload)
    if raw is None:
        return ALLOW

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        # Without an identity "a different session" cannot be established.
        return ALLOW
    session_id = session_id.strip()

    # A repository with no task state has no claims; skip git entirely. The
    # work directory is created by the first `work_start` on this machine.
    work_root = Path.home() / ".agent-harness" / "work"
    if not work_root.is_dir():
        return ALLOW

    cwd = payload.get("cwd")
    target = resolve_target(raw, cwd if isinstance(cwd, str) else None)
    try:
        repo = work.repository_for(_existing_ancestor(target))
    except work.WorkError:
        return ALLOW  # not inside a git repository

    try:
        relative = target.relative_to(repo.root)
    except ValueError:
        return ALLOW
    claimed = work.normalize_paths([relative.as_posix()])

    tasks = work.active_tasks(repo)
    if not tasks:
        return ALLOW

    primary = work.primary_root(repo)
    for task in tasks:
        if not work.paths_overlap(claimed, task.paths):
            continue
        if task.session_id == session_id:
            continue
        task_worktree = Path(task.worktree)
        if primary is not None and repo.root != primary and task_worktree.resolve() == repo.root:
            continue  # inside that task's private worktree: it is that task's work
        return Verdict(
            allow=False,
            reason=(
                f"agent-harness: `{claimed[0]}` is claimed by active task {task.id} "
                f"(session {task.session_id}, branch {task.branch}): {task.summary}\n"
                f"This session ({session_id}) does not own that task, so the edit was "
                "refused to prevent two agents changing the same file. Choose a path "
                "outside that claim, or wait until the owner calls work_finish; "
                "`work_status` lists active claims. If the task is really yours, "
                f"start tasks with session id `{session_id}` (printed at session "
                "start) so the guard can recognise them."
            ),
        )
    return ALLOW


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 -- a hook must never crash and block an unrelated tool call
        return 0
    verdict = decide(payload)
    if verdict.allow:
        return 0
    print(verdict.reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
