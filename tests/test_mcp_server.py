"""Tests for harness.mcp_server: import-safety without the optional `mcp`
SDK, tool dispatch against a synthetic graph, and error mapping.

The `mcp` package is not assumed to be installed in the test environment --
these tests prove `harness.mcp_server` is safe to import (and its dispatch
logic exercisable) even when it is not.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

MCP_SERVER_PATH = Path(__file__).resolve().parent.parent / "harness" / "mcp_server.py"


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


def test_import_without_mcp_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """`import harness.mcp_server` must succeed even when `mcp` is not
    installed/importable -- module-level code must not touch the SDK."""
    monkeypatch.setitem(sys.modules, "mcp", None)  # forces ImportError on `import mcp`
    for mod in list(sys.modules):
        if mod == "harness.mcp_server" or mod.startswith("harness.mcp_server."):
            del sys.modules[mod]

    import harness.mcp_server as mcp_server_reloaded  # noqa: F401 -- import is the assertion

    assert hasattr(mcp_server_reloaded, "dispatch")


def test_main_exits_cleanly_without_sdk(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(sys.modules, "mcp", None)
    from harness import mcp_server

    exit_code = mcp_server.main([])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "pip install agent-harness-kit[mcp]" in captured.err


def test_dispatch_find_returns_joined_lines(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_find", {"term": "createUser", "depth": 1})
    assert isinstance(result, str)
    assert "\n" in result
    assert "n1" in result


def test_dispatch_maps_errors_to_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", tmp_path / "no-such-graph.json")
    result = mcp_server.dispatch("graph_find", {"term": "anything"})
    assert isinstance(result, str)
    assert "graph.json not found" in result


def test_dispatch_reports_malformed_json_graph_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph.json that exists but is not valid JSON must come back as a
    normal text result, not an uncaught json.JSONDecodeError -- dispatch's
    docstring promises it never raises for expected failure modes."""
    from harness import mcp_server

    bad_graph = tmp_path / "graph.json"
    bad_graph.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", bad_graph)
    result = mcp_server.dispatch("graph_find", {"term": "anything"})
    assert isinstance(result, str)


def test_dispatch_reports_missing_graph_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", tmp_path / "missing.json")
    result = mcp_server.dispatch("graph_node", {"term": "n1"})
    assert "graph.json not found" in result
    # never raises -- the caller gets text back, not an exception
    assert isinstance(result, str)


def test_all_seven_tools_exposed() -> None:
    from harness import mcp_server

    assert set(mcp_server.TOOL_TO_COMMAND) == {
        "graph_find",
        "graph_node",
        "graph_callers",
        "graph_callees",
        "graph_impact",
        "graph_file",
        "graph_community",
    }


def test_dispatch_graph_find(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_find", {"term": "createUser"})
    assert "n1" in result and "n3" in result and "n2" not in result


def test_dispatch_graph_node(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_node", {"term": "n1"})
    assert "in-edges: 1  out-edges: 1" in result


def test_dispatch_graph_callers(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_callers", {"term": "n1"})
    assert "1 dependent(s) of n1:" in result
    assert "n3" in result


def test_dispatch_graph_callees(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_callees", {"term": "n1"})
    assert "1 dependency(ies) of n1:" in result
    assert "n2" in result


def test_dispatch_graph_impact(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_impact", {"term": "n2"})
    assert "2 transitive dependent(s) of n2:" in result
    assert "n1" in result and "n3" in result


def test_dispatch_graph_file(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_file", {"term": "email"})
    assert "1 node(s) in files matching 'email':" in result
    assert "n2" in result


def test_dispatch_graph_community(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_community", {"term": "api"})
    assert "1 node(s) in communities matching 'api':" in result
    assert "n3" in result


def test_dispatch_unknown_tool_returns_text_not_raise() -> None:
    from harness import mcp_server

    result = mcp_server.dispatch("not_a_real_tool", {"term": "x"})
    assert "unknown tool" in result


def test_dispatch_invalid_depth_returns_text_not_raise(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_callers", {"term": "n1", "depth": "not-a-number"})
    assert "invalid depth" in result


def test_no_module_level_mcp_import() -> None:
    """Static proof (via `ast`) that no top-level `import mcp` / `from mcp
    import ...` exists in mcp_server.py -- the SDK import must live inside a
    function (main/_build_server), never at module scope."""
    tree = ast.parse(MCP_SERVER_PATH.read_text(encoding="utf-8"))
    for node in tree.body:  # only top-level statements, not nested in functions
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.split(".")[0] == "mcp", (
                    f"module-level 'import {alias.name}' found -- must be inside a function"
                )
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.split(".")[0] == "mcp", (
                f"module-level 'from {node.module} import ...' found -- must be inside a function"
            )


def test_parse_argv_extracts_graph_override(tmp_path: Path) -> None:
    from harness import mcp_server

    graph_path = tmp_path / "graph.json"
    result = mcp_server._parse_argv(["--graph", str(graph_path)])
    assert result == graph_path


def test_parse_argv_returns_none_without_graph_flag() -> None:
    from harness import mcp_server

    assert mcp_server._parse_argv([]) is None


def test_module_importable_as_subprocess_without_mcp_installed() -> None:
    """End-to-end proof, in a fresh interpreter, that `import harness.mcp_server`
    does not require `mcp` to be installed (belt-and-suspenders alongside the
    ast-based static check and the monkeypatched in-process test)."""
    repo_root = MCP_SERVER_PATH.parent.parent
    result = subprocess.run(
        [sys.executable, "-c", "import harness.mcp_server; print('ok')"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
