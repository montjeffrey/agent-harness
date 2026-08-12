"""Tests for the gitignored working-store tier.

Two claims carry the whole design and both are checked against reality rather
than asserted: git really does ignore the directory on the strength of the
self-ignoring file alone, and the existing memory readers really cannot see
it. Everything else about `work/` is convenience.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_store, work_store

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


# --- placement and creation ----------------------------------------------


def test_work_dir_sits_beside_memory_not_inside_it(project: Path) -> None:
    """A store nested under memory/ would be walked by memory's own readers."""
    work = work_store.work_dir(project)
    assert work == project / ".agent-harness" / "work"
    assert memory_store.project_memory_dir(project) not in work.parents


def test_work_dir_is_not_created_by_resolving_it(project: Path) -> None:
    assert not work_store.work_dir(project).exists()
    assert not work_store.work_path(project, "eval", "a.jsonl").exists()


def test_ensure_writes_a_self_ignoring_gitignore(project: Path) -> None:
    work = work_store.ensure_work_dir(project)
    assert (work / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert work_store.is_ignored(project)


def test_ensure_is_idempotent_and_never_clobbers(project: Path) -> None:
    work = work_store.ensure_work_dir(project)
    (work / ".gitignore").write_text("*\n!keep-me.md\n", encoding="utf-8")
    work_store.ensure_work_dir(project)
    assert (work / ".gitignore").read_text(encoding="utf-8") == "*\n!keep-me.md\n"


def test_ensure_work_path_creates_intermediate_parents(project: Path) -> None:
    target = work_store.ensure_work_path(project, "eval", "results", "run.json")
    assert target.parent.is_dir()
    assert not target.exists()  # the caller writes the file, not us
    assert work_store.is_ignored(project)


# --- containment ----------------------------------------------------------


@pytest.mark.parametrize(
    "parts",
    [
        ("..", "escaped.json"),
        ("eval", "..", "..", "..", "outside.json"),
    ],
)
def test_work_path_refuses_to_escape_the_store(project: Path, parts) -> None:
    """Outside the tier nothing is ignored, so escaping is a leak, not a bug."""
    with pytest.raises(ValueError, match="escapes the working store"):
        work_store.work_path(project, *parts)


def test_work_path_allows_ordinary_nesting(project: Path) -> None:
    target = work_store.work_path(project, "eval", "results", "run.json")
    assert target.name == "run.json"
    assert work_store.work_dir(project) in target.parents


# --- the load-bearing claims ---------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_actually_ignores_the_store_without_touching_root_gitignore(
    project: Path,
) -> None:
    """The whole point: no edit to the host repo's own ignore rules."""
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    work = work_store.ensure_work_dir(project)
    (work / "eval").mkdir()
    (work / "eval" / "default.jsonl").write_text("{}\n", encoding="utf-8")

    assert not (project / ".gitignore").exists()

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".agent-harness/work/eval/default.jsonl"],
        cwd=project,
    )
    assert ignored.returncode == 0

    untracked = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "work" not in untracked.stdout


def test_memory_readers_cannot_see_the_working_store(project: Path) -> None:
    """`iter_entries` resolves one file; a sibling directory is invisible."""
    memory_store.append_entries(
        [
            {
                "type": "decision",
                "scope": "project",
                "status": "accepted",
                "confidence": "confirmed",
                "source": {"manual": True},
                "summary": "a real entry",
            }
        ],
        directory=memory_store.project_memory_dir(project),
        now=NOW,
    )
    decoy = work_store.ensure_work_path(project, "inbox.jsonl")
    decoy.write_text(
        '{"schema": 1, "id": "mem-2026-08-07-999", "type": "decision", '
        '"scope": "project", "status": "accepted", "summary": "decoy", '
        '"ts": "2026-08-07T12:00:00Z"}\n',
        encoding="utf-8",
    )

    summaries = [
        e["summary"]
        for e in memory_store.iter_entries(memory_store.project_memory_dir(project))
    ]
    assert summaries == ["a real entry"]
