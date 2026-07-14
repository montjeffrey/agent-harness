"""Tests for the SessionStart graph presence/staleness check."""

from __future__ import annotations

import json
from pathlib import Path

from harness import graph_check


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


def test_present_graph_without_git_is_silent(tmp_path: Path, monkeypatch) -> None:
    _write_graph(tmp_path, built_at="a" * 40)
    monkeypatch.setattr(graph_check, "git_head", lambda _root: None)
    assert graph_check.check(tmp_path) is None


def test_fresh_graph_is_silent(tmp_path: Path, monkeypatch) -> None:
    head = "ab12cd34" + "0" * 32
    _write_graph(tmp_path, built_at=head[:12])  # short prefix of HEAD counts as fresh
    monkeypatch.setattr(graph_check, "git_head", lambda _root: head)
    assert graph_check.check(tmp_path) is None


def test_stale_graph_reports_refresh(tmp_path: Path, monkeypatch) -> None:
    _write_graph(tmp_path, built_at="dead" + "b" * 36)
    monkeypatch.setattr(graph_check, "git_head", lambda _root: "f" * 40)
    message = graph_check.check(tmp_path)
    assert message is not None
    assert "STALE" in message
    assert "graph_refresh.py" in message


def test_graph_without_commit_field_is_silent(tmp_path: Path, monkeypatch) -> None:
    _write_graph(tmp_path, built_at=None)
    monkeypatch.setattr(graph_check, "git_head", lambda _root: "f" * 40)
    assert graph_check.check(tmp_path) is None


def test_find_graph_walks_up_from_subdir(tmp_path: Path) -> None:
    graph = _write_graph(tmp_path, built_at="c" * 40)
    subdir = tmp_path / "src" / "deep"
    subdir.mkdir(parents=True)
    assert graph_check.find_graph(subdir) == graph
