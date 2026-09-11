"""Tests for ChromaDB-backed semantic selection.

Everything here runs through a fake client_factory, so the suite exercises
the real code paths with or without the `semantic` extra installed -- the
same seam `tests/test_graph_index.py` uses for the code-graph index.

The load-bearing guarantees: `import harness.memory_index` never needs
chromadb, querying never builds an index, and `auto` falls back to
deterministic selection *visibly* rather than silently.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harness import embedding, memory, memory_context, memory_index, memory_store

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


class FakeCollection:
    """Minimal stand-in for a chromadb collection.

    `query` ranks by counting shared words between the query and each stored
    document -- crude, but it gives a deterministic ordering that differs
    from insertion order, which is exactly what the selector tests need.
    """

    def __init__(self) -> None:
        self.docs: dict[str, str] = {}
        self.metas: dict[str, dict] = {}
        self.queried_by: str | None = None

    def get(self, include: Any = None) -> dict:
        ids = list(self.docs)
        return {"ids": ids, "metadatas": [self.metas[i] for i in ids]}

    def add(self, ids, documents, metadatas) -> None:
        for i, d, m in zip(ids, documents, metadatas):
            self.docs[i] = d
            self.metas[i] = m

    upsert = add

    def delete(self, ids) -> None:
        for i in ids:
            self.docs.pop(i, None)
            self.metas.pop(i, None)

    def query(self, n_results, query_texts=None, query_embeddings=None) -> dict:
        """Rank by shared-word overlap, returned as a distance in [0, 1].

        The distance must reflect *match quality*, not position in this
        index's own result list. Real Chroma distances are comparable across
        indexes that share an embedding space, and the selector relies on
        that to merge project- and machine-scope hits into one ranking.
        A positional 0,1,2... would make every index's best hit tie at 0.0
        and quietly reduce the merge to "whichever index was queried first".

        Accepts either form real Chroma accepts. `FakeEmbeddingFunction`
        returns the text unchanged as its "vector", so ranking is identical
        down both paths and the existing assertions keep their meaning.
        """
        self.queried_by = "embeddings" if query_embeddings is not None else "texts"
        probe = query_embeddings if query_embeddings is not None else query_texts
        wanted = set(probe[0].lower().split())

        def distance(doc: str) -> float:
            if not wanted:
                return 1.0
            return 1.0 - len(wanted & set(doc.lower().split())) / len(wanted)

        scored = sorted(
            ((i, distance(d)) for i, d in self.docs.items()),
            key=lambda kv: (kv[1], kv[0]),
        )[:n_results]
        return {
            "ids": [[i for i, _ in scored]],
            "distances": [[d for _, d in scored]],
        }


class FakeClient:
    """Stand-in for a chromadb PersistentClient, one store per index dir."""

    stores: dict[str, FakeCollection] = {}

    def __init__(self, index_dir: Path) -> None:
        self.key = str(index_dir)

    def get_or_create_collection(self, name: str, **kwargs) -> FakeCollection:
        return FakeClient.stores.setdefault(f"{self.key}::{name}", FakeCollection())

    def get_collection(self, name: str, **kwargs) -> FakeCollection:
        try:
            return FakeClient.stores[f"{self.key}::{name}"]
        except KeyError:
            raise RuntimeError("collection does not exist") from None

    def get_max_batch_size(self) -> int:
        return 2


class FakeEmbeddingFunction:
    """Stand-in for a real embedder that counts how often it is called.

    Returns each document unchanged as its own "vector". `FakeCollection`
    ranks a vector exactly as it ranks text, so substituting this for the
    ONNX embedder changes no ranking -- it only makes the number of embed
    calls observable, which is the thing worth asserting.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, input: list[str]) -> list[str]:
        self.calls.append(list(input))
        return list(input)


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    FakeClient.stores.clear()
    return home


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbeddingFunction:
    """Keep the real ~80MB ONNX model out of every test in this module.

    Without this the default embedder resolves for real whenever chromadb
    happens to be installed, which would make the suite download and run a
    model -- breaking the file's stated promise that it exercises the real
    code paths with or without the `semantic` extra.

    Patched on `harness.embedding`, which is where the held instance lives
    and where `embed_query` looks it up. Patching the name on `memory_index`
    would bind nothing.
    """
    embedder = FakeEmbeddingFunction()
    monkeypatch.setattr(embedding, "default_embedding_function", lambda: embedder)
    return embedder


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _add(project: Path, summary: str, **overrides) -> None:
    entry = {
        "type": "decision",
        "scope": "project",
        "status": "accepted",
        "confidence": "confirmed",
        "source": {"manual": True},
        "summary": summary,
        **overrides,
    }
    memory_store.append_entries(
        [entry], directory=memory_store.project_memory_dir(project), now=NOW
    )


