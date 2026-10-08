"""Smoke tests for `agent-harness-enroll`'s four-step flow.

Graph and index are skipped with `--no-graph --no-index` throughout: this
file is about ordering and wiring between enroll and `memory_ingest`, not
about graphify or chromadb, which have their own test suites.
"""

from __future__ import annotations

import subprocess
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


def test_re_enrolling_advances_last_seen_and_keeps_first_seen(
    tmp_path: Path, capsys
) -> None:
    """Re-enrolment is how a long-lived project says "still here"; without a
    fresh `last_seen` the registry cannot tell it from an abandoned one."""
    root = tmp_path / "proj"
    root.mkdir()

    assert enroll.main([str(root), "--no-graph", "--no-index", "--no-docs"]) == 0
    project_id = memory_store.project_id_for(root)
    registry = memory_store.load_registry()
    stale = "2020-01-01T00:00:00Z"
    registry["projects"][project_id]["first_seen"] = stale
    registry["projects"][project_id]["last_seen"] = stale
    memory_store.save_registry(registry)

    assert enroll.main([str(root), "--no-graph", "--no-index", "--no-docs"]) == 0

    record = memory_store.load_registry()["projects"][project_id]
    assert record["first_seen"] == stale
    assert record["last_seen"] > stale


# --- git exclusion (ADR 0004) -------------------------------------------

QUIET = ["--no-graph", "--no-index", "--no-docs"]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    return path


def test_enroll_excludes_memory_from_git_without_touching_gitignore(
    tmp_path: Path, capsys
) -> None:
    repo = _repo(tmp_path / "repo")

    code = enroll.main([str(repo), *QUIET])

    out = capsys.readouterr().out
    assert code == 0
    assert "excluded .agent-harness/ via" in out
    assert not (repo / ".gitignore").exists()
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_enroll_says_nothing_about_git_outside_a_repository(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()

    assert enroll.main([str(root), *QUIET]) == 0

    captured = capsys.readouterr()
    assert "exclud" not in captured.out
    assert "exclud" not in captured.err
    assert not (root / ".git").exists()


def test_re_enrolling_repairs_a_project_enrolled_before_the_exclusion_existed(
    tmp_path: Path, capsys
) -> None:
    """A project whose memory predates ADR 0004 has storage but no exclusion.
    Enroll is the documented re-runnable repair path, so it must add it."""
    repo = _repo(tmp_path / "repo")
    memory_store.init_project(repo)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# wiped by an older tool\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain") != ""

    code = enroll.main([str(repo), *QUIET])

    out = capsys.readouterr().out
    assert code == 0
    assert "already initialized -- left untouched" in out
    assert "excluded .agent-harness/ via" in out
    assert memory_store.EXCLUDE_LINE in exclude.read_text(encoding="utf-8").splitlines()
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""

    enroll.main([str(repo), *QUIET])
    assert "already excluded via" in capsys.readouterr().out


def test_enroll_reports_but_never_untracks_a_tracked_memory_directory(
    tmp_path: Path, capsys
) -> None:
    repo = _repo(tmp_path / "repo")
    memory_store.init_project(repo)
    _git(repo, "add", "-f", ".agent-harness")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", "memory committed before ADR 0004")
    tracked_before = _git(repo, "ls-files", "--", ".agent-harness")
    assert tracked_before

    code = enroll.main([str(repo), *QUIET])

    captured = capsys.readouterr()
    assert code == 1, "a tracked memory directory needs the operator's attention"
    assert enroll.UNTRACK_COMMAND in captured.err
    assert "git rm -r --cached .agent-harness" in captured.err
    assert "does not do this for you" in captured.err
    # Nothing was untracked, staged, or committed on the operator's behalf.
    assert _git(repo, "ls-files", "--", ".agent-harness") == tracked_before
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / ".gitignore").exists()


def test_enroll_reports_an_unwritable_exclude_as_a_note_and_still_sets_up_memory(
    tmp_path: Path, capsys
) -> None:
    repo = _repo(tmp_path / "repo")
    info = repo / ".git" / "info"
    for child in info.iterdir():
        child.unlink()
    info.rmdir()
    info.write_text("not a directory\n", encoding="utf-8")

    code = enroll.main([str(repo), *QUIET])

    captured = capsys.readouterr()
    assert code == 1
    assert "could not update" in captured.err
    assert (repo / ".agent-harness" / "scope.md").is_file()
    assert "[1/4] memory" in captured.out


# --- semantic index (step 4) --------------------------------------------


def test_enroll_builds_the_semantic_index_and_leaves_a_manifest(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Enrolment is the one operator-driven moment that may touch chromadb,
    and it must leave the manifest `status`/`maintain` read drift from."""
    from test_memory_maintain import FakeClient

    from harness import memory_index, memory_maintain

    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "_default_client", FakeClient)
    FakeClient.stores.clear()
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text(README_TEXT, encoding="utf-8")

    code = enroll.main([str(root), "--no-graph"])

    out = capsys.readouterr().out
    assert code == 0
    assert "[4/4] index      embedded" in out
    assert "project" in out
    index_dir = memory_index.index_dir_for(memory_store.project_id_for(root))
    assert memory_maintain.manifest_path(index_dir).is_file()
    assert memory_maintain.project_drift(root)["project"].pending == 0
    assert "agent-harness-memory triage" in out
    assert "agent-harness-memory maintain" in out


def test_enroll_with_nothing_to_index_says_so(tmp_path: Path, capsys, monkeypatch) -> None:
    from test_memory_maintain import FakeClient

    from harness import memory_index

    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "_default_client", FakeClient)
    root = tmp_path / "proj"
    root.mkdir()
    assert enroll.main([str(root), "--no-graph", "--no-docs"]) == 0
    assert "[4/4] index      nothing to index yet" in capsys.readouterr().out


def test_enroll_degrades_visibly_without_chromadb(tmp_path: Path, capsys, monkeypatch) -> None:
    from harness import memory_index

    monkeypatch.setattr(memory_index, "chromadb_available", lambda: False)
    monkeypatch.setattr(
        memory_index, "chromadb_unavailable_reason", lambda: "ModuleNotFoundError: chromadb"
    )
    root = tmp_path / "proj"
    root.mkdir()

    code = enroll.main([str(root), "--no-graph", "--no-docs"])

    captured = capsys.readouterr()
    assert code == 1
    assert "chromadb is not importable (ModuleNotFoundError: chromadb)" in captured.err
    assert "[semantic]" in captured.err
    assert "agent-harness-memory index" in captured.err
    assert (root / ".agent-harness" / "scope.md").is_file(), "memory is set up regardless"


def test_a_failed_index_build_degrades_without_blocking_enrolment(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from harness import memory_index, memory_maintain

    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model download failed")

    monkeypatch.setattr(memory_maintain, "build_indexes", boom)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text(README_TEXT, encoding="utf-8")

    code = enroll.main([str(root), "--no-graph"])

    captured = capsys.readouterr()
    assert code == 1
    assert "[4/4] index      failed (RuntimeError: model download failed)" in captured.err
    assert "agent-harness-memory index" in captured.err
    assert "Next:" in captured.out
