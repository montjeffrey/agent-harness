"""Tests for harness.mcp_server: import-safety without the optional `mcp`
SDK, tool dispatch against a synthetic graph, and error mapping.

The `mcp` package is not assumed to be installed in the test environment --
these tests prove `harness.mcp_server` is safe to import (and its dispatch
logic exercisable) even when it is not.
"""

from __future__ import annotations

import ast
from collections import OrderedDict
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
    assert 'python -m pip install -e ".[mcp]"' in captured.err


def test_mcp_install_guidance_uses_the_local_checkout_extra() -> None:
    """This source-only project must never direct users to unpublished PyPI extras."""
    source = MCP_SERVER_PATH.read_text(encoding="utf-8")
    assert 'python -m pip install -e ".[mcp]"' in source
    assert "pip install agent-harness-kit[mcp]" not in source


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


def test_dispatch_forwards_memory_mode(graph_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    result = mcp_server.dispatch("graph_find", {"term": "createUser", "memory": "on"})
    assert "[memory]" in result


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


# --- process-local graph cache (MCP only) -----------------------------------


def _use_discovered_graph(
    monkeypatch: pytest.MonkeyPatch, graph_path: Path
) -> None:
    """Configure a dispatch test for the no-override MCP server path."""
    from harness import mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", None)
    monkeypatch.setattr(mcp_server, "_GRAPH_CACHE", OrderedDict())
    monkeypatch.setattr(mcp_server, "_discover_graph_path", lambda: graph_path)


def test_dispatch_reuses_cached_graph_and_index_for_unchanged_graph(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated MCP calls should parse/index one unchanged discovered graph once."""
    from harness import graph_query, mcp_server

    _use_discovered_graph(monkeypatch, graph_file)
    calls = 0
    original_load = graph_query.load_graph

    def counted_load(path: Path) -> dict:
        nonlocal calls
        calls += 1
        return original_load(path)

    monkeypatch.setattr(graph_query, "load_graph", counted_load)

    assert "n1" in mcp_server.dispatch("graph_find", {"term": "createUser", "memory": "off"})
    assert "n1" in mcp_server.dispatch("graph_find", {"term": "createUser", "memory": "off"})
    assert calls == 1


def test_dispatch_reuses_symbol_index_for_unchanged_memory_enabled_graph(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory-enabled MCP calls build each graph-derived index only once."""
    from harness import graph_location, graph_evidence, graph_query, mcp_server, memory_store

    _use_discovered_graph(monkeypatch, graph_file)
    monkeypatch.setattr(graph_location, "report_health", lambda _path: graph_location.GraphHealth.FRESH)
    monkeypatch.setattr(
        memory_store,
        "iter_entries",
        lambda _path: iter(({
            "id": "mem-1",
            "type": "task",
            "status": "accepted",
            "summary": "Update createUser.",
        },)),
    )

    graph_load_calls = 0
    symbol_index_calls = 0
    original_graph_load = graph_query.load_graph
    original_symbol_index = graph_evidence.memory_graph.build_symbol_index

    def counted_graph_load(path: Path) -> dict:
        nonlocal graph_load_calls
        graph_load_calls += 1
        return original_graph_load(path)

    def counted_symbol_index(graph: dict):
        nonlocal symbol_index_calls
        symbol_index_calls += 1
        return original_symbol_index(graph)

    monkeypatch.setattr(graph_query, "load_graph", counted_graph_load)
    monkeypatch.setattr(graph_evidence.memory_graph, "build_symbol_index", counted_symbol_index)

    assert "memory open task" in mcp_server.dispatch(
        "graph_find", {"term": "createUser", "memory": "on"}
    )
    assert "memory open task" in mcp_server.dispatch(
        "graph_find", {"term": "createUser", "memory": "on"}
    )
    assert graph_load_calls == 1
    assert symbol_index_calls == 1


def test_dispatch_refreshes_cached_graph_after_stat_signature_changes(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed graph file must replace—not reuse—the cached graph/index pair."""
    from harness import graph_query, mcp_server

    _use_discovered_graph(monkeypatch, graph_file)
    calls = 0
    original_load = graph_query.load_graph

    def counted_load(path: Path) -> dict:
        nonlocal calls
        calls += 1
        return original_load(path)

    monkeypatch.setattr(graph_query, "load_graph", counted_load)
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})

    changed = json.loads(graph_file.read_text(encoding="utf-8"))
    changed["nodes"][0]["label"] = "createUserAfterRefresh"
    graph_file.write_text(json.dumps(changed, indent=2), encoding="utf-8")

    result = mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert "createUserAfterRefresh" in result
    assert calls == 2


def test_dispatch_clears_failed_cache_entry_and_recovers_when_graph_is_restored(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing or malformed graph files must not leave stale data in the MCP cache."""
    from harness import mcp_server

    _use_discovered_graph(monkeypatch, graph_file)
    original = graph_file.read_text(encoding="utf-8")
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})

    graph_file.unlink()
    assert "graph.json not found" in mcp_server.dispatch(
        "graph_node", {"term": "n1", "memory": "off"}
    )

    graph_file.write_text("{not valid json", encoding="utf-8")
    assert "graph.json malformed" in mcp_server.dispatch(
        "graph_node", {"term": "n1", "memory": "off"}
    )

    graph_file.write_text(original, encoding="utf-8")
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})


