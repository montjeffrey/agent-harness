# 0011. Bound git subprocess concurrency at runtime.NumCPU

## Status

Accepted

## Date

2025-04-16

## Context

0003 made every question to git a process spawn. A model build asks several
per-branch questions (`git cherry`, upstream configuration, ahead/behind counts)
and several per-worktree questions (dirty count, in-progress operation). The
first Go builds ran all of them serially. On the reference repository (26
branches, 12 worktrees) a build with probing turned off took 11.4 s on the
owner's Windows laptop, nearly all of it spent waiting on process creation.

These calls are independent of each other, so they can run concurrently. The
question is how many at once.

The Node version (v0.1) also ran git calls concurrently. It used the
**`p-limit`** npm package with a fixed concurrency of **6**, a number chosen on
one developer's laptop and never revisited. The obvious move was to port that
number along with the rest of the analysis.

Measurements on the reference repository, Windows laptop with 8 logical CPUs,
probing off, three runs each, median:

| strategy | build time |
|---|--:|
| serial | 11.4 s |
| unbounded (one goroutine per call, no limit) | 7.9 s |
| fixed limit of 6 (as in v0.1) | 3.6 s |
| limit = `runtime.NumCPU()` (8 here) | 3.2 s |

Unbounded was much worse than bounded. Launching every git process at once made
Windows Defender's real-time scanning inspect all of them simultaneously, and
the processes then contended for the same pack files; the machine was
unresponsive for several seconds. On Linux the unbounded case was merely
wasteful, not pathological.

Options considered:

1. **Unbounded.** Rejected on the measurement above.
2. **Carry over v0.1's fixed `p-limit` concurrency of 6.** Rejected. A fixed
   number is tuned for one machine. On the 2-vCPU GitHub runners it
   oversubscribed the CPUs threefold and made CI timings noisy; on a 16-core
   workstation it left most of the machine idle. It also carries a dependency on
   a number nobody can justify.
3. **A limit equal to `runtime.NumCPU()`.** Scales with the machine, needs no
   configuration, and on the reference laptop was the fastest option measured.
4. **A user flag (`-jobs N`).** Deferred: no one has asked for it, and every flag
   is an interface we have to keep.

## Decision

Independent per-branch and per-worktree git calls run concurrently through
`golang.org/x/sync/errgroup`, with `SetLimit(runtime.NumCPU())`. Results are
written into a pre-sized slice by index, never appended, so the output order is
independent of goroutine scheduling and a build is reproducible. Anything that
is cheap and local (path checks, string parsing) stays serial.

## Consequences

- Build time on the reference repository dropped from 11.4 s to 3.2 s with
  probing off, with no configuration.
- Output ordering is deterministic. A later change that appends from goroutines
  instead of writing by index would make sort tie-breaks depend on timing and
  should be rejected in review.
- Shared caches used inside the concurrent section need their own lock.
- The limit applies per group, not globally. Two concurrent groups could in
  principle run 2x NumCPU processes; today groups do not overlap.
- Conflict probing is O(pairs) and is bounded separately; this record does not
  cover it.
