# 0005. Count unlanded work with git cherry patch ids

## Status

Accepted

## Date

2025-02-03

## Context

The central number landfirst reports for a branch is how much of its work has
not landed in the base: the commits that would be lost if the branch were
deleted now. v0.1 counted this with `git rev-list --count base..branch`, and the
Go rewrite initially ported that.

On the owner's main repository that produced *2073 unlanded commits across 17
branches*. The real figure was 45 commits across 8 branches. The difference was
not a bug in the counting; it was the wrong question. `rev-list` compares commit
hashes. A branch that was squash-merged, rebased-and-merged, or forked before a
history rewrite reports every post-fork commit as missing, because none of
those hashes exists in the base even though every change does. Three of the
branches that looked like lost agent work were ordinary squash-merged pull
requests.

A tool whose purpose is "did this land" cannot report a 46x overcount. Operators
learn to ignore a board that cries wolf, and then they ignore it the one time it
is right.

Options considered:

1. **`rev-list base..branch`** (status quo). Fast, one spawn per branch, wrong for
   squash and rebase workflows. Rejected.
2. **Compare trees**: is the branch tip's tree a subset of the base's? Answers
   "is everything in", but not "how much is missing", and fails as soon as the
   base has moved on and touched the same files.
3. **`git cherry base branch`**. Compares *patch ids*: a hash of the diff a
   commit introduces, ignoring whitespace and line numbers. Each commit is
   marked `+` (no equivalent change in the base) or `-` (an equivalent change is
   already there under a different hash).
4. **`git log --cherry-mark`**. Same underlying comparison with more output to
   parse; no advantage for our purpose.

## Decision

Unlanded work is counted with `git cherry` against the base ref, where the base
ref is the remote-tracking branch (`<remote>/<base>`) when it resolves and the
local base otherwise. "Landed" means "on the remote", not "in my local copy of
main".

- `+` lines are counted as `Unlanded`: genuinely absent from the base.
- `-` lines are counted as `Equivalent`: already present under another hash.
  These are shown, because "this branch's work is already in, under different
  commits" is itself useful information (it is usually a squash merge).
- A branch with zero `+` lines and no other problem is **safe to delete**.

The per-branch cherry result is cached for the duration of one build, keyed by
branch tip and base tip.

## Consequences

- Counts now match what operators believe when they look by hand, and the
  squash-merge false alarms are gone.
- `git cherry` walks the whole range and costs one spawn per branch, more than
  `rev-list --count`. On repositories with many branches it is one of the
  expensive calls, and it is only worth running for branches whose verdict
  actually depends on it.
- The README explains why the counts differ from `git log`, because the first
  reaction of most users is that landfirst is wrong.
- A patch that was modified while being applied (a conflict resolved
  differently) has a different patch id and is reported as unlanded. That is
  the conservative direction and we accept it.
