# 0003. Shell out to the git CLI instead of an in-process git library

## Status

Accepted

## Date

2025-01-15

## Context

Every fact landfirst reports comes from git: branches and their upstreams,
worktrees and their state, whether a change is already in the base, whether two
branches would conflict. The Go rewrite (0002) has to decide how to ask.

The questions landfirst asks are not the common ones. It needs patch-id
equivalence (`git cherry`) rather than hash ancestry, it needs to probe merges
without touching any working tree (`git merge-tree`), and it needs to see linked
worktrees exactly as git itself sees them, including in-progress operations
such as a rebase stopped halfway.

Options considered:

1. **go-git** (pure Go). Builds without cgo, which satisfies 0002. But it has no
   equivalent of `git cherry` or patch ids, no `merge-tree`, partial linked
   worktree support, and on a repository with a 400 MB packfile its object
   lookups were several times slower than the CLI in a quick spike. Each missing
   piece would have to be written by us and kept in agreement with git's own
   behaviour, which is exactly the class of bug this tool exists to avoid.
2. **git2go / libgit2.** Mature and fast, but requires cgo, which 0002 rules
   out. libgit2's merge also differs from git's in rename handling, so its
   conflict answers would not always match what the user sees when they merge.
3. **The git CLI** via `os/exec`. Always agrees with the user's own git, because
   it *is* the user's own git. Costs a process spawn per question.

## Decision

landfirst asks the `git` executable on the `PATH`, through a small
`internal/git` package:

- `git.Try` runs a command and returns trimmed stdout, or empty on failure, for
  questions where "no answer" is a legitimate answer.
- `git.Lines` splits output into lines.
- Commands use porcelain or explicit `--format` output so parsing does not
  depend on the user's locale or configuration.
- `git.RunCtx` takes optional extra environment variables, so a caller can
  isolate a command (for example, point it at a different object store)
  without affecting any other call.
- A process-wide spawn counter is kept so tests and benchmarks can assert how
  many git processes a build cost.

No in-process git library is a dependency.

## Consequences

- Answers match what the user's own git would say, including their version's
  quirks. That matters for conflict probing, where git versions genuinely
  differ (see later records on `merge-tree`).
- Every question costs a process spawn. On Linux that is cheap; on Windows it is
  roughly ten times the cost, so the *number* of git calls matters more than the
  work inside each one. Later performance work will be about reducing and
  bounding concurrent spawns (for example, to the CPU count), not about speeding up individual commands.
- Tests build real git repositories in temp directories rather than mocking a
  library interface. That is slower but means the tests exercise the same git
  the user runs.
- A missing or very old `git` is a startup error with a clear message, not a
  crash.
- Network-touching git commands (`fetch`, `ls-remote`, `push`) are a separate
  category: they can block on credentials or a slow remote, and none of them is
  run as part of building the model. Pushing happens only as an explicit,
  confirmed action.
