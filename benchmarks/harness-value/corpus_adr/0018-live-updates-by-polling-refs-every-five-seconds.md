# 0018. Live updates by polling refs every five seconds

## Status

Superseded by 0022

## Date

2025-07-09

## Context

Through v0.4 the panel only changes when the operator presses `r`, which runs a
full cold rebuild. The whole point of keeping landfirst open in a docked
terminal is to see agents' work arrive as it happens; a panel that is only as
current as the last keypress invites exactly the mistake the tool exists to
prevent (acting on an out-of-date picture).

The Node version had a file watcher. It watched the refs, `HEAD`, the index and
the worktree registrations under `.git` (never the working tree, so editing
files did not churn the view and `node_modules` could not drown it), and ran a
cheap pass before an expensive one. The Go rewrite dropped it.

We need some form of live update now, and we want it to be small enough to ship
in v0.4.x without reworking how the model is built.

Options considered:

1. **Port the Node watcher with `fsnotify`.** The most capable option, but at
   the time we had two open concerns with it: watch-handle limits (inotify's
   per-user watch limit on Linux, and the cost of one directory handle per
   watched directory on Windows), and the fact that refs live in a directory
   tree that grows as branches are created, which means adding watches
   dynamically. Neither is hard, but together they were more than we wanted to
   take on for this release. Deferred, not rejected.
2. **Poll a cheap fingerprint of the refs on a timer**, and only rebuild when it
   changes. The fingerprint is `git for-each-ref` over `refs/heads` and
   `refs/remotes` (object name and ref name), plus `git worktree list
   --porcelain`, hashed. Two spawns per tick, no new dependencies, works on
   every platform and filesystem including network shares.
3. **Rebuild on a timer unconditionally.** Rejected: a full build costs seconds
   on large repositories, so a periodic rebuild would keep a CPU busy for no
   reason most of the time.

Interval: the two-spawn fingerprint costs about 60 ms on Windows. At **5 seconds**
that is about 1% of a core, which is invisible on a laptop on battery. A
1-second interval was tried and rejected: around 6% CPU continuously on the
reference laptop, noticeable in battery life over a working day. 10 seconds felt
broken: an operator who pushes and then looks at the panel expects to see it.

## Decision

The panel polls the refs fingerprint every **5 seconds**. If the fingerprint
differs from the last one, it triggers the same rebuild `r` does. `r` remains
available and also resets the timer. No debounce is needed: the poller
samples state at most once per tick, so a burst of ref updates inside one
interval coalesces naturally.

## Consequences

- The panel becomes live with a worst case of about five seconds of lag, at
  negligible cost, without new dependencies.
- Changes inside a worktree that do not touch refs (a file edit making a
  worktree dirty, a rebase starting) are not detected until a ref moves or the
  operator presses `r`. Dirty counts can therefore be stale between ref moves.
- The rebuild triggered by a change is the same cold rebuild as `r`, so on a
  large repository each detected change still costs seconds. A two-stage build
  (cheap first, probes second) would make this much better and is the natural
  follow-up.
- This is explicitly an interim mechanism. If the lag or the polling cost
  becomes a complaint, the watcher option above should be revisited rather than
  the interval tuned.
