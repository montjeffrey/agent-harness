"""Tests for the PreToolUse graph-read guard hook.

Blocks direct `Read` of graphify-out/graph.json and redirects the agent to
harness/graph_query.py instead -- graph.json can be multiple MB and reading
it whole wastes a full context window.
"""

from __future__ import annotations

import io
import json

from hooks.claude import graph_read_guard


def test_is_blocked_matches_posix_style_path() -> None:
    assert graph_read_guard.is_blocked("/repo/graphify-out/graph.json")


def test_is_blocked_matches_windows_style_path() -> None:
    assert graph_read_guard.is_blocked("C:\\repo\\graphify-out\\graph.json")


def test_is_blocked_ignores_unrelated_paths() -> None:
    assert not graph_read_guard.is_blocked("/repo/src/main.py")
    assert not graph_read_guard.is_blocked("/repo/graphify-out/other.json")


def test_is_blocked_ignores_similarly_named_file() -> None:
    assert not graph_read_guard.is_blocked("/repo/graphify-out/graph.json.bak")


def test_main_exits_2_and_writes_guidance_when_blocked(monkeypatch, capsys) -> None:
    payload = json.dumps({"tool_input": {"file_path": "/repo/graphify-out/graph.json"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    exit_code = graph_read_guard.main()
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "graph_query.py" in captured.err


def test_main_exits_0_when_not_blocked(monkeypatch, capsys) -> None:
    payload = json.dumps({"tool_input": {"file_path": "/repo/src/main.py"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    exit_code = graph_read_guard.main()
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""


def test_main_never_crashes_on_malformed_stdin(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    exit_code = graph_read_guard.main()
    assert exit_code == 0


def test_main_never_crashes_on_missing_tool_input(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({})))
    exit_code = graph_read_guard.main()
    assert exit_code == 0


def test_is_blocked_matches_mixed_case() -> None:
    assert graph_read_guard.is_blocked("/repo/graphify-out/GRAPH.JSON")
    assert graph_read_guard.is_blocked("/repo/GRAPHIFY-OUT/graph.json")
    assert graph_read_guard.is_blocked("C:\\repo\\GraphifY-Out\\Graph.Json")


def test_is_blocked_ignores_segment_boundary_lookalikes() -> None:
    assert not graph_read_guard.is_blocked("/repo/subgraphify-out/graph.json")
    assert not graph_read_guard.is_blocked("/repo/my-graphify-out/graph.json")


def test_is_blocked_matches_dotdot_normalized_path() -> None:
    assert graph_read_guard.is_blocked("/repo/graphify-out/x/../graph.json")


def test_is_blocked_matches_doubled_separators() -> None:
    assert graph_read_guard.is_blocked("/repo/graphify-out//graph.json")


def test_is_blocked_matches_trailing_space_and_dot() -> None:
    assert graph_read_guard.is_blocked("/repo/graphify-out/graph.json ")
    assert graph_read_guard.is_blocked("/repo/graphify-out/graph.json.")


def test_main_never_crashes_on_invalid_utf8_stdin(monkeypatch) -> None:
    raw = io.TextIOWrapper(io.BytesIO(b"\xff\xfe not valid utf-8"), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", raw)
    exit_code = graph_read_guard.main()
    assert exit_code == 0
