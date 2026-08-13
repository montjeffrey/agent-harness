"""Tests for harness.graph_index -- the optional ChromaDB-backed semantic
index. Almost every test in this file requires no real chromadb install:
each chromadb-touching function is exercised through a fake, in-memory
client injected via `client_factory`, proving the module's client_factory
seam works and that `harness.graph_index` itself never imports chromadb at
module scope (importing it here, without chromadb installed, is itself
part of that proof). The one exception is
`test_default_client_disables_anonymized_telemetry`, which exercises
`_default_client` against a real chromadb install when available (skipped
otherwise) -- there is no fake-client way to prove what kwargs
`_default_client` actually hands to the real `chromadb.PersistentClient`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import graph_index


# --- chromadb_available / index_path_for -----------------------------------


def test_module_imports_without_chromadb_installed() -> None:
    """Importing harness.graph_index must never require chromadb -- this
    test file's own successful collection already proves the import
    succeeded; this test just names the guarantee explicitly."""
    assert graph_index is not None


def test_chromadb_available_requires_a_successful_import(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import importlib.util

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "chromadb":
            raise ImportError("incompatible sqlite runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(builtins, "__import__", broken_import)

    assert graph_index.chromadb_available() is False
    assert graph_index.chromadb_unavailable_reason() == "ImportError: incompatible sqlite runtime"


def test_probe_index_rejects_an_incomplete_directory_without_opening_a_client(
    tmp_path: Path,
) -> None:
    index_dir = tmp_path / "semantic-index"
    index_dir.mkdir()
    calls: list[Path] = []

    def client_factory(path: Path):
        calls.append(path)
        raise AssertionError("the probe must not instantiate an incomplete index")

    problem = graph_index.probe_index(index_dir, client_factory=client_factory)

    assert "chroma.sqlite3" in str(problem)
    assert calls == []


def test_index_path_for_is_sibling_of_graph_json(tmp_path: Path) -> None:
    graph_path = tmp_path / "graphify-out" / "graph.json"
    index_dir = graph_index.index_path_for(graph_path)
    assert index_dir == graph_path.parent / "semantic-index"
    assert index_dir.name == graph_index.INDEX_DIRNAME


# --- node_document / node_content_hash --------------------------------------


def test_node_document_joins_label_metadata_and_community() -> None:
    node = {
        "label": "createUser",
        "metadata": {"summary": "Creates a user record.", "docstring": "docs here"},
        "community_name": "users",
    }
    doc = graph_index.node_document(node)
    assert "createUser" in doc
    assert "Creates a user record." in doc
    assert "docs here" in doc
    assert "users" in doc


def test_node_document_tolerates_missing_fields() -> None:
    assert graph_index.node_document({"id": "x"}) == ""
    assert graph_index.node_document({"label": "onlyLabel"}) == "onlyLabel"
    # metadata present but not a dict must not raise
    assert graph_index.node_document({"label": "x", "metadata": "not-a-dict"}) == "x"
    # metadata is None must not raise
    assert graph_index.node_document({"label": "x", "metadata": None}) == "x"


def test_node_content_hash_is_deterministic() -> None:
    node = {"label": "a", "metadata": {"summary": "s"}, "community_name": "c"}
    h1 = graph_index.node_content_hash(node)
    h2 = graph_index.node_content_hash(dict(node))
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest


def test_node_content_hash_differs_for_different_content() -> None:
    a = {"label": "a"}
    b = {"label": "b"}
    assert graph_index.node_content_hash(a) != graph_index.node_content_hash(b)


def test_node_content_hash_stable_for_empty_document() -> None:
    # Two nodes with no embeddable text at all still hash consistently
    # (sha256 of the empty string), rather than raising.
    assert graph_index.node_content_hash({}) == graph_index.node_content_hash({"id": "y"})


# --- newest_mtime / index_is_stale ------------------------------------------


def test_newest_mtime_none_for_missing_dir(tmp_path: Path) -> None:
    assert graph_index.newest_mtime(tmp_path / "does-not-exist") is None


def test_newest_mtime_none_for_empty_dir(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert graph_index.newest_mtime(empty) is None


def test_index_is_stale_false_when_index_newer(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text("{}", encoding="utf-8")
    index_dir = tmp_path / "semantic-index"
    index_dir.mkdir()
    (index_dir / "chroma.sqlite3").write_text("x", encoding="utf-8")
    import os
    import time

    # ensure a real mtime gap
    now = time.time()
    os.utime(graph_path, (now - 100, now - 100))
    assert graph_index.index_is_stale(index_dir, graph_path) is False


def test_index_is_stale_true_when_graph_newer(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"
    index_dir = tmp_path / "semantic-index"
    index_dir.mkdir()
    (index_dir / "chroma.sqlite3").write_text("x", encoding="utf-8")

    import os
    import time

    now = time.time()
    os.utime(index_dir / "chroma.sqlite3", (now - 100, now - 100))
    graph_path.write_text("{}", encoding="utf-8")
    os.utime(graph_path, (now, now))
    assert graph_index.index_is_stale(index_dir, graph_path) is True


# --- _default_client ---------------------------------------------------------


def test_default_client_disables_anonymized_telemetry(tmp_path: Path) -> None:
    """_default_client must construct chromadb.PersistentClient with
    anonymized_telemetry explicitly disabled. chromadb.config.Settings
    defaults anonymized_telemetry to True; leaving it on would contradict
    README.md's "no telemetry leaves the machine" promise."""
    pytest.importorskip("chromadb")
    client = graph_index._default_client(tmp_path)
    assert client.get_settings().anonymized_telemetry is False


