"""Tests for the memory-to-graph join: blast radius attached to memory entries.

The guarantee under test is that this join is *conservative*. A memory entry
that names a symbol unambiguously gets a blast-radius line; an entry that
names something ambiguous, or merely uses an English word that happens to be
a node label, gets nothing. A wrong blast radius is worse than none, because
an agent will act on it.

Fixtures here are synthetic. `graph_query.py` documents the constraint
against putting real company graph data in test fixtures, and that applies
just as much to a graph built from a private repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import memory_graph

# A miniature graph shaped like the real graphify export: `label` carries a
# trailing `()` for callables and is bare for types/constants, node ids are
# path-derived slugs, and an edge {source: A, target: B} means "A depends on
# B" -- so B's dependents are found by walking in-edges.
NODES = [
    # The symbol under test and its four transitive dependents.
    ("ctx_build_packet", "build_packet()", "harness/memory_context.py"),
    ("cli_cmd_context", "cmd_context()", "harness/memory.py"),
    ("cli_main", "main()", "harness/memory.py"),
    ("mcp_dispatch_memory", "dispatch_memory()", "harness/mcp_server.py"),
    ("hook_emit", "emit()", "hooks/claude/memory_context_hook.py"),
    # A leaf: resolvable, but nothing depends on it.
    ("ctx_render_json", "render_json()", "harness/memory_context.py"),
    # Two nodes sharing one label -- the ambiguity case.
    ("app_a_page", "page.tsx", "src/app/a/page.tsx"),
    ("app_b_page", "page.tsx", "src/app/b/page.tsx"),
    # A node whose label is an ordinary English word. Nothing should ever
    # resolve to it from prose, or every entry mentioning auth lights up.
    ("misc_auth", "auth", "src/lib/auth.ts"),
    # Further unique symbols, for the multi-symbol and cap tests.
    ("store_append_entries", "append_entries()", "harness/memory_store.py"),
    ("review_promote", "promote()", "harness/memory_review.py"),
    ("tasks_bottlenecks", "bottlenecks()", "harness/memory_tasks.py"),
]

EDGES = [
    ("cli_cmd_context", "ctx_build_packet"),
    ("mcp_dispatch_memory", "ctx_build_packet"),
    ("hook_emit", "ctx_build_packet"),
    # Transitive: main -> cmd_context -> build_packet.
    ("cli_main", "cli_cmd_context"),
    # append_entries has a single dependent.
    ("review_promote", "store_append_entries"),
]


def _graph_document() -> dict:
    return {
        "directed": True,
        "nodes": [
            {
                "id": nid,
                "label": label,
                "source_file": source_file,
                "source_location": "L1",
                "community_name": "Memory",
            }
            for nid, label, source_file in NODES
        ],
        "links": [
            {"source": s, "target": t, "relation": "calls"} for s, t in EDGES
        ],
    }


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    path = tmp_path / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_graph_document()), encoding="utf-8")
    return path


@pytest.fixture
def annotate(graph_path: Path):
    annotator, _notes = memory_graph.build_annotator(
        graph_path.parent.parent, graph_path=graph_path, check_health=False
    )
    assert annotator is not None
    return annotator


def _entry(summary: str, **overrides) -> dict:
    return {
        "id": "mem-2026-08-10-001",
        "type": "decision",
        "scope": "project",
        "status": "accepted",
        "summary": summary,
        **overrides,
    }


# --- symbol extraction ----------------------------------------------------


def test_a_backticked_symbol_resolves_to_its_node(annotate) -> None:
    impacts = annotate(_entry("Rewrote `build_packet` to take an annotator."))
    assert [i["symbol"] for i in impacts] == ["build_packet"]
    assert impacts[0]["node_id"] == "ctx_build_packet"


def test_a_bare_symbol_matches_a_label_carrying_call_parens(annotate) -> None:
    """graphify labels callables `name()`; entries write them bare."""
    impacts = annotate(_entry("append_entries now validates before writing."))
    assert [i["node_id"] for i in impacts] == ["store_append_entries"]


def test_an_ordinary_english_word_never_resolves(annotate) -> None:
    """`auth` is a real node label. Prose must not light it up."""
    assert annotate(_entry("Reworked the auth flow end to end.")) == ()


def test_backticks_override_the_shape_filter(annotate) -> None:
    """An author writing `auth` in backticks has stated intent explicitly."""
    impacts = annotate(_entry("Reworked `auth` end to end."))
    assert [i["node_id"] for i in impacts] == ["misc_auth"]


def test_an_ambiguous_label_is_dropped_rather_than_guessed(annotate) -> None:
    """Two nodes are labelled page.tsx; naming one would be a coin flip."""
    assert annotate(_entry("Split `page.tsx` into a server shell.")) == ()


def test_an_unknown_symbol_is_simply_absent(annotate) -> None:
    assert annotate(_entry("Renamed `does_not_exist` for clarity.")) == ()


def test_tags_are_searched_under_the_same_shape_rule(annotate) -> None:
    entry = _entry("Tidied the writer.", tags=["memory", "append_entries"])
    assert [i["node_id"] for i in annotate(entry)] == ["store_append_entries"]


# --- blast radius ---------------------------------------------------------


def test_impact_counts_transitive_dependents_not_just_direct(annotate) -> None:
    impacts = annotate(_entry("Rewrote `build_packet`."))
    # cmd_context, dispatch_memory, emit directly; main through cmd_context.
    assert impacts[0]["dependents"] == 4


def test_impact_names_the_distinct_files_involved(annotate) -> None:
    impacts = annotate(_entry("Rewrote `build_packet`."))
    # Sorted, not discovery-ordered: the same graph must always render the
    # same line, and traversal order is an implementation detail.
    assert impacts[0]["files"] == [
        "harness/mcp_server.py",
        "harness/memory.py",
        "hooks/claude/memory_context_hook.py",
    ]


def test_a_leaf_symbol_reports_no_dependents(annotate) -> None:
    impacts = annotate(_entry("Left `render_json` alone."))
    assert impacts[0]["dependents"] == 0
    assert "no known dependents" in impacts[0]["line"]


def test_the_rendered_line_is_indented_and_ascii_only(annotate) -> None:
    line = annotate(_entry("Rewrote `build_packet`."))[0]["line"]
    assert line.startswith("  ")
    line.encode("ascii")  # raises if a non-ASCII glyph crept in
    assert "build_packet" in line and "4 dependent" in line


def test_files_beyond_the_cap_are_counted_not_listed(
    graph_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_graph, "FILE_LIMIT", 2)
    annotator, _ = memory_graph.build_annotator(
        graph_path.parent.parent, graph_path=graph_path, check_health=False
    )
    line = annotator(_entry("Rewrote `build_packet`."))[0]["line"]
    assert "harness/memory.py" in line
    assert "+1 more" in line


def test_symbols_beyond_the_cap_are_dropped(
    graph_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_graph, "SYMBOL_LIMIT", 2)
    annotator, _ = memory_graph.build_annotator(
        graph_path.parent.parent, graph_path=graph_path, check_health=False
    )
    impacts = annotator(
        _entry("Touched `build_packet`, `append_entries`, and `bottlenecks`.")
    )
    assert len(impacts) == 2


def test_the_same_entry_always_annotates_identically(annotate) -> None:
    entry = _entry("Rewrote `build_packet` and `append_entries`.")
    assert annotate(entry) == annotate(entry)


# --- degradation ----------------------------------------------------------


def test_no_graph_yields_no_annotator_and_no_error(tmp_path: Path) -> None:
    annotator, notes = memory_graph.build_annotator(tmp_path, check_health=False)
    assert annotator is None
    assert notes == []


def test_a_corrupt_graph_degrades_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json at all", encoding="utf-8")
    annotator, notes = memory_graph.build_annotator(
        tmp_path, graph_path=path, check_health=False
    )
    assert annotator is None
    assert any("graph" in n for n in notes)


def test_a_graph_without_nodes_degrades_instead_of_raising(tmp_path: Path) -> None:
    path = tmp_path / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"links": []}), encoding="utf-8")
    annotator, notes = memory_graph.build_annotator(
        tmp_path, graph_path=path, check_health=False
    )
    assert annotator is None
    assert notes


def test_a_malformed_entry_never_raises(annotate) -> None:
    assert annotate({}) == ()
    assert annotate({"summary": None, "tags": None}) == ()
    assert annotate({"summary": 17, "tags": "not-a-list"}) == ()


def test_an_unhealthy_graph_is_reported_as_a_note(
    graph_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import graph_location

    monkeypatch.setattr(
        memory_graph.graph_location,
        "report_health",
        lambda *a, **k: graph_location.GraphHealth.DIRTY,
    )
    annotator, notes = memory_graph.build_annotator(
        graph_path.parent.parent, graph_path=graph_path, check_health=True
    )
    assert annotator is not None
    assert any("dirty" in n for n in notes)


def test_a_fresh_graph_needs_no_caveat(
    graph_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import graph_location

    monkeypatch.setattr(
        memory_graph.graph_location,
        "report_health",
        lambda *a, **k: graph_location.GraphHealth.FRESH,
    )
    _annotator, notes = memory_graph.build_annotator(
        graph_path.parent.parent, graph_path=graph_path, check_health=True
    )
    assert notes == []
