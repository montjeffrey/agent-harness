"""Tests for the `agent-harness-memory` CLI: init, remember, and status.

Exit codes are part of the contract (automation reads them): 0 success,
1 "nothing to do / not initialized", 2 rejected input.

Every test monkeypatches HOME/USERPROFILE to `tmp_path` so nothing here ever
touches the real `~/.agent-harness/memory/` directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import memory, memory_store


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _entries(project: Path) -> list[dict]:
    return list(memory_store.iter_entries(memory_store.project_memory_dir(project)))


# --- init -----------------------------------------------------------------


def test_init_creates_layout_and_reports_the_project_id(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert memory_store.project_id_for(project) in out
    assert (project / ".agent-harness" / "memory" / "durable.md").is_file()


def test_init_is_idempotent(project: Path) -> None:
    assert memory.main(["init", str(project)]) == 0
    assert memory.main(["init", str(project)]) == 0


def test_init_defaults_to_the_current_directory(project: Path, monkeypatch) -> None:
    monkeypatch.chdir(project)
    assert memory.main(["init"]) == 0
    assert (project / ".agent-harness" / "scope.md").is_file()


# --- remember -------------------------------------------------------------


def test_remember_appends_a_proposed_entry(project: Path) -> None:
    memory.main(["init", str(project)])
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Chose JSONL over SQLite for the inbox."]
    )
    assert code == 0
    [rec] = _entries(project)
    assert rec["type"] == "decision"
    assert rec["status"] == "proposed"
    assert rec["confidence"] == "inferred"
    assert rec["id"].startswith("mem-")


def test_remember_works_without_an_explicit_init(project: Path) -> None:
    """Capture must never be blocked by forgetting to initialize first."""
    assert memory.main(
        ["remember", "--project", str(project), "--type", "task",
         "--summary", "Wire the memory CLI into the packaging manifest."]
    ) == 0
    assert len(_entries(project)) == 1


def test_remember_splits_comma_separated_tags(project: Path) -> None:
    memory.main(
        ["remember", "--project", str(project), "--type", "lesson",
         "--summary", "Rebase before merging long-lived branches.",
         "--tags", "git, workflow"]
    )
    [rec] = _entries(project)
    assert rec["tags"] == ["git", "workflow"]


def test_remember_records_commit_and_file_as_source(project: Path) -> None:
    memory.main(
        ["remember", "--project", str(project), "--type", "discovery",
         "--summary", "Telemetry collect was double-counting sessions.",
         "--commit", "9248b7a", "--file", "harness/telemetry.py"]
    )
    [rec] = _entries(project)
    assert rec["source"]["commit"] == "9248b7a"
    assert rec["source"]["file"] == "harness/telemetry.py"


def test_remember_can_accept_a_confirmed_entry_with_manual_confirmation(project: Path) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Gemini telemetry ships in Phase 2.",
         "--confirmed", "--accept", "--manual"]
    )
    assert code == 0
    [rec] = _entries(project)
    assert rec["status"] == "accepted"
    assert rec["confidence"] == "confirmed"


def test_remember_refuses_to_accept_an_inferred_entry(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Something the agent merely guessed.", "--accept"]
    )
    assert code == 2
    assert "inferred" in capsys.readouterr().err
    assert _entries(project) == []


def test_remember_refuses_to_accept_without_a_source(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Confirmed but unsourced.", "--confirmed", "--accept"]
    )
    assert code == 2
    assert "source" in capsys.readouterr().err


def test_remember_rejects_a_probable_secret_and_writes_nothing(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "discovery",
         "--summary", "token ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]
    )
    assert code == 2
    assert "secret" in capsys.readouterr().err
    assert _entries(project) == []


def test_remember_rejects_an_oversized_summary(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "task",
         "--summary", "x" * (memory_store.MAX_SUMMARY_CHARS + 1)]
    )
    assert code == 2
    assert "summary" in capsys.readouterr().err


def test_remember_rejects_an_unknown_type_at_the_argparse_layer(project: Path) -> None:
    with pytest.raises(SystemExit):
        memory.main(["remember", "--project", str(project), "--type", "vibe",
                     "--summary", "nope"])


# --- status ---------------------------------------------------------------


def test_status_on_an_uninitialized_project_says_so(project: Path, capsys) -> None:
    code = memory.main(["status", "--project", str(project)])
    assert code == 1
    assert "init" in capsys.readouterr().out.lower()


def test_status_reports_counts_by_status(project: Path, capsys) -> None:
    memory.main(["init", str(project)])
    memory.main(["remember", "--project", str(project), "--type", "task",
                 "--summary", "Implement weekly compaction."])
    memory.main(["remember", "--project", str(project), "--type", "blocker",
                 "--summary", "Compaction needs a project-scoped lock."])

    assert memory.main(["status", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "2" in out
    assert "proposed" in out


def test_status_warns_while_scope_is_still_the_untouched_template(project: Path, capsys) -> None:
    memory.main(["init", str(project)])
    assert memory.main(["status", "--project", str(project)]) == 0
    assert "scope" in capsys.readouterr().out.lower()
