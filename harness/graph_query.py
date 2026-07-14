"""Query a graphify graph.json without loading it whole into an agent context.

Usage:
    python graph_query.py find <term>            # search node labels/files
    python graph_query.py node <id>               # node details + direct edges
    python graph_query.py callers <id> [depth]    # who depends on this (default 1)
    python graph_query.py callees <id> [depth]    # what this depends on (default 1)
    python graph_query.py impact <id>             # transitive dependents (blast radius)
    python graph_query.py file <path-fragment>    # nodes defined in matching files
    python graph_query.py community <name-frag>   # nodes in matching communities

Options:
    --graph PATH   explicit path to graph.json (skips auto-discovery)

By default the graph is located by walking up from the current working
directory looking for a `graphify-out/graph.json`. Complements graphify's
own CLI: `python -m graphify explain "X"` and `python -m graphify path "A" "B"`.
Refresh the graph with graph_refresh.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

MAX_ROWS = 60
GRAPH_RELATIVE_PATH = Path("graphify-out") / "graph.json"


class GraphIndex(NamedTuple):
    nodes: dict[str, dict]
    out_edges: dict[str, list[dict]]
    in_edges: dict[str, list[dict]]


def find_graph_path(start: Path | None = None) -> Path:
    """Walk upward from `start` (default: cwd) looking for graphify-out/graph.json."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        graph_path = candidate / GRAPH_RELATIVE_PATH
        if graph_path.exists():
            return graph_path
    raise SystemExit(
        f"graph.json not found under {current} or any parent directory -- "
        "run graph_refresh.py first, or pass --graph PATH."
    )


def load_graph(graph_path: Path) -> dict:
    if not graph_path.exists():
        raise SystemExit(f"graph.json not found at {graph_path}")
    with graph_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def node_line(n: dict) -> str:
    loc = f"{n.get('source_file', '?')}:{n.get('source_location', '?')}"
    return f"{n['id']}  [{n.get('community_name', '')}]  {n.get('label', '')}  ({loc})"


def build_index(g: dict) -> GraphIndex:
    nodes = {n["id"]: n for n in g["nodes"]}
    out_edges: dict[str, list[dict]] = defaultdict(list)
    in_edges: dict[str, list[dict]] = defaultdict(list)
    for e in g["links"]:
        out_edges[e["source"]].append(e)
        in_edges[e["target"]].append(e)
    return GraphIndex(nodes, out_edges, in_edges)


def resolve_id(term: str, nodes: dict[str, dict]) -> str:
    if term in nodes:
        return term
    low = term.lower()
    exact = [i for i, n in nodes.items() if n.get("label", "").lower() == low]
    if len(exact) == 1:
        return exact[0]
    partial = [
        i for i, n in nodes.items() if low in n.get("label", "").lower() or low in i.lower()
    ]
    if len(partial) == 1:
        return partial[0]
    if not partial and not exact:
        raise SystemExit(f"no node matches '{term}' -- try: find {term}")
    matches = exact or partial
    lines = [f"'{term}' is ambiguous ({len(matches)} matches):"]
    lines.extend(f"  {node_line(nodes[i])}" for i in matches[:MAX_ROWS])
    raise SystemExit("\n".join(lines))


def traverse(start: str, edges: dict[str, list[dict]], key: str, depth: int) -> dict[str, int]:
    seen: dict[str, int] = {}
    frontier = [start]
    for d in range(1, depth + 1):
        nxt: list[str] = []
        for nid in frontier:
            for e in edges.get(nid, []):
                other = e[key]
                if other not in seen and other != start:
                    seen[other] = d
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    return seen


def format_rows(rows: list[str]) -> list[str]:
    if len(rows) <= MAX_ROWS:
        return rows
    return [*rows[:MAX_ROWS], f"... {len(rows) - MAX_ROWS} more (narrow your query)"]


def cmd_find(g: dict, term: str) -> list[str]:
    low = term.lower()
    hits = [
        n
        for n in g["nodes"]
        if low in n.get("label", "").lower()
        or low in n["id"].lower()
        or low in n.get("source_file", "").lower()
    ]
    return [f"{len(hits)} node(s) match '{term}':", *format_rows([node_line(n) for n in hits])]


def cmd_file(g: dict, term: str) -> list[str]:
    low = term.lower().replace("\\", "/")
    hits = [n for n in g["nodes"] if low in n.get("source_file", "").lower()]
    return [
        f"{len(hits)} node(s) in files matching '{term}':",
        *format_rows([node_line(n) for n in hits]),
    ]


def cmd_community(g: dict, term: str) -> list[str]:
    low = term.lower()
    hits = [n for n in g["nodes"] if low in n.get("community_name", "").lower()]
    return [
        f"{len(hits)} node(s) in communities matching '{term}':",
        *format_rows([node_line(n) for n in hits]),
    ]


def cmd_node(index: GraphIndex, nid: str) -> list[str]:
    n = index.nodes[nid]
    lines = [
        node_line(n),
        f"  metadata: {n.get('metadata', '')}",
        f"  in-edges: {len(index.in_edges.get(nid, []))}  out-edges: {len(index.out_edges.get(nid, []))}",
    ]
    for e in (index.in_edges.get(nid, []) + index.out_edges.get(nid, []))[:MAX_ROWS]:
        arrow = "<-" if e["target"] == nid else "->"
        other = e["source"] if e["target"] == nid else e["target"]
        lines.append(f"  {arrow} {e['relation']:<12} {other}")
    return lines


def cmd_traverse(index: GraphIndex, nid: str, mode: str, depth: int) -> list[str]:
    edges, key = (
        (index.in_edges, "source") if mode in ("callers", "impact") else (index.out_edges, "target")
    )
    eff_depth = 99 if mode == "impact" else depth
    found = traverse(nid, edges, key, eff_depth)
    label = {
        "callers": "dependent(s)",
        "callees": "dependency(ies)",
        "impact": "transitive dependent(s)",
    }[mode]
    rows = [
        f"d{d}  {node_line(index.nodes[i])}"
        for i, d in sorted(found.items(), key=lambda kv: kv[1])
        if i in index.nodes
    ]
    return [f"{len(found)} {label} of {nid}:", *format_rows(rows)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", type=Path, default=None, help="explicit path to graph.json")
    parser.add_argument("command", choices=["find", "node", "callers", "callees", "impact", "file", "community"])
    parser.add_argument("term", help="node id, label fragment, file fragment, or community fragment")
    parser.add_argument("depth", nargs="?", type=int, default=1, help="traversal depth for callers/callees")
    return parser


def run(argv: list[str]) -> list[str]:
    """Execute a query and return the output lines (used directly by tests)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    graph_path = args.graph if args.graph else find_graph_path()
    g = load_graph(graph_path)
    index = build_index(g)

    if args.command == "find":
        return cmd_find(g, args.term)
    if args.command == "file":
        return cmd_file(g, args.term)
    if args.command == "community":
        return cmd_community(g, args.term)

    nid = resolve_id(args.term, index.nodes)
    if args.command == "node":
        return cmd_node(index, nid)
    return cmd_traverse(index, nid, args.command, args.depth)


def main(argv: list[str] | None = None) -> int:
    try:
        lines = run(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse already printed its own usage/error text for arg-parsing
        # failures (exc.code is an int); only echo our own string messages.
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 1
        return exc.code if isinstance(exc.code, int) else 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
