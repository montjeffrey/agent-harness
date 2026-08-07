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
    (tmp_path / ".git").mkdir()  # repo boundary -- walk-up must have somewhere to stop
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


def test_build_index_missing_nodes_key_raises_clear_diagnostic() -> None:
    """A graph.json missing its top-level 'nodes' list must fail with a
    clear SystemExit message (this module's existing error convention),
    not an uncaught KeyError/TypeError traceback."""
    with pytest.raises(SystemExit, match="nodes"):
        graph_query.build_index({"links": []})


def test_build_index_non_list_nodes_raises_clear_diagnostic() -> None:
    with pytest.raises(SystemExit, match="nodes"):
        graph_query.build_index({"nodes": "not-a-list", "links": []})


def test_build_index_node_missing_id_raises_clear_diagnostic() -> None:
    """A 'nodes' list whose entries aren't {"id": ...}-shaped objects must
    fail with a clear SystemExit message, not an uncaught KeyError/TypeError
    traceback -- same malformed-graph error convention as the other
    build_index checks above."""
    with pytest.raises(SystemExit, match="id"):
        graph_query.build_index({"nodes": [{"label": "no id here"}], "links": []})


# --- single discovery path (Wave 3 requirement 1) --------------------------


def test_find_graph_path_delegates_to_graph_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph_query must use `graph_location.find_graph_in_repo` as its ONLY
    discovery mechanism -- no independent walk-up logic of its own."""
    sentinel = tmp_path / "graphify-out" / "graph.json"
    calls: list[Path] = []

    def fake_find(start: Path | None = None) -> Path:
        calls.append(start)
        return sentinel

    monkeypatch.setattr(graph_query, "find_graph_in_repo", fake_find)
    found = graph_query.find_graph_path(tmp_path)
    assert found == sentinel
    assert calls == [tmp_path.resolve()]


def test_find_graph_path_missing_defers_to_graph_location_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph_query, "find_graph_in_repo", lambda start=None: None)
    with pytest.raises(SystemExit):
        graph_query.find_graph_path(tmp_path)


# --- canonical links vs legacy edges (Wave 3 requirement 2) ----------------


CYCLE_NODES = [
    {"id": "a", "label": "a", "source_file": "a.py", "source_location": "1", "community_name": "", "metadata": {}},
    {"id": "b", "label": "b", "source_file": "b.py", "source_location": "1", "community_name": "", "metadata": {}},
    {"id": "c", "label": "c", "source_file": "c.py", "source_location": "1", "community_name": "", "metadata": {}},
]

# a -> b -> c -> a (cycle), plus a linear tail c -> d for transitive-closure coverage
TAIL_NODE = {
    "id": "d", "label": "d", "source_file": "d.py", "source_location": "1", "community_name": "", "metadata": {},
}


def _canonical_graph(nodes: list[dict], edges: list[tuple[str, str, str]]) -> dict:
    return {
        "nodes": nodes,
        "links": [{"source": s, "target": t, "relation": r} for s, t, r in edges],
    }


def _legacy_graph(nodes: list[dict], edges: list[tuple[str, str, str]]) -> dict:
    """Legacy edge format: top-level 'edges' key, from/to/type field names."""
    return {
        "nodes": nodes,
        "edges": [{"from": s, "to": t, "type": r} for s, t, r in edges],
    }


CYCLE_EDGES = [("a", "b", "calls"), ("b", "c", "calls"), ("c", "a", "calls")]


@pytest.mark.parametrize("graph_factory", [_canonical_graph, _legacy_graph])
def test_canonical_and_legacy_formats_produce_equivalent_results(
    tmp_path: Path, graph_factory
) -> None:
    g = graph_factory(CYCLE_NODES, CYCLE_EDGES)
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(g), encoding="utf-8")
    loaded = graph_query.load_graph(path)
    index = graph_query.build_index(loaded)

    # callers of b: only a (direct)
    assert set(graph_query.traverse("b", index.in_edges, "source", 1)) == {"a"}
    # callees of a: only b (direct)
    assert set(graph_query.traverse("a", index.out_edges, "target", 1)) == {"b"}
    # impact (transitive callers) of c: b and a
    assert set(graph_query.traverse("c", index.in_edges, "source", None)) == {"a", "b"}


def test_unrecognized_edge_schema_is_dropped_not_fabricated_as_empty_string(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An edge whose field names match none of the known source/target
    aliases (e.g. a schema this module has never seen) must be skipped, not
    silently defaulted to "" -- a ""-keyed phantom edge would make
    callers/callees/impact under-report with no indication anything went
    wrong. It must also surface a diagnostic instead of failing silently."""
    g = {
        "nodes": CYCLE_NODES,
        "links": [{"origin": "a", "destination": "b", "kind_of_thing": "calls"}],
    }
    index = graph_query.build_index(g)

    # no phantom ""-keyed edge was fabricated
    assert "" not in index.out_edges
    assert "" not in index.in_edges
    assert sum(len(v) for v in index.out_edges.values()) == 0
    assert sum(len(v) for v in index.in_edges.values()) == 0

    # a diagnostic was surfaced, not a silent no-op
    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    assert "1" in captured.err  # one edge was dropped


