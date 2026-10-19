# 0023. Debounce watcher bursts at 150 ms

## Status

Accepted

## Date

2025-09-03

## Context

The fsnotify watcher (0022) reports file-level events in the common git
directory. A single git operation produces many of them. Creating a ref writes a
`.lock` file, writes the ref into it and renames it into place; a fetch updates
several remote refs and may rewrite `packed-refs`; a rebase of N commits moves
`HEAD` and the branch ref repeatedly and writes `ORIG_HEAD` and reflogs as it
goes. Reacting to every event would run the fingerprint check dozens of times
per operation, and would sometimes catch the repository in a half-written state
in the middle of an operation.

So events must be coalesced: wait until the burst is over, then check once. The
questions are what "over" means and how long to wait.

We measured bursts on the reference repository by logging event timestamps
during typical operations (fetch with several updated refs, push, branch
create/delete, worktree add, and an interactive rebase of 20 commits). Within a
single operation, the **largest gap between consecutive events was 90 ms**, on
Windows, during the rebase, where git pauses to write each rewritten commit.
On Linux the largest gap was about 40 ms.

Options considered:

1. **Leading-edge throttle**: react to the first event immediately, then ignore
   events for a period. Rejected: the first event of a burst is by definition
   the start of an operation, so the check runs against a half-finished state
   (a rebase in progress, a lock file not yet renamed) and the panel flickers
   through an intermediate verdict before settling.
2. **Trailing-edge debounce of 500 ms**, the value the Node watcher used.
   **Rejected**: combined with the fast build stage this put first visible change
   at around 0.8 s after an operation finished, which operators noticed and
   described as "laggy", and it bought nothing, since no measured gap came
   anywhere near it.
3. **Trailing-edge debounce of 150 ms**: wait until no event has arrived for
   150 ms. That is above the largest measured intra-burst gap (90 ms) with
   margin for slower disks, and short enough that the delay is not perceptible
   on top of the build.
4. **Trailing-edge debounce of 100 ms.** Only 10 ms above the measured worst
   case, which we did not consider enough margin for a slow disk or a busy
   antivirus scanner.

## Decision

The watcher uses a trailing-edge debounce: after each fsnotify event, a timer is
(re)set to **150 ms**; when it fires with no further events, the engine runs one
fingerprint check, and only builds a snapshot if refs actually moved. This is the
number the v0.5 plan specified for P1-3, and this record is where its derivation
lives.

## Consequences

- A burst produced by one git operation results in one check, and almost always
  in one snapshot.
- The panel reflects an operation roughly 150 ms plus build time after it ends.
- An operation with internal pauses longer than 150 ms (for example, a rebase
  stopped for conflict resolution) produces more than one check. That is correct:
  the state in between is real, and the operator should see it.
- If the measured worst-case gap changes (a new platform, network filesystems),
  the window should be re-measured, not guessed, and a new record written.
