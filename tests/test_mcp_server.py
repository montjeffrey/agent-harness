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


# --- graph-health cache ---------------------------------------------------
#
# `graph_location.report_health` shells out to git twice. This server is
# long-lived, so `memory_context` was paying for both subprocesses on every
# single call for a verdict that almost never changes. The cache is keyed on
# the graph file's stat signature plus a short TTL -- the signature catches a
# rebuild, and the TTL catches a dirty -> clean working-tree transition, which
# never touches graph.json and so is invisible to the signature alone.


def _count_health_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every real `report_health` call the cache lets through."""
    from harness import graph_location, mcp_server

    seen: list[Path] = []

    def fake_report_health(path: Path, repo_root: Path | None = None):
        seen.append(Path(path))
        return graph_location.GraphHealth.FRESH

    monkeypatch.setattr(graph_location, "report_health", fake_report_health)
    monkeypatch.setattr(mcp_server, "_HEALTH_CACHE", OrderedDict())
    return seen


def test_graph_health_is_computed_once_for_an_unchanged_graph(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import mcp_server

    seen = _count_health_calls(monkeypatch)

    first = mcp_server._cached_graph_health(graph_file)
    second = mcp_server._cached_graph_health(graph_file)

    assert first == second
    assert len(seen) == 1, "the second call should have been served from cache"


def test_graph_health_is_recomputed_after_the_ttl_expires(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dirty -> clean transition never touches graph.json, so only elapsed
    time can notice it. Without the TTL the stale verdict would be pinned for
    the whole life of the server process."""
    from harness import mcp_server

    seen = _count_health_calls(monkeypatch)

    clock = [1000.0]
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: clock[0])

    mcp_server._cached_graph_health(graph_file)
    clock[0] += mcp_server._HEALTH_TTL_SECONDS / 2
    mcp_server._cached_graph_health(graph_file)
    assert len(seen) == 1

    clock[0] += mcp_server._HEALTH_TTL_SECONDS
    mcp_server._cached_graph_health(graph_file)
    assert len(seen) == 2


def test_graph_health_is_recomputed_after_the_graph_is_rebuilt(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuilt graph invalidates the verdict immediately, without waiting
    for the TTL -- the stat signature, not the clock, catches this one."""
    from harness import mcp_server

    seen = _count_health_calls(monkeypatch)
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: 1000.0)

    mcp_server._cached_graph_health(graph_file)
    graph_file.write_text(
        json.dumps({"nodes": [], "links": [], "built_at_commit": "feedface"}),
        encoding="utf-8",
    )
    mcp_server._cached_graph_health(graph_file)

    assert len(seen) == 2


def test_graph_health_cache_evicts_least_recently_used_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import mcp_server

    _count_health_calls(monkeypatch)
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: 1000.0)

    for number in range(mcp_server._HEALTH_CACHE_LIMIT + 1):
        graph = tmp_path / f"graph{number}.json"
        graph.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        mcp_server._cached_graph_health(graph)

    assert len(mcp_server._HEALTH_CACHE) == mcp_server._HEALTH_CACHE_LIMIT
    assert (tmp_path / "graph0.json").resolve() not in mcp_server._HEALTH_CACHE


def test_graph_health_for_a_missing_graph_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed stat must never leave (or serve) a cached verdict, so a graph
    that appears later is judged fresh rather than from stale state."""
    from harness import mcp_server

    seen = _count_health_calls(monkeypatch)
    missing = tmp_path / "absent.json"

    mcp_server._cached_graph_health(missing)
    mcp_server._cached_graph_health(missing)

    assert len(seen) == 2
    assert missing.resolve() not in mcp_server._HEALTH_CACHE


def test_warm_optional_native_imports_reports_success_without_raising():
    """The warm-up reports readiness and never raises.

    Guards the 2026-09-11 finding: importing chromadb lazily inside a tool
    call on an anyio worker thread never completes, while the same import on
    the main thread at startup costs under a second. This asserts the
    contract of the warm-up itself, not the timing, which only reproduces
    through a live stdio transport (see tests/test_mcp_stdio.py).
    """
    from harness import mcp_server

    reason = mcp_server._warm_optional_native_imports()
    assert reason is None or isinstance(reason, str)


def test_warm_optional_native_imports_absorbs_a_broken_optional_dependency(monkeypatch):
    """A broken optional extra must not stop the server from starting."""
    from harness import mcp_server, memory_index

    def explode():
        raise RuntimeError("simulated broken chromadb install")

    monkeypatch.setattr(memory_index, "chromadb_status", explode)
    reason = mcp_server._warm_optional_native_imports()
    assert reason is not None
    assert "simulated broken chromadb install" in reason


def test_warm_optional_native_imports_passes_through_unavailable_reason(monkeypatch):
    """An absent extra is reported as its reason, not as a crash."""
    from harness import mcp_server, memory_index

    monkeypatch.setattr(
        memory_index, "chromadb_status", lambda: (False, "ModuleNotFoundError: chromadb")
    )
    assert mcp_server._warm_optional_native_imports() == "ModuleNotFoundError: chromadb"


def test_main_warms_imports_before_serving(monkeypatch):
    """The warm-up runs before the event loop starts, which is the whole point."""
    from harness import mcp_server

    order = []
    monkeypatch.setattr(
        mcp_server, "_warm_optional_native_imports", lambda: order.append("warm")
    )

    class FakeServer:
        def run(self, transport):
            order.append("run")

    monkeypatch.setattr(mcp_server, "_build_server", lambda: FakeServer())
    assert mcp_server.main([]) == 0
    assert order == ["warm", "run"]


# --- memory_search, filters, cross-project search, registry touch ---------


def _memory_project(root: Path) -> Path:
    from harness import memory_store

    root.mkdir(parents=True, exist_ok=True)
    memory_store.init_project(root)
    return root


def _remember(root: Path, summary: str, **overrides) -> None:
    from harness import memory_store

    entry = {
        "type": "decision", "scope": "project", "status": "accepted",
        "confidence": "confirmed", "source": {"manual": True}, "summary": summary,
        **overrides,
    }
    memory_store.append_entries([entry], directory=memory_store.project_memory_dir(root))


def _machine(summary: str, **overrides) -> None:
    from harness import memory_store

    directory = memory_store.machine_memory_dir()
    directory.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "lesson", "scope": "machine", "status": "accepted",
        "confidence": "confirmed", "source": {"manual": True}, "summary": summary,
        **overrides,
    }
    memory_store.append_entries([entry], directory=directory)


