"""Tests for the shared graph-location contract (harness.graph_location):
repo-bound discovery and fresh/stale/dirty/invalid health reporting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import graph_location
from harness.graph_location import GraphHealth


def _write_graph(root: Path, built_at: str | None = None) -> Path:
    out = root / "graphify-out"
    out.mkdir(parents=True)
    payload: dict = {"nodes": [], "links": []}
    if built_at is not None:
        payload["built_at_commit"] = built_at
    graph = out / "graph.json"
    graph.write_text(json.dumps(payload), encoding="utf-8")
    return graph


def _make_git_marker(root: Path) -> None:
    (root / ".git").mkdir(parents=True)


# --- repo-bound discovery -------------------------------------------------


def test_find_graph_in_repo_bounded_to_start_without_git_boundary(tmp_path: Path) -> None:
    """HIGH-3: with no `.git` boundary anywhere, discovery must NOT walk
    unbounded -- it only ever checks `start` itself, never an ancestor
    that could belong to an unrelated project (zip extract, pre-`git init`
    checkout, scratch dir under home)."""
    _write_graph(tmp_path)
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert graph_location.find_graph_in_repo(nested) is None


def test_find_graph_in_repo_finds_graph_at_start_without_git_boundary(tmp_path: Path) -> None:
    graph = _write_graph(tmp_path)
    assert graph_location.find_graph_in_repo(tmp_path) == graph


def test_find_graph_in_repo_returns_none_when_missing(tmp_path: Path) -> None:
    empty_dir = tmp_path / "no_graph_here"
    empty_dir.mkdir()
    assert graph_location.find_graph_in_repo(empty_dir) is None


def test_find_graph_in_repo_does_not_borrow_unrelated_ancestor_graph_several_levels_up(
    tmp_path: Path,
) -> None:
    """No `.git` anywhere: a graph.json several levels up (e.g. left behind
    by an unrelated project or a scratch checkout) must never be borrowed."""
    _write_graph(tmp_path)
    deeply_nested = tmp_path / "unrelated" / "scratch" / "workdir"
    deeply_nested.mkdir(parents=True)
    assert graph_location.find_graph_in_repo(deeply_nested) is None


def test_find_graph_in_repo_resolves_symlinks_and_stays_within_boundary(tmp_path: Path) -> None:
    real_repo = tmp_path / "real_repo"
    _make_git_marker(real_repo)
    graph = _write_graph(real_repo)

    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "link_to_repo"
    try:
        link.symlink_to(real_repo, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported/permitted in this environment")

    found = graph_location.find_graph_in_repo(link)
    assert found == graph
    # the resolved graph must live inside the real repo boundary, not
    # wherever the (unresolved) symlink happened to sit
    assert real_repo.resolve() in found.resolve().parents


def test_nested_repo_never_borrows_parent_graph(tmp_path: Path) -> None:
    """A nested repo (its own .git) must not resolve to an outer repo's graph."""
    parent = tmp_path / "parent"
    _make_git_marker(parent)
    _write_graph(parent)

    nested_repo = parent / "vendor" / "nested-repo"
    _make_git_marker(nested_repo)
    start = nested_repo / "src"
    start.mkdir(parents=True)

    assert graph_location.find_graph_in_repo(start) is None


