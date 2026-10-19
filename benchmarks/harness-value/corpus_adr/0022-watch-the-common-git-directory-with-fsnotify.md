# 0022. Watch the common git directory with fsnotify

## Status

Accepted. Supersedes 0018.

## Date

2025-08-27

## Context

0018 made the panel live by polling a refs fingerprint every five seconds, and
called itself an interim mechanism. Two complaints arrived within weeks, both of
the kind 0018 anticipated.

First, the lag. An operator watches an agent's terminal print "pushed", glances
at landfirst, and still sees "unpushed" for up to five seconds. In a tool whose
job is to say whether work landed, a few seconds of contradicting the agent
reads as a bug, and two operators filed it as one.

Second, cost on large repositories. `for-each-ref` over a repository with
thousands of remote branches is not 60 ms; on one early user's repository it was
closer to 400 ms, every five seconds, all day.

The v0.5 responsiveness plan (item P1-3) called for porting the Node watcher:
watch the git directory, never the working tree; coalesce bursts; check cheaply
whether anything really changed before paying for a snapshot.

Options considered:

1. **Keep polling and shorten the interval.** Makes the cost complaint worse to
   fix the lag complaint. Rejected.
2. **`github.com/rjeczalik/notify`.** Attractive because it supports recursive
   watches natively, which would remove the need to add a watch for every
   directory under `refs/`. **Rejected**: its macOS backend uses FSEvents through
   **cgo**, which breaks the `CGO_ENABLED=0` cross-compiled release (0002). Its
   kqueue fallback avoids cgo but needs a file descriptor per watched file, which
   is worse than what we are replacing. The project has also seen little
   maintenance recently.
3. **`github.com/fsnotify/fsnotify`.** Pure Go on every target, widely used,
   maintained. No recursive watch, so we add each directory ourselves.
4. **Watch the working tree as well**, to catch files becoming dirty. Rejected,
   as in the Node version: editing files would churn the view, and a
   `node_modules` install would drown it.

## Decision

`engine.Watch` uses a single fsnotify watcher over the repository's **common**
git directory (resolved with `git rev-parse --git-common-dir`, so every linked
worktree shares it): the common directory itself, every directory under `refs/`,
and each linked worktree's private git directory (which holds that worktree's
own `HEAD`). The working tree is never watched.

fsnotify events are only a trigger. After a burst settles, the engine recomputes
the cheap refs fingerprint (unchanged from 0018: a SHA-256 over `for-each-ref`
and `worktree list --porcelain`) and only builds a snapshot if it moved.

If fsnotify cannot start at all, or if adding any single path fails, the
watcher is closed and the engine falls back to polling the same fingerprint
every **2 seconds**, and reports that it is in fallback mode. A partially watched
tree would silently miss changes, which is worse than a visible fallback.

The fallback interval is shorter than 0018's five seconds because it now runs
only where watching failed (network shares, some WSL mounts, exhausted watch
limits) and those are precisely the environments where operators had noticed the
lag.

## Consequences

- Updates arrive within a fraction of a second of a ref moving, and the idle
  cost is zero outside the fallback.
- Debouncing bursts of events is required, and how long to wait is its own
  decision.
- A worktree added after `Watch` starts is not watched until restart; the
  `worktrees` directory's new subdirectories are a known gap.
- Tests substitute the fsnotify constructor to force the fallback path, rather
  than trying to exhaust real OS watch limits.
