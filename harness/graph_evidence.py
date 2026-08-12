"""Graph -> memory: what a graph node has already been decided about.

This is the *reverse* of :mod:`memory_graph`, and the two are easy to
confuse, so the direction is worth stating plainly:

- :mod:`memory_graph` takes a memory entry and returns its blast radius --
  memory in, graph out. It answers "what does this decision touch?"
- This module takes graph nodes and returns the memory that mentions them --
  graph in, memory out. It answers "what has already been said about this
  symbol?", so `find` can rank a symbol a recent decision touched ahead of
  equally-named ones nobody has discussed.

Both directions share one resolution rule and neither guesses. This is
intentionally a small join layer: it does not search prose or use semantic
indexes. Memory entries are associated only through the conservative symbol
extraction and exact-unique resolution in :mod:`memory_graph`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from harness import memory_graph, memory_store


@dataclass(frozen=True)
class NodeEvidence:
    """Accepted decision/reference records and live work for one node."""

    decisions: tuple[dict, ...] = ()
    tasks: tuple[dict, ...] = ()
    blockers: tuple[dict, ...] = ()
    recency: int = 0

    @property
    def score(self) -> int:
        """A stable relevance tiebreaker; blockers outweigh passive history."""
        return (
            len(self.blockers) * 100
            + len(self.tasks) * 10
            + len(self.decisions)
            + self.recency
        )

    def lines(self) -> list[str]:
        lines: list[str] = []
        for label, records in (
            ("accepted decision/reference", self.decisions),
            ("open task", self.tasks),
            ("blocker", self.blockers),
        ):
            for record in records:
                lines.append(
                    f"  memory {label}: {record.get('id', '?')} -- "
                    f"{record.get('summary', '')}"
                )
        return lines


def _kind(record: Mapping) -> str | None:
    entry_type = record.get("type")
    status = record.get("status")
    if status == "accepted" and entry_type not in {"task", "blocker"}:
        return "decisions"
    if entry_type == "task" and status in {"proposed", "accepted"}:
        return "tasks"
    if entry_type == "blocker" and status in {"proposed", "accepted"}:
        return "blockers"
    return None


def _timestamp(record: Mapping) -> datetime | None:
    """Parse a UTC-normalized entry timestamp, treating invalid input as absent."""
    value = record.get("ts")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _recency_scores(grouped: Mapping[str, Mapping[str, list[dict]]]) -> dict[str, int]:
    """Return bounded, corpus-relative recency scores for eligible node evidence."""
    latest = {
        node_id: max(
            (timestamp for records in parts.values() for record in records
             if (timestamp := _timestamp(record)) is not None),
            default=None,
        )
        for node_id, parts in grouped.items()
    }
    timestamps = sorted({timestamp for timestamp in latest.values() if timestamp is not None})
    if not timestamps:
        return {node_id: 0 for node_id in grouped}
    rank = {timestamp: position for position, timestamp in enumerate(timestamps)}
    ceiling = 8
    divisor = max(len(timestamps) - 1, 1)
    return {
        node_id: 0 if timestamp is None else 1 + (rank[timestamp] * (ceiling - 1) // divisor)
        for node_id, timestamp in latest.items()
    }


def collect_evidence(
    entries: list[Mapping], index: memory_graph.SymbolIndex
) -> dict[str, NodeEvidence]:
    """Aggregate eligible entries by their *exactly* resolved graph node.

    A record mentioning several unique symbols is evidence for each of those
    nodes.  Ordering uses entry id (then summary) rather than inbox order so
    reformatting an inbox cannot reshuffle graph-query output.
    """
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: {"decisions": [], "tasks": [], "blockers": []}
    )
    for entry in entries:
        kind = _kind(entry)
        if kind is None:
            continue
        record = dict(entry)
        node_ids: set[str] = set()
        for symbol in memory_graph.extract_symbols(record):
            node_id = memory_graph.resolve_unique(symbol, index)
            if node_id is not None:
                node_ids.add(node_id)
        for node_id in node_ids:
            grouped[node_id][kind].append(record)

    def ordered(records: list[dict]) -> tuple[dict, ...]:
        return tuple(sorted(records, key=lambda r: (str(r.get("id") or ""), str(r.get("summary") or ""))))

    recency_scores = _recency_scores(grouped)
    return {
        node_id: NodeEvidence(
            decisions=ordered(parts["decisions"]),
            tasks=ordered(parts["tasks"]),
            blockers=ordered(parts["blockers"]),
            recency=recency_scores[node_id],
        )
        for node_id, parts in grouped.items()
    }


def project_evidence(project_root: Path, graph: Mapping) -> dict[str, NodeEvidence]:
    """Read project memory and return graph-node evidence, or an empty map."""
    index = memory_graph.build_symbol_index(graph)
    if index is None:
        return {}
    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(project_root)))
    return collect_evidence(entries, index)
