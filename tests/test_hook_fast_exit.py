"""The claim guard and the progress hook run in a fresh interpreter on every
edit / every turn. Pin what they cost when there is nothing to do: no harness
import and no git process, and how few git processes they launch when there is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_store, work

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "hooks" / "claude" / "claim_guard.py"
PROGRESS = REPO_ROOT / "hooks" / "claude" / "progress_capture_hook.py"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

# Runs a hook script as __main__-equivalent in a clean interpreter, counting
# `git` launches made through subprocess.run, and reports which `harness`
# modules were imported by the time it returned.
DRIVER = """
import importlib.util, json, subprocess, sys
from pathlib import Path
hook = Path(sys.argv[1])
launches = []
real_run = subprocess.run
def counting_run(cmd, *a, **k):
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
        launches.append(list(cmd[3:5]))
    return real_run(cmd, *a, **k)
subprocess.run = counting_run
spec = importlib.util.spec_from_file_location(hook.stem, hook)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
code = mod.main()
print(json.dumps({
    "exit": code,
    "git": len(launches),
    "harness": sorted(m for m in sys.modules if m == "harness" or m.startswith("harness.")),
}))
"""


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _hook(script: Path, payload: dict, home: Path, cwd: Path) -> tuple[dict, str]:
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    result = subprocess.run(
        [sys.executable, "-c", DRIVER, str(script)],
        input=json.dumps(payload),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1]), result.stderr


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init")
    _run(root, "config", "user.name", "Test User")
    _run(root, "config", "user.email", "test@example.test")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _run(root, "add", ".")
    _run(root, "commit", "-m", "initial")
    return root


def _edit(path: Path, session: str = "other") -> dict:
    return {"tool_name": "Edit", "session_id": session, "tool_input": {"file_path": str(path)}}


# --- claim guard ------------------------------------------------------------


def test_guard_without_a_work_directory_imports_nothing_and_launches_no_git(
    repo: Path, isolated_home: Path
) -> None:
    out, _ = _hook(GUARD, _edit(repo / "a.txt"), isolated_home, repo)
    assert out == {"exit": 0, "git": 0, "harness": []}


def test_guard_with_work_directory_but_no_task_state_imports_nothing(
    repo: Path, isolated_home: Path
) -> None:
    # A progress-capture session marker creates work/<repo>/sessions but no tasks.json.
    (isolated_home / ".agent-harness" / "work" / "repo-x" / "sessions").mkdir(parents=True)
    out, _ = _hook(GUARD, _edit(repo / "a.txt"), isolated_home, repo)
    assert out == {"exit": 0, "git": 0, "harness": []}


def test_guard_resolves_the_repository_with_one_git_launch(repo: Path, isolated_home: Path) -> None:
    work.start(repo, "Change a", "owner", ["a.txt"], now=NOW)
    out, err = _hook(GUARD, _edit(repo / "a.txt"), isolated_home, repo)
    assert out["exit"] == 2
    assert "claimed by active task" in err
    assert out["git"] == 1


def test_guard_allows_with_one_git_launch_when_the_path_is_unclaimed(repo: Path, isolated_home: Path) -> None:
    work.start(repo, "Change a", "owner", ["a.txt"], now=NOW)
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    out, _ = _hook(GUARD, _edit(repo / "b.txt"), isolated_home, repo)
    assert out["exit"] == 0
    assert out["git"] == 1


# --- claim guard: the decision does not depend on how the repository is found


@pytest.fixture
def claimed(repo: Path):
    from hooks.claude import claim_guard

    work.start(repo, "Change a", "owner", ["a.txt", "sub"], now=NOW)
    return claim_guard


def test_guard_decisions_for_a_claimed_unclaimed_and_own_path(repo: Path, claimed) -> None:
    (repo / "sub").mkdir()
    ev = lambda p, s="other": {"tool_name": "Write", "session_id": s, "tool_input": {"file_path": str(p)}}  # noqa: E731
    assert claimed.decide(ev(repo / "a.txt")).allow is False
    assert claimed.decide(ev(repo / "sub" / "new" / "x.py")).allow is False  # from a missing subdirectory
    assert claimed.decide(ev(repo / "zzz.txt")).allow is True
    assert claimed.decide(ev(repo / "a.txt", "owner")).allow is True


def test_guard_in_a_linked_worktree_still_denies_foreign_claims_and_allows_its_own(
    repo: Path, claimed
) -> None:
    mine = work.start(repo, "Other work", "session-b", ["b.txt"], agent="claude", now=NOW)
    inside = Path(mine.worktree)
    ev = lambda p, s: {"tool_name": "Edit", "session_id": s, "tool_input": {"file_path": str(p)}}  # noqa: E731
    # Inside the task's own worktree the path is that task's work; the same
    # path in the primary checkout is still guarded, and so is another task's.
    assert claimed.decide(ev(inside / "b.txt", "session-c")).allow is True
    assert claimed.decide(ev(repo / "b.txt", "session-c")).allow is False
    assert claimed.decide(ev(inside / "a.txt", "session-c")).allow is False


def test_guard_allows_when_head_is_unborn(repo: Path, claimed) -> None:
    """The old four-call lookup failed on `rev-parse HEAD` here and allowed."""
    _run(repo, "checkout", "--orphan", "fresh")
    event = {"tool_name": "Edit", "session_id": "other", "tool_input": {"file_path": str(repo / "a.txt")}}
    assert claimed.decide(event).allow is True


def test_guard_allows_a_path_outside_any_repository(repo: Path, claimed, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "a.txt"
    outside.parent.mkdir()
    event = {"tool_name": "Edit", "session_id": "other", "tool_input": {"file_path": str(outside)}}
    assert claimed.decide(event).allow is True


# --- progress capture -------------------------------------------------------


def test_progress_in_a_project_that_is_not_enrolled_imports_nothing_and_launches_no_git(
    repo: Path, isolated_home: Path
) -> None:
    out, _ = _hook(PROGRESS, {"session_id": "s1", "cwd": str(repo)}, isolated_home, repo)
    assert out == {"exit": 0, "git": 0, "harness": []}


def test_progress_with_a_blank_session_imports_nothing(repo: Path, isolated_home: Path) -> None:
    memory_store.init_project(repo, now=NOW)
    out, _ = _hook(PROGRESS, {"session_id": "  ", "cwd": str(repo)}, isolated_home, repo)
    assert out == {"exit": 0, "git": 0, "harness": []}


def test_progress_in_an_enrolled_project_launches_three_git_processes(
    repo: Path, isolated_home: Path
) -> None:
    """One `ls-files` for the trusted-store check plus two to resolve the repository."""
    memory_store.init_project(repo, now=NOW)
    out, _ = _hook(PROGRESS, {"session_id": "s1", "cwd": str(repo)}, isolated_home, repo)
    assert out["exit"] == 0
    assert out["git"] == 3


# --- repository_for / repository_root_for -----------------------------------


def test_repository_root_for_matches_repository_for_in_a_subdirectory_and_a_worktree(repo: Path) -> None:
    (repo / "sub").mkdir()
    task = work.start(repo, "Other work", "session-b", ["b.txt"], agent="claude", now=NOW)
    for where in (repo, repo / "sub", Path(task.worktree)):
        full = work.repository_for(where)
        narrow = work.repository_root_for(where)
        assert (narrow.root, narrow.common_git_dir, narrow.id) == (full.root, full.common_git_dir, full.id)
        assert narrow.head == full.head


def test_repository_root_for_raises_work_error_outside_a_repository(tmp_path: Path) -> None:
    with pytest.raises(work.WorkError):
        work.repository_root_for(tmp_path)


@pytest.mark.parametrize("separator", [" ", " ", "\x85", "\x0c", "\x0b"])
def test_repository_root_for_keeps_line_separator_characters_in_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, separator: str
) -> None:
    # These are legal in a directory name. Splitting the rev-parse answer on
    # anything but "\n" would turn one path into two lines, raise, and make the
    # claim guard allow an edit it should have checked.
    root = tmp_path / f"odd{separator}name"
    answer = "\n".join([root.as_posix(), (root / ".git").as_posix(), "0" * 40])
    monkeypatch.setattr(work, "_git", lambda *_args, **_kwargs: answer)

    found = work.repository_root_for(tmp_path)

    assert separator in str(found.root)
    assert found.head == "0" * 40
