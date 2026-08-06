"""Optional ChromaDB-backed semantic index over a graphify graph.json.

This module is shipped as part of the core `harness` package, but the core
package is stdlib-only -- `import harness.graph_index` MUST succeed even
when `chromadb` is not installed. To guarantee that, `chromadb` is never
imported at module scope; it is only ever imported lazily, inside the
functions that actually need it (`_default_client`, via `client_factory`).

Install the optional extra to use this module for anything beyond
`chromadb_available()`/`index_path_for()`:

    pip install "agent-harness[semantic]"

The index lives at `graphify-out/semantic-index/` (see
`harness.graph_location.INDEX_RELATIVE_PATH`), a sibling of graph.json
inside the directory the harness already gitignores.

Embedding: Chroma's default local embedding function (ONNX all-MiniLM-L6-v2)
is used unless a caller supplies its own `embedding_function` -- fully local,
no API key, no network call per-query (aside from a one-time ~80MB model
download the first time Chroma needs it). Node text (label, metadata
summary/docstring, community name) never leaves the machine.

Building/refreshing the index is a separate, explicit step -- see
`update_index()` and `harness/graph_refresh.py`. Nothing in this module (or
in `harness/graph_query.py`'s `search` subcommand) builds an index
implicitly on first query.
"""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness.graph_location import INDEX_RELATIVE_PATH

INDEX_DIRNAME = INDEX_RELATIVE_PATH.name
COLLECTION_NAME = "graph_nodes"

# A client_factory takes the index directory and returns a chromadb-API-shaped
# client object (real or fake/test-double); it is the seam that lets every
# function in this module be tested without chromadb installed.
ClientFactory = Callable[[Path], Any]


def chromadb_available() -> bool:
    """True if the `chromadb` package is importable. Never raises.

    Mirrors the `importlib.util.find_spec` probe pattern already used by
    `harness.doctor.check_graphify` for the same "is this optional package
    importable" question.
    """
    try:
        return importlib.util.find_spec("chromadb") is not None
    except (ImportError, ValueError):
        return False


def index_path_for(graph_path: Path) -> Path:
    """The semantic-index directory that goes with a given graph.json path.

    `graph_path` is expected to be `.../graphify-out/graph.json`; the index
    lives alongside it as `.../graphify-out/semantic-index/`.
    """
    return graph_path.parent / INDEX_DIRNAME


def node_document(node: dict) -> str:
    """Assemble the text a node is embedded from: label, metadata
    summary/docstring, and community name -- joined with newlines, empty
    pieces dropped.

    Tolerant of missing fields: a bare `{"id": "x"}` node produces an empty
    string rather than raising a KeyError/AttributeError.
    """
    metadata = node.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    parts = [
        node.get("label") or "",
        metadata.get("summary") or "",
        metadata.get("docstring") or "",
        node.get("community_name") or "",
    ]
    return "\n".join(p for p in parts if p)


def node_content_hash(node: dict) -> str:
    """SHA-256 hex digest of `node_document(node)` -- used to detect whether
    a node's embeddable text changed since the index was last built, so
    `update_index` can skip re-embedding unchanged nodes."""
    return hashlib.sha256(node_document(node).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IndexStats:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0


def newest_mtime(directory: Path) -> float | None:
    """Newest mtime (seconds since epoch) among all files under `directory`,
    recursively. None if the directory doesn't exist or contains no files.
    """
    if not directory.exists():
        return None
    mtimes = [f.stat().st_mtime for f in directory.rglob("*") if f.is_file()]
    return max(mtimes) if mtimes else None


def index_is_stale(index_dir: Path, graph_path: Path) -> bool:
    """True if `index_dir` (assumed to exist) is older than `graph_path`,
    by mtime comparison of the index's newest file against graph.json.

    Returns False (never "stale") if the index has no files yet, or if
    `graph_path` doesn't exist -- callers that need to distinguish
    "no index yet" from "stale index" should check `index_dir.exists()`
    themselves first; this function only answers the staleness question.
    """
    index_mtime = newest_mtime(index_dir)
    if index_mtime is None or not graph_path.exists():
        return False
    return index_mtime < graph_path.stat().st_mtime


def _default_client(index_dir: Path) -> Any:
    import chromadb  # local import -- see module docstring; never at module scope

    return chromadb.PersistentClient(path=str(index_dir))


def _get_collection(client: Any, embedding_function: Any = None) -> Any:
    kwargs: dict[str, Any] = {}
    if embedding_function is not None:
        kwargs["embedding_function"] = embedding_function
    return client.get_or_create_collection(name=COLLECTION_NAME, **kwargs)


def update_index(
    graph: dict,
    index_dir: Path,
    client_factory: ClientFactory | None = None,
    embedding_function: Any = None,
) -> IndexStats:
    """Incrementally upsert `graph`'s nodes into the semantic index at
    `index_dir`.

    Only nodes whose `node_content_hash` changed since the index was last
    built are re-embedded/upserted; node ids no longer present in `graph`
    are deleted from the index; everything else is left untouched.

    `client_factory(index_dir)` defaults to a local
    `chromadb.PersistentClient(path=str(index_dir))` -- pass a fake in tests
    to exercise this function without chromadb installed.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    factory = client_factory or _default_client
    client = factory(index_dir)
    collection = _get_collection(client, embedding_function)

    nodes = {n["id"]: n for n in graph.get("nodes", []) if "id" in n}

    existing = collection.get(include=["metadatas"])
    existing_ids = list(existing.get("ids") or [])
    existing_metadatas = list(existing.get("metadatas") or [])
    existing_hashes = {
        nid: (meta or {}).get("content_hash")
        for nid, meta in zip(existing_ids, existing_metadatas)
    }

    to_delete = [nid for nid in existing_ids if nid not in nodes]
    if to_delete:
        collection.delete(ids=to_delete)

    add_ids: list[str] = []
    add_docs: list[str] = []
    add_metas: list[dict] = []
    update_ids: list[str] = []
    update_docs: list[str] = []
    update_metas: list[dict] = []
    unchanged = 0

    for nid, node in nodes.items():
        doc = node_document(node)
        content_hash = node_content_hash(node)
        if nid not in existing_hashes:
            add_ids.append(nid)
            add_docs.append(doc)
            add_metas.append({"content_hash": content_hash})
        elif existing_hashes[nid] != content_hash:
            update_ids.append(nid)
            update_docs.append(doc)
            update_metas.append({"content_hash": content_hash})
        else:
            unchanged += 1

    if add_ids:
        collection.add(ids=add_ids, documents=add_docs, metadatas=add_metas)
    if update_ids:
        collection.upsert(ids=update_ids, documents=update_docs, metadatas=update_metas)

    return IndexStats(
        added=len(add_ids),
        updated=len(update_ids),
        deleted=len(to_delete),
        unchanged=unchanged,
    )


def query_index(
    index_dir: Path,
    text: str,
    limit: int,
    client_factory: ClientFactory | None = None,
    embedding_function: Any = None,
) -> list[tuple[str, float]]:
    """Query the semantic index for the `limit` nodes closest to `text`.

    Returns `(node_id, distance)` pairs, closest first. Distance is
    whatever Chroma's configured similarity space returns (squared L2 for
    the default local embedding function) -- lower means more similar.
    """
    factory = client_factory or _default_client
    client = factory(index_dir)
    collection = _get_collection(client, embedding_function)
    result = collection.query(query_texts=[text], n_results=limit)
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    return list(zip(ids, distances))
