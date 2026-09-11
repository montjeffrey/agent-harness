"""Tests for the PreToolUse claim guard hook.

The guard makes `work_start` path claims binding for Claude Code sessions.
Two properties carry the design and both are pinned here: it denies exactly
one situation -- an active task, a different session, an overlapping path --
and it allows everything else, most importantly every kind of failure.
Denying on an error would turn a harness hiccup into a blocked edit.
"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import work
from hooks.claude import claim_guard

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
OWNER = "session-owner"
OTHER = "session-other"


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


def _event(path: str, session: str | None = OTHER, tool: str = "Edit", cwd: str | None = None) -> dict:
    payload: dict = {"tool_name": tool, "tool_input": {"file_path": path}}
    if session is not None:
        payload["session_id"] = session
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


# --- the one deny -----------------------------------------------------------


def test_denies_a_path_claimed_by_another_sessions_active_task(repo: Path) -> None:
    task = work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)

    verdict = claim_guard.decide(_event(str(repo / "src" / "app.py")))

    assert verdict.allow is False
    assert task.id in verdict.reason
    assert OWNER in verdict.reason
    assert "work_finish" in verdict.reason and "work_status" in verdict.reason
    assert OTHER in verdict.reason  # tells the agent which id this session has


def test_a_directory_claim_covers_files_beneath_it(repo: Path) -> None:
    work.start(repo, "Rework src", OWNER, ["src"], now=NOW)
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"))).allow is False


def test_a_relative_path_is_anchored_at_the_events_cwd(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    verdict = claim_guard.decide(_event("src/app.py", cwd=str(repo)))
    assert verdict.allow is False


def test_a_write_into_a_directory_that_does_not_exist_yet_is_still_checked(repo: Path) -> None:
    work.start(repo, "Rework src", OWNER, ["src"], now=NOW)
    target = repo / "src" / "new" / "deep" / "module.py"
    assert claim_guard.decide(_event(str(target), tool="Write")).allow is False


def test_notebook_edit_uses_notebook_path(repo: Path) -> None:
    work.start(repo, "Notebook", OWNER, ["analysis.ipynb"], now=NOW)
    payload = {
        "tool_name": "NotebookEdit",
        "session_id": OTHER,
        "tool_input": {"notebook_path": str(repo / "analysis.ipynb")},
    }
    assert claim_guard.decide(payload).allow is False


def test_backslash_paths_match_posix_claims(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    windows_style = str(repo / "src" / "app.py").replace("/", "\\")
    assert claim_guard.decide(_event(windows_style)).allow is False


# --- every allow ------------------------------------------------------------


def test_a_session_is_never_blocked_by_its_own_claim(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"), session=OWNER)).allow


def test_a_finished_task_blocks_nobody(repo: Path) -> None:
    task = work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    work.finish(repo, task.id, OWNER, now=NOW)
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"))).allow


def test_an_unclaimed_path_is_allowed_while_other_claims_exist(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    assert claim_guard.decide(_event(str(repo / "README.md"))).allow


def test_editing_inside_a_tasks_own_private_worktree_is_that_tasks_work(repo: Path) -> None:
    """The worktree is the isolation boundary: whoever writes there is doing
    that task, whatever session id the tool call happens to carry."""
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    isolated = work.start(repo, "Change readme", "session-b", ["README.md"], agent="claude", now=NOW)
    assert Path(isolated.worktree) != repo

    inside = Path(isolated.worktree) / "README.md"
    assert claim_guard.decide(_event(str(inside), session="session-c")).allow
    # ... but the same path in the primary checkout is still guarded.
    assert claim_guard.decide(_event(str(repo / "README.md"), session="session-c")).allow is False


def test_no_session_id_means_no_different_session_and_therefore_allow(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"), session=None)).allow
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"), session="   ")).allow


def test_tools_the_guard_does_not_cover_pass_through(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"), tool="Read")).allow


def test_a_file_outside_any_repository_is_allowed(repo: Path, tmp_path: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    elsewhere = tmp_path / "elsewhere" / "src" / "app.py"
    elsewhere.parent.mkdir(parents=True)
    assert claim_guard.decide(_event(str(elsewhere))).allow


def test_no_work_directory_means_allow_without_touching_git(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first `work_start` on a machine creates the work directory; until
    then there is nothing to check and no git call is worth paying for."""

    def boom(*args, **kwargs):
        raise AssertionError("git must not be consulted")

    monkeypatch.setattr(work, "repository_for", boom)
    assert not (Path.home() / ".agent-harness" / "work").exists()
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"))).allow


