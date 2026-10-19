# 0021. Snapshot revision is a content hash of derived state

## Status

Accepted

## Date

2025-08-13

## Context

0020 left one question open: every `Snapshot` needs an identity, a revision, so
that a client can tell whether what it is looking at is still current, and so
that an action requested against one state is refused if the state has moved on.
Live updates (0018, and whatever replaces it) also need to know whether a
rebuild produced anything new, so that the panel is not redrawn, and the
operator's selection not disturbed, when nothing changed.

The revision must change when, and only when, something a client could act on
has changed.

Options considered:

1. **A monotonic counter** incremented on every build. Simple. Rejected: it
   changes on every rebuild even when nothing changed, so it cannot answer "did
   anything change"; it resets when the process restarts, so a client holding a
   revision across a restart would be accepted against unrelated state; and two
   engine instances on the same repository (the panel and a future `serve`
   process) would issue conflicting revisions for identical state.
2. **The build timestamp** (`GeneratedAt`). Same problems as a counter, plus clock
   resolution on Windows makes two quick builds share a timestamp.
3. **The refs fingerprint** used by the poller (a hash of `for-each-ref` and
   `worktree list`). Cheap, but it misses changes that matter and are not refs:
   a worktree becoming dirty, an operation starting, a change in the fetch time.
   It is a good trigger for a rebuild, not a good identity for a result.
4. **A hash of the derived state itself.** Serialise the parts of the snapshot
   that represent repository state and hash the bytes. Identical state gives an
   identical revision in any process, and any visible change gives a new one.

## Decision

`Snapshot.Revision` is the hex-encoded SHA-256 of the JSON encoding of the
snapshot's state-bearing fields: repository info, freshness, units, worktrees,
fork points, contentions and the merge plan. Fields describing *how* the
snapshot was built rather than *what* it says (build duration, stage) are
excluded, so a slower or faster build of the same state has the same revision.

The fields are encoded through an explicit struct listing exactly which parts
are hashed, rather than by hashing the whole snapshot and blanking fields, so
that adding a field to `Snapshot` does not silently change what the revision
covers. Slices are already in deterministic order (0011's write-by-index rule
and the sort orders defined for each collection), so encoding is reproducible.

## Consequences

- "Did anything change" is a string comparison. Live updates can emit an event
  only when the revision moves. The watcher's debounce and fingerprint decide
  when to rebuild; the revision decides whether the rebuild produced anything
  new.
- Actions carry the revision they were computed against, and the engine refuses
  an action whose revision no longer matches the current one.
- Anything included in the hashed fields that changes on every build would make
  the revision change on every build, so the build timestamp and duration are
  deliberately outside it.
- JSON encoding of maps is sorted by key in Go, so maps are safe; floating-point
  values are avoided in hashed fields.
- If a non-state field is accidentally added to the hashed struct, the symptom
  is an update event on every rebuild, which is easy to notice in the panel.
