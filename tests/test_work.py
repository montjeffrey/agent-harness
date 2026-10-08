"""Task-scoped branches, private plans, and path conflict coordination."""

from __future__ import annotations

import json
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


# --- git subprocesses must never inherit the caller's stdin ---------------
#
# The `work_*` tools run inside the long-lived stdio MCP server, whose stdin
# is the live JSON-RPC pipe. A git child that inherits that pipe blocks before
# git even starts -- GIT_TRACE emits nothing -- until the 30 s timeout fires,
# so every call costs its whole timeout instead of milliseconds. Closing stdin
# is the fix; this is the deterministic guard that must never regress.
# (The end-to-end reproduction needs a running server: tests/test_mcp_stdio.py.)


def test_git_helper_closes_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "output\n", "")

    monkeypatch.setattr(work.subprocess, "run", fake_run)

    assert work._git(tmp_path, "rev-parse", "HEAD") == "output"
    assert calls[0]["stdin"] is subprocess.DEVNULL


def test_repository_for_closes_stdin_on_every_git_call(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Whole-path guard: `repository_for` runs four git commands, including the
    branch-resolution call that bypasses `_git`. None may inherit stdin."""
    calls: list[dict] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        calls.append(kwargs)
        return real_run(args, **kwargs)

    monkeypatch.setattr(work.subprocess, "run", recording_run)

    work.repository_for(repo)

    assert len(calls) >= 4, f"expected every git call to be recorded, saw {len(calls)}"
    for kwargs in calls:
        assert kwargs["stdin"] is subprocess.DEVNULL


# --- git evidence recorded at finish ---------------------------------------
#
# `finish` releases the claim and records where the work ended. It must not
# do anything else to git: the branch and worktree stay exactly as they were.


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(f"{message}\n", encoding="utf-8")
    _run(repo, "add", name)
    _run(repo, "commit", "-m", message)
    return _run(repo, "rev-parse", "HEAD")


def test_finish_records_branch_base_and_final_sha_without_touching_git(repo: Path) -> None:
    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    tip = _commit(Path(task.worktree), "src/app.py", "change the value")

    finished = work.finish(repo, task.id, "codex-chat-a", now=NOW)

    assert finished.status == work.DONE
    assert finished.branch == task.branch
    assert finished.base_sha == task.base_sha != tip
    assert finished.final_sha == tip
    # Nothing was rebased, merged, or removed.
    assert _run(repo, "rev-parse", task.branch) == tip
    assert _run(repo, "branch", "--show-current") == task.branch
    assert Path(finished.worktree).is_dir()
    # And the record round-trips through the store.
    assert work.find_task(repo, task.id)[1].final_sha == tip


def test_finish_in_an_external_worktree_reads_the_branch_tip(repo: Path) -> None:
    work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    second = work.start(repo, "Change readme", "claude-chat-b", ["README.md"], agent="claude", now=NOW)
    tip = _commit(Path(second.worktree), "README.md", "rewrite readme")

    finished = work.finish(repo, second.id, "claude-chat-b", now=NOW)

    assert finished.final_sha == tip
    assert _run(repo, "rev-parse", "HEAD") != tip  # the primary checkout was not touched


def test_finish_still_releases_a_task_whose_branch_is_gone(repo: Path) -> None:
    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    _run(repo, "switch", "-c", "elsewhere")
    _run(repo, "branch", "-D", task.branch)

    finished = work.finish(repo, task.id, "codex-chat-a", now=NOW)

    assert finished.status == work.DONE
    assert finished.final_sha is None
    assert work.list_tasks(repo, active_only=True) == []


def test_records_written_before_final_sha_existed_still_load(repo: Path) -> None:
    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    state = work.state_path(work.repository_for(repo))
    payload = json.loads(state.read_text(encoding="utf-8"))
    for item in payload["tasks"]:
        item.pop("final_sha")
    state.write_text(json.dumps(payload), encoding="utf-8")

    [loaded] = work.list_tasks(repo)

    assert loaded.id == task.id and loaded.final_sha is None


def test_cli_finish_reports_the_final_sha(repo: Path, capsys: pytest.CaptureFixture) -> None:
    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    tip = _commit(Path(task.worktree), "src/app.py", "change the value")

    rc = work.main(["finish", task.id, "--session", "codex-chat-a", "--project", str(repo)])

    assert rc == 0
    assert f"final: {tip}" in capsys.readouterr().out


# --- helpers the hooks build on --------------------------------------------


def test_commit_log_parses_sha_subject_and_files_newest_first(repo: Path) -> None:
    base = _run(repo, "rev-parse", "HEAD")
    first = _commit(repo, "a.py", "first change")
    (repo / "b.py").write_text("b\n", encoding="utf-8")
    (repo / "c.py").write_text("c\n", encoding="utf-8")
    _run(repo, "add", "b.py", "c.py")
    _run(repo, "commit", "-m", "second change")
    second = _run(repo, "rev-parse", "HEAD")

    log = work.commit_log(repo, base)

    assert [c.sha for c in log] == [second, first]
    assert log[0].subject == "second change" and log[0].files == ("b.py", "c.py")
    assert log[1].files == ("a.py",)
    assert work.commit_log(repo, base, limit=1) == log[:1]
    assert work.commit_log(repo, "") == []
    assert work.commit_count(repo, base, second) == 2
    assert work.commit_count(repo, "", second) is None


def test_commit_log_closes_stdin_like_every_other_git_call(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    base = _run(repo, "rev-parse", "HEAD")  # before the patch: `_run` is not under test
    calls: list[dict] = []
    real_run = subprocess.run

    def recording_run(args, **kwargs):
        calls.append(kwargs)
        return real_run(args, **kwargs)

    monkeypatch.setattr(work.subprocess, "run", recording_run)
    work.commit_log(repo, base)
    assert calls and all(kwargs["stdin"] is subprocess.DEVNULL for kwargs in calls)


def test_session_markers_round_trip_and_never_raise_on_garbage(repo: Path) -> None:
    repository = work.repository_for(repo)
    assert work.load_session_marker(repository, "sess") is None

    path = work.save_session_marker(repository, "sess", {"head": "abc", "reported_tasks": []})

    assert path.is_relative_to(work.state_dir(repository))
    assert not path.is_relative_to(repo)
    assert work.load_session_marker(repository, "sess") == {"head": "abc", "reported_tasks": []}
    assert work.session_marker_path(repository, "sess") != work.session_marker_path(repository, "other")
    path.write_text("{garbage", encoding="utf-8")
    assert work.load_session_marker(repository, "sess") is None


def test_active_and_recorded_tasks_read_state_for_a_resolved_repository(repo: Path) -> None:
    repository = work.repository_for(repo)
    assert work.active_tasks(repository) == [] and work.recorded_tasks(repository) == []
    task = work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    assert [t.id for t in work.active_tasks(repository)] == [task.id]
    work.finish(repo, task.id, "codex-chat-a", now=NOW)
    assert work.active_tasks(repository) == []
    assert [t.id for t in work.recorded_tasks(repository)] == [task.id]


def test_primary_root_is_the_same_from_a_linked_worktree(repo: Path) -> None:
    work.start(repo, "Change application", "codex-chat-a", ["src/app.py"], now=NOW)
    second = work.start(repo, "Change readme", "claude-chat-b", ["README.md"], agent="claude", now=NOW)

    from_primary = work.repository_for(repo)
    from_worktree = work.repository_for(Path(second.worktree))

    assert work.primary_root(from_primary) == repo.resolve()
    assert work.primary_root(from_worktree) == repo.resolve()
    assert from_worktree.root != from_primary.root