def test_memory_search_is_a_registered_tool_that_points_away_from_memory_context() -> None:
    from harness import mcp_server

    assert "memory_search" in mcp_server.MEMORY_TOOL_DESCRIPTIONS
    description = mcp_server.MEMORY_TOOL_DESCRIPTIONS["memory_search"]
    assert "INSTEAD of memory_context" in description
    assert "mid-task" in description
    assert "projects" in description
    context = mcp_server.MEMORY_TOOL_DESCRIPTIONS["memory_context"]
    assert "memory_search" in context and "tags" in context and "exclude" in context
    assert "review" not in description and "accept" not in description.replace("accepted", "")


def test_memory_search_requires_a_query(tmp_path: Path) -> None:
    from harness import mcp_server

    project = _memory_project(tmp_path / "proj")
    out = mcp_server.dispatch_memory("memory_search", {"project": str(project), "query": "  "})
    assert "query is required" in out


def test_memory_search_returns_focused_labelled_hits(tmp_path: Path) -> None:
    from harness import mcp_server, memory_store

    project = _memory_project(tmp_path / "proj")
    _remember(project, "Retry policy uses exponential backoff", tags=["retry"])
    _remember(project, "Logging is local JSONL", tags=["logging"])
    _machine("Machine lesson about retry budgets", tags=["retry"])

    out = mcp_server.dispatch_memory(
        "memory_search", {"project": str(project), "query": "retry backoff", "limit": 2}
    )
    project_id = memory_store.project_id_for(project)
    assert out.startswith("# Memory search: retry backoff")
    assert f"- [{project_id}] `mem-" in out and "exponential backoff" in out
    assert "- [machine] `mem-" in out and "retry budgets" in out
    assert "local JSONL" not in out
    assert "## Scope" not in out and "## Warnings" not in out


def test_memory_search_filters_apply_before_ranking_and_are_stated(tmp_path: Path) -> None:
    from harness import mcp_server

    project = _memory_project(tmp_path / "proj")
    _remember(project, "Retry decision", tags=["retry"])
    _remember(project, "Retry task", type="task", status="proposed", confidence="inferred",
              source=None, tags=["retry"])
    out = mcp_server.dispatch_memory(
        "memory_search", {"project": str(project), "query": "retry", "types": "task"}
    )
    assert "Retry task" in out and "Retry decision" not in out
    assert "filters applied (types: task); 1 entry excluded before ranking" in out
    bad = mcp_server.dispatch_memory(
        "memory_search", {"project": str(project), "query": "retry", "types": "nope"}
    )
    assert "unknown type(s) nope" in bad


