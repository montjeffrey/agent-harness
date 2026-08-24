"""Deterministic curation keeps evidence-backed memory out of human queues."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from harness import memory, memory_curation, memory_ingest, memory_store

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _entries(root: Path) -> list[dict]:
    return list(memory_store.iter_entries(memory_store.project_memory_dir(root)))


def _ingest_readme(root: Path, text: str = "# Project\n\nA documented local project.\n") -> None:
    (root / "README.md").write_text(text, encoding="utf-8")
    memory_ingest.ingest(root, now=NOW)


def test_curate_auto_accepts_an_unchanged_ingested_document_pointer(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _ingest_readme(root)

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.accepted == (entry["id"],)
    assert entry["status"] == "accepted"
    assert entry["confidence"] == "confirmed"
    assert entry["source"]["file"] == "README.md"
    assert "manual" not in entry["source"]


def test_curate_keeps_a_changed_document_pointer_for_human_review(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _ingest_readme(root)
    (root / "README.md").write_text("# Project\n\nChanged after ingestion.\n", encoding="utf-8")

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.accepted == ()
    assert result.pending == ((entry["id"], "source file changed since ingestion"),)
    assert entry["status"] == "proposed"


def test_curate_rejects_only_exact_duplicate_proposals(tmp_path: Path) -> None:
    root = _project(tmp_path)
    directory = memory_store.project_memory_dir(root)
    memory_store.append_entries(
        [
            {"type": "lesson", "scope": "project", "summary": "Quote Windows paths."},
            {"type": "lesson", "scope": "project", "summary": "quote windows paths"},
        ],
        directory=directory,
        now=NOW,
    )

    result = memory_curation.curate(root, now=NOW)
    first, second = _entries(root)

    assert result.rejected == (second["id"],)
    assert first["status"] == "proposed"
    assert second["status"] == "rejected"
    assert second["review_note"] == f"automatic curation: exact duplicate of {first['id']}"


def test_curate_leaves_general_agent_observations_pending_and_is_idempotent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    directory = memory_store.project_memory_dir(root)
    memory_store.append_entries(
        [{"type": "discovery", "scope": "project", "summary": "Found a likely caching race."}],
        directory=directory,
        now=NOW,
    )

    first = memory_curation.curate(root, now=NOW)
    second = memory_curation.curate(root, now=NOW)

    assert first.changed is False
    assert first.pending[0][1] == "requires human evidence"
    assert second == first


def test_curate_cli_reports_a_bounded_machine_readable_outcome(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    _ingest_readme(root)

    code = memory.main(["curate", "--project", str(root)])
    output = capsys.readouterr().out

    assert code == 0
    assert "status: success" in output
    assert "auto-accepted 1" in output
    assert "artifacts: accepted=" in output
