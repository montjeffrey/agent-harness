"""ChromaDB-backed semantic selection for memory context packets.

This is the default selection strategy: `context --strategy auto` (the
default) uses it whenever chromadb is installed and an index already exists,
and falls back to `memory_context.deterministic_selector` otherwise, saying
so in the packet. Semantic ranking is what makes a large memory store stay
useful -- keyword overlap misses "the auth rewrite" when the entry says
"rewrote the login flow" -- but it is a ranking improvement layered on the
same contract, never a precondition for the harness working.

Two hard rules, both inherited from `harness.graph_index`, which solved the
same problems for the code graph:

1. `chromadb` is NEVER imported at module scope. `import harness.memory_index`
   must succeed on a stdlib-only install, or every context call would break
   for anyone without the `semantic` extra.
2. Querying never builds an index. Chroma's first embedding call downloads an
   ~80MB ONNX model; `context` runs in the agent's hot path, where the design
   forbids expensive work and network access. Building is the separate,
   explicit `agent-harness-memory index` command.

The index lives machine-side at
``~/.agent-harness/memory/index/<project-id>/`` rather than inside the
repository's `.agent-harness/`. Project memory is committed by default, and a
Chroma index is a derived binary SQLite store -- putting it under the project
root would sweep it into the repo on the next `git add -A`.

Embedding uses Chroma's default local function (all-MiniLM-L6-v2, ONNX). No
API key, no per-query network call, and entry summaries never leave the
machine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Safe at module scope: `harness.embedding` imports chromadb no more eagerly
# than this module does, so rule 1 above still holds. Aliased because
# `embedding` is also the natural name for the vector itself at the call site.
from harness import embedding as embedding_module

from harness import memory_store

COLLECTION_NAME = "memory_entries"
INDEX_DIRNAME = "index"

# Machine-scope memory gets its own index rather than being copied into every
# project's. `project_id_for` slugs to `[a-z0-9-]+`, so a leading underscore
# can never collide with a real project id.
MACHINE_INDEX_NAME = "_machine"

ClientFactory = Callable[[Path], Any]

STRATEGIES: tuple[str, ...] = ("auto", "semantic", "deterministic")


@dataclass(frozen=True)
class IndexStats:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0


def top_k_agreement(
    candidates: Sequence[dict],
    query: str | None,
    semantic: Callable[[Sequence[dict], str | None, int], list[dict]],
    limit: int,
) -> dict[str, object]:
    """Compare deterministic and semantic selection without changing either.

    This is an opt-in diagnostic for long-lived callers.  It deliberately
    observes ranking only: semantic hits are still memory entries, never a
    source of graph symbols or graph evidence.
    """
    from harness import memory_context

    size = max(0, min(limit, len(candidates)))
    deterministic = memory_context.deterministic_selector(candidates, query, size)
    semantic_ranked = semantic(candidates, query, size)[:size]
    deterministic_ids = [index_key(entry) for entry in deterministic]
    semantic_ids = [index_key(entry) for entry in semantic_ranked]
    return {
        "limit": size,
        "deterministic": deterministic_ids,
        "semantic": semantic_ids,
        "overlap": len(set(deterministic_ids) & set(semantic_ids)),
    }


def chromadb_status() -> tuple[bool, str | None]:
    """Return whether Chroma can actually import, with a safe failure reason."""
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


def index_dir_for(project_id: str) -> Path:
    """The semantic index directory holding one project's own entries."""
    return memory_store.machine_memory_dir() / INDEX_DIRNAME / project_id


def machine_index_dir() -> Path:
    """The single index holding machine-scope entries, shared by all projects.

    Machine memory is reusable knowledge that belongs to no one project.
    Embedding it into every project's index would mean N copies of the same
    vectors and a re-index of every project each time one machine lesson is
    accepted; one shared index costs a second query instead.
    """
    return memory_store.machine_memory_dir() / INDEX_DIRNAME / MACHINE_INDEX_NAME