# --- fake in-memory chromadb-shaped client for update_index/query_index -----


class FakeCollection:
    def __init__(self) -> None:
        self._store: dict[str, tuple[str, dict]] = {}
        self.queried_by: str | None = None

    def get(self, include=None):  # noqa: ANN001 -- test double, mirrors chromadb's kwarg
        ids = list(self._store.keys())
        return {
            "ids": ids,
            "metadatas": [self._store[i][1] for i in ids],
            "documents": [self._store[i][0] for i in ids],
        }

    def add(self, ids, documents, metadatas):  # noqa: ANN001
        for i, d, m in zip(ids, documents, metadatas):
            self._store[i] = (d, m)

    def upsert(self, ids, documents, metadatas):  # noqa: ANN001
        self.add(ids, documents, metadatas)

    def delete(self, ids):  # noqa: ANN001
        for i in ids:
            self._store.pop(i, None)

    def query(self, n_results, query_texts=None, query_embeddings=None):  # noqa: ANN001
        """Accepts either form real Chroma accepts, and records which arrived.

        Ordering ignores the probe entirely, so tests that assert on rows keep
        their meaning down both paths; `queried_by` is what makes the choice
        of path observable.
        """
        self.queried_by = "embeddings" if query_embeddings is not None else "texts"
        ids = list(self._store.keys())[:n_results]
        distances = [float(i) for i in range(len(ids))]
        return {"ids": [ids], "distances": [distances]}


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def get_or_create_collection(self, name, **kwargs):  # noqa: ANN001
        return self.collections.setdefault(name, FakeCollection())

    def get_collection(self, name, **kwargs):  # noqa: ANN001 -- mirrors chromadb: raises when missing
        if name not in self.collections:
            raise KeyError(f"Collection {name} does not exist.")
        return self.collections[name]


def _graph(nodes: list[dict]) -> dict:
    return {"nodes": nodes, "links": []}


def test_update_index_adds_new_nodes(tmp_path: Path) -> None:
    client = FakeClient()
    graph = _graph([
        {"id": "n1", "label": "createUser"},
        {"id": "n2", "label": "sendEmail"},
    ])
    stats = graph_index.update_index(graph, tmp_path, client_factory=lambda _d: client)
    assert stats.added == 2
    assert stats.updated == 0
    assert stats.deleted == 0
    assert stats.unchanged == 0


def test_update_index_second_call_with_no_changes_is_all_unchanged(tmp_path: Path) -> None:
    client = FakeClient()
    graph = _graph([{"id": "n1", "label": "createUser"}])
    graph_index.update_index(graph, tmp_path, client_factory=lambda _d: client)
    stats = graph_index.update_index(graph, tmp_path, client_factory=lambda _d: client)
    assert stats == graph_index.IndexStats(added=0, updated=0, deleted=0, unchanged=1)


def test_update_index_detects_changed_content_as_update(tmp_path: Path) -> None:
    client = FakeClient()
    graph_v1 = _graph([{"id": "n1", "label": "createUser"}])
    graph_index.update_index(graph_v1, tmp_path, client_factory=lambda _d: client)

    graph_v2 = _graph([{"id": "n1", "label": "createUserAccount"}])
    stats = graph_index.update_index(graph_v2, tmp_path, client_factory=lambda _d: client)
    assert stats == graph_index.IndexStats(added=0, updated=1, deleted=0, unchanged=0)


