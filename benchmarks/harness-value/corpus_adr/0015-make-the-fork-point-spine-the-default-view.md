# 0015. Make the fork-point spine the default view

## Status

Accepted

## Date

2025-06-04

## Context

Through v0.3 the panel opened on the commit graph. It was the most visually
impressive part of the tool and the least useful for the question the tool
exists to answer.

A commit graph answers "what happened, and in what order". The operator's
question is different: where does each branch land relative to the base, and
which branches are fighting over the same code? In the owner's words, the view
should never leave him questioning where a branch lands compared with the other
contention points. No ordering of a commit graph made that visible on the
reference repository; branches that forked from the same commit were scattered
across the graph by commit date.

Three designs were mocked up with the owner's real data and reviewed with him:

1. **A denser commit graph** with fork points highlighted. Rejected: still
   organised by time, still answers the wrong question.
2. **A matrix** of branch against branch, coloured by conflict count. Useful as a
   detail view, but on its own it loses where each branch sits relative to the
   base, and with 26 branches it is 325 cells of mostly noise.
3. **A fork-point spine.** The base runs down the page. Every branch attaches at
   the commit it actually forked from. Distance down the spine is staleness;
   branches that left the base together sit together.

He picked the spine immediately. On the reference repository it exposed
something none of the other views had: 8 branches sharing a single fork point,
which were the group genuinely in contention, with the remaining branches
hanging off older commits further down as stale forks.

## Decision

The panel opens on **topology**: the fork-point spine. The commit graph remains
as the second tab. Tabs are `1 topology` and `2 graph`, switched with `1`/`2` or
`tab`.

- Each fork point is a commit on the base with the branches that diverged from
  it, how far it is behind the base tip, and a count of how many branches share
  it.
- Each branch row shows its verdict marker, a bar proportional to its unlanded
  work, its worktree (path, `dirty:N`, any operation in progress) or
  `(no worktree)`.
- Under each branch, contention with branches at the same fork point is shown
  inline (definition in the next ADR).
- Fork points are ordered from the base tip downwards.

## Consequences

- The first screen answers the operator's question directly. Feedback after
  the switch was that the graph tab is now opened mostly to inspect a specific
  branch's history, which is what it is good for.
- Fork grouping depends on computing the right merge-base for each branch
  against the base ref, which adds a spawn per branch; this is accepted.
- Contention is currently inline text, not drawn geometry. The owner asked for
  the matrix as an overlay on the spine eventually; the spine is designed so
  that overlay can be added without changing the layout.
- `p` (decide), `w`/`W` (worktree add/remove) act on the selected branch in the
  topology view only, because that is where the selection is meaningful.