def test_memory_context_filters_narrow_the_packet_and_the_adaptive_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must count the entries that survive the filter, so narrowing a
    large store to a few entries never pays for a model to reorder them."""
    from harness import mcp_server, memory_context, memory_index

    project = _memory_project(tmp_path / "proj")
    for n in range(12):
        _remember(project, f"General fact {n}", tags=["general"])
    _remember(project, "Retry decision one", tags=["retry"])
    _remember(project, "Retry decision two", tags=["retry"])

    seen: dict = {}

    def recording(project_id, strategy="auto", **kwargs):
        seen.update(kwargs)
        return memory_context.deterministic_selector, "deterministic", []

    monkeypatch.setattr(memory_index, "resolve_selector", recording)
    out = mcp_server.dispatch_memory(
        "memory_context", {"project": str(project), "query": "retry", "tags": "retry"}
    )
    assert seen["entry_count"] == 2
    assert "Retry decision one" in out and "General fact 3" not in out
    assert "_Note: filters applied (tags: retry); 12 entries excluded before ranking_" in out

    seen.clear()
    mcp_server.dispatch_memory("memory_context", {"project": str(project), "query": "retry"})
    assert seen["entry_count"] == 14


def test_memory_context_rejects_an_unknown_type_filter_as_text(tmp_path: Path) -> None:
    from harness import mcp_server

    project = _memory_project(tmp_path / "proj")
    out = mcp_server.dispatch_memory("memory_context", {"project": str(project), "types": "bogus"})
    assert "unknown type(s) bogus" in out


def test_memory_search_across_all_registered_projects(tmp_path: Path) -> None:
    """Two temp projects in the isolated registry: hits from both, each
    labelled, a vanished project reported, and machine memory read once."""
    import shutil

    from harness import mcp_server, memory_store

    alpha = _memory_project(tmp_path / "alpha")
    beta = _memory_project(tmp_path / "beta")
    gone = _memory_project(tmp_path / "gone")
    _remember(alpha, "Alpha retry policy uses backoff", tags=["retry"])
    _remember(beta, "Beta retry client shares the backoff", tags=["retry"])
    _remember(beta, "Beta unrelated logging note", tags=["logging"])
    _machine("Machine retry lesson", tags=["retry"])
    shutil.rmtree(gone)
    alpha_id, beta_id, gone_id = (memory_store.project_id_for(p) for p in (alpha, beta, gone))
    assert set(memory_store.load_registry()["projects"]) == {alpha_id, beta_id, gone_id}

    out = mcp_server.dispatch_memory(
        "memory_search", {"project": str(alpha), "query": "retry backoff", "projects": "all"}
    )
    assert f"[{alpha_id}]" in out and "Alpha retry policy" in out
    assert f"[{beta_id}]" in out and "Beta retry client" in out
    assert out.count("[machine]") == 1
    assert f"project {gone_id}: registered path" in out and "no longer exists; skipped" in out
    assert f"{alpha_id}=deterministic" in out and f"{beta_id}=deterministic" in out


def test_memory_search_with_an_explicit_project_list_and_unknown_id(tmp_path: Path) -> None:
    from harness import mcp_server, memory_store

    alpha = _memory_project(tmp_path / "alpha")
    beta = _memory_project(tmp_path / "beta")
    _remember(beta, "Beta retry client", tags=["retry"])
    beta_id = memory_store.project_id_for(beta)

    out = mcp_server.dispatch_memory(
        "memory_search",
        {"project": str(alpha), "query": "retry", "projects": f"{beta_id},nope-00000000"},
    )
    assert f"[{beta_id}]" in out and "Beta retry client" in out
    assert "'nope-00000000' is not registered" in out


def test_memory_search_related_follows_accepted_machine_relationships(tmp_path: Path) -> None:
    from harness import mcp_server, memory_store

    alpha = _memory_project(tmp_path / "alpha")
    beta = _memory_project(tmp_path / "beta")
    gamma = _memory_project(tmp_path / "gamma")
    _remember(beta, "Beta retry client", tags=["retry"])
    _remember(gamma, "Gamma retry client", tags=["retry"])
    alpha_id, beta_id, gamma_id = (memory_store.project_id_for(p) for p in (alpha, beta, gamma))

    unrelated = mcp_server.dispatch_memory(
        "memory_search", {"project": str(alpha), "query": "retry", "projects": "related"}
    )
    assert "no accepted relationship names another registered project" in unrelated
    assert f"[{beta_id}]" not in unrelated

    _machine(f"alpha shares its retry client with {beta_id}", type="relationship",
             projects=[alpha_id, beta_id])
    _machine(f"maybe also {gamma_id}", type="relationship", status="proposed",
             confidence="inferred", source=None)
    related = mcp_server.dispatch_memory(
        "memory_search", {"project": str(alpha), "query": "retry", "projects": "related"}
    )
    assert f"[{beta_id}]" in related and "Beta retry client" in related
    assert f"[{gamma_id}]" not in related  # only accepted relationships count


def test_memory_search_without_a_registry_stays_on_the_current_project(tmp_path: Path) -> None:
    from harness import mcp_server, memory_store

    project = tmp_path / "unregistered"
    project.mkdir()
    (project / ".agent-harness" / "memory").mkdir(parents=True)
    _remember(project, "Only local knowledge", tags=["local"])
    assert memory_store.load_registry()["projects"] == {}

    out = mcp_server.dispatch_memory(
        "memory_search", {"project": str(project), "query": "local knowledge", "projects": "all"}
    )
    assert "Only local knowledge" in out
    assert "registry missing, unreadable, or empty" in out


def test_a_tool_call_refreshes_last_seen_but_never_enrolls(tmp_path: Path) -> None:
    from harness import mcp_server, memory_store

    project = _memory_project(tmp_path / "proj")
    project_id = memory_store.project_id_for(project)
    registry = memory_store.load_registry()
    registry["projects"][project_id]["last_seen"] = "2020-01-01T00:00:00Z"
    memory_store.save_registry(registry)

    mcp_server.dispatch_memory("memory_status", {"project": str(project)})
    assert memory_store.load_registry()["projects"][project_id]["last_seen"] > "2020-01-01T00:00:00Z"

    stranger = tmp_path / "stranger"
    stranger.mkdir()
    mcp_server.dispatch_memory("memory_status", {"project": str(stranger)})
    assert memory_store.project_id_for(stranger) not in memory_store.load_registry()["projects"]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture()
def work_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_work_start_attaches_a_packet_ranked_by_the_task(work_repo: Path) -> None:
    from harness import mcp_server, memory_store

    memory_store.init_project(work_repo)
    _remember(work_repo, "Retry policy uses exponential backoff", tags=["retry"])
    _remember(work_repo, "Logging is local JSONL", tags=["logging"])

    out = mcp_server.dispatch_work(
        "work_start",
        {"project": str(work_repo), "summary": "Harden the retry client", "session": "s-1",
         "paths": "src/app.py", "tags": "retry, backoff"},
    )
    head, memory = out.split("\n\nmemory:\n", 1)
    assert head.startswith("status: success\nsummary: started ")
    assert "branch:" in head and "base:" in head
    assert memory.startswith("# Project context:")
    assert "task-scoped packet ranked by this task" in memory
    assert memory.index("exponential backoff") < memory.index("local JSONL")
    assert f"/{mcp_server.TASK_PACKET_MAX_CHARS} chars" in memory


def test_work_start_says_when_there_is_no_memory_to_attach(work_repo: Path) -> None:
    from harness import mcp_server

    out = mcp_server.dispatch_work(
        "work_start",
        {"project": str(work_repo), "summary": "Change app", "session": "s-1", "paths": "src/app.py"},
    )
    assert out.startswith("status: success")
    assert "memory: no project memory at" in out


def test_work_start_still_starts_when_the_packet_fails(
    work_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import mcp_server, memory_context, memory_store, work

    memory_store.init_project(work_repo)
    _remember(work_repo, "A fact")

    def boom(*args, **kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(memory_context, "build_packet", boom)
    out = mcp_server.dispatch_work(
        "work_start",
        {"project": str(work_repo), "summary": "Change app", "session": "s-1", "paths": "src/app.py"},
    )
    assert out.startswith("status: success")
    assert "task-scoped packet unavailable (RuntimeError: index exploded)" in out
    assert "the task started anyway" in out
    assert [t.status for t in work.list_tasks(work_repo, active_only=True)] == [work.ACTIVE]
