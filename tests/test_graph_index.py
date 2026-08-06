"""Tests for harness.graph_index -- the optional ChromaDB-backed semantic
index. No test in this file requires a real chromadb install: every
chromadb-touching function is exercised through a fake, in-memory client
injected via `client_factory`, proving the module's client_factory seam
works and that `harness.graph_index` itself never imports chromadb at
module scope (importing it here, without chromadb installed, is itself
part of that proof).
"""

from __future__ import annotations

from pathlib import Path

from harness import graph_index


# --- chromadb_available / index_path_for -----------------------------------


def test_module_imports_without_chromadb_installed() -> None:
    """Importing harness.graph_index must never require chromadb -- this
    test file's own successful collection already proves the import
    succeeded; this test just names the guarantee explicitly."""
    assert graph_index is not None


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


# --- fake in-memory chromadb-shaped client for update_index/query_index -----


class FakeCollection:
    def __init__(self) -> None:
        self._store: dict[str, tuple[str, dict]] = {}

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

    def query(self, query_texts, n_results):  # noqa: ANN001
        ids = list(self._store.keys())[:n_results]
        distances = [float(i) for i in range(len(ids))]
        return {"ids": [ids], "distances": [distances]}


class FakeClient:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def get_or_create_collection(self, name, **kwargs):  # noqa: ANN001
        return self.collections.setdefault(name, FakeCollection())


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
