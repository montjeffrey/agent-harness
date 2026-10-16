# 0007. Mark units stale fifteen minutes after the last fetch

## Status

Superseded by 0019

## Date

2025-02-19

## Context

0006 gives us `Freshness.FetchHeadAt`: when this repository last fetched, read
from the mtime of `FETCH_HEAD`. On its
own that is a timestamp in a header line, and the early panel showed it exactly
that way ("fetched 3h ago"). Watching people use it, nobody read the header.
They read the verdict column, saw "never pushed" next to a branch an agent had
pushed from another machine, and went to chase a problem that did not exist.

So the freshness signal has to reach the units themselves: each unit's verdict
should carry a flag saying "this was computed against a view of the remote that
may be out of date", and the panel should mark those verdicts. That requires a
threshold: how old can the last fetch be before we stop trusting
remote-relative verdicts?

The workflow we were designing for at the time: several agents each pushing
small commits frequently, and an operator who keeps a terminal open and fetches
"whenever something looks off". Agents in that setup push every few minutes, so
a view of the remote that is a quarter of an hour old has very likely missed at
least one push.

Options considered:

- **5 minutes.** Closest to the agents' push cadence, but in a short trial the
  stale marker flickered on and off all day, because operators fetched at
  irregular intervals and most of the time the last fetch was somewhat more than
  five minutes old. A marker that is nearly always on carries no information.
- **15 minutes.** Long enough that a recent manual fetch clears the marker for a
  meaningful stretch, short enough that it is unlikely to hide an agent push
  for long.
- **No threshold; show the age next to each verdict.** Rejected for the same
  reason the header line failed: an age is something you have to interpret,
  and nobody does it per row.
- **Make it configurable.** Deferred. We do not yet know what the right number
  is for different workflows, and a flag added now would freeze a guess into
  the interface.

## Decision

A unit is marked **stale** when `FetchHeadAt` is more than **15 minutes** old,
or when the repository has never fetched (zero time). The threshold is a
constant in the model build, applied uniformly to every unit after units are
built, and is carried on each unit as a boolean `Stale`. How each adapter
presents the flag (a marker in the panel, a note in `-plain`) is left to the
adapter and is not part of this decision.

The comparison treats the zero time without a special case: the age of a zero
timestamp is enormous, so a never-fetched repository is stale by the same rule.

## Consequences

- Every unit carries, next to its verdict, whether that verdict was computed
  against a possibly out-of-date view of the remote, so an adapter can explain
  the most common false alarm (a branch pushed from elsewhere reading as never
  pushed) instead of leaving the operator to chase it.
- The threshold is a guess about operator behaviour, made before we had usage
  data. We expect to revisit it once there is a few weeks of real use to look
  at, and this record should be superseded rather than edited when we do.
- Because the flag is applied to every unit, a stale repository shows every row
  as stale. That is accurate (the whole view is old) but visually heavy, and
  is the first thing to watch for in feedback.
- Test fixtures that never fetch will have every unit stale. Tests that assert
  on verdict text need to account for the marker.
