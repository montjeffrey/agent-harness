"""Tests for seeding memory from a project's existing documentation.

The property that matters most is idempotency: enrolling twice, or running
`ingest` twice, on an unchanged tree must propose nothing the second time.
Everything else here is in service of that -- discovery, parsing, and the
scope.md seed are only correct if a stable rerun is correct.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from harness import memory_ingest, memory_store

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

README_TEXT = """[![CI](https://example.com/badge.svg)](https://example.com)

# Widget Factory

A small local-first tool for turning raw widgets into finished widgets
without a network round trip.

## Installation

Run the installer.

## Usage

See the docs.
"""

ADR_TEXT = """# ADR 0001: Use local storage

Date: 2026-08-01

## Status

Accepted

## Context

We need durable state without a hosted service.

## Decision

Use local JSONL files.
"""


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _write(root: Path, relpath: str, text: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- parsing helpers --------------------------------------------------------


def test_document_title_prefers_the_h1() -> None:
    assert memory_ingest.document_title(Path("readme.md"), README_TEXT) == "Widget Factory"


def test_document_title_falls_back_to_filename() -> None:
    title = memory_ingest.document_title(Path("release-notes.md"), "no heading here")
    assert title == "release notes"


def test_section_headings_are_level_two_only() -> None:
    headings = memory_ingest.section_headings(README_TEXT)
    assert headings == ["Installation", "Usage"]


def test_first_paragraph_skips_badges_and_headings() -> None:
    paragraph = memory_ingest.first_paragraph(README_TEXT)
    assert paragraph.startswith("A small local-first tool")
    assert "badge" not in paragraph.lower()


def test_adr_status_reads_the_status_section() -> None:
    assert memory_ingest.adr_status(ADR_TEXT) == "Accepted"


def test_adr_status_is_blank_when_undeclared() -> None:
    assert memory_ingest.adr_status("# ADR 0002: Something\n\nNo status here.\n") == ""


# --- discovery ---------------------------------------------------------------


def test_discover_finds_root_docs_and_adrs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "README.md", README_TEXT)
    _write(root, "docs/adr/0001-use-local-storage.md", ADR_TEXT)
    _write(root, "docs/extra.md", "# Extra\n\nMore detail.\n")

    found = memory_ingest.discover(root)
    kinds = {p.name: kind for p, kind in found}

    assert kinds["README.md"] == "doc"
    assert kinds["0001-use-local-storage.md"] == "adr"
    assert kinds["extra.md"] == "doc"


def test_discover_deduplicates_overlapping_adr_directories(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "docs/adr/0001-a.md", "# A\n")
    _write(root, "docs/decisions/0002-b.md", "# B\n")

    found = memory_ingest.discover(root)
    names = sorted(p.name for p, _ in found)
    assert names == ["0001-a.md", "0002-b.md"]


def test_discover_ignores_readme_and_template_inside_adr_dir(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "docs/adr/README.md", "# ADR index\n")
    _write(root, "docs/adr/template.md", "# Template\n")
    _write(root, "docs/adr/0001-real.md", ADR_TEXT)

    found = memory_ingest.discover(root)
    names = [p.name for p, kind in found if kind == "adr"]
    assert names == ["0001-real.md"]


def test_discover_caps_at_max_documents(tmp_path: Path) -> None:
    root = _project(tmp_path)
    for i in range(memory_ingest.MAX_DOCUMENTS + 10):
        _write(root, f"docs/note-{i:03d}.md", f"# Note {i}\n\nBody.\n")

    found = memory_ingest.discover(root)
    assert len(found) == memory_ingest.MAX_DOCUMENTS


# --- entry building ------------------------------------------------------


def test_build_entry_for_a_doc_is_a_discovery_pointer(tmp_path: Path) -> None:
    root = _project(tmp_path)
    path = _write(root, "README.md", README_TEXT)
    entry = memory_ingest.build_entry(path, "doc", root, README_TEXT)

    assert entry["type"] == "discovery"
    assert entry["scope"] == "project"
    assert entry["status"] == "proposed"
    assert entry["confidence"] == "inferred"
    assert entry["source"]["file"] == "README.md"
    assert "Widget Factory" in entry["summary"]
    assert "README.md" in entry["summary"]
    assert "Installation" in entry["summary"]
    assert memory_ingest.INGEST_TAG in entry["tags"]


def test_build_entry_for_an_adr_is_a_decision_pointer(tmp_path: Path) -> None:
    root = _project(tmp_path)
    path = _write(root, "docs/adr/0001-use-local-storage.md", ADR_TEXT)
    entry = memory_ingest.build_entry(path, "adr", root, ADR_TEXT)

    assert entry["type"] == "decision"
    assert "ADR 0001" in entry["summary"]
    assert "Use local storage" in entry["summary"]
    assert "[status: Accepted]" in entry["summary"]
    assert "adr" in entry["tags"]


def test_build_entry_for_an_adr_does_not_repeat_its_own_number(tmp_path: Path) -> None:
    # Most ADR templates put "ADR 0001: <title>" in the H1 itself. The
    # summary's own "ADR 0001 records:" prefix must not double it up into
    # "ADR 0001 records: ADR 0001: <title>".
    root = _project(tmp_path)
    path = _write(root, "docs/adr/0001-use-local-storage.md", ADR_TEXT)
    entry = memory_ingest.build_entry(path, "adr", root, ADR_TEXT)

    assert entry["summary"].count("ADR 0001") == 1
    assert "Use local storage" in entry["summary"]


def test_build_entry_summary_never_exceeds_the_store_limit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    long_text = "# " + ("Very long title " * 60) + "\n\nBody prose. " * 60
    path = _write(root, "docs/long.md", long_text)
    entry = memory_ingest.build_entry(path, "doc", root, long_text)

    assert len(entry["summary"]) < memory_store.MAX_SUMMARY_CHARS
    memory_store.validate_entry(entry, now=NOW)  # must not raise


# --- scope.md seeding ------------------------------------------------------


def test_seed_scope_purpose_fills_the_placeholder(tmp_path: Path) -> None:
    root = _project(tmp_path)
    seeded, reason = memory_ingest.seed_scope_purpose(root, README_TEXT)

    assert seeded is True
    text = (root / ".agent-harness" / "scope.md").read_text(encoding="utf-8")
    assert "A small local-first tool" in text
    assert memory_ingest.SCOPE_PURPOSE_PLACEHOLDER not in text
    assert "Seeded from README.md" in text


def test_seed_scope_purpose_never_overwrites_a_human_answer(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scope_path = root / ".agent-harness" / "scope.md"
    scope_path.write_text(
        scope_path.read_text(encoding="utf-8").replace(
            memory_ingest.SCOPE_PURPOSE_PLACEHOLDER, "A human already wrote this."
        ),
        encoding="utf-8",
    )

    seeded, reason = memory_ingest.seed_scope_purpose(root, README_TEXT)

    assert seeded is False
    assert "already written" in reason
    assert "A human already wrote this." in scope_path.read_text(encoding="utf-8")


def test_seed_scope_purpose_dry_run_does_not_write(tmp_path: Path) -> None:
    root = _project(tmp_path)
    seeded, _ = memory_ingest.seed_scope_purpose(root, README_TEXT, dry_run=True)

    assert seeded is True
    text = (root / ".agent-harness" / "scope.md").read_text(encoding="utf-8")
    assert memory_ingest.SCOPE_PURPOSE_PLACEHOLDER in text


def test_seed_scope_purpose_declines_on_thin_readmes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    seeded, reason = memory_ingest.seed_scope_purpose(root, "# Title\n\nHi.\n")

    assert seeded is False
    assert "no usable opening prose" in reason


# --- end-to-end ingest -----------------------------------------------------


def test_ingest_proposes_pointers_and_seeds_scope(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "README.md", README_TEXT)
    _write(root, "docs/adr/0001-use-local-storage.md", ADR_TEXT)

    result = memory_ingest.ingest(root, now=NOW)

    assert result.scanned == 2
    assert len(result.proposed) == 2
    assert result.scope_seeded is True

    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(root)))
    tagged = [e for e in entries if memory_ingest.INGEST_TAG in (e.get("tags") or [])]
    assert len(tagged) == 2


def test_ingest_is_idempotent_on_an_unchanged_tree(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "README.md", README_TEXT)
    _write(root, "docs/adr/0001-use-local-storage.md", ADR_TEXT)

    first = memory_ingest.ingest(root, now=NOW)
    second = memory_ingest.ingest(root, now=NOW)

    assert len(first.proposed) == 2
    assert len(second.proposed) == 0
    assert second.unchanged == 2
    assert second.scope_seeded is False  # placeholder is already gone


def test_ingest_reproposes_a_document_whose_content_changed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "docs/adr/0001-use-local-storage.md", ADR_TEXT)
    memory_ingest.ingest(root, now=NOW)

    changed = ADR_TEXT.replace("Accepted", "Superseded")
    _write(root, "docs/adr/0001-use-local-storage.md", changed)
    second = memory_ingest.ingest(root, now=NOW)

    assert len(second.proposed) == 1
    assert "Superseded" in second.proposed[0]["summary"]


def test_ingest_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "README.md", README_TEXT)

    result = memory_ingest.ingest(root, now=NOW, dry_run=True)

    assert len(result.proposed) == 1
    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(root)))
    assert entries == []
    text = (root / ".agent-harness" / "scope.md").read_text(encoding="utf-8")
    assert memory_ingest.SCOPE_PURPOSE_PLACEHOLDER in text  # untouched


def test_ingest_a_probable_secret_does_not_block_the_rest(tmp_path: Path) -> None:
    # Only a `doc` summary carries body prose (via first_paragraph); an ADR
    # summary carries only its title, status, and path, so a secret has to
    # sit in the opening paragraph of a plain doc to reach validate_entry.
    root = _project(tmp_path)
    _write(root, "README.md", README_TEXT)
    _write(root, "docs/adr/0001-use-local-storage.md", ADR_TEXT)
    leaky_doc = "# Leaky Doc\n\nRotate AKIAABCDEFGHIJKLMNOP before shipping.\n"
    _write(root, "docs/leaky.md", leaky_doc)

    result = memory_ingest.ingest(root, now=NOW)

    proposed_files = {e["source"]["file"] for e in result.proposed}
    assert proposed_files == {"README.md", "docs/adr/0001-use-local-storage.md"}
    assert any("leaky.md" in name for name, _ in result.rejected)


def test_ingest_handles_a_project_with_no_documentation(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = memory_ingest.ingest(root, now=NOW)

    assert result.scanned == 0
    assert result.proposed == []
    assert result.wrote_anything is False


def test_ingest_respects_no_scope_flag(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _write(root, "README.md", README_TEXT)

    result = memory_ingest.ingest(root, now=NOW, seed_scope=False)

    assert result.scope_seeded is False
    text = (root / ".agent-harness" / "scope.md").read_text(encoding="utf-8")
    assert memory_ingest.SCOPE_PURPOSE_PLACEHOLDER in text
