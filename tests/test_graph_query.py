"""Tests for harness.graph_query against a small synthetic graph.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import graph_query


@pytest.fixture()
def graph_file(tmp_path: Path) -> Path:
    graph = {
        "built_at_commit": "deadbeef",
        "nodes": [
            {
                "id": "n1",
                "label": "createUser",
                "source_file": "src/users/create.py",
                "source_location": "10-20",
                "community_name": "users",
                "metadata": {"kind": "function"},
            },
            {
                "id": "n2",
                "label": "sendWelcomeEmail",
                "source_file": "src/email/welcome.py",
                "source_location": "5-15",
                "community_name": "email",
                "metadata": {"kind": "function"},
            },
            {
                "id": "n3",
                "label": "createUserHandler",
                "source_file": "src/api/handlers.py",
                "source_location": "1-9",
                "community_name": "api",
                "metadata": {},
            },
        ],
        "links": [
            {"source": "n3", "target": "n1", "relation": "calls"},
            {"source": "n1", "target": "n2", "relation": "calls"},
        ],
    }
    path = tmp_path / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def test_find_graph_path_walks_up(tmp_path: Path, graph_file: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    found = graph_query.find_graph_path(nested)
    assert found == graph_file


def test_find_graph_path_missing_raises(tmp_path: Path) -> None:
    empty_dir = tmp_path / "no_graph_here"
    empty_dir.mkdir()
    with pytest.raises(SystemExit):
        graph_query.find_graph_path(empty_dir)


def test_cmd_find_matches_by_label(graph_file: Path) -> None:
    lines = graph_query.run(["--graph", str(graph_file), "find", "createUser"])
    assert "2 node(s) match 'createUser':" in lines[0]
    joined = "\n".join(lines)
    assert "n1" in joined
    assert "n3" in joined
    assert "n2" not in joined


def test_cmd_node_details(graph_file: Path) -> None:
    lines = graph_query.run(["--graph", str(graph_file), "node", "n1"])
    joined = "\n".join(lines)
    assert "n1" in joined
    assert "in-edges: 1  out-edges: 1" in joined


def test_cmd_callers(graph_file: Path) -> None:
    lines = graph_query.run(["--graph", str(graph_file), "callers", "n1"])
    assert "1 dependent(s) of n1:" in lines[0]
    assert any("n3" in line for line in lines)


def test_cmd_callees(graph_file: Path) -> None:
    lines = graph_query.run(["--graph", str(graph_file), "callees", "n1"])
    assert "1 dependency(ies) of n1:" in lines[0]
    assert any("n2" in line for line in lines)


def test_cmd_impact_transitive(graph_file: Path) -> None:
    # n3 -> n1 -> n2, so impact of n2 should include both n1 (direct) and n3 (transitive).
    lines = graph_query.run(["--graph", str(graph_file), "impact", "n2"])
    assert "2 transitive dependent(s) of n2:" in lines[0]
    joined = "\n".join(lines)
    assert "n1" in joined
    assert "n3" in joined


def test_cmd_file(graph_file: Path) -> None:
    lines = graph_query.run(["--graph", str(graph_file), "file", "email"])
    assert "1 node(s) in files matching 'email':" in lines[0]
    assert any("n2" in line for line in lines)


def test_cmd_community(graph_file: Path) -> None:
    lines = graph_query.run(["--graph", str(graph_file), "community", "api"])
    assert "1 node(s) in communities matching 'api':" in lines[0]
    assert any("n3" in line for line in lines)


def test_resolve_id_ambiguous_raises(graph_file: Path) -> None:
    g = graph_query.load_graph(graph_file)
    index = graph_query.build_index(g)
    with pytest.raises(SystemExit):
        graph_query.resolve_id("create", index.nodes)


def test_main_returns_zero_on_success(graph_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = graph_query.main(["--graph", str(graph_file), "find", "n1"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "n1" in captured.out


def test_main_returns_nonzero_on_missing_node(
    graph_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = graph_query.main(["--graph", str(graph_file), "node", "does-not-exist"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "no node matches" in captured.err
