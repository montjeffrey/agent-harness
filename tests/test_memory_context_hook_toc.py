"""Tests for the SessionStart hook's table-of-contents shape.

`tests/test_memory_wiring.py` pins the behavioural contract (fail open,
adaptive, budgeted). This file pins what changed when the packet became a
table of contents: every section is represented, ids survive clipping, the
session id is printed, and the session marker is recorded once.
"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_store, work
from hooks.claude import memory_context_hook as hook

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

SCOPE = """# Project scope

## Purpose

A small project used to test the table of contents.

## Boundaries

- Nothing outside the tests.

## Active objective

Ship the table of contents.
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(hook.STRATEGY_ENV, raising=False)
    monkeypatch.delenv(hook.MAX_CHARS_ENV, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    (root / ".agent-harness" / "scope.md").write_text(SCOPE, encoding="utf-8")
    return root


def _add(project: Path, summary: str, **overrides) -> None:
    entry = {
        "type": "decision",
        "scope": "project",
        "status": "accepted",
        "confidence": "confirmed",
        "source": {"manual": True},
        "summary": summary,
        **overrides,
    }
    memory_store.append_entries([entry], directory=memory_store.project_memory_dir(project), now=NOW)


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# --- shape -----------------------------------------------------------------


def test_the_table_of_contents_has_every_section(project: Path) -> None:
    _add(project, "Telemetry is local-only JSONL.")
    _add(project, "Fix the flaky test.", type="task", status="proposed", confidence="inferred", tags=["tests"])
    _add(project, "CI is down.", type="blocker", status="proposed", confidence="inferred", tags=["tests"])

    text = hook.emit(project)

    assert "# Project context:" in text
    assert "table of contents" in text
    assert "## Scope" in text and "**Purpose:**" in text and "**Active objective:**" in text
    assert "Also in scope.md: Boundaries" in text
    assert "## Open work (2 of 2)" in text
    assert "## Bottlenecks" in text and "tests (2 open, 1 blocking)" in text
    assert "## Knowledge (1 of 1)" in text
    assert "local-only JSONL" in text
    assert "`memory_context`" in text and "`memory_search`" in text


def test_blockers_are_listed_before_tasks(project: Path) -> None:
    _add(project, "Fix the flaky test.", type="task", status="proposed", confidence="inferred")
    _add(project, "CI is down.", type="blocker", status="proposed", confidence="inferred")
    text = hook.emit(project)
    assert text.index("[blocker]") < text.index("[task]")


def test_long_summaries_are_clipped_but_ids_survive(project: Path) -> None:
    _add(project, "Long: " + "word " * 90)
    text = hook.emit(project)
    [line] = [l for l in text.splitlines() if l.startswith("- `mem-")]
    assert len(line) < hook.KNOWLEDGE_CHARS + 60
    assert line.endswith("...")
    assert "`mem-2026-09-11-001`" in line


def test_no_section_starves_another(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seven open tasks must not push every knowledge id out of the packet."""
    for n in range(12):
        _add(project, f"Open task {n}: " + "detail " * 30, type="task", status="proposed", confidence="inferred")
    for n in range(12):
        _add(project, f"Known fact {n}: " + "detail " * 30)

    text = hook.emit(project)

    assert "Open task 0" in text
    assert "Known fact" in text
    assert "## Bottlenecks" not in text  # no tags, so no bottleneck line to show
    assert "more; `memory_context` shows all." in text


def test_the_body_respects_the_budget_and_reports_it(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for n in range(40):
        _add(project, f"Fact {n}: " + "detail " * 30)
    monkeypatch.setenv(hook.MAX_CHARS_ENV, "800")

    text = hook.emit(project)

    header = [l for l in text.splitlines() if l.startswith("_Generated")][0]
    spent = int(header.split("table of contents, ")[1].split("/")[0])
    assert spent <= 800
    assert len(text) <= 800 + 600


def test_the_session_id_is_printed_only_when_known(project: Path) -> None:
    _add(project, "Something recorded.")
    assert "_Session" not in hook.emit(project)
    text = hook.emit(project, session_id="abc-123")
    assert "_Session `abc-123`" in text and "work_start" in text


def test_the_hook_never_loads_the_code_graph(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import memory_graph

    def boom(*args, **kwargs):
        raise AssertionError("the table of contents must not pay for the graph")

    monkeypatch.setattr(memory_graph, "build_annotator", boom)
    _add(project, "Something recorded.")
    assert "Something recorded." in hook.emit(project)


# --- the session marker ----------------------------------------------------


@pytest.fixture
def git_project(project: Path) -> Path:
    _run(project, "init")
    _run(project, "config", "user.name", "Test User")
    _run(project, "config", "user.email", "test@example.test")
    (project / "README.md").write_text("# Test\n", encoding="utf-8")
    _run(project, "add", "README.md")
    _run(project, "commit", "-m", "initial")
    return project


def test_main_records_the_sessions_starting_head_from_the_stdin_event(
    git_project: Path, monkeypatch, capsys
) -> None:
    _add(git_project, "Something recorded.")
    event = {"session_id": "sess-1", "cwd": str(git_project), "hook_event_name": "SessionStart"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    assert hook.main([]) == 0

    out = capsys.readouterr().out
    assert "_Session `sess-1`" in out
    marker = work.load_session_marker(work.repository_for(git_project), "sess-1")
    assert marker["head"] == _run(git_project, "rev-parse", "HEAD")
    assert marker["reported_tasks"] == []


def test_a_resumed_session_keeps_its_original_marker(git_project: Path, monkeypatch) -> None:
    _add(git_project, "Something recorded.")
    event = json.dumps({"session_id": "sess-1", "cwd": str(git_project)})
    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    hook.main([])
    first = _run(git_project, "rev-parse", "HEAD")
    (git_project / "more.txt").write_text("x\n", encoding="utf-8")
    _run(git_project, "add", "more.txt")
    _run(git_project, "commit", "-m", "later")

    monkeypatch.setattr("sys.stdin", io.StringIO(event))
    hook.main([])

    assert work.load_session_marker(work.repository_for(git_project), "sess-1")["head"] == first


def test_record_session_start_is_a_no_op_outside_an_enrolled_project(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert hook.record_session_start(bare, "sess-1") is None
    assert not (Path.home() / ".agent-harness").exists()


def test_main_still_emits_when_the_marker_cannot_be_written(project: Path, monkeypatch, capsys) -> None:
    """An enrolled project that is not a git repository: the marker fails,
    the packet does not."""
    _add(project, "Something recorded.")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-1", "cwd": str(project)})))
    assert hook.main([]) == 0
    assert "Something recorded." in capsys.readouterr().out


def test_main_tolerates_garbage_on_stdin(project: Path, monkeypatch, capsys) -> None:
    _add(project, "Something recorded.")
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.main([str(project)]) == 0
    assert "Something recorded." in capsys.readouterr().out
