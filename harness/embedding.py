"""One held ONNX embedder, shared by every semantic index in the harness.

Chroma attaches `DefaultEmbeddingFunction` to a collection, and that class
does `return ONNXMiniLM_L6_V2()(input)` -- a *new* instance per call
(chromadb/api/types.py). Both the tokenizer and the ONNX InferenceSession are
`cached_property` on that instance, so discarding it discards the cache and
rebuilds a full inference session on every embed. Measured on chromadb 1.5.9:
~300ms per call through `DefaultEmbeddingFunction` against ~30ms through one
held instance, once warm.

Holding the instance is the fix, and it belongs here rather than in either
index module: a process that searched both indexes would otherwise keep two
ONNX sessions alive to do one job. `harness.mcp_server` is the long-lived
process where that would first bite -- it serves memory tools today and
already imports the graph side, though it does not yet expose `search`.

The same hard rule the index modules follow applies here: `chromadb` is NEVER
imported at module scope. `import harness.embedding` must succeed on a
stdlib-only install, or the harness would break for anyone without the
`semantic` extra.

Callers pass the resulting vector to Chroma as `query_embeddings`. Setting a
collection-level `embedding_function` instead does not work: Chroma refuses
one that differs from the persisted name ("Embedding function conflict: new:
onnx_mini_lm_l6_v2 vs persisted: default") and directs callers to embed
separately. Ranking is unaffected either way, because the vector comes from
the very model Chroma would have used.
"""

from __future__ import annotations

from typing import Any

_EMBEDDING_FUNCTION: Any = None
_EMBEDDING_FUNCTION_RESOLVED = False


def _build_embedding_function() -> Any:
    """Construct the same ONNX embedder Chroma would use, to be held onto."""
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    return ONNXMiniLM_L6_V2()


def reset() -> None:
    """Drop the held embedder. For tests; production resolves once per run."""
    global _EMBEDDING_FUNCTION, _EMBEDDING_FUNCTION_RESOLVED
    _EMBEDDING_FUNCTION = None
    _EMBEDDING_FUNCTION_RESOLVED = False


def default_embedding_function() -> Any:
    """One embedder for the whole process, or None if Chroma cannot supply it.

    Returning None rather than raising is what keeps the optional dependency
    optional. The single `except Exception` covers both ways this can fail --
    `chromadb` not installed at all, and an installed Chroma whose model or
    module layout this code cannot use -- and both must degrade to letting
    Chroma embed from text, which is slower but correct.

    The outcome is remembered either way, so a machine without the extra pays
    one failed import per process rather than one per query.
    """
    global _EMBEDDING_FUNCTION, _EMBEDDING_FUNCTION_RESOLVED
    if _EMBEDDING_FUNCTION_RESOLVED:
        return _EMBEDDING_FUNCTION
    _EMBEDDING_FUNCTION_RESOLVED = True
    try:
        _EMBEDDING_FUNCTION = _build_embedding_function()
    except Exception:
        _EMBEDDING_FUNCTION = None
    return _EMBEDDING_FUNCTION


def embed_query(text: str, embedding_function: Any = None) -> Any:
    """Embed one query string, or return None when no embedder is available.

    Chroma rejects a query vector that arrives as numpy scalars, so the vector
    is normalised to a plain list here rather than at each call site.
    """
    function = (
        embedding_function
        if embedding_function is not None
        else default_embedding_function()
    )
    if function is None:
        return None
    vector = function([text])[0]
    return vector.tolist() if hasattr(vector, "tolist") else vector
