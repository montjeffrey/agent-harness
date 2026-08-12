"""Tests for deterministic memory evidence attached to graph nodes."""

from __future__ import annotations

from harness import graph_evidence, memory_graph


def _entry(entry_id: str, type: str, status: str, summary: str) -> dict:
    return {"id": entry_id, "type": type, "status": status, "summary": summary}


def _dated_entry(
    entry_id: str, type: str, status: str, summary: str, ts: str
) -> dict:
    entry = _entry(entry_id, type, status, summary)
    entry["ts"] = ts
    return entry


def test_collect_evidence_groups_only_exactly_resolved_live_records() -> None:
    index = memory_graph.build_symbol_index(
        {"nodes": [
            {"id": "unique", "label": "build_packet()"},
            {"id": "other", "label": "page.tsx"},
            {"id": "ambiguous", "label": "page.tsx"},
        ], "links": []}
    )
    assert index is not None

    evidence = graph_evidence.collect_evidence([
        _entry("d2", "decision", "accepted", "Use `build_packet`."),
        _entry("t2", "task", "accepted", "Finish `build_packet`."),
        _entry("b1", "blocker", "proposed", "Blocked on `build_packet`."),
        _entry("old", "task", "resolved", "Old `build_packet` work."),
        _entry("bad", "decision", "accepted", "Use `page.tsx`."),
    ], index)

    found = evidence["unique"]
    assert [record["id"] for record in found.decisions] == ["d2"]
    assert [record["id"] for record in found.tasks] == ["t2"]
    assert [record["id"] for record in found.blockers] == ["b1"]
    assert "other" not in evidence and "ambiguous" not in evidence


def test_evidence_records_sort_deterministically_by_id() -> None:
    index = memory_graph.build_symbol_index(
        {"nodes": [{"id": "unique", "label": "build_packet()"}], "links": []}
    )
    assert index is not None
    evidence = graph_evidence.collect_evidence([
        _entry("d2", "decision", "accepted", "Use `build_packet`."),
        _entry("d1", "discovery", "accepted", "Read `build_packet`."),
    ], index)

    assert [record["id"] for record in evidence["unique"].decisions] == ["d1", "d2"]


def test_recent_evidence_ranks_a_node_above_equivalent_old_evidence() -> None:
    index = memory_graph.build_symbol_index(
        {"nodes": [
            {"id": "old", "label": "old_symbol()"},
            {"id": "new", "label": "new_symbol()"},
        ], "links": []}
    )
    assert index is not None

    evidence = graph_evidence.collect_evidence([
        _dated_entry("old", "decision", "accepted", "Use `old_symbol`.", "2020-01-01T00:00:00Z"),
        _dated_entry("new", "decision", "accepted", "Use `new_symbol`.", "2026-08-10T00:00:00Z"),
    ], index)

    assert evidence["new"].score > evidence["old"].score


def test_blocker_priority_cannot_be_overridden_by_recency() -> None:
    index = memory_graph.build_symbol_index(
        {"nodes": [
            {"id": "blocker", "label": "blocked_symbol()"},
            {"id": "decision", "label": "decision_symbol()"},
        ], "links": []}
    )
    assert index is not None

    evidence = graph_evidence.collect_evidence([
        _dated_entry("blocker", "blocker", "proposed", "Blocked on `blocked_symbol`.", "2020-01-01T00:00:00Z"),
        _dated_entry("decision", "decision", "accepted", "Use `decision_symbol`.", "2099-12-31T00:00:00Z"),
    ], index)

    assert evidence["blocker"].score > evidence["decision"].score


def test_malformed_timestamp_is_deterministic_lowest_recency() -> None:
    index = memory_graph.build_symbol_index(
        {"nodes": [
            {"id": "dated", "label": "dated_symbol()"},
            {"id": "malformed", "label": "malformed_symbol()"},
        ], "links": []}
    )
    assert index is not None

    evidence = graph_evidence.collect_evidence([
        _dated_entry("malformed", "decision", "accepted", "Use `malformed_symbol`.", "not-a-timestamp"),
        _dated_entry("dated", "decision", "accepted", "Use `dated_symbol`.", "2026-08-10T00:00:00Z"),
    ], index)

    assert evidence["dated"].score > evidence["malformed"].score