def test_dispatch_evicts_changed_graph_when_its_read_fails(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stat-to-open failure must return text and cannot retain stale graph data."""
    from harness import graph_query, mcp_server

    _use_discovered_graph(monkeypatch, graph_file)
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    graph_file.write_text(graph_file.read_text(encoding="utf-8") + " ", encoding="utf-8")

    def denied_load(_path: Path) -> dict:
        raise PermissionError("access denied")

    monkeypatch.setattr(graph_query, "load_graph", denied_load)
    result = mcp_server.dispatch("graph_find", {"term": "createUser", "memory": "off"})
    assert "graph.json unreadable" in result
    assert graph_file.resolve() not in mcp_server._GRAPH_CACHE


def test_dispatch_evicts_invalid_utf_graph_and_recovers(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid UTF-8 cannot strand a stale cached graph after recovery."""
    from harness import mcp_server

    _use_discovered_graph(monkeypatch, graph_file)
    original = graph_file.read_bytes()
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})

    graph_file.write_bytes(b"\x80")
    result = mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert "graph.json unreadable" in result
    assert graph_file.resolve() not in mcp_server._GRAPH_CACHE

    graph_file.write_bytes(original)
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})


def test_dispatch_cache_isolated_by_resolved_graph_path(
    graph_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache entry from one discovered project must never serve another project."""
    from harness import graph_query, mcp_server

    other = tmp_path / "other-project" / "graphify-out" / "graph.json"
    other.parent.mkdir(parents=True)
    other_graph = json.loads(graph_file.read_text(encoding="utf-8"))
    other_graph["nodes"][0]["label"] = "otherProjectOnly"
    other.write_text(json.dumps(other_graph), encoding="utf-8")
    _use_discovered_graph(monkeypatch, graph_file)

    discovered = [graph_file, other, graph_file]
    monkeypatch.setattr(mcp_server, "_discover_graph_path", lambda: discovered.pop(0))
    calls = 0
    original_load = graph_query.load_graph

    def counted_load(path: Path) -> dict:
        nonlocal calls
        calls += 1
        return original_load(path)

    monkeypatch.setattr(graph_query, "load_graph", counted_load)

    assert "createUser" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert "otherProjectOnly" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert "createUser" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert calls == 2


def test_dispatch_cache_evicts_least_recently_used_graph_after_limit(
    graph_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process-local graph cache remains bounded and evicts its LRU entry."""
    from harness import graph_query, mcp_server

    graph_paths: list[Path] = []
    original = graph_file.read_bytes()
    for number in range(mcp_server._GRAPH_CACHE_LIMIT + 1):
        graph_path = tmp_path / f"project-{number}" / "graphify-out" / "graph.json"
        graph_path.parent.mkdir(parents=True)
        graph_path.write_bytes(original)
        graph_paths.append(graph_path)

    _use_discovered_graph(monkeypatch, graph_paths[0])
    discovered = [*graph_paths, graph_paths[0]]
    monkeypatch.setattr(mcp_server, "_discover_graph_path", lambda: discovered.pop(0))
    loads = 0
    original_load = graph_query.load_graph

    def counted_load(path: Path) -> dict:
        nonlocal loads
        loads += 1
        return original_load(path)

    monkeypatch.setattr(graph_query, "load_graph", counted_load)
    for _ in graph_paths:
        assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})

    assert len(mcp_server._GRAPH_CACHE) == mcp_server._GRAPH_CACHE_LIMIT
    assert graph_paths[0].resolve() not in mcp_server._GRAPH_CACHE
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert loads == len(graph_paths) + 1


def test_dispatch_explicit_graph_override_is_not_cached(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --graph override reloads on every MCP call by design."""
    from harness import graph_query, mcp_server

    monkeypatch.setattr(mcp_server, "_GRAPH_OVERRIDE", graph_file)
    loads = 0
    original_load = graph_query.load_graph

    def counted_load(path: Path) -> dict:
        nonlocal loads
        loads += 1
        return original_load(path)

    monkeypatch.setattr(graph_query, "load_graph", counted_load)
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert "n1" in mcp_server.dispatch("graph_node", {"term": "n1", "memory": "off"})
    assert loads == 2


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
