# 0025. Derive verdicts in a pure state package

## Status

Accepted

## Date

2025-09-24

## Context

A branch's verdict (orphaned, wrong upstream, never pushed, diverged, unpushed,
detached, safe to delete, in progress) is the single most important thing
landfirst says about it. Until v0.6 the verdict was computed inside the model
build, interleaved with the git calls that gathered its inputs. Every rule was
expressed as "run this git command, and if the output looks like this, return
that verdict".

That structure caused three recurring problems:

1. **Precedence bugs.** A branch can satisfy several conditions at once: its
   upstream is gone *and* it has unlanded work *and* its worktree is mid-rebase.
   Which verdict wins was decided by the order of `if` statements spread over
   two files, and twice a refactor silently changed it.
2. **Untestable rules.** Testing "a branch whose upstream was deleted and whose
   work is not in the base is orphaned" required building a git repository,
   pushing to a bare remote, deleting the remote branch and pruning. Each such
   fixture took hundreds of milliseconds, so the precedence combinations were
   never tested exhaustively.
3. **Inconsistent observations.** Because git was queried at different moments
   while the verdict was being decided, a ref moving mid-build could produce a
   verdict computed from two different states.

Options considered:

1. **Keep the logic in the model, add more fixture tests.** Rejected: addresses
   only the symptom, and makes the test suite slower.
2. **Table-driven verdicts in the model package**, with git calls moved ahead of
   the table. Better, but the model package imports git, so any test of the
   table still links git-calling code, and the temptation to "just check one
   more thing" inside the table stays.
3. **A separate package** that takes an already-observed tuple of facts and
   returns a verdict, importing nothing from the repository.

## Decision

Verdicts are derived in `internal/engine/state` by a pure function,
`Derive(Observation) Verdict`:

- `Observation` holds every fact about one branch that the verdict depends on:
  the worktree's in-progress operation, whether an upstream is configured,
  whether it is gone, whether it mismatches the branch name, ahead/behind
  counts, and the `git cherry` counts (unlanded and equivalent).
- Every field is read from git exactly once, upstream of `Derive`.
- `Derive` performs no I/O and imports only the standard library.
- Precedence is a single ordered table in one function, documented row by row.

The state package defines its own `InProgressOp` and `Verdict` string enums with
the same literal values as the model's, and the model converts with a plain type
conversion. This duplication is deliberate: the model calls `Derive`, so the
state package importing the model would be an import cycle.

## Consequences

- Precedence is tested exhaustively with plain structs: every combination of
  observation fields, with no git fixtures, in milliseconds.
- A verdict can never be computed from two different repository states, because
  its inputs are gathered before it is computed.
- The duplicated enums must be kept in sync by hand. Do not "fix" the
  duplication by importing the model; that is the cycle it exists to avoid.
- Staleness (0019) is not an input to `Derive`; a stale view changes how much a
  verdict should be trusted, not which verdict it is. The staleness predicate
  (FETCH_HEAD's mtime against a threshold) lives in the same package as a separate pure function, so it too is testable
  without fixtures, and the threshold is passed in by the caller.
