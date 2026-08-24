"""Smoke tests for `agent-harness-enroll`'s four-step flow.

Graph and index are skipped with `--no-graph --no-index` throughout: this
file is about ordering and wiring between enroll and `memory_ingest`, not
about graphify or chromadb, which have their own test suites.
"""

from __future__ import annotations

from pathlib import Path

from harness import enroll, memory_ingest, memory_store

README_TEXT = """# Widget Factory

A small local-first tool for turning raw widgets into finished widgets.

## Usage

See the docs.
"""


def test_enroll_runs_the_docs_step_and_seeds_scope(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text(README_TEXT, encoding="utf-8")

    code = enroll.main([str(root), "--no-graph", "--no-index"])

    out = capsys.readouterr().out
    assert "[2/4] docs" in out
    assert "proposed" in out

    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(root)))
    assert any(memory_ingest.INGEST_TAG in (e.get("tags") or []) for e in entries)

    scope_text = (root / ".agent-harness" / "scope.md").read_text(encoding="utf-8")
    assert "A small local-first tool" in scope_text
    assert code == 0


def test_enroll_no_docs_flag_skips_ingestion(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text(README_TEXT, encoding="utf-8")

    enroll.main([str(root), "--no-graph", "--no-index", "--no-docs"])

    out = capsys.readouterr().out
    assert "[2/4] docs       skipped (--no-docs)" in out
    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(root)))
    assert entries == []


def test_enroll_is_idempotent_across_two_runs(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text(README_TEXT, encoding="utf-8")

    enroll.main([str(root), "--no-graph", "--no-index"])
    capsys.readouterr()
    enroll.main([str(root), "--no-graph", "--no-index"])
    out = capsys.readouterr().out

    assert "0 proposed" in out
    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(root)))
    tagged = [e for e in entries if memory_ingest.INGEST_TAG in (e.get("tags") or [])]
    assert len(tagged) == 1


def test_enroll_on_a_project_with_no_docs_still_succeeds(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "proj"
    root.mkdir()

    code = enroll.main([str(root), "--no-graph", "--no-index"])

    out = capsys.readouterr().out
    assert "[2/4] docs" in out
    assert code == 0
