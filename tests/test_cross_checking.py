"""Cross-check contracts between semantic memory ranking and graph evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harness import graph_evidence, graph_query, mcp_server, memory_context, memory_graph, memory_index, memory_store


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


def _entry(entry_id: str, summary: str) -> dict:
    return {
        "id": entry_id,
        "type": "decision",
        "scope": "project",
        "status": "accepted",
        "confidence": "confirmed",
        "source": {"manual": True},
        "summary": summary,
    }


def test_top_k_agreement_reports_semantic_disagreement() -> None:
    candidates = [_entry("mem-a", "first"), _entry("mem-b", "second")]

    def semantic(_candidates, _query, _limit):
        return [candidates[1], candidates[0]]

    result = memory_index.top_k_agreement(candidates, "anything", semantic, limit=1)

    assert result == {
        "limit": 1,
        "deterministic": ["project:mem-a"],
        "semantic": ["project:mem-b"],
        "overlap": 0,
    }


def test_top_k_agreement_scope_qualifies_colliding_entry_ids() -> None:
    project = {**_entry("mem-same", "project"), "scope": "project"}
    machine = {**_entry("mem-same", "machine"), "scope": "machine"}

    result = memory_index.top_k_agreement(
        [project], "anything", lambda *_args: [machine], limit=1
    )

    assert result["overlap"] == 0


def test_semantic_paraphrase_never_creates_graph_evidence() -> None:
    graph = {
        "nodes": [{"id": "build", "label": "build_packet()", "source_file": "harness/memory_context.py"}],
        "links": [],
    }
    index = memory_graph.build_symbol_index(graph)
    assert index is not None

    # A semantic result may be a useful paraphrase, but it contains no exact
    # symbol reference and therefore cannot create or link a graph node.
    evidence = graph_evidence.collect_evidence(
        [_entry("mem-paraphrase", "The packet assembler was rewritten.")], index
    )

    assert evidence == {}


def test_resolved_graph_evidence_matches_graph_traversal() -> None:
    graph = {
        "nodes": [
            {"id": "target", "label": "build_packet()", "source_file": "harness/memory_context.py"},
            {"id": "caller", "label": "cmd_context()", "source_file": "harness/memory.py"},
            {"id": "transitive", "label": "main()", "source_file": "harness/memory.py"},
        ],
        "links": [
            {"source": "caller", "target": "target", "relation": "calls"},
            {"source": "transitive", "target": "caller", "relation": "calls"},
        ],
    }
    symbol_index = memory_graph.build_symbol_index(graph)
    assert symbol_index is not None
    evidence = graph_evidence.collect_evidence([_entry("mem-resolved", "Changed `build_packet`. ")], symbol_index)
    traversal = graph_query.traverse("target", symbol_index.in_edges, "source", None)

    assert set(evidence) == {"target"}
    assert memory_graph.blast_radius("target", symbol_index)[0] == len(traversal)


def test_mcp_cross_check_is_opt_in_and_reports_disagreement(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    memory_store.init_project(project, now=NOW)
    memory_store.append_entries(
        [_entry("mem-a", "alpha"), _entry("mem-b", "beta")],
        directory=memory_store.project_memory_dir(project), now=NOW,
    )

    def semantic(candidates, _query, limit):
        return list(reversed(candidates))[:limit]

    monkeypatch.setattr(
        memory_index,
        "resolve_selector",
        lambda *_args, **_kwargs: (semantic, "semantic", []),
    )
    monkeypatch.setattr(memory_graph, "build_annotator", lambda *_args, **_kwargs: (None, []))

    ordinary = mcp_server.dispatch_memory("memory_context", {"project": str(project)})
    diagnosed = mcp_server.dispatch_memory(
        "memory_context",
        {"project": str(project), "cross_check": True, "cross_check_limit": 1},
    )

    assert "selection agreement" not in ordinary
    assert "selection agreement: 0/" in diagnosed