def index_dirs_for(project_id: str) -> dict[str, Path]:
    """Scope -> index directory, the mapping `semantic_selector` routes on."""
    return {"project": index_dir_for(project_id), "machine": machine_index_dir()}


def index_key(entry: dict) -> str:
    """The id an entry is stored under in the index: scope-qualified.

    Entry ids are unique *per store*, not globally -- `next_entry_id` scans a
    single directory, so the project inbox and the machine inbox routinely
    allocate the same `mem-YYYY-MM-DD-001` on the same day. One index spans
    both stores, so keying on the bare id would let a machine entry silently
    displace a project entry (and vice versa) and would let a query hit from
    one scope be attributed to a same-id entry in the other.
    """
    return f"{entry.get('scope') or 'project'}:{entry.get('id')}"


def entry_document(entry: dict) -> str:
    """The text an entry is embedded from: summary, tags, and type."""
    parts = [
        str(entry.get("summary") or ""),
        " ".join(entry.get("tags") or []),
        str(entry.get("type") or ""),
    ]
    return "\n".join(p for p in parts if p)


def entry_content_hash(entry: dict) -> str:
    """Digest of `entry_document`, so unchanged entries are not re-embedded."""
    return hashlib.sha256(entry_document(entry).encode("utf-8")).hexdigest()


def _default_client(index_dir: Path) -> Any:
    import chromadb  # local import -- see module docstring; never at module scope

    # chromadb's own Settings default anonymized_telemetry to True; leaving it
    # on would contradict this project's "nothing leaves the machine" promise.
    settings = chromadb.config.Settings(anonymized_telemetry=False)
    return chromadb.PersistentClient(path=str(index_dir), settings=settings)


