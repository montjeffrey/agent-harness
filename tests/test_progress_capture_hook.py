"""Tests for the Stop / SessionEnd progress-capture hook.

What matters: proposals come from git evidence, land exactly as
`memory_remember` would write them, are never duplicated across runs, and
nothing about the hook can block a session from ending.
"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_store, work
from hooks.claude import progress_capture_hook as hook

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
SESSION = "session-a"


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(f"{message}\n", encoding="utf-8")
    _run(repo, "add", name)
    _run(repo, "commit", "-m", message)
    return _run(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init")
    _run(root, "config", "user.name", "Test User")
    _run(root, "config", "user.email", "test@example.test")
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    _run(root, "add", ".")
    _run(root, "commit", "-m", "initial")
    memory_store.init_project(root, now=NOW)
    return root


def _entries(repo: Path) -> list[dict]:
    return list(memory_store.iter_entries(memory_store.project_memory_dir(repo)))


# --- commits ---------------------------------------------------------------


def test_the_first_scan_records_a_marker_and_proposes_nothing(repo: Path) -> None:
    result = hook.scan(repo, SESSION, now=NOW)

    assert result.proposed == [] and result.skipped is None
    marker = work.load_session_marker(work.repository_for(repo), SESSION)
    assert marker["head"] == _run(repo, "rev-parse", "HEAD")
    assert _entries(repo) == []


def test_a_new_commit_becomes_one_proposed_inferred_discovery(repo: Path) -> None:
    hook.scan(repo, SESSION, now=NOW)
    sha = _commit(repo, "feature.py", "feat: add the feature")

    result = hook.scan(repo, SESSION, now=NOW)

    [entry] = _entries(repo)
    assert len(result.proposed) == 1
    assert entry["type"] == "discovery"
    assert entry["status"] == "proposed" and entry["confidence"] == "inferred"
    assert entry["source"]["commit"] == sha
    assert entry["source"]["project"] == memory_store.project_id_for(repo)
    assert "feat: add the feature" in entry["summary"]
    assert "feature.py" in entry["summary"]
    assert _run(repo, "branch", "--show-current") in entry["summary"]
    assert "progress" in entry["tags"] and "commit" in entry["tags"]


def test_a_second_scan_with_nothing_new_writes_nothing(repo: Path) -> None:
    hook.scan(repo, SESSION, now=NOW)
    _commit(repo, "a.py", "first")
    hook.scan(repo, SESSION, now=NOW)

    again = hook.scan(repo, SESSION, now=NOW)

    assert again.proposed == []
    assert len(_entries(repo)) == 1


def test_commits_are_proposed_once_even_if_the_marker_is_lost(repo: Path) -> None:
    """The inbox itself is the backstop against double proposals."""
    hook.scan(repo, SESSION, now=NOW)
    _commit(repo, "a.py", "first")
    hook.scan(repo, SESSION, now=NOW)
    repository = work.repository_for(repo)
    marker = work.load_session_marker(repository, SESSION)
    # Rewind the marker to before the commit, as a crash mid-scan could.
    work.save_session_marker(repository, SESSION, {**marker, "head": _run(repo, "rev-parse", "HEAD~1")})

    hook.scan(repo, SESSION, now=NOW)

    assert len(_entries(repo)) == 1


def test_many_commits_are_capped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "COMMIT_LIMIT", 3)
    hook.scan(repo, SESSION, now=NOW)
    for n in range(5):
        _commit(repo, f"f{n}.py", f"commit {n}")

    hook.scan(repo, SESSION, now=NOW)

    assert len(_entries(repo)) == 3


def test_a_vanished_base_resets_the_marker_without_proposing(repo: Path) -> None:
    repository = work.repository_for(repo)
    hook.scan(repo, SESSION, now=NOW)
    marker = work.load_session_marker(repository, SESSION)
    work.save_session_marker(repository, SESSION, {**marker, "head": "0" * 40})
    _commit(repo, "a.py", "first")

    result = hook.scan(repo, SESSION, now=NOW)

    assert result.proposed == [] and result.skipped is None
    assert work.load_session_marker(repository, SESSION)["head"] == _run(repo, "rev-parse", "HEAD")


# --- finished tasks --------------------------------------------------------


def test_a_finished_task_becomes_one_entry_with_its_git_evidence(repo: Path) -> None:
    hook.scan(repo, SESSION, now=NOW)
    task = work.start(repo, "Change the readme", SESSION, ["README.md"], now=NOW)
    sha = _commit(repo, "README.md", "docs: rewrite readme")
    finished = work.finish(repo, task.id, SESSION, now=NOW)
    assert finished.final_sha == sha

    result = hook.scan(repo, SESSION, now=NOW)

    # One entry for the task; its commit is evidence inside it, not a second entry.
    [entry] = _entries(repo)
    assert len(result.proposed) == 1
    assert entry["summary"].startswith(f"Finished task {task.id}")
    assert "Change the readme" in entry["summary"]
    assert task.branch in entry["summary"]
    assert "1 commit(s)" in entry["summary"]
    assert "docs: rewrite readme" in entry["summary"]
    assert "README.md" in entry["summary"]
    assert entry["source"]["commit"] == sha
    assert entry["status"] == "proposed" and entry["confidence"] == "inferred"
    assert set(entry["tags"]) >= {"progress", "task-finished", task.id}


def test_a_finished_task_is_proposed_once(repo: Path) -> None:
    hook.scan(repo, SESSION, now=NOW)
    task = work.start(repo, "Change the readme", SESSION, ["README.md"], now=NOW)
    _commit(repo, "README.md", "docs: rewrite readme")
    work.finish(repo, task.id, SESSION, now=NOW)

    hook.scan(repo, SESSION, now=NOW)
    hook.scan(repo, SESSION, now=NOW)

    assert len(_entries(repo)) == 1
    marker = work.load_session_marker(work.repository_for(repo), SESSION)
    assert marker["reported_tasks"] == [task.id]


def test_a_finished_task_is_not_re_proposed_after_the_marker_is_deleted(repo: Path) -> None:
    task = work.start(repo, "Change the readme", SESSION, ["README.md"], now=NOW)
    work.finish(repo, task.id, SESSION, now=NOW)
    hook.scan(repo, SESSION, now=NOW)
    work.session_marker_path(work.repository_for(repo), SESSION).unlink()

    hook.scan(repo, SESSION, now=NOW)

    assert len(_entries(repo)) == 1


def test_another_sessions_finished_task_is_not_this_sessions_progress(repo: Path) -> None:
    task = work.start(repo, "Change the readme", "session-b", ["README.md"], now=NOW)
    work.finish(repo, task.id, "session-b", now=NOW)

    result = hook.scan(repo, SESSION, now=NOW)

    assert result.proposed == [] and _entries(repo) == []


def test_a_task_finished_without_commits_still_records_its_span(repo: Path) -> None:
    task = work.start(repo, "Change the readme", SESSION, ["README.md"], now=NOW)
    work.finish(repo, task.id, SESSION, now=NOW)

    hook.scan(repo, SESSION, now=NOW)

    [entry] = _entries(repo)
    assert "0 commit(s)" in entry["summary"]


# --- nothing here can block a session --------------------------------------


def test_an_unenrolled_project_is_skipped_without_writing(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    result = hook.scan(bare, SESSION, now=NOW)
    assert result.skipped == "project is not enrolled"
    assert not (Path.home() / ".agent-harness").exists()


def test_a_blank_session_is_skipped(repo: Path) -> None:
    assert hook.scan(repo, "  ", now=NOW).skipped == "no session id"


def test_an_enrolled_directory_that_is_not_a_repository_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    result = hook.scan(root, SESSION, now=NOW)
    assert result.proposed == []
    assert result.skipped is not None  # git said no; the reason is kept, not raised


def test_scan_swallows_a_harness_bug(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("simulated harness bug")

    monkeypatch.setattr(work, "repository_for", explode)
    result = hook.scan(repo, SESSION, now=NOW)
    assert "simulated harness bug" in result.skipped


def test_main_exits_0_for_every_input(repo: Path, monkeypatch) -> None:
    for raw in ("not json", "[1]", json.dumps({"session_id": 5}), json.dumps({"cwd": str(repo)})):
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        assert hook.main() == 0
    assert _entries(repo) == []


def test_main_exits_0_even_when_scan_raises(repo: Path, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(hook, "scan", explode)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SESSION, "cwd": str(repo)})))
    assert hook.main() == 0


def test_main_scans_the_events_cwd_and_stays_silent(repo: Path, monkeypatch, capsys) -> None:
    hook.scan(repo, SESSION, now=NOW)
    _commit(repo, "a.py", "first")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SESSION, "cwd": str(repo)})))

    assert hook.main() == 0

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert len(_entries(repo)) == 1


# --- entry shape -----------------------------------------------------------


def test_entries_never_exceed_the_summary_limit() -> None:
    commit = work.Commit(sha="a" * 40, subject="x" * 600, files=tuple(f"file{n}.py" for n in range(30)))
    entry = hook.commit_entry(commit, "main", "proj")
    memory_store.validate_entry(entry, now=NOW)  # raises if the shape is wrong
    assert len(entry["summary"]) <= memory_store.MAX_SUMMARY_CHARS
    assert "+22 more" in entry["summary"]