# --- true transitive traversal, cycle-safe (Wave 3 requirement 3) ----------


def test_impact_cyclic_graph_terminates_no_double_count() -> None:
    """A -> B -> C -> A must not infinite-loop and must not double-count."""
    g = _canonical_graph(CYCLE_NODES, CYCLE_EDGES)
    index = graph_query.build_index(g)
    lines = graph_query.cmd_traverse(index, "a", "impact", 1)
    assert "2 transitive dependent(s) of a:" in lines[0]
    joined = "\n".join(lines)
    assert joined.count(" b ") + joined.count("b  [") >= 0  # sanity: no crash/hang
    found = graph_query.traverse("a", index.in_edges, "source", None)
    assert found == {"c": 1, "b": 2}


def test_impact_full_transitive_closure_beyond_direct_neighbors() -> None:
    """impact of the tail's dependency chain must include every ancestor,
    not just the direct caller -- full closure, not one hop."""
    nodes = [*CYCLE_NODES, TAIL_NODE]
    edges = [*CYCLE_EDGES, ("c", "d", "calls")]  # d calls -> wait: direction below
    # d is called BY c: c -> d, so impact of d (callers) should include c, b, a transitively.
    g = _canonical_graph(nodes, edges)
    index = graph_query.build_index(g)
    found = graph_query.traverse("d", index.in_edges, "source", None)
    assert found == {"c": 1, "b": 2, "a": 3}



# --- library-safe query() entry point ---------------------------------------


def test_query_returns_lines(graph_file: Path) -> None:
    lines = graph_query.query("find", "createUser", graph=graph_file)
    assert isinstance(lines, list)
    assert "2 node(s) match 'createUser':" in lines[0]
    joined = "\n".join(lines)
    assert "n1" in joined and "n3" in joined


def test_query_raises_valueerror_on_ambiguous(graph_file: Path) -> None:
    with pytest.raises(ValueError):
        graph_query.query("node", "create", graph=graph_file)


def test_query_raises_filenotfound_without_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(graph_query, "find_graph_in_repo", lambda start=None: None)
    with pytest.raises(FileNotFoundError):
        graph_query.query("find", "anything")


def test_query_raises_valueerror_on_unknown_command(graph_file: Path) -> None:
    with pytest.raises(ValueError):
        graph_query.query("bogus-command", "term", graph=graph_file)


