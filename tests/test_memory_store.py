"""Tests for harness.memory_store: entry-contract validation, secret
rejection, stable ID allocation, JSONL append, tolerant reading, and the
project/machine storage layouts.

Every test monkeypatches HOME/USERPROFILE to `tmp_path` so nothing here ever
touches the real `~/.agent-harness/memory/` directory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_store

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


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


def _entry(**overrides: object) -> dict:
    base = {
        "type": "decision",
        "scope": "project",
        "summary": "Use JSONL for the proposal inbox.",
    }
    base.update(overrides)
    return base


# --- storage layout -------------------------------------------------------


def test_machine_memory_dir_is_under_home(fake_home: Path) -> None:
    assert memory_store.machine_memory_dir() == fake_home / ".agent-harness" / "memory"


def test_project_memory_dir_is_repo_relative(project: Path) -> None:
    assert memory_store.project_memory_dir(project) == project / ".agent-harness" / "memory"


# --- entry contract -------------------------------------------------------


def test_validate_entry_fills_schema_ts_status_and_confidence() -> None:
    rec = memory_store.validate_entry(_entry(), now=NOW)
    assert rec["schema"] == memory_store.SCHEMA_VERSION
    assert rec["ts"] == "2026-07-22T12:00:00Z"
    assert rec["status"] == "proposed"
    assert rec["confidence"] == "inferred"


def test_validate_entry_never_mutates_input() -> None:
    original = _entry()
    memory_store.validate_entry(original, now=NOW)
    assert original == _entry()


def test_validate_entry_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="type"):
        memory_store.validate_entry(_entry(type="vibe"), now=NOW)


def test_validate_entry_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        memory_store.validate_entry(_entry(scope="galaxy"), now=NOW)


def test_validate_entry_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="status"):
        memory_store.validate_entry(_entry(status="maybe"), now=NOW)


def test_validate_entry_rejects_missing_summary() -> None:
    with pytest.raises(ValueError, match="summary"):
        memory_store.validate_entry({"type": "decision", "scope": "project"}, now=NOW)


def test_validate_entry_rejects_blank_summary() -> None:
    with pytest.raises(ValueError, match="summary"):
        memory_store.validate_entry(_entry(summary="   "), now=NOW)


def test_validate_entry_rejects_oversized_summary() -> None:
    too_long = "x" * (memory_store.MAX_SUMMARY_CHARS + 1)
    with pytest.raises(ValueError, match="summary"):
        memory_store.validate_entry(_entry(summary=too_long), now=NOW)


def test_validate_entry_accepts_summary_at_the_limit() -> None:
    exact = "x" * memory_store.MAX_SUMMARY_CHARS
    rec = memory_store.validate_entry(_entry(summary=exact), now=NOW)
    assert rec["summary"] == exact


def test_validate_entry_preserves_unknown_fields_for_forward_compat() -> None:
    rec = memory_store.validate_entry(_entry(future_field="keep me"), now=NOW)
    assert rec["future_field"] == "keep me"


def test_validate_entry_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValueError, match="schema"):
        memory_store.validate_entry(_entry(schema=99), now=NOW)


def test_validate_entry_normalizes_tags_to_a_list_of_strings() -> None:
    rec = memory_store.validate_entry(_entry(tags=["a", "b"]), now=NOW)
    assert rec["tags"] == ["a", "b"]


def test_validate_entry_rejects_non_list_tags() -> None:
    with pytest.raises(ValueError, match="tags"):
        memory_store.validate_entry(_entry(tags="a,b"), now=NOW)


# --- the inferred/accepted trust rules ------------------------------------


def test_inferred_entry_cannot_be_accepted() -> None:
    """Design rule: inferred entries remain proposed until reviewed."""
    with pytest.raises(ValueError, match="inferred"):
        memory_store.validate_entry(
            _entry(confidence="inferred", status="accepted"), now=NOW
        )


def test_accepted_entry_requires_a_source_or_manual_confirmation() -> None:
    with pytest.raises(ValueError, match="source"):
        memory_store.validate_entry(
            _entry(confidence="confirmed", status="accepted"), now=NOW
        )


def test_accepted_entry_is_allowed_with_a_commit_source() -> None:
    rec = memory_store.validate_entry(
        _entry(confidence="confirmed", status="accepted", source={"commit": "abc1234"}),
        now=NOW,
    )
    assert rec["status"] == "accepted"


def test_accepted_entry_is_allowed_with_explicit_manual_confirmation() -> None:
    rec = memory_store.validate_entry(
        _entry(
            confidence="confirmed",
            status="accepted",
            source={"manual": True},
        ),
        now=NOW,
    )
    assert rec["status"] == "accepted"


# --- privacy: secrets must never reach an inbox ---------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "key is sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
        "token ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "-----BEGIN RSA PRIVATE KEY-----",
        "password = hunter2please",
    ],
)
def test_validate_entry_rejects_probable_secrets(summary: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        memory_store.validate_entry(_entry(summary=summary), now=NOW)


def test_validate_entry_allows_ordinary_prose_about_keys() -> None:
    """The secret scan must not be so eager that normal sentences trip it."""
    rec = memory_store.validate_entry(
        _entry(summary="Rotate the API key quarterly; store it in the vault."),
        now=NOW,
    )
    assert rec["summary"].startswith("Rotate the API key")


# --- ID allocation --------------------------------------------------------


def test_next_entry_id_starts_at_001_for_a_new_day(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    assert memory_store.next_entry_id(directory, now=NOW) == "mem-2026-07-22-001"


def test_next_entry_id_increments_past_existing_same_day_ids(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_store.append_entries([_entry()], directory=directory, now=NOW)
    memory_store.append_entries([_entry()], directory=directory, now=NOW)
    assert memory_store.next_entry_id(directory, now=NOW) == "mem-2026-07-22-003"


def test_append_entries_assigns_unique_ids_within_one_batch(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_store.append_entries([_entry(), _entry(), _entry()], directory=directory, now=NOW)
    ids = [rec["id"] for rec in memory_store.iter_entries(directory)]
    assert ids == ["mem-2026-07-22-001", "mem-2026-07-22-002", "mem-2026-07-22-003"]


def test_append_entries_respects_a_caller_supplied_id(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_store.append_entries(
        [_entry(id="mem-2026-07-22-042")], directory=directory, now=NOW
    )
    [rec] = list(memory_store.iter_entries(directory))
    assert rec["id"] == "mem-2026-07-22-042"


# --- append / read --------------------------------------------------------


def test_append_entries_creates_dir_and_inbox(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    written = memory_store.append_entries([_entry()], directory=directory, now=NOW)
    assert written == 1
    assert (directory / "inbox.jsonl").exists()


def test_append_entries_validates_all_before_writing_any(project: Path) -> None:
    """One bad entry must abort the whole batch -- no partial writes."""
    directory = memory_store.project_memory_dir(project)
    with pytest.raises(ValueError):
        memory_store.append_entries(
            [_entry(), _entry(type="nonsense")], directory=directory, now=NOW
        )
    assert not (directory / "inbox.jsonl").exists()


def test_append_entries_appends_rather_than_truncating(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_store.append_entries([_entry()], directory=directory, now=NOW)
    memory_store.append_entries([_entry()], directory=directory, now=NOW)
    assert len(list(memory_store.iter_entries(directory))) == 2


def test_append_entries_returns_zero_for_an_empty_batch(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    assert memory_store.append_entries([], directory=directory, now=NOW) == 0


def test_iter_entries_on_missing_directory_yields_nothing(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    assert list(memory_store.iter_entries(directory)) == []


def test_iter_entries_skips_malformed_lines_without_raising(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_store.append_entries([_entry()], directory=directory, now=NOW)
    inbox = directory / "inbox.jsonl"
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
        fh.write("[]\n")
        fh.write('{"schema": 99, "summary": "from the future"}\n')
        fh.write("\n")
    assert len(list(memory_store.iter_entries(directory))) == 1


def test_iter_entries_filters_by_status_type_and_scope(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_store.append_entries(
        [
            _entry(type="decision"),
            _entry(type="blocker"),
            _entry(
                type="lesson",
                confidence="confirmed",
                status="accepted",
                source={"manual": True},
            ),
        ],
        directory=directory,
        now=NOW,
    )
    assert len(list(memory_store.iter_entries(directory, type="blocker"))) == 1
    assert len(list(memory_store.iter_entries(directory, status="accepted"))) == 1
    assert len(list(memory_store.iter_entries(directory, status="proposed"))) == 2
    assert len(list(memory_store.iter_entries(directory, scope="machine"))) == 0


def test_append_entries_writes_one_json_object_per_line(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_store.append_entries([_entry(), _entry()], directory=directory, now=NOW)
    lines = (directory / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert isinstance(json.loads(line), dict)


# --- project initialization ----------------------------------------------


def test_init_project_creates_the_documented_layout(project: Path) -> None:
    memory_store.init_project(project, now=NOW)
    base = project / ".agent-harness"
    assert (base / "scope.md").is_file()
    assert (base / "memory" / "active.md").is_file()
    assert (base / "memory" / "durable.md").is_file()
    assert (base / "memory" / "archive").is_dir()


def test_init_project_is_idempotent_and_preserves_content(project: Path) -> None:
    memory_store.init_project(project, now=NOW)
    scope = project / ".agent-harness" / "scope.md"
    scope.write_text("# My hand-written scope\n", encoding="utf-8")

    memory_store.init_project(project, now=NOW)

    assert scope.read_text(encoding="utf-8") == "# My hand-written scope\n"


def test_init_project_registers_the_project_in_the_machine_registry(project: Path) -> None:
    result = memory_store.init_project(project, now=NOW)
    registry = memory_store.load_registry()
    assert result.project_id in registry["projects"]


def test_project_id_is_stable_across_repeated_init(project: Path) -> None:
    first = memory_store.init_project(project, now=NOW)
    second = memory_store.init_project(project, now=NOW)
    assert first.project_id == second.project_id
    assert len(memory_store.load_registry()["projects"]) == 1


def test_different_paths_get_different_project_ids(tmp_path: Path) -> None:
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    assert memory_store.init_project(a, now=NOW).project_id != (
        memory_store.init_project(b, now=NOW).project_id
    )


def test_load_registry_on_a_fresh_machine_returns_an_empty_registry() -> None:
    registry = memory_store.load_registry()
    assert registry["projects"] == {}


def test_load_registry_tolerates_a_corrupt_registry_file(fake_home: Path) -> None:
    directory = memory_store.machine_memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "registry.json").write_text("{ broken", encoding="utf-8")
    assert memory_store.load_registry()["projects"] == {}