def test_update_index_deletes_removed_nodes(tmp_path: Path) -> None:
    client = FakeClient()
    graph_v1 = _graph([
        {"id": "n1", "label": "createUser"},
        {"id": "n2", "label": "deleteUser"},
    ])
    graph_index.update_index(graph_v1, tmp_path, client_factory=lambda _d: client)

    graph_v2 = _graph([{"id": "n1", "label": "createUser"}])
    stats = graph_index.update_index(graph_v2, tmp_path, client_factory=lambda _d: client)
    assert stats == graph_index.IndexStats(added=0, updated=0, deleted=1, unchanged=1)
    assert "n2" not in client.collections[graph_index.COLLECTION_NAME]._store


def test_update_index_mixed_add_update_delete_unchanged(tmp_path: Path) -> None:
    client = FakeClient()
    graph_v1 = _graph([
        {"id": "n1", "label": "stable"},
        {"id": "n2", "label": "willChange"},
        {"id": "n3", "label": "willBeDeleted"},
    ])
    graph_index.update_index(graph_v1, tmp_path, client_factory=lambda _d: client)

    graph_v2 = _graph([
        {"id": "n1", "label": "stable"},
        {"id": "n2", "label": "hasChanged"},
        {"id": "n4", "label": "brandNew"},
    ])
    stats = graph_index.update_index(graph_v2, tmp_path, client_factory=lambda _d: client)
    assert stats == graph_index.IndexStats(added=1, updated=1, deleted=1, unchanged=1)


def test_update_index_creates_index_dir(tmp_path: Path) -> None:
    client = FakeClient()
    index_dir = tmp_path / "does" / "not" / "exist" / "yet"
    graph_index.update_index(_graph([]), index_dir, client_factory=lambda _d: client)
    assert index_dir.exists()


def test_query_index_returns_id_distance_pairs(tmp_path: Path) -> None:
    client = FakeClient()
    graph = _graph([
        {"id": "n1", "label": "createUser"},
        {"id": "n2", "label": "sendEmail"},
    ])
    graph_index.update_index(graph, tmp_path, client_factory=lambda _d: client)

    results = graph_index.query_index(tmp_path, "create a user", 5, client_factory=lambda _d: client)
    assert len(results) == 2
    for nid, distance in results:
        assert nid in {"n1", "n2"}
        assert isinstance(distance, float)


def test_query_index_respects_limit(tmp_path: Path) -> None:
    client = FakeClient()
    graph = _graph([{"id": f"n{i}", "label": f"label{i}"} for i in range(10)])
    graph_index.update_index(graph, tmp_path, client_factory=lambda _d: client)

    results = graph_index.query_index(tmp_path, "anything", 3, client_factory=lambda _d: client)
    assert len(results) == 3


# --- add()/upsert() batch chunking -------------------------------------------


class _RecordingCollection(FakeCollection):
    """FakeCollection that records the size of each add()/upsert() call and
    enforces its owning client's max batch size, mirroring chromadb's own
    batch-size validation (which raises when a call exceeds the limit)."""

    def __init__(self, client: "FakeClientWithMaxBatchSize") -> None:
        super().__init__()
        self._client = client

    def add(self, ids, documents, metadatas):  # noqa: ANN001
        if len(ids) > self._client.max_batch_size:
            raise ValueError(f"batch of {len(ids)} exceeds max {self._client.max_batch_size}")
        self._client.add_call_sizes.append(len(ids))
        super().add(ids, documents, metadatas)

    def upsert(self, ids, documents, metadatas):  # noqa: ANN001
        if len(ids) > self._client.max_batch_size:
            raise ValueError(f"batch of {len(ids)} exceeds max {self._client.max_batch_size}")
        self._client.upsert_call_sizes.append(len(ids))
        super().upsert(ids, documents, metadatas)


class FakeClientWithMaxBatchSize(FakeClient):
    """FakeClient that also exposes get_max_batch_size(), like real chromadb,
    and records how many items each add()/upsert() call received -- used to
    prove update_index chunks large batches instead of exceeding the limit."""

    def __init__(self, max_batch_size: int) -> None:
        super().__init__()
        self.max_batch_size = max_batch_size
        self.add_call_sizes: list[int] = []
        self.upsert_call_sizes: list[int] = []

    def get_max_batch_size(self) -> int:
        return self.max_batch_size

    def get_or_create_collection(self, name, **kwargs):  # noqa: ANN001
        return self.collections.setdefault(name, _RecordingCollection(self))


