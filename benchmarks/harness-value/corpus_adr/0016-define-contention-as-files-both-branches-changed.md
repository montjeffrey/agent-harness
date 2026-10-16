# 0016. Define contention as files both branches changed since their shared fork

## Status

Accepted

## Date

2025-06-11

## Context

Until v0.4 the merge plan ranked pairs of branches by their raw conflict count:
the number of files `merge-tree` reported as conflicting if the two were merged.
On the reference repository that number turned out to be useless, and the
reason it was useless is worth recording.

The distribution was extreme: across all probed pairs, the median was 14
conflicting files and the maximum was 203. The pairs at the top were not the
ones fighting over code. They were pairs where one branch forked from the base
months earlier than the other. Merging them conflicts on hundreds of files
simply because one side carries months of unrelated history that the other
does not. That is drift. It tells you to rebase the old branch, and nothing
about whether the two pieces of work overlap.

Pairs that forked *together* and both edited the same handful of files were
the genuinely contended ones, and they were buried under the drift.

The fork-point spine (0015) already groups branches by the commit they forked
from. That grouping gives a natural definition.

Options considered:

1. **Raw conflict count across all pairs.** Status quo. Rejected: dominated by
   drift.
2. **Raw conflict count, normalised by fork distance.** Tried. Any normalisation
   we chose either still ranked drift pairs highly or hid genuine conflicts
   between branches of different ages. The two quantities are not on one scale.
3. **Overlap of changed files since a shared fork point, for same-fork pairs
   only.** For two branches that share a fork point, compute the set of files
   each has changed since that fork (`git diff --name-only <fork> <branch>`),
   and take the intersection. Those are files both are actively editing.

## Decision

**Contention** between two branches is defined only for branches that share a
fork point, and is the set of files *both* branches changed since that fork.
Pairs from different fork points are not compared at all.

Each contention records:

- `Real`: the number of files in that intersection, and `Files`, the first 12
  of them sorted, for display;
- `Structural`: conflicts reported by a merge probe (0009) that fall *outside*
  the shared set, which is drift even within a fork group;
- `SameFork`: always true under this definition, kept explicit so a future
  cross-fork comparison cannot be confused with it.

Contentions are ordered by `Real`, descending. The merge plan derives each
item's conflicts from contentions rather than probing on its own.

## Consequences

- The signal is clean: a pair shows up only if two branches that started from
  the same code are both changing the same files. On the reference repository
  the contended group of 8 branches surfaced immediately.
- The changed-file sets cost one `git diff --name-only` per branch per fork group
  and are computed whether or not probing is enabled, so `-no-probe` still shows
  contention, just without `Structural`.
- Probing is now only needed for same-fork pairs with at least one shared file,
  which shrinks the probed set dramatically compared with all pairs. That makes
  a much smaller probe cap reasonable (`-max-pairs`, recorded separately).
- A branch that forked from a commit no other branch shares cannot be in
  contention by definition. That is correct: its problem is drift, which the
  spine already shows by its depth.
