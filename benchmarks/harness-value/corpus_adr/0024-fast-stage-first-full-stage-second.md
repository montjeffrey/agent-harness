# 0024. Fast stage first, full stage second

## Status

Accepted

## Date

2025-09-17

## Context

The v0.5 responsiveness work measured the reference repository (26 branches, 12
worktrees, 1370 commits): the default window with conflict probes on took
230,901 ms, almost four minutes, and `-all` with probes off took 15,717 ms.
Nothing at those numbers is interactive, and with live updates (0022, 0023) a
slow build is paid on every detected change, not just at startup.

Most of a full build's cost is conflict probing (0009, 0017): O(pairs) `merge-tree`
spawns. Most of what the operator looks at first (verdicts, unlanded counts,
worktree state, the spine) needs no probing at all. The Node version exploited
exactly this: every change ran a fast pass with no probes so the surface moved
immediately, then a probe pass that superseded it.

The targets set by the plan were: first paint under 1 s, warm rebuild under
300 ms, full topology under 3 s, on the reference repository.

Options considered:

1. **One build, with a spinner while it runs.** Honest, but the panel shows the
   previous state (or nothing) for the whole duration. Rejected.
2. **Make probing asynchronous inside one build**, streaming contentions into an
   already-displayed snapshot as they complete. Rejected: it would give the
   snapshot many intermediate revisions (0021) and make "the state you are acting
   on" ambiguous while probes trickle in.
3. **Two stages, each a complete snapshot.** A fast stage without probing,
   displayed immediately; then a full stage with probing, which replaces it when
   done. Each stage is a self-consistent snapshot with its own revision.

## Decision

`Snapshot` takes a `Stage`:

- `StageFast` skips contention and merge-plan probing. Contention text from
  changed-file overlap may still be shown; anything that depends on a probe is
  absent rather than guessed.
- `StageFull` runs everything.

The engine exposes the stage as a parameter and leaves sequencing to the
caller. The intended use in the panel is to request a fast snapshot, display
it, then request a full one and display that when it completes. That
sequencing is adopted in the adapters incrementally; until it is, callers pick
one stage (full, or fast under `-no-probe`), and anything inside the engine
that needs authoritative state, such as computing a decision tree or
re-snapshotting after an action, asks for `StageFull`.

Both stages already skip the per-branch `git cherry` spawn for branches whose
verdict does not depend on it (a review finding during this work), which gives
the fast stage most of the spawn savings the plan expected it to need.

The stage is recorded in the snapshot but excluded from its revision, so a fast
and a full snapshot of a repository with no contentions have the same revision.

Watcher events are debounced before either stage is requested, and `-max-pairs`
applies only to the full stage, since the fast stage probes nothing. Within
each stage, independent git calls stay under the NumCPU bound; probes
themselves run one at a time.

`-no-probe` makes every build a fast build. `-plain` and `-json` build a single
snapshot (full unless `-no-probe`), since they are one-shot.

## Consequences

- Once the panel sequences the stages, it changes within the fast-stage build
  time of a change, and gains probe results a little later.
- Operators may then briefly see a plan without conflict information; an
  adapter should say when probe results are pending rather than presenting the
  fast plan as final.
- Actions computed against a fast snapshot remain valid only if the full
  snapshot that replaces it has the same revision; otherwise they are refused
  as stale, like any other action against a moved state.
- Persisting probe results across restarts (keyed by SHA pair, under the OS
  cache directory, never in `.git`) is the natural next step for cold starts and
  is left to a future record.
