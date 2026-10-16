# 0009. Probe conflicts with merge-tree in an isolated object store

## Status

Accepted

## Date

2025-03-19

## Context

The merge plan suggests an order in which finished branches should be merged,
and the order depends on which branches would conflict with each other. That
requires asking git, for pairs of branches, "would merging these conflict, and
on which files?" without touching anyone's working tree, because those trees
belong to agents who may be using them right now.

`git merge-tree --write-tree` (git 2.38 and later) answers exactly that: it
performs a real merge in memory and reports conflicts, without a working tree or
index. But it is not side-effect free. It writes the resulting tree objects into
the repository's object store. After a day of probing, the owner's repository
had 258 new unreachable objects in it. They are harmless to git, but the
repository lives in a cloud-synced folder, every new loose object is a new file
for the sync client to upload, and a tool that quietly grows a user's `.git` is
a tool people stop trusting.

Options considered:

1. **Probe with `git merge --no-commit --no-ff` in a scratch worktree**, then
   abort. Works on any git version. Rejected: it needs a worktree per probe (or
   one reused worktree with careful cleanup), it writes an index and working
   files, it took about 1.2 s per pair on Windows, and an interrupted probe
   leaves a merge in progress in a worktree the user can see.
2. **Use `merge-tree --write-tree` and run `git gc --prune=now` afterwards.**
   Rejected: gc rewrites packs, which is far more churn in a synced folder than
   the objects it would remove, and pruning a shared object store while agents
   are committing is precisely the kind of action this tool must never take.
3. **Point `merge-tree` at a throwaway object store.** Set
   `GIT_OBJECT_DIRECTORY` to a fresh temporary directory and
   `GIT_ALTERNATE_OBJECT_DIRECTORIES` to the real store, so git can read every
   existing object but writes new ones into the temporary directory, which is
   deleted after probing.

## Decision

Conflict probes use `git merge-tree --write-tree` with an isolated object store:
a temporary directory created with `os.MkdirTemp` (prefix `landfirst-objects-`)
as `GIT_OBJECT_DIRECTORY`, the repository's real `objects` directory (resolved
via `--git-common-dir`, so linked worktrees share it) as an alternate, and the
temporary directory removed when probing finishes.

A test creates a repository, probes the same pairs repeatedly, and asserts that
the number of loose objects in the real store is unchanged. That test is not to
be weakened.

## Consequences

- Probing no longer writes to the user's `.git` at all. Nothing landfirst does
  in the course of building a report writes into `.git`; reports and scratch
  data go to the OS temp directory.
- Probes still cost a process spawn each and, on large repositories, real CPU
  time. Probing is O(pairs), and is the dominant cost of a full build; how many
  pairs to probe (what became `-max-pairs`) is a separate decision. Probes run
  one at a time rather than in parallel with other git calls, because each
  `merge-tree` is already CPU heavy.
- `merge-tree --write-tree` requires git 2.38. What happens on older git is a
  separate decision as well, because the obvious fallbacks are wrong in ways
  that are easy to miss.
- If the process is killed mid-probe, the temporary directory may be left
  behind in the OS temp location, which the OS cleans. That is acceptable.
