"""Tests for review: the only path from `proposed` to `accepted`.

Covers `harness.memory_review` transitions and the `review` CLI surface.
Exit codes are contract: 0 success, 1 nothing to do / not found, 2 rejected.

Every test fakes HOME so nothing touches the real `~/.agent-harness/`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory, memory_review, memory_store

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


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
    memory_store.init_project(root, now=NOW)
    return root


def _dir(project: Path) -> Path:
    return memory_store.project_memory_dir(project)


def _remember(project: Path, **overrides) -> str:
    entry = {
        "type": "decision",
        "scope": "project",
        "summary": "Chose JSONL over SQLite for the inbox.",
        **overrides,
    }
    memory_store.append_entries([entry], directory=_dir(project), now=NOW)
    return list(memory_store.iter_entries(_dir(project)))[-1]["id"]


def _entry(project: Path, entry_id: str) -> dict:
    for rec in memory_store.iter_entries(_dir(project)):
        if rec["id"] == entry_id:
            return rec
    raise AssertionError(f"{entry_id} not found")


# --- accept ---------------------------------------------------------------


def test_accept_promotes_to_accepted_and_confirmed(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert result.entry["status"] == "accepted"
    assert result.entry["confidence"] == "confirmed"
    assert _entry(project, entry_id)["status"] == "accepted"


def test_accept_records_the_operator_attestation_as_evidence(project: Path) -> None:
    """Accepting with no commit/file is itself the evidence, and must say so."""
    entry_id = _remember(project)
    result = memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert result.entry["source"]["manual"] is True


def test_accept_prefers_a_supplied_commit_over_manual_attestation(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "accept", commit="a5f6f70", now=NOW
    )
    assert result.entry["source"]["commit"] == "a5f6f70"
    assert "manual" not in result.entry["source"]


def test_accept_can_amend_the_summary(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "accept",
        summary="Inbox is JSONL; durable is Markdown.", now=NOW,
    )
    assert result.entry["summary"] == "Inbox is JSONL; durable is Markdown."


def test_accept_preserves_existing_project_attribution(project: Path) -> None:
    entry_id = _remember(project, source={"project": "proj-abc12345"})
    result = memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert result.entry["source"]["project"] == "proj-abc12345"


# --- reject / resolve -----------------------------------------------------


def test_reject_closes_the_entry_without_accepting_it(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "reject", note="superseded by the design doc", now=NOW
    )
    assert result.entry["status"] == "rejected"
    assert result.entry["review_note"] == "superseded by the design doc"


def test_resolve_requires_evidence(project: Path) -> None:
    entry_id = _remember(project, type="task", summary="Wire up the context command.")
    with pytest.raises(ValueError, match="evidence"):
        memory_review.apply_review(_dir(project), entry_id, "resolve", now=NOW)


def test_resolve_keeps_its_evidence(project: Path) -> None:
    entry_id = _remember(project, type="blocker", summary="Compaction needs a lock.")
    result = memory_review.apply_review(
        _dir(project), entry_id, "resolve", commit="9248b7a", now=NOW
    )
    assert result.entry["status"] == "resolved"
    assert result.entry["resolution"]["commit"] == "9248b7a"


def test_only_tasks_and_blockers_can_be_resolved(project: Path) -> None:
    entry_id = _remember(project, type="decision")
    with pytest.raises(ValueError, match="resolved"):
        memory_review.apply_review(
            _dir(project), entry_id, "resolve", note="done", now=NOW
        )


# --- supersede ------------------------------------------------------------


def test_supersede_preserves_the_original_and_links_the_replacement(
    project: Path,
) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "supersede",
        summary="Switched the inbox from JSONL to SQLite after all.", now=NOW,
    )
    original = _entry(project, entry_id)
    assert original["status"] == "superseded"
    assert original["summary"] == "Chose JSONL over SQLite for the inbox."
    assert original["superseded_by"] == result.created["id"]
    assert result.created["supersedes"] == entry_id
    assert result.created["status"] == "accepted"


def test_supersede_needs_a_replacement_summary(project: Path) -> None:
    entry_id = _remember(project)
    with pytest.raises(ValueError, match="summary"):
        memory_review.apply_review(_dir(project), entry_id, "supersede", now=NOW)


def test_supersede_carries_the_original_tags_forward(project: Path) -> None:
    entry_id = _remember(project, tags=["storage", "schema"])
    result = memory_review.apply_review(
        _dir(project), entry_id, "supersede", summary="Now SQLite.", now=NOW
    )
    assert result.created["tags"] == ["storage", "schema"]


# --- guards ---------------------------------------------------------------


def test_a_closed_entry_is_not_reviewed_again(project: Path) -> None:
    entry_id = _remember(project)
    memory_review.apply_review(_dir(project), entry_id, "reject", now=NOW)
    with pytest.raises(ValueError, match="already rejected"):
        memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)


def test_an_unknown_entry_id_raises_lookup_error(project: Path) -> None:
    with pytest.raises(LookupError):
        memory_review.apply_review(
            _dir(project), "mem-1999-01-01-001", "accept", now=NOW
        )


def test_an_unknown_action_raises(project: Path) -> None:
    entry_id = _remember(project)
    with pytest.raises(ValueError, match="unknown review action"):
        memory_review.apply_review(_dir(project), entry_id, "vibe", now=NOW)


def test_a_failed_transition_leaves_the_inbox_byte_identical(project: Path) -> None:
    entry_id = _remember(project, type="decision")
    path = _dir(project) / "inbox.jsonl"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        memory_review.apply_review(
            _dir(project), entry_id, "resolve", note="x", now=NOW
        )
    assert path.read_bytes() == before


def test_review_carries_unparseable_lines_across_untouched(project: Path) -> None:
    """A corrupt line elsewhere must survive the review of an unrelated entry."""
    entry_id = _remember(project)
    path = _dir(project) / "inbox.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")

    memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert "{not json at all" in path.read_text(encoding="utf-8")


def test_review_does_not_renumber_surrounding_entries(project: Path) -> None:
    first = _remember(project)
    second = _remember(project, summary="A second, unrelated decision.")
    memory_review.apply_review(_dir(project), first, "accept", now=NOW)
    ids = [r["id"] for r in memory_store.iter_entries(_dir(project))]
    assert ids == [first, second]


# --- store primitives -----------------------------------------------------


def test_write_lines_replaces_atomically_and_leaves_no_temp_file(project: Path) -> None:
    directory = _dir(project)
    memory_store.write_lines(['{"schema": 1}'], directory)
    assert (directory / "inbox.jsonl").read_text(encoding="utf-8") == '{"schema": 1}\n'
    assert not (directory / "inbox.jsonl.tmp").exists()


def test_rewrite_entries_refuses_records_without_an_id(project: Path) -> None:
    with pytest.raises(ValueError, match="id"):
        memory_store.rewrite_entries(
            [{"type": "task", "scope": "project", "summary": "no id"}],
            directory=_dir(project), now=NOW,
        )


def test_rewrite_entries_validates_before_writing_anything(project: Path) -> None:
    _remember(project)
    path = _dir(project) / "inbox.jsonl"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        memory_store.rewrite_entries(
            [{"id": "mem-2026-08-07-009", "type": "vibe", "scope": "project",
              "summary": "bad type"}],
            directory=_dir(project), now=NOW,
        )
    assert path.read_bytes() == before


# --- CLI ------------------------------------------------------------------


def test_review_list_reports_pending_entries(project: Path, capsys) -> None:
    _remember(project)
    assert memory.main(["review", "list", "--project", str(project)]) == 0
    assert "awaiting review" in capsys.readouterr().out


def test_review_list_on_an_empty_inbox_exits_1(project: Path, capsys) -> None:
    assert memory.main(["review", "list", "--project", str(project)]) == 1
    assert "nothing awaiting review" in capsys.readouterr().out


def test_review_accept_through_the_cli(project: Path, capsys) -> None:
    entry_id = _remember(project)
    assert memory.main(["review", "accept", entry_id, "--project", str(project)]) == 0
    assert "accepted" in capsys.readouterr().out
    assert _entry(project, entry_id)["status"] == "accepted"


def test_review_action_without_an_id_exits_2(project: Path, capsys) -> None:
    assert memory.main(["review", "accept", "--project", str(project)]) == 2
    assert "entry id" in capsys.readouterr().err


def test_review_unknown_id_exits_1(project: Path, capsys) -> None:
    code = memory.main(
        ["review", "accept", "mem-1999-01-01-001", "--project", str(project)]
    )
    assert code == 1
    assert "no entry" in capsys.readouterr().err


def test_review_bad_transition_exits_2(project: Path, capsys) -> None:
    entry_id = _remember(project, type="decision")
    code = memory.main(
        ["review", "resolve", entry_id, "--project", str(project), "--note", "done"]
    )
    assert code == 2
    assert "review rejected" in capsys.readouterr().err


def test_review_operates_on_machine_scope_too(project: Path) -> None:
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "summary": "Quote OneDrive paths."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    entry_id = list(
        memory_store.iter_entries(memory_store.machine_memory_dir())
    )[-1]["id"]
    code = memory.main(
        ["review", "accept", entry_id, "--project", str(project), "--scope", "machine"]
    )
    assert code == 0
    machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    assert machine[-1]["status"] == "accepted"


def test_accepted_entries_survive_a_round_trip_through_json(project: Path) -> None:
    entry_id = _remember(project)
    memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    lines = (_dir(project) / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["status"] == "accepted"