# --- fail open: every error is an allow ----------------------------------


def test_unreadable_task_state_allows(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    state = work.state_path(work.repository_for(repo))
    state.write_text("{not json", encoding="utf-8")
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"))).allow


def test_an_unsupported_state_schema_allows(repo: Path) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    state = work.state_path(work.repository_for(repo))
    state.write_text(json.dumps({"schema": 99, "tasks": []}), encoding="utf-8")
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"))).allow


def test_an_unexpected_harness_exception_allows(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)

    def explode(repo):
        raise RuntimeError("simulated harness bug")

    monkeypatch.setattr(work, "active_tasks", explode)
    assert claim_guard.decide(_event(str(repo / "src" / "app.py"))).allow


def test_a_path_the_claim_grammar_rejects_allows(repo: Path) -> None:
    """`normalize_paths` refuses wildcard characters; a real file named that
    way is unusual but must not become an unexplained block."""
    work.start(repo, "Rework src", OWNER, ["src"], now=NOW)
    assert claim_guard.decide(_event(str(repo / "src" / "odd[1].py"))).allow


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool_input": "not a dict"},
        {"tool_input": {}},
        {"tool_input": {"file_path": 42}},
        {"tool_input": {"file_path": ""}},
        [],
        None,
        "string",
    ],
)
def test_malformed_events_allow(payload) -> None:
    assert claim_guard.decide(payload).allow


# --- process contract: same conventions as graph_read_guard ---------------


def test_main_exits_2_and_writes_guidance_when_denied(repo: Path, monkeypatch, capsys) -> None:
    task = work.start(repo, "Change the app", OWNER, ["src/app.py"], now=NOW)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(str(repo / "src" / "app.py")))))

    exit_code = claim_guard.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert task.id in captured.err
    assert captured.out == ""


def test_main_exits_0_silently_when_allowed(repo: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_event(str(repo / "README.md")))))
    assert claim_guard.main() == 0
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""


def test_main_never_crashes_on_malformed_stdin(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert claim_guard.main() == 0


def test_main_never_crashes_on_invalid_utf8_stdin(monkeypatch) -> None:
    raw = io.TextIOWrapper(io.BytesIO(b"\xff\xfe not valid utf-8"), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", raw)
    assert claim_guard.main() == 0


def test_main_never_crashes_on_non_object_json(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2, 3]"))
    assert claim_guard.main() == 0


# --- registration ----------------------------------------------------------


def _fragment() -> dict:
    import importlib.resources as resources

    return json.loads(
        (resources.files("hooks.claude") / "settings-fragment.json").read_text(encoding="utf-8")
    )


def test_the_fragment_registers_the_guard_on_edit_and_write_with_a_timeout() -> None:
    entries = _fragment()["hooks"]["PreToolUse"]
    [entry] = [e for e in entries if "claim_guard.py" in e["hooks"][0]["command"]]
    matcher = entry["matcher"].split("|")
    assert "Edit" in matcher and "Write" in matcher
    assert entry["hooks"][0]["timeout"] > 0
    # The graph read guard keeps its own, narrower matcher.
    [read_guard] = [e for e in entries if "graph_read_guard.py" in e["hooks"][0]["command"]]
    assert read_guard["matcher"] == "Read"


def test_the_fragment_registers_progress_capture_on_stop_and_session_end() -> None:
    hooks = _fragment()["hooks"]
    for event in ("Stop", "SessionEnd"):
        commands = [h for entry in hooks[event] for h in entry["hooks"]]
        [progress] = [h for h in commands if "progress_capture_hook.py" in h["command"]]
        assert progress["timeout"] > 0
