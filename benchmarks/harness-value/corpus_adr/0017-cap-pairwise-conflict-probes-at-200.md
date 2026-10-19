# 0017. Cap pairwise conflict probes at 200 by default

## Status

Accepted

## Date

2025-06-25

## Context

Conflict probing (0009) costs one `git merge-tree` spawn per pair, and pairs grow
quadratically with branches. Before 0016, every pair of branches was a
candidate: a 41-branch repository has 820 pairs, and at roughly 280 ms per probe
on Windows that is close to four minutes for a single build. There was an
interim cap in the code, but nobody could say where its value had come from.

0016 changed the population. Only branches that share a fork point are
compared, and only pairs that both changed at least one common file need a
probe. The question is how large that population actually gets, and whether a
build should ever stop probing part-way.

We measured the three repositories we use as references (the owner's main
repository and two large agent-heavy repositories from early users). The
**largest same-fork group** seen in any of them was **17 branches**, giving 136
candidate pairs, of which fewer actually shared files. No other group exceeded
nine branches.

Options considered:

1. **No cap.** Correct on the reference repositories, unbounded on a
   pathological one (for example, a repository where a bot opens a branch per
   dependency update, all from the same base commit). A panel that hangs on
   such a repository with no way to recover is not acceptable.
2. **A wall-clock budget**, proposed as a `-probe-timeout` flag (default 10 s):
   probe pairs until the budget runs out, then stop. Attractive because it
   bounds the thing users actually feel. **Rejected**: the result would depend on
   machine load, antivirus activity and disk cache state, so two builds of the
   same commits could report different contention sets. That breaks the
   property that the same repository state produces the same output, makes
   tests timing-dependent, and would make live updates flicker as probes
   happened to finish or not.
3. **A count cap**, deterministic by construction: probe at most N pairs per
   build, in a fixed order.

## Decision

Probing is capped by count, with the flag `-max-pairs` (default **200**). The cap
counts same-fork pairs that share at least one changed file, in the
deterministic order fork points and branches are already sorted in. When the cap
is reached, probing stops for that build; contentions already found are kept.
`-no-probe` skips probing entirely. A value of 0 passed to the engine means
"use the default".

200 was chosen to cover the largest same-fork group we have measured (17
branches, 136 pairs) with headroom for growth, while bounding the worst case at
about a minute on a slow Windows machine.

## Consequences

- The same repository state always produces the same contention set, regardless
  of machine speed.
- On a pathological repository, contention beyond the first 200 qualifying pairs
  is not reported. That is a known gap; users with such repositories can raise
  the cap or use `-no-probe`.
- Probing is still the dominant cost of a full build. Making it cheaper is a
  caching and incrementality problem, not a cap problem, and should not be
  solved by lowering the default.
- If the largest same-fork group in real use grows past about 20 branches, the
  default should be revisited with new measurements, in a new record.
