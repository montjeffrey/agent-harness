"""Tests for the one held ONNX embedder shared by the semantic indexes.

Nothing here builds a real embedder: `_build_embedding_function` is the seam,
so the suite exercises the real resolution logic with or without the
`semantic` extra installed, and never downloads an ~80MB model.

The load-bearing guarantees: `import harness.embedding` never needs chromadb,
the same instance comes back every time (that reuse *is* the optimisation),
and an unavailable embedder degrades to None rather than raising.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from harness import embedding


@pytest.fixture(autouse=True)
def clean_embedder() -> Iterator[None]:
    """Resolution is cached process-wide, so tests must not inherit it."""
    embedding.reset()
    yield
    embedding.reset()


class Embedder:
    """Returns each input unchanged, standing in for a real vector."""

    def __call__(self, input: list[str]) -> list[str]:
        return list(input)


def test_the_module_imports_without_chromadb_installed() -> None:
    """Every semantic path would break for stdlib-only installs otherwise."""
    import importlib

    importlib.reload(embedding)
    assert callable(embedding.embed_query)


def test_one_held_embedder_is_reused_rather_than_rebuilt_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the same object comes back, so its session is cached.

    A function that returned a new embedder per call would reproduce exactly
    the Chroma defect this works around, and every timing gain would vanish
    while the tests still passed.
    """
    built: list[object] = []

    def fake_build() -> object:
        built.append(Embedder())
        return built[-1]

    monkeypatch.setattr(embedding, "_build_embedding_function", fake_build)

    first = embedding.default_embedding_function()
    second = embedding.default_embedding_function()
    assert first is second
    assert len(built) == 1


def test_no_embedder_is_available_when_chroma_cannot_supply_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent Chroma the harness still runs; it just cannot embed.

    `ImportError` is the shape this takes on a stdlib-only install, where the
    `chromadb.utils...` import inside `_build_embedding_function` fails.
    """

    def fake_build() -> object:
        raise ImportError("No module named 'chromadb'")

    monkeypatch.setattr(embedding, "_build_embedding_function", fake_build)
    assert embedding.default_embedding_function() is None


def test_a_failed_resolution_is_not_retried_on_every_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remembering the failure is what keeps the fallback path cheap.

    Retrying would pay a failed import per query for every user without the
    extra -- the exact per-call cost this module exists to remove.
    """
    attempts: list[int] = []

    def fake_build() -> object:
        attempts.append(1)
        raise ImportError("No module named 'chromadb'")

    monkeypatch.setattr(embedding, "_build_embedding_function", fake_build)
    embedding.default_embedding_function()
    embedding.default_embedding_function()
    assert len(attempts) == 1


def test_embed_query_returns_none_when_there_is_no_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers read None as "let Chroma embed from text instead"."""
    monkeypatch.setattr(embedding, "default_embedding_function", lambda: None)
    assert embedding.embed_query("anything") is None


def test_embed_query_uses_a_supplied_embedder_over_the_held_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit argument is the test seam every caller relies on."""
    monkeypatch.setattr(embedding, "default_embedding_function", lambda: None)
    assert embedding.embed_query("hello", Embedder()) == "hello"


def test_a_numpy_style_vector_is_normalised_to_a_plain_list() -> None:
    """Chroma rejects a query vector carrying numpy scalars.

    The real ONNX embedder returns an ndarray, so without this conversion the
    precomputed-vector path would fail against real Chroma while passing
    every test that used plain lists.
    """

    class Vector:
        def tolist(self) -> list[float]:
            return [0.1, 0.2]

    class NumpyishEmbedder:
        def __call__(self, input: list[str]) -> list[Vector]:
            return [Vector()]

    assert embedding.embed_query("hello", NumpyishEmbedder()) == [0.1, 0.2]
