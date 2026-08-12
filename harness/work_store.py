"""The working-store tier: ``<project>/.agent-harness/work/``, never committed.

Memory already has two homes with two different lifetimes. Project memory
(``.agent-harness/memory/``) is committed with the repository and is meant to
outlive the machine it was written on. Machine memory
(``~/.agent-harness/memory/``) is machine-local and shared across every
enrolled project. Both are durable by design.

Working stores are the third kind: per-project scratch that is useful while a
piece of work is in flight and worthless afterwards -- retrieval eval sets,
scored results, routing artifacts. Committing them would put machine-specific
paths and throwaway measurements into a shared repository's history.

They are excluded from versioning by a ``.gitignore`` containing a single
``*`` written *inside* ``work/``. That pattern ignores the whole directory
including the ignore file itself, so enabling this tier in a new project never
requires editing -- or even reading -- that project's root ``.gitignore``.
``install_cli.ensure_gitignore_entry`` takes the other approach for
``graphify-out/`` because that path predates this tier; new state should not
need a host-file edit to stay untracked.

Nothing here reads or writes memory entries. ``memory_store.iter_entries``
resolves exactly one file, ``<directory>/inbox.jsonl``, so a sibling ``work/``
directory is invisible to ``status``, ``compact``, ``review``, and ``index``
without any of them needing to know it exists.
"""

from __future__ import annotations

from pathlib import Path

HARNESS_DIRNAME = ".agent-harness"
WORK_DIRNAME = "work"
GITIGNORE_NAME = ".gitignore"

# A lone `*` ignores every path in this directory, this file included. Git
# still reads an ignored `.gitignore`, so the rule applies to itself without
# the file needing to be tracked.
GITIGNORE_BODY = "*\n"


def work_dir(project_root: Path) -> Path:
    """The working-store root for `project_root`. Not created by this call."""
    return Path(project_root) / HARNESS_DIRNAME / WORK_DIRNAME


def work_path(project_root: Path, *parts: str) -> Path:
    """Resolve a path under the working store, refusing to escape it.

    Pure path arithmetic -- nothing is created. `parts` are treated as
    untrusted (they reach here from CLI arguments and case-file names), so a
    ``..`` segment or an absolute component raises rather than silently
    resolving to somewhere outside the tier, where nothing is gitignored.
    """
    root = work_dir(project_root)
    candidate = root.joinpath(*parts)
    base = _normalize(root)
    target = _normalize(candidate)
    if target != base and base not in target.parents:
        raise ValueError(f"path escapes the working store: {candidate}")
    return candidate


def ensure_work_dir(project_root: Path) -> Path:
    """Create the working store and its self-ignoring ``.gitignore``.

    Idempotent, and never clobbers: an existing ``.gitignore`` is left exactly
    as found, because a user who widened it -- or narrowed it to keep one
    deliberate file -- meant to.
    """
    root = work_dir(project_root)
    root.mkdir(parents=True, exist_ok=True)
    ignore = root / GITIGNORE_NAME
    if not ignore.exists():
        ignore.write_text(GITIGNORE_BODY, encoding="utf-8")
    return root


def ensure_work_path(project_root: Path, *parts: str) -> Path:
    """`work_path`, with the working store and the target's parents created."""
    target = work_path(project_root, *parts)
    ensure_work_dir(project_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def is_ignored(project_root: Path) -> bool:
    """Whether the working store currently carries its ignore rule.

    Checked rather than assumed: the tier's only protection against being
    committed is one small file, and a user who deleted it should be told
    before the next `git add -A` sweeps the directory in.
    """
    return (work_dir(project_root) / GITIGNORE_NAME).is_file()


def _normalize(path: Path) -> Path:
    """Absolute, symlink-free-enough form for containment comparison.

    ``resolve()`` rather than ``absolute()``: the parts being joined are
    untrusted, and only resolution collapses a ``..`` that would otherwise
    compare as a legitimate child component. ``strict=False`` because the
    target routinely does not exist yet.
    """
    return Path(path).expanduser().resolve(strict=False)