def _entries(project: Path) -> list[dict]:
    return list(memory_store.iter_entries(memory_store.project_memory_dir(project)))


# --- optional-dependency discipline --------------------------------------


def test_the_module_imports_without_chromadb_installed() -> None:
    """The whole context path would break for stdlib-only installs otherwise."""
    import importlib

    importlib.reload(memory_index)
    assert callable(memory_index.resolve_selector)


def test_chromadb_available_never_raises() -> None:
    assert memory_index.chromadb_available() in (True, False)


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

    assert memory_index.chromadb_available() is False
    assert memory_index.chromadb_unavailable_reason() == "ImportError: incompatible sqlite runtime"


def test_probe_index_rejects_an_incomplete_directory_without_opening_a_client(
    tmp_path: Path,
) -> None:
    index_dir = tmp_path / "memory-index"
    index_dir.mkdir()
    calls: list[Path] = []

    def client_factory(path: Path):
        calls.append(path)
        raise AssertionError("the probe must not instantiate an incomplete index")

    problem = memory_index.probe_index(index_dir, client_factory=client_factory)

    assert "chroma.sqlite3" in str(problem)
    assert calls == []


def test_an_unopenable_collection_reports_why_not_just_that_it_is_absent(
    tmp_path: Path,
) -> None:
    """A collection that exists but refuses to open must say so, in the message.

    Chroma rejects a query-time `embedding_function` whose name differs from
    the persisted one. That surfaced as "no collection named ..." -- which
    reads as a missing store and invites a rebuild that cannot fix it. The
    reason has to travel in `str(exc)`, because `probe_index` and the packet
    notes render only that, never `__cause__`.
    """
    index_dir = tmp_path / "memory-index"
    index_dir.mkdir()
    (index_dir / "chroma.sqlite3").write_bytes(b"")

    class RefusingClient:
        def get_collection(self, name: str, **kwargs: Any):
            raise ValueError(
                "Embedding function conflict: new: onnx_mini_lm_l6_v2 vs persisted: default"
            )

    problem = memory_index.probe_index(index_dir, client_factory=lambda _p: RefusingClient())

    assert problem is not None
    assert "Embedding function conflict" in problem
    assert "ValueError" in problem


# --- adaptive strategy ----------------------------------------------------


def test_adaptive_declines_when_there_is_no_query_to_rank_against() -> None:
    """Without a query the semantic selector returns deterministic order."""
    reason = memory_index.semantic_pointless_reason(None, 500)
    assert reason is not None and "objective" in reason
    assert memory_index.semantic_pointless_reason("   ", 500) is not None


def test_adaptive_declines_on_a_store_too_small_for_ranking_to_matter() -> None:
    reason = memory_index.semantic_pointless_reason("billing", 1)
    assert reason is not None and "fits its budget" in reason


def test_adaptive_accepts_a_real_query_against_a_real_store() -> None:
    assert memory_index.semantic_pointless_reason("billing", 500) is None


def test_the_adaptive_size_threshold_is_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(memory_index.ADAPTIVE_MIN_ENTRIES_ENV, "2")
    assert memory_index.adaptive_min_entries() == 2
    assert memory_index.semantic_pointless_reason("billing", 3) is None
    monkeypatch.setenv(memory_index.ADAPTIVE_MIN_ENTRIES_ENV, "not-a-number")
    assert memory_index.adaptive_min_entries() == memory_index.ADAPTIVE_MIN_ENTRIES