def test_update_index_chunks_add_batches_under_max_batch_size(tmp_path: Path) -> None:
    """A node set larger than the client's max batch size must be split
    into multiple add() calls, each within the limit, rather than a single
    oversized call that chromadb would reject."""
    client = FakeClientWithMaxBatchSize(max_batch_size=3)
    graph = _graph([{"id": f"n{i}", "label": f"label{i}"} for i in range(10)])

    stats = graph_index.update_index(graph, tmp_path, client_factory=lambda _d: client)

    assert stats.added == 10
    assert all(size <= 3 for size in client.add_call_sizes)
    assert sum(client.add_call_sizes) == 10
    assert len(client.add_call_sizes) == 4  # ceil(10 / 3)


def test_update_index_chunks_upsert_batches_under_max_batch_size(tmp_path: Path) -> None:
    """Same chunking guarantee for the update (upsert) path."""
    client = FakeClientWithMaxBatchSize(max_batch_size=4)
    graph_v1 = _graph([{"id": f"n{i}", "label": f"v1-{i}"} for i in range(9)])
    graph_index.update_index(graph_v1, tmp_path, client_factory=lambda _d: client)
    client.add_call_sizes.clear()

    graph_v2 = _graph([{"id": f"n{i}", "label": f"v2-{i}"} for i in range(9)])
    stats = graph_index.update_index(graph_v2, tmp_path, client_factory=lambda _d: client)

    assert stats.updated == 9
    assert all(size <= 4 for size in client.upsert_call_sizes)
    assert sum(client.upsert_call_sizes) == 9
    assert len(client.upsert_call_sizes) == 3  # ceil(9 / 4)


# --- query_index against a missing collection --------------------------------


def test_query_index_raises_file_not_found_when_collection_missing(tmp_path: Path) -> None:
    """If index_dir exists but its collection was never created/populated
    (e.g. a prior update_index run failed partway through), query_index
    must raise FileNotFoundError rather than silently creating a fresh,
    empty collection and returning zero results -- see
    graph_index._get_existing_collection."""
    client = FakeClient()  # no update_index call -- collection never created
    with pytest.raises(FileNotFoundError):
        graph_index.query_index(tmp_path, "anything", 5, client_factory=lambda _d: client)


# --- embedding the query ourselves rather than letting Chroma do it ----------
#
# Chroma's DefaultEmbeddingFunction builds a fresh ONNXMiniLM_L6_V2 per call,
# discarding the instance whose cached_property holds the inference session.
# Measured against a 500-node index: one cold `search` costs ~705ms through
# query_texts against ~469ms through a precomputed vector. See
# harness.embedding.


def test_a_precomputed_embedding_is_sent_instead_of_the_query_text(
    tmp_path: Path,
) -> None:
    """Passing a vector must reach Chroma as `query_embeddings`.

    Sending `query_texts` alongside would hand the work straight back to the
    collection's own embedding function, which is the cost being avoided.
    """
    client = FakeClient()
    graph_index.update_index(
        _graph([{"id": "n1", "label": "send_welcome_email"}]),
        tmp_path,
        client_factory=lambda _d: client,
    )

    graph_index.query_index(
        tmp_path,
        "unused text",
        5,
        client_factory=lambda _d: client,
        embedding=[0.1, 0.2, 0.3],
    )

    assert client.collections[graph_index.COLLECTION_NAME].queried_by == "embeddings"


def test_the_text_path_still_works_when_no_embedding_is_supplied(
    tmp_path: Path,
) -> None:
    """No embedder available must stay a working query, only a slower one.

    Every caller that has not been taught to pre-embed -- and every install
    without the `semantic` extra -- depends on this default.
    """
    client = FakeClient()
    graph_index.update_index(
        _graph([{"id": "n1", "label": "send_welcome_email"}]),
        tmp_path,
        client_factory=lambda _d: client,
    )

    results = graph_index.query_index(
        tmp_path, "welcome email", 5, client_factory=lambda _d: client
    )

    assert client.collections[graph_index.COLLECTION_NAME].queried_by == "texts"
    assert [nid for nid, _distance in results] == ["n1"]
