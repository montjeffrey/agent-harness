"""Tests for the SessionStart graph presence/staleness check."""

from __future__ import annotations

import json
from pathlib import Path

from harness import graph_check, graph_location


def _write_graph(root: Path, built_at: str | None = None) -> Path:
    out = root / "graphify-out"
    out.mkdir(parents=True)
    payload: dict = {"nodes": [], "links": []}
    if built_at is not None:
        payload["built_at_commit"] = built_at
    graph = out / "graph.json"
    graph.write_text(json.dumps(payload), encoding="utf-8")
    return graph


def test_missing_graph_reports_build_command(tmp_path: Path) -> None:
    message = graph_check.check(tmp_path)
    assert message is not None
    assert "No code graph found" in message
    assert "graph_refresh.py" in message
    assert "confirm with the operator" in message


def test_present_graph_without_git_reports_unknown_not_silent(tmp_path: Path) -> None:
    """HIGH-1/HIGH-2: no git available (real non-git tmp_path, no
    monkeypatching) must surface a visible 'cannot verify' message -- the
    old behavior silently said nothing, which is exactly the bug fixed
    here."""
    _write_graph(tmp_path, built_at="a" * 40)
    message = graph_check.check(tmp_path)
    assert message is not None
    assert "could not verify" in message.lower()
    assert "graph_refresh.py" in message


def test_fresh_graph_is_silent(tmp_path: Path, monkeypatch) -> None:
    head = "ab12cd34" + "0" * 32
    _write_graph(tmp_path, built_at=head[:12])  # short prefix of HEAD counts as fresh
    monkeypatch.setattr(graph_location, "git_head", lambda _root: head)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    assert graph_check.check(tmp_path) is None


def test_stale_graph_reports_refresh(tmp_path: Path, monkeypatch) -> None:
    _write_graph(tmp_path, built_at="dead" + "b" * 36)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: "f" * 40)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    message = graph_check.check(tmp_path)
    assert message is not None
    assert "STALE" in message
    assert "graph_refresh.py" in message


def test_graph_without_commit_field_is_silent(tmp_path: Path, monkeypatch) -> None:
    _write_graph(tmp_path, built_at=None)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: "f" * 40)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    assert graph_check.check(tmp_path) is None


def test_find_graph_walks_up_from_subdir(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()  # repo boundary -- walk-up must have somewhere to stop
    graph = _write_graph(tmp_path, built_at="c" * 40)
    subdir = tmp_path / "src" / "deep"
    subdir.mkdir(parents=True)
    assert graph_check.find_graph(subdir) == graph


def test_invalid_graph_reports_corrupt_json(tmp_path: Path) -> None:
    out = tmp_path / "graphify-out"
    out.mkdir(parents=True)
    (out / "graph.json").write_text('{"nodes": [truncated', encoding="utf-8")
    message = graph_check.check(tmp_path)
    assert message is not None
    assert "INVALID" in message
    assert "graph_refresh.py" in message


def test_invalid_graph_is_never_reported_fresh(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "graphify-out"
    out.mkdir(parents=True)
    (out / "graph.json").write_text('{"nodes": [truncated', encoding="utf-8")
    monkeypatch.setattr(graph_location, "git_head", lambda _root: "a" * 40)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: False)
    message = graph_check.check(tmp_path)
    assert message is not None
    assert "INVALID" in message


def test_dirty_working_tree_reports_dirty_not_fresh(tmp_path: Path, monkeypatch) -> None:
    head = "a" * 40
    _write_graph(tmp_path, built_at=head)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: head)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: True)
    message = graph_check.check(tmp_path)
    assert message is not None
    assert "uncommitted changes" in message
    assert "graph_refresh.py" in message


def test_dirty_takes_precedence_over_fresh_commit_match(tmp_path: Path, monkeypatch) -> None:
    """Regression guard: even when built_at_commit == HEAD, a dirty working
    tree must never be reported as silently fresh."""
    head = "b" * 40
    _write_graph(tmp_path, built_at=head)
    monkeypatch.setattr(graph_location, "git_head", lambda _root: head)
    monkeypatch.setattr(graph_location, "git_is_dirty", lambda _root: True)
    assert graph_check.check(tmp_path) is not None