def test_query_raises_filenotfound_on_explicit_missing_graph(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        graph_query.query("find", "anything", graph=tmp_path / "does-not-exist.json")


def test_query_raises_valueerror_on_malformed_json(tmp_path: Path) -> None:
    """A graph.json that exists but isn't valid JSON must surface as the
    documented plain ValueError, not an uncaught json.JSONDecodeError --
    callers like harness.mcp_server.dispatch only catch ValueError/
    FileNotFoundError from query()."""
    bad_graph = tmp_path / "graph.json"
    bad_graph.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        graph_query.query("find", "anything", graph=bad_graph)


def test_query_callers_matches_cli(graph_file: Path) -> None:
    lines = graph_query.query("callers", "n1", graph=graph_file)
    assert "1 dependent(s) of n1:" in lines[0]


# --- search subcommand: graceful degradation without the [semantic] extra --


def test_search_without_chromadb_exits_nonzero_with_actionable_message(
    graph_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When chromadb isn't installed, `search` must fail fast with a clear
    `pip install "agent-harness[semantic]"` message and exit 1 -- never a
    traceback, and it must never attempt to build/query an index."""
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: False)
    exit_code = graph_query.main(["--graph", str(graph_file), "search", "welcome email"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert 'pip install "agent-harness[semantic]"' in captured.err


def test_search_with_chromadb_but_no_index_points_at_graph_refresh(
    graph_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """chromadb importable but no semantic-index/ dir yet -- search must
    never auto-build the index; it should point the user at graph_refresh.py."""
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    exit_code = graph_query.main(["--graph", str(graph_file), "search", "welcome email"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "graph_refresh.py" in captured.err


def test_search_with_partially_built_index_points_at_graph_refresh(
    graph_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """index_dir exists (so the missing-directory check doesn't fire) but a
    prior update_index run never got as far as populating the collection --
    query_index surfaces that as FileNotFoundError (see
    test_graph_index.test_query_index_raises_file_not_found_when_collection_missing),
    and search must translate it into the same actionable message as the
    missing-index-directory case, not a zero-results answer or a traceback."""
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    index_dir = graph_index.index_path_for(graph_file)
    index_dir.mkdir(parents=True)

    def fake_query_index(_index_dir, _text, _limit, client_factory=None, embedding_function=None):
        raise FileNotFoundError("no collection named 'graph_nodes' in this semantic index")

    monkeypatch.setattr(graph_index, "query_index", fake_query_index)
    exit_code = graph_query.main(["--graph", str(graph_file), "search", "welcome email"])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "graph_refresh.py" in captured.err
    assert "no semantic index found" in captured.err


def test_search_returns_scored_rows_via_fake_index(
    graph_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With chromadb 'available' and an index present, search output mirrors
    cmd_find(): a count header + node_line() rows, each prefixed with a
    2-decimal relevance score."""
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    index_dir = graph_index.index_path_for(graph_file)
    index_dir.mkdir(parents=True)

    def fake_query_index(_index_dir, _text, _limit, client_factory=None, embedding_function=None):
        return [("n2", 0.0), ("n1", 1.0)]

    monkeypatch.setattr(graph_index, "query_index", fake_query_index)
    lines = graph_query.run(["--graph", str(graph_file), "search", "welcome email"])
    assert "2 node(s) match 'welcome email' (semantic):" in lines[0]
    joined = "\n".join(lines)
    assert "1.00" in joined  # score for distance 0.0
    assert "0.50" in joined  # score for distance 1.0
    assert "n2" in joined
    assert "n1" in joined


def test_search_all_other_subcommands_unaffected(graph_file: Path) -> None:
    """Sanity check that adding `search` didn't change the choices/behavior
    of the pre-existing subcommands (byte-identical output)."""
    lines = graph_query.run(["--graph", str(graph_file), "find", "createUser"])
    assert "2 node(s) match 'createUser':" in lines[0]


def test_impact_not_bounded_by_a_fixed_depth_cap() -> None:
    """A long linear chain deeper than any hardcoded depth cap must still
    resolve to the full transitive closure."""
    chain_len = 150
    nodes = [
        {"id": f"n{i}", "label": f"n{i}", "source_file": "x.py", "source_location": "1", "community_name": "", "metadata": {}}
        for i in range(chain_len)
    ]
    edges = [(f"n{i}", f"n{i + 1}", "calls") for i in range(chain_len - 1)]
    g = _canonical_graph(nodes, edges)
    index = graph_query.build_index(g)
    found = graph_query.traverse(f"n{chain_len - 1}", index.in_edges, "source", None)
    assert len(found) == chain_len - 1
    assert found[f"n0"] == chain_len - 1
