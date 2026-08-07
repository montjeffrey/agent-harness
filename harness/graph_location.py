"""Shared graph-location contract: where a repo's graph.json lives, and
whether it can be trusted.

This module is the single source of truth for two concerns that used to be
duplicated (and subtly inconsistent) across `graph_query.py` and
`graph_check.py`:

1. **Discovery** -- given a starting directory, find that repo's
   `graphify-out/graph.json`. Discovery is **repository-bound**: it walks
   upward from `start` but stops at the nearest repo boundary (a directory
   containing a `.git` entry, which covers plain repos, worktrees, and
   submodules -- all of which have *some* `.git` path). A nested repo
   (a `.git` boundary between `start` and an outer repo's graph) will
   **never** resolve to the outer repo's graph.json. If no `.git` boundary
   exists anywhere in the ancestry, discovery is bounded to `start` itself
   only -- it never walks unbounded, since without a repo boundary there is
   no principled place to stop, and an unbounded walk risks silently
   borrowing an unrelated ancestor's graph (a zip extract, a scratch dir
   under home, a checkout made before `git init`).

2. **Health** -- given a graph.json path, classify it as `fresh`, `stale`,
   `dirty`, `unknown`, `invalid`, or `missing`. Corrupt/truncated JSON is
   always `invalid`; an uncommitted working tree is always `dirty`; and if
   git's clean/dirty state or HEAD commit cannot be determined at all (git
   not installed, not a repo, PATH issue, etc.) the result is `unknown` --
   never silently `fresh`. `fresh` is only ever returned when the working
   tree is affirmatively known to be clean AND HEAD is affirmatively known.

Public API (stable -- consumed by graph_query.py, graph_check.py,
graph_refresh.py, and the Wave 3 Query Contract Agent):

    GRAPH_RELATIVE_PATH            -- Path("graphify-out") / "graph.json"
    INDEX_RELATIVE_PATH            -- Path("graphify-out") / "semantic-index"
    GraphHealth                    -- str Enum: FRESH/STALE/DIRTY/UNKNOWN/INVALID/MISSING
    find_repo_root(start)          -> Path | None
    find_graph_in_repo(start=None) -> Path | None   (never raises)
    load_graph_metadata(path)      -> dict | None    (None means invalid/corrupt)
    git_head(repo_root)            -> str | None
    git_is_dirty(repo_root)        -> bool | None    (None means "could not tell")
    report_health(graph_path, repo_root=None) -> GraphHealth
"""

from __future__ import annotations

import json
import subprocess
from enum import Enum
from pathlib import Path

GRAPH_RELATIVE_PATH = Path("graphify-out") / "graph.json"

# Sibling of GRAPH_RELATIVE_PATH, inside the same (already-gitignored)
# graphify-out/ directory. This is the single source of truth for where the
# optional [semantic] extra's ChromaDB-backed index lives -- harness.graph_index
# and harness.doctor both derive from this constant rather than duplicating
# the literal "semantic-index" path fragment.
INDEX_RELATIVE_PATH = Path("graphify-out") / "semantic-index"


class GraphHealth(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    DIRTY = "dirty"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    MISSING = "missing"


def find_repo_root(start: Path, stop_at: Path | None = None) -> Path | None:
    """Walk upward from `start` looking for a `.git` entry.

    Returns the first ancestor (inclusive of `start`) containing `.git`, or
    None if no repo boundary is found.

    `stop_at` caps the walk at that directory (inclusive): nothing above it
    is examined. Production callers leave it None and walk to the filesystem
    root. It exists because "no `.git` anywhere in the ancestry" is a real,
    load-bearing state that is otherwise impossible to construct in a test --
    any repo above the test directory (a dotfiles repo in the user's home,
    say) silently satisfies the walk and makes the no-boundary tests assert
    the opposite of what they read as.
    """
    current = start.resolve()
    boundary = stop_at.resolve() if stop_at is not None else None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
        if boundary is not None and candidate == boundary:
            return None
    return None


def _ancestors_within_boundary(start: Path, repo_root: Path | None) -> list[Path]:
    """Ancestors of `start` (inclusive) to search for a graph.

    When `repo_root` is known, the walk stops at `repo_root` -- it never
    crosses into a parent directory's own repo. When `repo_root` is None
    (no `.git` boundary found anywhere), there is no principled boundary to
    walk to, so the search is bounded to `start` itself only -- it never
    walks unbounded up the filesystem, which would risk silently borrowing
    an unrelated ancestor's graph.json.
    """
    current = start.resolve()
    if repo_root is None:
        return [current]
    bounded: list[Path] = []
    for candidate in (current, *current.parents):
        bounded.append(candidate)
        if candidate == repo_root:
            break
    return bounded


def find_graph_in_repo(
    start: Path | None = None, stop_at: Path | None = None
) -> Path | None:
    """Locate `graphify-out/graph.json`, bounded to `start`'s own repo.

    Never raises. Returns None if no graph.json exists within the
    discoverable boundary. `stop_at` caps the repo-boundary search -- see
    `find_repo_root`; production callers leave it None.
    """
    current = (start or Path.cwd()).resolve()
    repo_root = find_repo_root(current, stop_at=stop_at)
    for candidate in _ancestors_within_boundary(current, repo_root):
        graph_path = candidate / GRAPH_RELATIVE_PATH
        if graph_path.exists():
            return graph_path
    return None


def load_graph_metadata(graph_path: Path) -> dict | None:
    """Parse `graph_path` as JSON and sanity-check its top-level shape.

    Returns None (never raises) when the file is missing, unreadable, not
    valid UTF-8, not valid JSON, or does not look like a graphify graph (no
    `nodes`/`links` keys) -- callers treat None as `GraphHealth.INVALID`.
    """
    try:
        text = graph_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError and similar decode failures
        # that `errors="replace"` doesn't already absorb.
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "nodes" not in data or "links" not in data:
        return None
    return data


def git_head(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_is_dirty(repo_root: Path) -> bool | None:
    """True if the working tree has uncommitted changes, False if clean,
    None if git status could not be determined (e.g. not a git repo)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def report_health(graph_path: Path, repo_root: Path | None = None) -> GraphHealth:
    """Classify `graph_path`'s health. Never returns FRESH for a corrupt,
    dirty, or undetermined git state.

    FRESH is only ever returned when `git_is_dirty()` affirmatively returns
    `False` AND `git_head()` returns a real commit -- if either could not be
    determined (git absent, PATH issue, not a repo, etc.), the result is
    UNKNOWN rather than being silently treated as clean/fresh.
    """
    if not graph_path.exists():
        return GraphHealth.MISSING
    metadata = load_graph_metadata(graph_path)
    if metadata is None:
        return GraphHealth.INVALID

    root = repo_root or graph_path.parent.parent
    dirty = git_is_dirty(root)
    if dirty:
        return GraphHealth.DIRTY

    head = git_head(root)
    if dirty is None or head is None:
        return GraphHealth.UNKNOWN

    built = metadata.get("built_at_commit")
    if built and not head.startswith(built) and not built.startswith(head):
        return GraphHealth.STALE
    return GraphHealth.FRESH
