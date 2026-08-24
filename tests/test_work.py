"""Task-scoped branches, private plans, and path conflict coordination."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import mcp_server, work

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init")
    _run(root, "config", "user.name", "Test User")
    _run(root, "config", "user.email", "test@example.test")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    _run(root, "add", ".")
    _run(root, "commit", "-m", "initial")
    return root


def test_start_records_a_private_plan_and_switches_a_clean_primary_checkout(repo: Path) -> None:
    task = work.start(repo, "Change the app value", "codex-chat-a", ["src/app.py"], now=NOW)

    assert task.status == work.ACTIVE
    assert task.worktree == str(repo)
    assert task.base_sha == _run(repo, "rev-parse", "HEAD")
    assert _run(repo, "branch", "--show-current") == task.branch
    assert Path(task.plan_path).is_file()
    assert not Path(task.plan_path).is_relative_to(repo)
    assert _run(repo, "status", "--porcelain") == ""


def test_active_overlapping_paths_are_refused_even_for_two_codex_sessions(repo: Path) -> None:
    work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)

    with pytest.raises(work.WorkError, match="overlap active task"):
        work.start(repo, "Change source tree", "codex-chat-b", ["src"], now=NOW)


def test_parallel_non_overlapping_task_gets_an_external_worktree(repo: Path) -> None:
    first = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    second = work.start(repo, "Change readme", "claude-chat-b", ["README.md"], agent="claude", now=NOW)

    assert first.worktree == str(repo)
    assert Path(second.worktree).is_dir()
    assert not Path(second.worktree).is_relative_to(repo)
    assert _run(repo, "-C", second.worktree, "branch", "--show-current") == second.branch
    assert _run(repo, "status", "--porcelain") == ""

    # Agents operate from the returned worktree, so task state must resolve
    # through Git's common directory rather than the original checkout path.
    assert work.find_task(Path(second.worktree), second.id)[1] == second


def test_dirty_primary_checkout_causes_auto_mode_to_isolate(repo: Path) -> None:
    (repo / "uncommitted.txt").write_text("local work\n", encoding="utf-8")

    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)

    assert Path(task.worktree).is_dir()
    assert Path(task.worktree) != repo
    assert (repo / "uncommitted.txt").exists()


def test_diff_uses_the_recorded_base_and_includes_uncommitted_changes(repo: Path) -> None:
    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    target = Path(task.worktree) / "src" / "app.py"
    target.write_text("value = 2\n", encoding="utf-8")

    result = work.diff(repo, task.id)

    assert "-value = 1" in result
    assert "+value = 2" in result


def test_finish_requires_owning_session_and_releases_path_claim(repo: Path) -> None:
    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)

    with pytest.raises(work.WorkError, match="belongs to session"):
        work.finish(repo, task.id, "codex-chat-b", now=NOW)

    finished = work.finish(repo, task.id, "codex-chat-a", now=NOW)
    replacement = work.start(repo, "Take over application", "codex-chat-b", ["src/app.py"], now=NOW)

    assert finished.status == work.DONE
    assert replacement.status == work.ACTIVE


def test_cli_reports_recoverable_error_shape_for_invalid_path(repo: Path, capsys: pytest.CaptureFixture) -> None:
    rc = work.main([
        "start", "--project", str(repo), "--summary", "Bad claim", "--session", "codex-chat-a", "--path", "../outside"
    ])

    captured = capsys.readouterr()
    assert rc == 2
    assert "status: error" in captured.err
    assert "next_actions:" in captured.err


def test_mcp_work_actions_expose_start_status_diff_and_finish(repo: Path) -> None:
    started = mcp_server.dispatch_work(
        "work_start",
        {"project": str(repo), "summary": "Change application", "session": "codex-chat-a", "paths": "src/app.py"},
    )
    task_id = started.split("started ", 1)[1].split("\n", 1)[0]
    (repo / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")

    status = mcp_server.dispatch_work("work_status", {"project": str(repo), "active_only": True})
    changed = mcp_server.dispatch_work("work_diff", {"project": str(repo), "task_id": task_id})
    finished = mcp_server.dispatch_work(
        "work_finish", {"project": str(repo), "task_id": task_id, "session": "codex-chat-a"}
    )

    assert "status: success" in started
    assert task_id in status
    assert "+value = 2" in changed
    assert "released path claims" in finished


def test_mcp_work_start_surfaces_overlap_as_a_recoverable_error(repo: Path) -> None:
    mcp_server.dispatch_work(
        "work_start",
        {"project": str(repo), "summary": "Change application", "session": "codex-chat-a", "paths": "src/app.py"},
    )

    out = mcp_server.dispatch_work(
        "work_start",
        {"project": str(repo), "summary": "Change source", "session": "codex-chat-b", "paths": "src"},
    )

    assert "status: error" in out
    assert "overlap active task" in out
