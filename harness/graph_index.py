"""Optional ChromaDB-backed semantic index over a graphify graph.json.

This module is shipped as part of the core `harness` package, but the core
package is stdlib-only -- `import harness.graph_index` MUST succeed even
when `chromadb` is not installed. To guarantee that, `chromadb` is never
imported at module scope; it is only ever imported lazily, inside the
functions that actually need it (`_default_client`, via `client_factory`).

Install the optional extra to use this module for anything beyond
`chromadb_available()`/`index_path_for()`:

    python -m pip install -e ".[semantic]"  # run from this checkout

The index lives at `graphify-out/semantic-index/` (see
`harness.graph_location.INDEX_RELATIVE_PATH`), a sibling of graph.json
inside the directory enroll and install exclude from git.

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


def chromadb_status() -> tuple[bool, str | None]:
    """Return whether Chroma can actually import, with a safe failure reason.

    ``find_spec`` only proves a package directory exists.  A broken optional
    install can still fail while importing its extension modules, so semantic
    callers must use this guarded real import before advertising the feature.
    """
    try:
        import chromadb  # noqa: F401 - deliberately local optional import
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def chromadb_available() -> bool:
    """True only when the optional ``chromadb`` package imports cleanly."""
    return chromadb_status()[0]


def chromadb_unavailable_reason() -> str | None:
    """Explain a failed guarded Chroma import without raising."""
    return chromadb_status()[1]


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

    # anonymized_telemetry defaults to True in chromadb's own Settings; leaving
    # it on would contradict the "no telemetry leaves the machine" promise in
    # README.md (chromadb<1.x ships this as a real PostHog capture call that
    # this flag disables outright -- see chromadb.telemetry.product.posthog).
    settings = chromadb.config.Settings(anonymized_telemetry=False)
    return chromadb.PersistentClient(path=str(index_dir), settings=settings)


def _get_collection(client: Any, embedding_function: Any = None) -> Any:
    kwargs: dict[str, Any] = {}
    if embedding_function is not None:
        kwargs["embedding_function"] = embedding_function
    return client.get_or_create_collection(name=COLLECTION_NAME, **kwargs)


def _max_batch_size(client: Any) -> int | None:
    """The chromadb-enforced max add()/upsert() batch size for `client`
    (`client.get_max_batch_size()`), or None if the client doesn't expose
    one -- e.g. a minimal test double. Callers should treat None as "no
    known limit, don't chunk" rather than raising."""
    getter = getattr(client, "get_max_batch_size", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _add_in_batches(
    collection: Any,
    method: str,
    ids: list[str],
    docs: list[str],
    metas: list[dict],
    batch_size: int | None,
) -> None:
    """Call `collection.<method>(ids=..., documents=..., metadatas=...)`
    once per chunk of at most `batch_size` items.

    Chroma enforces a max batch size per add()/upsert() call (environment-
    dependent) and raises `chromadb.errors.InvalidArgumentError` when it's
    exceeded -- without chunking, a first build of a large repo (or a large
    changed-node set) can blow past it, and `update_semantic_index`'s caller
    treats that as non-fatal, silently leaving the index empty/stale.
    `batch_size` of None/<=0 means "no limit known" -- everything goes in
    one call, matching the pre-chunking behavior.
    """
    if not ids:
        return
    op = getattr(collection, method)
    size = batch_size if batch_size and batch_size > 0 else len(ids)
    for start in range(0, len(ids), size):
        end = start + size
        op(ids=ids[start:end], documents=docs[start:end], metadatas=metas[start:end])


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

    max_batch_size = _max_batch_size(client)
    _add_in_batches(collection, "add", add_ids, add_docs, add_metas, max_batch_size)
    _add_in_batches(collection, "upsert", update_ids, update_docs, update_metas, max_batch_size)

    return IndexStats(
        added=len(add_ids),
        updated=len(update_ids),
        deleted=len(to_delete),
        unchanged=unchanged,
    )


def _get_existing_collection(client: Any, embedding_function: Any = None) -> Any:
    """Look up the semantic index's collection for the read-only query path.

    Deliberately uses `client.get_collection` -- NOT `get_or_create_collection`
    (which `_get_collection`/`update_index` use on the write path, where
    fabricating a fresh collection on first build is the whole point) --
    so that a missing collection raises instead of silently handing back a
    brand-new, empty one. Without this, if a prior `update_index` run made
    it past `index_dir.mkdir()` but crashed before the collection was
    populated, `query_index` would look successful while always returning
    zero results, instead of surfacing the same "no semantic index found,
    build one first" guidance `graph_query.cmd_search` already gives when
    `index_dir` itself doesn't exist.

    Raises `FileNotFoundError` when the collection doesn't exist, wrapping
    whatever `client.get_collection` raised for that -- real chromadb raises
    `InvalidCollectionException` pre-1.0 or `NotFoundError` from 1.0+ (both
    subclasses of `chromadb.errors.ChromaError`; verified directly against
    chromadb 0.5.23 and 1.5.9, the ends of the `chromadb>=0.5` range this
    project declares). The original exception is chained (`from exc`) so
    anything unexpected is still visible, not silently swallowed; this
    module is never allowed to import chromadb at module scope (see module
    docstring), so callers here can't `except chromadb.errors.ChromaError`
    directly, and a `client_factory` test double naturally won't raise a
    real chromadb exception type anyway -- translating unconditionally is
    what makes this work the same whether or not chromadb is installed.
    """
    kwargs: dict[str, Any] = {}
    if embedding_function is not None:
        kwargs["embedding_function"] = embedding_function
    try:
        return client.get_collection(name=COLLECTION_NAME, **kwargs)
    except Exception as exc:
        # Carry the underlying reason in the *message*, not only in `__cause__`:
        # `probe_index` and the fallback notes render `str(exc)` alone, so a
        # cause left only on the chain is invisible to the person reading the
        # packet or the doctor report. A collection that exists but refuses to
        # open -- Chroma rejects a query-time `embedding_function` whose name
        # differs from the persisted one -- otherwise reads as a missing store
        # and invites a rebuild, which cannot fix it.
        raise FileNotFoundError(
            f"could not open collection '{COLLECTION_NAME}' in this semantic index"
            f" ({type(exc).__name__}: {exc})"
        ) from exc


def probe_index(
    index_dir: Path, client_factory: ClientFactory | None = None
) -> str | None:
    """Return a visible readiness error, if a persisted index cannot be read.

    This opens the client and existing collection only.  It never creates a
    collection, queries it, embeds text, or rebuilds an index.
    """
    database_path = Path(index_dir) / "chroma.sqlite3"
    if not database_path.is_file():
        return f"FileNotFoundError: missing persistent database {database_path.name}"
    factory = client_factory or _default_client
    try:
        _get_existing_collection(factory(Path(index_dir)))
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def query_index(
    index_dir: Path,
    text: str,
    limit: int,
    client_factory: ClientFactory | None = None,
    embedding_function: Any = None,
    embedding: Any = None,
) -> list[tuple[str, float]]:
    """Query the semantic index for the `limit` nodes closest to `text`.

    Returns `(node_id, distance)` pairs, closest first. Distance is
    whatever Chroma's configured similarity space returns (squared L2 for
    the default local embedding function) -- lower means more similar.

    Supplying `embedding` -- a vector the caller already computed, via
    `harness.embedding.embed_query` -- keeps the collection's own embedding
    function out of the hot path. That function is Chroma's
    `DefaultEmbeddingFunction`, which rebuilds a whole ONNX inference session
    per call. Measured against a 500-node index: one cold `search` costs
    ~705ms through `query_texts` against ~469ms through a precomputed vector,
    and the margin widens to ~3.6x per query in any process that searches more
    than once. Ranking is unaffected, because the vector comes from the very
    model Chroma would have used.

    Passing a collection-level `embedding_function` is not an alternative:
    Chroma rejects one whose name differs from the persisted one. See
    `harness.embedding`.

    Raises `FileNotFoundError` if `index_dir` exists but the index's
    collection doesn't (e.g. a prior `update_index` build failed partway
    through) -- see `_get_existing_collection`.
    """
    factory = client_factory or _default_client
    client = factory(index_dir)
    collection = _get_existing_collection(client, embedding_function)
    if embedding is None:
        result = collection.query(query_texts=[text], n_results=limit)
    else:
        result = collection.query(query_embeddings=[embedding], n_results=limit)
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    return list(zip(ids, distances))
