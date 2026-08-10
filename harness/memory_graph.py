"""The join between memory and the code graph: what a remembered decision touches.

Memory answers "here is what we decided". The code graph answers "here is
what calls this". Separately they are two unrelated replies to the same
question. This module joins them, so a recalled entry reads as *the auth
rewrite you decided last week touches these six symbols* instead of leaving
an agent to guess which files a sentence was about.

The join is one direction only: memory entries in, blast radius out. It
resolves symbol-shaped names out of an entry's summary and tags, matches
them against graph node labels, and renders the transitive dependents of
each match as an extra line inside that entry's rendered unit.

Two properties matter more than coverage:

**Conservative resolution.** A name is annotated only when it is
symbol-*shaped* (backticked, snake_case, camelCase, `call()`, or a filename)
AND resolves to exactly one node. An ambiguous label like `page.tsx`, which
dozens of nodes share in any sizeable Next.js app, is dropped rather than
guessed at.
An ordinary English word is never looked up at all, or every entry
mentioning "auth" would sprout a blast radius for `src/lib/auth.ts`. A wrong
blast radius is worse than none, because an agent will act on it.

**Total degradation.** No graph, an unreadable graph, a graph with no nodes,
a hand-mangled entry -- every one of these yields no annotation and no
exception. Memory that works without a graph must keep working without a
graph; this is an enrichment, never a dependency. When a graph *is* found
but git says it no longer matches the working tree, the caveat is surfaced
as a packet note rather than silently trusted.

Deterministic and offline: no network, no model, no clock. The same store and
the same graph always produce the same annotations.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from harness import graph_location, graph_query

# What `harness.memory_context` accepts as its `annotator` seam: one entry in,
# zero or more impact mappings out. Each mapping carries a pre-rendered
# `line` (what the packet shows) plus structured fields (what JSON consumers
# read). An annotator must never raise -- see `build_annotator`.
Annotator = Callable[[Mapping], "tuple[dict, ...]"]

# How many resolved symbols one entry may be annotated with. An entry naming
# eight symbols is prose, not a change record; annotating all of them would
# spend the whole section budget on a single line.
SYMBOL_LIMIT = 3

# How many files one blast-radius line names before collapsing to "+N more".
FILE_LIMIT = 3

# Upper bound on the file list carried in the structured record. The rendered
# line is already bounded by FILE_LIMIT; this bounds the JSON packet, where a
# hub symbol with 400 dependents would otherwise dominate the payload.
MAX_FILES_RECORDED = 20

# Backtick-quoted text is an explicit statement that something is code, so it
# bypasses the shape filter entirely and is looked up as written.
_BACKTICKED = re.compile(r"`([^`\n]{1,120})`")

# Bare tokens: an identifier, optionally dotted (`module.thing`, `page.tsx`),
# optionally suffixed with call parens.
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?:\(\))?")

_CAMEL = re.compile(r"[a-z0-9][A-Z]")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")

# Health states that make a blast radius potentially wrong. FRESH is the only
# state that earns silence; everything else says so, because an agent acting
# on a stale blast radius is precisely the failure this module exists to
# avoid creating.
_HEALTH_CAVEATS: dict[graph_location.GraphHealth, str] = {
    graph_location.GraphHealth.DIRTY: (
        "code graph is dirty (uncommitted changes since it was built); "
        "blast-radius lines may be out of date"
    ),
    graph_location.GraphHealth.STALE: (
        "code graph is stale (built at an older commit); blast-radius lines "
        "may be out of date"
    ),
    graph_location.GraphHealth.UNKNOWN: (
        "code graph freshness could not be verified; blast-radius lines may "
        "be out of date"
    ),
}


class SymbolIndex(NamedTuple):
    """A graph prepared for label lookup and dependent traversal."""

    nodes: dict[str, dict]
    in_edges: dict[str, list[dict]]
    by_label: dict[str, list[str]]


# --- symbol extraction ----------------------------------------------------


def _normalize(term: str) -> str:
    """Fold a written name and a graph label onto one comparison key.

    graphify labels callables `name()` and types bare, while an entry writes
    both bare -- so the trailing parens come off both sides, and matching is
    case-insensitive because prose capitalizes sentence-initially.
    """
    return term.strip().strip(".,;:!?()[]{}\"'").removesuffix("()").strip().lower()


def _looks_like_symbol(token: str) -> bool:
    """Whether a bare token is shaped like code rather than like English.

    Deliberately strict. Every rule here is a rule about *form*, never about
    a wordlist: `append_entries` and `renderMarkdown()` are code by their
    shape, `auth` and `flow` are not, regardless of whether some node happens
    to carry them as a label.
    """
    if token.endswith("()"):
        return True
    if "_" in token.strip("_"):
        return True
    if _CAMEL.search(token):
        return True
    return bool(_EXTENSION.search(token))


def extract_symbols(entry: Mapping) -> list[str]:
    """Candidate symbol names from an entry's summary and tags, in order.

    Order is first-appearance, deduplicated on the normalized key, so the
    caps applied downstream keep the names the author wrote first rather than
    an arbitrary subset. Non-string fields are ignored rather than coerced --
    a hand-edited entry must not be able to raise from here.
    """
    texts: list[str] = []
    summary = entry.get("summary")
    if isinstance(summary, str):
        texts.append(summary)
    tags = entry.get("tags")
    if isinstance(tags, (list, tuple)):
        texts.extend(t for t in tags if isinstance(t, str))

    found: list[str] = []
    seen: set[str] = set()

    def offer(raw: str) -> None:
        key = _normalize(raw)
        if key and key not in seen:
            seen.add(key)
            found.append(raw.strip())

    for text in texts:
        for match in _BACKTICKED.finditer(text):
            offer(match.group(1))
        # Backticked spans are consumed above; blanking them keeps the bare
        # scan from re-offering the same name with different punctuation.
        bare = _BACKTICKED.sub(" ", text)
        for match in _TOKEN.finditer(bare):
            token = match.group(0)
            if _looks_like_symbol(token):
                offer(token)

    return found


# --- graph preparation ----------------------------------------------------


def build_symbol_index(graph: Mapping) -> SymbolIndex | None:
    """Prepare `graph` for lookup, or None if it is not a usable graph."""
    try:
        index = graph_query.build_index(dict(graph))
    except SystemExit:
        # build_index raises SystemExit for a malformed nodes list. That is
        # the right contract for a CLI and the wrong one here: a bad graph
        # must cost the packet its annotations, not the process its life.
        return None
    if not index.nodes:
        return None

    by_label: dict[str, list[str]] = {}
    for nid, node in index.nodes.items():
        key = _normalize(str(node.get("label") or ""))
        if key:
            by_label.setdefault(key, []).append(nid)
    return SymbolIndex(index.nodes, index.in_edges, by_label)


def resolve_unique(term: str, index: SymbolIndex) -> str | None:
    """Resolve `term` to a node id, or None if it is unknown or ambiguous.

    Unlike `graph_query.resolve_id` this never raises, never falls back to
    substring matching, and never picks among several candidates: exactly one
    match, or nothing.
    """
    if term in index.nodes:
        return term
    key = _normalize(term)
    if not key:
        return None
    matches = index.by_label.get(key)
    if matches is not None and len(matches) == 1:
        return matches[0]
    return None


def blast_radius(node_id: str, index: SymbolIndex) -> tuple[int, list[str]]:
    """Transitive dependents of `node_id`, and the files they live in.

    Same traversal `graph_query`'s `impact` command performs -- unbounded
    depth over in-edges -- so the count here and the count an agent gets from
    `graph_query.py impact` can never disagree.

    Files come back nearest-first: ordered by the fewest hops to any
    dependent living in them, ties broken alphabetically. Plain alphabetical
    order looks tidier and is actively misleading, because only the first
    `FILE_LIMIT` of them get rendered -- on a real graph that handed two
    unrelated symbols the same three distant `page.tsx` files while their
    actual call sites went unmentioned. Sorting by distance puts the direct
    callers in the line and pushes the transitive tail into "+N more".
    """
    found = graph_query.traverse(node_id, index.in_edges, "source", None)
    nearest: dict[str, int] = {}
    for nid, depth in found.items():
        node = index.nodes.get(nid)
        source_file = node.get("source_file") if node else None
        if not source_file:
            continue
        key = str(source_file)
        if depth < nearest.get(key, depth + 1):
            nearest[key] = depth
    files = sorted(nearest, key=lambda f: (nearest[f], f))
    return len(found), files


def render_impact(symbol: str, dependents: int, files: Sequence[str]) -> str:
    """One blast-radius line, indented to sit under its entry.

    ASCII only: this line is rendered into terminals and hook transcripts
    whose encoding is not guaranteed to be UTF-8, and a packet that raises
    UnicodeEncodeError at session start helps nobody.
    """
    if not dependents:
        return f"    graph: `{symbol}` -- no known dependents"
    noun = "dependent" if dependents == 1 else "dependents"
    head = f"    graph: `{symbol}` -- {dependents} {noun}"
    if not files:
        return head
    shown = list(files[:FILE_LIMIT])
    hidden = len(files) - len(shown)
    if hidden > 0:
        shown.append(f"+{hidden} more")
    place = "file" if len(files) == 1 else "files"
    return f"{head} in {len(files)} {place} ({', '.join(shown)})"


# --- the annotator --------------------------------------------------------


def build_annotator(
    project_root: Path,
    graph_path: Path | None = None,
    check_health: bool = True,
) -> tuple[Annotator | None, list[str]]:
    """Build an annotator for `project_root`, plus any packet-level notes.

    Returns `(None, notes)` when no usable graph exists -- callers pass the
    None straight through to `memory_context.build_packet`, which then
    produces exactly the packet it produced before this module existed.

    The graph is loaded and indexed once here, and blast radii are memoized
    per node, so annotating a forty-entry packet costs one parse and a
    handful of traversals rather than one of each per entry.

    `check_health` runs `graph_location.report_health`, which shells out to
    git twice. Callers on a latency budget can turn it off and lose only the
    staleness caveat.
    """
    root = Path(project_root)
    path = (
        graph_path
        if graph_path is not None
        else graph_location.find_graph_in_repo(root)
    )
    if path is None:
        # No graph is the ordinary case in a repo that has never built one.
        # It is not a problem and does not deserve a note.
        return None, []

    graph = graph_location.load_graph_metadata(path)
    if graph is None:
        return None, [
            f"code graph at {path} could not be read (invalid or truncated); "
            f"blast-radius annotation is off"
        ]

    index = build_symbol_index(graph)
    if index is None:
        return None, [
            f"code graph at {path} has no usable nodes; blast-radius "
            f"annotation is off"
        ]

    notes: list[str] = []
    if check_health:
        try:
            # Freshness is a property of the repo that *owns* the graph, which
            # is `report_health`'s own default (`graph.json`'s grandparent).
            # Passing `project_root` instead would judge an explicitly-supplied
            # `--graph` against the wrong working tree.
            health = graph_location.report_health(path)
        except Exception:  # noqa: BLE001 -- health is advisory, never fatal
            health = graph_location.GraphHealth.UNKNOWN
        caveat = _HEALTH_CAVEATS.get(health)
        if caveat:
            notes.append(caveat)

    cache: dict[str, tuple[int, list[str]]] = {}

    def annotate(entry: Mapping) -> tuple[dict, ...]:
        try:
            impacts: list[dict] = []
            for symbol in extract_symbols(entry):
                node_id = resolve_unique(symbol, index)
                if node_id is None:
                    continue
                if node_id not in cache:
                    cache[node_id] = blast_radius(node_id, index)
                dependents, files = cache[node_id]
                impacts.append(
                    {
                        "symbol": symbol,
                        "node_id": node_id,
                        "source_file": index.nodes[node_id].get("source_file"),
                        "dependents": dependents,
                        "files": files[:MAX_FILES_RECORDED],
                        "line": render_impact(symbol, dependents, files),
                    }
                )
                if len(impacts) >= SYMBOL_LIMIT:
                    break
            return tuple(impacts)
        except Exception:  # noqa: BLE001 -- see the module docstring
            # An annotator that raises would take the whole context packet
            # down with it. Enrichment failing closed is the only acceptable
            # failure mode here.
            return ()

    return annotate, notes
