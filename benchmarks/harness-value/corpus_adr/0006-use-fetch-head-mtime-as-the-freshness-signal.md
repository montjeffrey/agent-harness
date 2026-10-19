# 0006. Use FETCH_HEAD's mtime as the freshness signal

## Status

Accepted

## Date

2025-02-12

## Context

Every verdict landfirst gives (unpushed, diverged, orphaned, safe to delete) is
computed against remote-tracking refs, and remote-tracking refs are only as
current as the last fetch. If nobody has fetched since an agent pushed from
another machine, landfirst will report that agent's branch as never pushed, and
it will be confidently wrong.

We cannot fix that by fetching (see Decision), but we can tell the operator how
old their view of the remote is, and mark verdicts as possibly stale. That needs
a freshness signal: a cheap, local answer to "when did this repository last
hear from its remote?"

Options considered:

1. **Ask the remote directly with `git ls-remote`** on every build and compare
   its answer with the remote-tracking refs. This is the only option that
   detects staleness rather than estimating it. Rejected: it is a network call on
   every refresh. On the reference repository it took 400 to 1,100 ms per remote
   over a home connection, it fails outright when offline, and on Windows the
   Git Credential Manager can pop a sign-in window in the middle of a
   supposedly read-only refresh. A panel that blocks on the network or opens
   dialogs is not a panel anyone keeps open.
2. **The reflog of the remote-tracking base ref** (`refs/remotes/<remote>/<base>`):
   take the timestamp of its newest entry. Rejected: reflogs are only written
   when `core.logAllRefUpdates` is on, which is false by default in bare
   repositories and is disabled in several CI clones we checked; and, more
   subtly, a fetch that brings nothing new does not move the ref and so writes no
   reflog entry. A repository fetched one minute ago would read as stale if the
   remote had not changed in a week.
3. **The modification time of `FETCH_HEAD`**. git rewrites `FETCH_HEAD` on every
   fetch, including a fetch that brings nothing new, so its mtime is "when did
   we last ask", which is exactly the question. It is a single `stat`. Its path
   is resolved with `git rev-parse --git-path FETCH_HEAD`, which handles linked
   worktrees and a relocated git directory correctly.

## Decision

Freshness is the mtime of `FETCH_HEAD`, resolved through `git rev-parse
--git-path FETCH_HEAD` and read with `os.Stat`. It is reported as
`Freshness.FetchHeadAt`. A missing `FETCH_HEAD` (never fetched) gives the zero
time, and callers treat the zero time as "never".

landfirst never fetches on its own. Fetching writes to `refs/remotes`, can
prompt for credentials, and would change the very state being observed in the
middle of building a model of it. The operator fetches; landfirst reports how
long ago that was.

## Consequences

- Freshness costs one spawn and one stat, with no network.
- It is an estimate. `git pull` and `git fetch` both update it; a tool that
  updates remote-tracking refs by other means (some IDE integrations) may not,
  and will read as stale. We accept the false-stale direction.
- Turning freshness into a per-unit "stale" flag needs a threshold. That is a
  separate decision, recorded in its own ADR, because the threshold is a
  judgment about operator behaviour and is expected to change.
- A never-fetched repository (common for freshly created test fixtures) reads as
  maximally stale. That is correct, and tests that care should fetch.