def _max_batch_size(client: Any) -> int | None:
    getter = getattr(client, "get_max_batch_size", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _write_in_batches(
    collection: Any,
    method: str,
    ids: list[str],
    docs: list[str],
    metas: list[dict],
    batch_size: int | None,
) -> None:
    """Chroma caps add()/upsert() batch size and raises past it; chunk to fit."""
    if not ids:
        return
    op = getattr(collection, method)
    size = batch_size if batch_size and batch_size > 0 else len(ids)
    for start in range(0, len(ids), size):
        end = start + size
        op(ids=ids[start:end], documents=docs[start:end], metadatas=metas[start:end])


def build_index(
    entries: Sequence[dict],
    index_dir: Path,
    client_factory: ClientFactory | None = None,
    embedding_function: Any = None,
) -> IndexStats:
    """Incrementally upsert `entries` into the semantic index at `index_dir`.

    Only entries whose `entry_content_hash` changed are re-embedded; ids no
    longer present are deleted. Entries with an empty document (no summary,
    tags, or type) are skipped -- embedding an empty string produces a vector
    that matches everything equally, which is worse than an absent record.
    """
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    factory = client_factory or _default_client
    client = factory(index_dir)

    kwargs: dict[str, Any] = {}
    if embedding_function is not None:
        kwargs["embedding_function"] = embedding_function
    collection = client.get_or_create_collection(name=COLLECTION_NAME, **kwargs)

    wanted = {
        index_key(e): e for e in entries if e.get("id") and entry_document(e).strip()
    }

    existing = collection.get(include=["metadatas"])
    existing_ids = list(existing.get("ids") or [])
    existing_metas = list(existing.get("metadatas") or [])
    existing_hashes = {
        eid: (meta or {}).get("content_hash")
        for eid, meta in zip(existing_ids, existing_metas)
    }

    to_delete = [eid for eid in existing_ids if eid not in wanted]
    if to_delete:
        collection.delete(ids=to_delete)

    add: tuple[list[str], list[str], list[dict]] = ([], [], [])
    update: tuple[list[str], list[str], list[dict]] = ([], [], [])
    unchanged = 0

    for key, entry in wanted.items():
        content_hash = entry_content_hash(entry)
        if key not in existing_hashes:
            target = add
        elif existing_hashes[key] != content_hash:
            target = update
        else:
            unchanged += 1
            continue
        target[0].append(key)
        target[1].append(entry_document(entry))
        target[2].append(
            {
                "content_hash": content_hash,
                "entry_id": str(entry.get("id") or ""),
                "status": str(entry.get("status") or ""),
                "scope": str(entry.get("scope") or ""),
                "type": str(entry.get("type") or ""),
            }
        )

    batch = _max_batch_size(client)
    _write_in_batches(collection, "add", *add, batch)
    _write_in_batches(collection, "upsert", *update, batch)

    return IndexStats(
        added=len(add[0]),
        updated=len(update[0]),
        deleted=len(to_delete),
        unchanged=unchanged,
    )


def _existing_collection(client: Any, embedding_function: Any = None) -> Any:
    """Open the collection for reading, refusing to fabricate an empty one.

    `get_collection`, not `get_or_create_collection`: if a build crashed after
    creating the directory but before populating it, creating a fresh empty
    collection here would make every query "succeed" with zero results
    forever. Raising instead lets the caller fall back to deterministic
    selection and say why.
    """
    kwargs: dict[str, Any] = {}
    if embedding_function is not None:
        kwargs["embedding_function"] = embedding_function
    try:
        return client.get_collection(name=COLLECTION_NAME, **kwargs)
    except Exception as exc:
        raise FileNotFoundError(
            f"no collection named '{COLLECTION_NAME}' in this memory index"
        ) from exc


def probe_index(
    index_dir: Path, client_factory: ClientFactory | None = None
) -> str | None:
    """Return a visible readiness error, if a persisted index cannot be read.

    The probe opens an existing collection but does not query, embed, create,
    or rebuild anything, so a doctor run remains cheap and side-effect free.
    """
    database_path = Path(index_dir) / "chroma.sqlite3"
    if not database_path.is_file():
        return f"FileNotFoundError: missing persistent database {database_path.name}"
    factory = client_factory or _default_client
    try:
        _existing_collection(factory(Path(index_dir)))
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
    """Return `(entry_id, distance)` pairs for `text`, closest first.

    Supplying `embedding` -- a vector the caller already computed -- is what
    keeps the collection's own embedding function out of the hot path. It is
    also the only way to substitute an embedder at all: Chroma refuses a
    collection-level embedding function that differs from the persisted one
    ("Embedding function conflict: new: onnx_mini_lm_l6_v2 vs persisted:
    default") and directs callers to embed separately instead. Ranking is
    unaffected -- verified identical on this project's live index -- because
    the vector comes from the very model Chroma would have used.
    """
    factory = client_factory or _default_client
    client = factory(Path(index_dir))
    collection = _existing_collection(client, embedding_function)
    if embedding is None:
        result = collection.query(query_texts=[text], n_results=limit)
    else:
        result = collection.query(query_embeddings=[embedding], n_results=limit)
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    return list(zip(ids, distances))


def semantic_selector(
    index_dirs: Mapping[str, Path],
    client_factory: ClientFactory | None = None,
    embedding_function: Any = None,
):
    """Build a `memory_context.Selector` over one index per memory scope.

    Candidates are grouped by their own `scope` and each group is queried
    against its scope's index, then all hits are merged on distance. Grouping
    rather than querying one index is what lets a mixed-scope section (the
    relationships section draws from both stores) rank correctly; distances
    are comparable across indexes because both use the same embedding
    function and similarity space.

    Anything the indexes did not return -- an entry recorded since the last
    build, one whose text was empty, or every entry of a scope whose index
    was never built -- is appended in deterministic order. A stale or partial
    index therefore degrades the *ranking* and never drops a record from the
    packet.

    Failures to read an index propagate; `resolve_selector` decides whether
    to fall back, so that decision and its user-visible explanation live in
    one place.
    """
    from harness import memory_context

    def select(candidates: Sequence[dict], query: str | None, limit: int) -> list[dict]:
        ordered = memory_context.deterministic_selector(
            candidates, query, len(candidates)
        )
        if not query or not candidates:
            return ordered[:limit]

        by_key = {index_key(c): c for c in candidates}
        grouped: dict[str, list[dict]] = {}
        for candidate in candidates:
            grouped.setdefault(str(candidate.get("scope") or "project"), []).append(
                candidate
            )

        # Embed once, then reuse that vector for every scope. The scopes hold
        # different entries but the *query* is the same string, so embedding
        # inside the loop paid the full per-query cost once per index -- and
        # would multiply again once partitions add more of them.
        embedding = embedding_module.embed_query(query, embedding_function)

        # Ask each index for more than we need: it spans a whole store, but
        # `candidates` is one already-filtered section, so many hits will not
        # belong to it.
        hits: list[tuple[float, str]] = []
        for scope, group in grouped.items():
            index_dir = index_dirs.get(scope)
            if index_dir is None or not Path(index_dir).exists():
                continue
            for hit_key, distance in query_index(
                index_dir,
                query,
                max(len(group) * 2, limit * 4, 20),
                client_factory=client_factory,
                embedding=embedding,
            ):
                hits.append((float(distance), str(hit_key)))
        hits.sort(key=lambda pair: pair[0])

        ranked: list[dict] = []
        seen: set[str] = set()
        for _distance, key in hits:
            entry = by_key.get(key)
            if entry is not None and key not in seen:
                seen.add(key)
                ranked.append(entry)
        ranked.extend(c for c in ordered if index_key(c) not in seen)
        return ranked[:limit]

    return select


def resolve_selector(
    project_id: str,
    strategy: str = "auto",
    client_factory: ClientFactory | None = None,
) -> tuple[Any, str, list[str]]:
    """Pick the selector for `strategy`, returning it with the strategy name
    actually used and any notes explaining a fallback.

    `auto` -- the default -- prefers semantic and degrades quietly but
    visibly: every fallback appends a note that is printed on the packet, so
    a user who expects semantic selection is never silently served
    deterministic results and left assuming otherwise.

    An explicit `semantic` raises instead of falling back: asking for a
    specific strategy and getting a different one without an error is exactly
    what makes a benchmark meaningless.
    """
    from harness import memory_context

    if strategy not in STRATEGIES:
        raise ValueError(
            f"unknown strategy: {strategy!r} (expected one of {STRATEGIES})"
        )

    if strategy == "deterministic":
        return memory_context.deterministic_selector, "deterministic", []

    dirs = index_dirs_for(project_id)
    built = {scope: path for scope, path in dirs.items() if path.exists()}

    problem: str | None = None
    if not chromadb_available():
        reason = chromadb_unavailable_reason()
        problem = (
            "chromadb is not importable"
            + (f" ({reason})" if reason else "")
            + '; from this checkout run `python -m pip install -e ".[semantic]"`, then '
            "run `agent-harness-memory index`"
        )
    elif not built:
        problem = (
            f"no semantic index at {dirs['project']} -- run "
            f"`agent-harness-memory index` to build one"
        )
    else:
        unreadable = [
            (scope, probe_index(path, client_factory=client_factory))
            for scope, path in built.items()
        ]
        unreadable = [
            (scope, reason) for scope, reason in unreadable if reason is not None
        ]
        if unreadable:
            problem = "; ".join(
                f"{scope}-scope semantic index is not query-ready ({reason})"
                for scope, reason in unreadable
            )

    if problem is None:
        # A partially built set is usable: the unbuilt scope's entries fall
        # through to deterministic order inside the selector rather than
        # disappearing. Say which scope is missing so the degradation is
        # visible, per the never-silent rule.
        notes = [
            f"no {scope}-scope semantic index yet; those entries were ranked "
            f"deterministically"
            for scope in dirs
            if scope not in built
        ]
        return (
            semantic_selector(built, client_factory=client_factory),
            "semantic",
            notes,
        )

    if strategy == "semantic":
        raise FileNotFoundError(problem)
    return (
        memory_context.deterministic_selector,
        "deterministic",
        [f"semantic selection unavailable ({problem}); used deterministic selection"],
    )