def test_nested_repo_finds_its_own_graph_not_parents(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    _make_git_marker(parent)
    _write_graph(parent, built_at="parentcommit")

    nested_repo = parent / "vendor" / "nested-repo"
    _make_git_marker(nested_repo)
    own_graph = _write_graph(nested_repo, built_at="nestedcommit")

    start = nested_repo / "src"
    start.mkdir(parents=True)

    found = graph_location.find_graph_in_repo(start)
    assert found == own_graph


def test_find_repo_root_stops_at_nearest_git_marker(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    _make_git_marker(parent)
    nested_repo = parent / "vendor" / "nested-repo"
    _make_git_marker(nested_repo)
    start = nested_repo / "src"
    start.mkdir(parents=True)

    assert graph_location.find_repo_root(start) == nested_repo


def test_find_repo_root_returns_none_with_no_git_marker(tmp_path: Path) -> None:
    empty_dir = tmp_path / "no_git_here"
    empty_dir.mkdir()
    assert graph_location.find_repo_root(empty_dir) is None


# --- health reporting -------------------------------------------------


def test_report_health_missing_graph(tmp_path: Path) -> None:
    health = graph_location.report_health(tmp_path / "graphify-out" / "graph.json")
    assert health == GraphHealth.MISSING


def test_report_health_invalid_for_corrupt_json(tmp_path: Path) -> None:
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_text('{"nodes": [truncated garbage', encoding="utf-8")
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.INVALID


def test_report_health_invalid_for_wrong_shape(tmp_path: Path) -> None:
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.INVALID


def test_report_health_never_reports_fresh_for_invalid(tmp_path: Path, monkeypatch) -> None:
    """Regression guard: even if git state looks pristine, corrupt JSON must
    never be reported as fresh."""
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: "a" * 40)
    assert graph_location.report_health(graph, tmp_path) != GraphHealth.FRESH
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.INVALID


def test_report_health_dirty_when_working_tree_has_changes(tmp_path: Path, monkeypatch) -> None:
    graph = _write_graph(tmp_path, built_at="a" * 40)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: True)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: "a" * 40)
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.DIRTY


def test_report_health_never_reports_fresh_when_dirty(tmp_path: Path, monkeypatch) -> None:
    """Regression guard: a dirty working tree must never be reported fresh,
    even when built_at_commit matches HEAD exactly."""
    head = "a" * 40
    graph = _write_graph(tmp_path, built_at=head)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: True)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: head)
    assert graph_location.report_health(graph, tmp_path) != GraphHealth.FRESH


def test_report_health_stale_when_commit_mismatches(tmp_path: Path, monkeypatch) -> None:
    graph = _write_graph(tmp_path, built_at="dead" + "b" * 36)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: "f" * 40)
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.STALE


def test_report_health_fresh_when_clean_and_matching(tmp_path: Path, monkeypatch) -> None:
    head = "ab12cd34" + "0" * 32
    graph = _write_graph(tmp_path, built_at=head[:12])
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: head)
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.FRESH


def test_report_health_unknown_when_git_state_cannot_be_determined(tmp_path: Path) -> None:
    """HIGH-1 regression guard: a REAL non-git tmp_path (no `.git` anywhere,
    `git status`/`git rev-parse` will both fail) with a valid graph present
    must never be silently reported FRESH. No monkeypatching of the git
    helpers here -- this exercises the real subprocess path."""
    graph = _write_graph(tmp_path, built_at="a" * 40)
    health = graph_location.report_health(graph, tmp_path)
    assert health != GraphHealth.FRESH
    assert health == GraphHealth.UNKNOWN


def test_report_health_unknown_when_dirty_undetermined_but_head_known(tmp_path: Path, monkeypatch) -> None:
    graph = _write_graph(tmp_path, built_at="a" * 40)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: None)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: "a" * 40)
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.UNKNOWN


def test_report_health_unknown_when_dirty_false_but_head_unknown(tmp_path: Path, monkeypatch) -> None:
    graph = _write_graph(tmp_path, built_at="a" * 40)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: None)
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.UNKNOWN


def test_load_graph_metadata_returns_none_for_invalid_utf8(tmp_path: Path) -> None:
    """MEDIUM-1 regression guard: invalid UTF-8 bytes must degrade to
    INVALID (None), never raise UnicodeDecodeError."""
    path = tmp_path / "graph.json"
    path.write_bytes(b"\xff\xfe not valid utf-8 at all")
    assert graph_location.load_graph_metadata(path) is None


def test_report_health_invalid_for_binary_garbage_not_a_crash(tmp_path: Path) -> None:
    out = tmp_path / "graphify-out"
    out.mkdir()
    graph = out / "graph.json"
    graph.write_bytes(b"\xff\xfe\x00\x01binary garbage")
    assert graph_location.report_health(graph, tmp_path) == GraphHealth.INVALID


def test_load_graph_metadata_returns_none_for_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text('{"nodes": [{"id": "n1"', encoding="utf-8")
    assert graph_location.load_graph_metadata(path) is None


def test_load_graph_metadata_parses_valid_graph(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text(json.dumps({"nodes": [], "links": [], "built_at_commit": "abc123"}), encoding="utf-8")
    metadata = graph_location.load_graph_metadata(path)
    assert metadata is not None
    assert metadata["built_at_commit"] == "abc123"