def test_adaptive_decides_before_chromadb_is_ever_touched(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The saved cost is the import and the ONNX session, not the query.

    A gate that consulted chromadb to decide would have already spent exactly
    what it exists to avoid.
    """
    monkeypatch.setattr(
        memory_index,
        "chromadb_available",
        lambda: pytest.fail("adaptive must decide without importing chromadb"),
    )
    project_id = memory_store.project_id_for(project)
    selector, strategy, notes = memory_index.resolve_selector(
        project_id, "adaptive", query=None, entry_count=0
    )
    assert strategy == "deterministic"
    assert selector is memory_context.deterministic_selector
    assert any("semantic selection skipped" in note for note in notes)


def test_adaptive_never_silently_satisfies_a_request_for_semantic(
    project: Path,
) -> None:
    """`semantic` must still raise rather than degrade -- benchmarks rely on it."""
    project_id = memory_store.project_id_for(project)
    with pytest.raises(FileNotFoundError):
        memory_index.resolve_selector(
            project_id, "semantic", query=None, entry_count=0
        )


def test_the_index_lives_outside_the_repository(project: Path) -> None:
    """A binary index under .agent-harness/ would be committed with the repo."""
    project_id = memory_store.project_id_for(project)
    index_dir = memory_index.index_dir_for(project_id)
    assert memory_store.machine_memory_dir() in index_dir.parents
    assert project not in index_dir.parents


# --- building -------------------------------------------------------------


def test_build_index_adds_every_entry(project: Path) -> None:
    _add(project, "Rate limiting uses a token bucket.")
    _add(project, "Telemetry is local-only JSONL.")
    stats = memory_index.build_index(
        _entries(project), Path("idx"), client_factory=FakeClient
    )
    assert stats.added == 2
    assert stats.unchanged == 0


def test_rebuilding_unchanged_entries_re_embeds_nothing(project: Path) -> None:
    _add(project, "Rate limiting uses a token bucket.")
    memory_index.build_index(_entries(project), Path("idx"), client_factory=FakeClient)
    stats = memory_index.build_index(
        _entries(project), Path("idx"), client_factory=FakeClient
    )
    assert stats.added == 0
    assert stats.unchanged == 1


def test_a_changed_summary_is_re_embedded(project: Path) -> None:
    _add(project, "Original wording.")
    memory_index.build_index(_entries(project), Path("idx"), client_factory=FakeClient)
    entries = _entries(project)
    entries[0]["summary"] = "Completely different wording now."
    stats = memory_index.build_index(entries, Path("idx"), client_factory=FakeClient)
    assert stats.updated == 1


def test_entries_removed_from_the_store_are_deleted_from_the_index(
    project: Path,
) -> None:
    _add(project, "First fact.")
    _add(project, "Second fact.")
    memory_index.build_index(_entries(project), Path("idx"), client_factory=FakeClient)
    stats = memory_index.build_index(
        _entries(project)[:1], Path("idx"), client_factory=FakeClient
    )
    assert stats.deleted == 1


def test_batching_respects_the_client_max_batch_size(project: Path) -> None:
    """FakeClient caps batches at 2; five entries must still all land."""
    for n in range(5):
        _add(project, f"Fact number {n}.")
    stats = memory_index.build_index(
        _entries(project), Path("idx"), client_factory=FakeClient
    )
    assert stats.added == 5


def test_same_day_entries_in_both_stores_do_not_collide(project: Path) -> None:
    """Ids are unique per store, not globally -- one index spans both stores.

    Keyed on the bare id, a machine entry allocated `mem-...-001` on the same
    day as a project entry would silently displace it and never be embedded.
    """
    _add(project, "A project-scope fact.")
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": "A machine-scope lesson."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    both = _entries(project) + list(
        memory_store.iter_entries(memory_store.machine_memory_dir())
    )
    assert both[0]["id"] == both[1]["id"]  # the collision this guards against

    stats = memory_index.build_index(both, Path("idx"), client_factory=FakeClient)
    assert stats.added == 2


def test_a_hit_from_one_scope_is_not_attributed_to_the_other(project: Path) -> None:
    _add(project, "Rate limiting uses a token bucket.")
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": "Completely unrelated spreadsheet trivia."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    memory_index.build_index(
        _entries(project) + machine, Path("idx"), client_factory=FakeClient
    )

    select = memory_index.semantic_selector({"project": Path("idx")}, client_factory=FakeClient)
    ranked = select(machine, "rate limiting token bucket", 10)
    assert ranked[0]["summary"] == "Completely unrelated spreadsheet trivia."


def test_entries_with_no_embeddable_text_are_skipped(project: Path) -> None:
    entries = [{"id": "mem-2026-08-07-900", "summary": "", "tags": [], "type": ""}]
    stats = memory_index.build_index(entries, Path("idx"), client_factory=FakeClient)
    assert stats.added == 0


# --- querying and selection ----------------------------------------------


def test_querying_a_never_built_index_raises_rather_than_returning_nothing(
    tmp_path: Path,
) -> None:
    """A silently empty result set would look like 'no matches' forever."""
    with pytest.raises(FileNotFoundError):
        memory_index.query_index(
            tmp_path / "missing", "anything", 5, client_factory=FakeClient
        )


def test_the_semantic_selector_reorders_by_similarity(project: Path) -> None:
    _add(project, "Telemetry is local-only JSONL.")
    _add(project, "Rate limiting uses a token bucket.")
    entries = _entries(project)
    memory_index.build_index(entries, Path("idx"), client_factory=FakeClient)

    select = memory_index.semantic_selector({"project": Path("idx")}, client_factory=FakeClient)
    ranked = select(entries, "rate limiting token bucket", 10)
    assert ranked[0]["summary"] == "Rate limiting uses a token bucket."


def test_the_semantic_selector_never_drops_an_unindexed_candidate(
    project: Path,
) -> None:
    """An entry recorded since the last build must still reach the packet."""
    _add(project, "Indexed fact about caching.")
    memory_index.build_index(_entries(project), Path("idx"), client_factory=FakeClient)
    _add(project, "Recorded after the index was built.")

    select = memory_index.semantic_selector({"project": Path("idx")}, client_factory=FakeClient)
    ranked = select(_entries(project), "caching", 10)
    assert len(ranked) == 2
    assert any("after the index" in r["summary"] for r in ranked)


def test_the_semantic_selector_defers_to_deterministic_order_without_a_query(
    project: Path,
) -> None:
    _add(project, "First fact.")
    _add(project, "Second fact.")
    entries = _entries(project)
    memory_index.build_index(entries, Path("idx"), client_factory=FakeClient)

    select = memory_index.semantic_selector({"project": Path("idx")}, client_factory=FakeClient)
    assert select(entries, None, 10) == memory_context.deterministic_selector(
        entries, None, 10
    )


def test_the_selector_respects_its_limit(project: Path) -> None:
    for n in range(10):
        _add(project, f"Fact number {n} about caching.")
    entries = _entries(project)
    memory_index.build_index(entries, Path("idx"), client_factory=FakeClient)
    select = memory_index.semantic_selector({"project": Path("idx")}, client_factory=FakeClient)
    assert len(select(entries, "caching", 3)) == 3


# --- strategy resolution --------------------------------------------------


def test_auto_falls_back_to_deterministic_when_no_index_exists(project: Path) -> None:
    project_id = memory_store.project_id_for(project)
    selector, strategy, notes = memory_index.resolve_selector(project_id, "auto")
    assert strategy == "deterministic"
    assert selector is memory_context.deterministic_selector
    assert notes and "semantic selection unavailable" in notes[0]


def test_auto_uses_semantic_once_an_index_exists(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(
        memory_index, "probe_index", lambda _idx, client_factory=None: None, raising=False
    )
    project_id = memory_store.project_id_for(project)
    memory_index.index_dir_for(project_id).mkdir(parents=True)

    _selector, strategy, notes = memory_index.resolve_selector(
        project_id, "auto", client_factory=FakeClient
    )
    assert strategy == "semantic"
    # Only the project index was created here; the machine-scope note is
    # covered by test_a_partially_built_index_set_reports_which_scope_is_missing.
    assert all("project-scope" not in n for n in notes)


def test_explicit_semantic_raises_instead_of_falling_back(project: Path) -> None:
    """A benchmark that silently ran the other strategy would be worthless."""
    project_id = memory_store.project_id_for(project)
    with pytest.raises(FileNotFoundError):
        memory_index.resolve_selector(project_id, "semantic")


def test_auto_falls_back_when_a_present_index_is_incomplete(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    project_id = memory_store.project_id_for(project)
    memory_index.index_dir_for(project_id).mkdir(parents=True)

    selector, strategy, notes = memory_index.resolve_selector(project_id, "auto")

    assert strategy == "deterministic"
    assert selector is memory_context.deterministic_selector
    assert any("chroma.sqlite3" in note for note in notes)


def test_auto_import_failure_includes_the_source_checkout_install_command(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: False)
    monkeypatch.setattr(
        memory_index,
        "chromadb_unavailable_reason",
        lambda: "ImportError: incompatible sqlite runtime",
    )

    _selector, strategy, notes = memory_index.resolve_selector(
        memory_store.project_id_for(project), "auto"
    )

    assert strategy == "deterministic"
    assert 'python -m pip install -e ".[semantic]"' in notes[0]


def test_explicit_semantic_reports_an_incomplete_present_index(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    project_id = memory_store.project_id_for(project)
    memory_index.index_dir_for(project_id).mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="chroma.sqlite3"):
        memory_index.resolve_selector(project_id, "semantic")


def test_explicit_deterministic_never_touches_the_index(project: Path) -> None:
    project_id = memory_store.project_id_for(project)
    selector, strategy, notes = memory_index.resolve_selector(
        project_id, "deterministic"
    )
    assert (selector, strategy, notes) == (
        memory_context.deterministic_selector, "deterministic", []
    )


def test_an_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown strategy"):
        memory_index.resolve_selector("proj-abc12345", "telepathy")


# --- CLI ------------------------------------------------------------------


def test_context_reports_the_fallback_instead_of_hiding_it(
    project: Path, capsys
) -> None:
    _add(project, "A recorded decision.")
    assert memory.main(["context", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "selection: deterministic" in out
    assert "semantic selection unavailable" in out


def test_context_with_explicit_semantic_and_no_index_exits_1(
    project: Path, capsys
) -> None:
    _add(project, "A recorded decision.")
    code = memory.main(["context", "--project", str(project), "--strategy", "semantic"])
    assert code == 1
    assert "semantic" in capsys.readouterr().err


def test_context_json_records_which_strategy_ran(project: Path, capsys) -> None:
    _add(project, "A recorded decision.")
    memory.main(["context", "--project", str(project), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["strategy"] == "deterministic"
    assert payload["notes"]


def test_index_command_without_chromadb_exits_1(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: False)
    _add(project, "A recorded decision.")
    assert memory.main(["index", "--project", str(project)]) == 1
    assert "chromadb is not installed" in capsys.readouterr().err


def test_index_command_with_no_entries_exits_1(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    assert memory.main(["index", "--project", str(project)]) == 1
    assert "no memory entries" in capsys.readouterr().out


def test_index_command_builds_and_reports_stats(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "_default_client", FakeClient)
    _add(project, "A recorded decision.")
    assert memory.main(["index", "--project", str(project)]) == 0
    assert "added=1" in capsys.readouterr().out


def test_context_uses_semantic_end_to_end_once_indexed(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "_default_client", FakeClient)
    _add(project, "Telemetry is local-only JSONL.")
    _add(project, "Rate limiting uses a token bucket.")

    assert memory.main(["index", "--project", str(project)]) == 0
    capsys.readouterr()
    (memory_index.index_dir_for(memory_store.project_id_for(project)) / "chroma.sqlite3").touch()

    assert memory.main(
        ["context", "--project", str(project), "--json",
         "--query", "rate limiting token bucket"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["strategy"] == "semantic"
    # No machine-scope entries exist, so that index was never built -- which
    # must be reported, not hidden.
    assert any("machine-scope" in n for n in payload["notes"])
    durable = next(s for s in payload["sections"] if s["key"] == "project_durable")
    assert durable["records"][0]["summary"] == "Rate limiting uses a token bucket."


# --- split project/machine indexes ---------------------------------------


def test_machine_memory_gets_one_shared_index_not_a_copy_per_project(
    project: Path,
) -> None:
    """N projects must not mean N embedded copies of the same machine memory."""
    other = memory_index.index_dir_for("some-other-project-1a2b3c4d")
    assert memory_index.machine_index_dir() != memory_index.index_dir_for(
        memory_store.project_id_for(project)
    )
    assert memory_index.machine_index_dir() != other


def test_the_machine_index_name_cannot_collide_with_a_real_project_id(
    project: Path,
) -> None:
    """project_id_for slugs to [a-z0-9-]; a leading underscore is unreachable."""
    assert memory_index.MACHINE_INDEX_NAME.startswith("_")
    assert not memory_store.project_id_for(project).startswith("_")


def test_index_command_builds_both_indexes(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "_default_client", FakeClient)
    _add(project, "A project-scope decision.")
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": "A machine-scope lesson."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    assert memory.main(["index", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "project: indexed 1" in out
    assert "machine: indexed 1" in out


def test_the_selector_merges_hits_across_both_scope_indexes(project: Path) -> None:
    """The relationships section draws from both stores, so ranking must too."""
    _add(project, "Project note about spreadsheet formatting.")
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": "Rate limiting uses a token bucket."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    memory_index.build_index(_entries(project), Path("p"), client_factory=FakeClient)
    memory_index.build_index(machine, Path("m"), client_factory=FakeClient)

    select = memory_index.semantic_selector(
        {"project": Path("p"), "machine": Path("m")}, client_factory=FakeClient
    )
    mixed = _entries(project) + machine
    ranked = select(mixed, "rate limiting token bucket", 10)
    assert ranked[0]["summary"] == "Rate limiting uses a token bucket."
    assert len(ranked) == 2


def test_a_scope_with_no_index_still_reaches_the_packet(project: Path) -> None:
    """Entries of an unbuilt scope rank deterministically, never disappear."""
    _add(project, "Indexed project fact.")
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": "Never-indexed machine lesson."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    memory_index.build_index(_entries(project), Path("p"), client_factory=FakeClient)

    select = memory_index.semantic_selector(
        {"project": Path("p")}, client_factory=FakeClient
    )
    ranked = select(_entries(project) + machine, "indexed fact", 10)
    assert len(ranked) == 2
    assert any("Never-indexed" in r["summary"] for r in ranked)


def test_a_partially_built_index_set_reports_which_scope_is_missing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(
        memory_index, "probe_index", lambda _idx, client_factory=None: None, raising=False
    )
    project_id = memory_store.project_id_for(project)
    memory_index.index_dir_for(project_id).mkdir(parents=True)

    _selector, strategy, notes = memory_index.resolve_selector(
        project_id, "auto", client_factory=FakeClient
    )
    assert strategy == "semantic"
    assert any("machine-scope" in n for n in notes)


def test_both_indexes_built_means_no_notes_at_all(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(
        memory_index, "probe_index", lambda _idx, client_factory=None: None, raising=False
    )
    project_id = memory_store.project_id_for(project)
    memory_index.index_dir_for(project_id).mkdir(parents=True)
    memory_index.machine_index_dir().mkdir(parents=True)

    _selector, strategy, notes = memory_index.resolve_selector(
        project_id, "auto", client_factory=FakeClient
    )
    assert (strategy, notes) == ("semantic", [])


# --- embedding the query once, ourselves ---------------------------------
#
# Chroma's DefaultEmbeddingFunction constructs a fresh ONNXMiniLM_L6_V2 on
# every call, discarding the instance whose cached_property holds the ONNX
# session -- so letting Chroma embed from `query_texts` rebuilt a full
# inference session per scope, per query. Measured on this project's own eval
# set: ~754ms per embed that way versus ~72ms through one held instance.
# Resolution of the held instance itself is covered by tests/test_embedding.py;
# what matters here is that selection uses it, once, and still works without it.


def test_a_precomputed_embedding_is_sent_instead_of_the_query_text(
    project: Path,
) -> None:
    """Passing a vector must reach Chroma as `query_embeddings`.

    Sending `query_texts` alongside would hand the work straight back to the
    collection's own embedding function, which is what we are avoiding.
    """
    _add(project, "Chose Postgres over MySQL for row level security.")
    memory_index.build_index(_entries(project), Path("idx"), client_factory=FakeClient)

    memory_index.query_index(
        Path("idx"), "unused text", 5, client_factory=FakeClient, embedding="postgres row"
    )
    collection = FakeClient.stores["idx::memory_entries"]
    assert collection.queried_by == "embeddings"


def test_the_query_is_embedded_once_no_matter_how_many_scopes_are_searched(
    project: Path, fake_embedder: FakeEmbeddingFunction
) -> None:
    """Two scope indexes must not mean two identical embeddings.

    `semantic_selector` groups candidates by scope and queries one index per
    group. Embedding inside that loop meant paying the full per-query cost
    once per scope for the very same string -- and it would multiply again
    once partitions add more indexes.
    """
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": "Kill processes by exact PID, never by image name."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    _add(project, "Chose Postgres over MySQL for row level security.")
    machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    memory_index.build_index(_entries(project), Path("p"), client_factory=FakeClient)
    memory_index.build_index(machine, Path("m"), client_factory=FakeClient)

    select = memory_index.semantic_selector(
        {"project": Path("p"), "machine": Path("m")}, client_factory=FakeClient
    )
    fake_embedder.calls.clear()
    ranked = select(_entries(project) + machine, "kill processes by pid", 10)

    assert fake_embedder.calls == [["kill processes by pid"]]
    assert len(ranked) == 2
    assert FakeClient.stores["p::memory_entries"].queried_by == "embeddings"
    assert FakeClient.stores["m::memory_entries"].queried_by == "embeddings"


def test_selection_still_ranks_by_text_when_no_embedder_is_available(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an embedder, fall back to letting Chroma embed from text.

    Ranking quality is unchanged; only the cost differs. Refusing to select
    here would turn a performance optimisation into a hard dependency.
    """
    monkeypatch.setattr(embedding, "default_embedding_function", lambda: None)
    _add(project, "Chose Postgres over MySQL for row level security.")
    _add(project, "Rewrote the login flow to use magic links.")
    memory_index.build_index(_entries(project), Path("idx"), client_factory=FakeClient)

    select = memory_index.semantic_selector(
        {"project": Path("idx")}, client_factory=FakeClient
    )
    ranked = select(_entries(project), "login magic links", 10)

    assert FakeClient.stores["idx::memory_entries"].queried_by == "texts"
    assert "magic links" in ranked[0]["summary"]


# --- per-scope selector resolution (cross-project search) -----------------


def test_scopes_limits_which_indexes_a_selector_routes_on(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A search source is one scope; a note about the *other* scope's missing
    index must not be attached to it."""
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(
        memory_index, "probe_index", lambda _idx, client_factory=None: None, raising=False
    )
    project_id = memory_store.project_id_for(project)
    memory_index.index_dir_for(project_id).mkdir(parents=True)

    _sel, strategy, notes = memory_index.resolve_selector(
        project_id, "auto", client_factory=FakeClient, scopes=("project",)
    )
    assert strategy == "semantic"
    assert notes == []

    _sel, strategy, notes = memory_index.resolve_selector(
        project_id, "auto", client_factory=FakeClient, scopes=("machine",)
    )
    assert strategy == "deterministic"
    assert any(str(memory_index.machine_index_dir()) in n for n in notes)

    with pytest.raises(ValueError, match="unknown scopes"):
        memory_index.resolve_selector(project_id, "auto", scopes=("nowhere",))


def test_a_search_uses_each_projects_own_index_and_states_the_rest(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project B has an index, project A does not: B is ranked semantically
    through B's index, A deterministically with a note naming A, and no
    index is ever built by the search."""
    from harness import mcp_server

    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(
        memory_index, "probe_index", lambda _idx, client_factory=None: None, raising=False
    )
    monkeypatch.setenv(memory_index.ADAPTIVE_MIN_ENTRIES_ENV, "0")

    other = tmp_path / "other"
    other.mkdir()
    memory_store.init_project(other, now=NOW)
    _add(other, "Beta chose exponential backoff for retries")
    _add(other, "Beta logs to JSONL")
    other_id = memory_store.project_id_for(other)
    memory_index.build_index(_entries(other), memory_index.index_dir_for(other_id), client_factory=FakeClient)

    _add(project, "Alpha retry decision")
    project_id = memory_store.project_id_for(project)

    def never_build(*args, **kwargs):
        raise AssertionError("a search must never build an index")

    monkeypatch.setattr(memory_index, "build_index", never_build)

    result = memory_context.search(
        "retries backoff",
        [(project_id, _entries(project)), ("machine", []), (other_id, _entries(other))],
        limit=4,
        resolver=mcp_server._search_selector_resolver(project_id, "retries backoff", client_factory=FakeClient),
    )
    assert dict(result.strategies) == {
        project_id: "deterministic", "machine": "no live entries", other_id: "semantic"
    }
    assert any(n.startswith(f"{project_id}: ") and "no semantic index" in n for n in result.notes)
    assert not any("machine-scope" in n for n in result.notes)
    assert [h.entry["summary"] for h in result.hits if h.source == other_id][0].startswith("Beta chose")
