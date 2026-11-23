# Automatic trust and synced memory

Date: 2026-11-23
Status: direction approved by the maintainer; nothing in this document is built
unless a section says so.

## Goal

Three changes to the memory store, in this order:

1. Agent-recorded memory becomes usable without a person accepting each entry.
2. One project has one memory, on every device and in every worktree.
3. Obsidian stops being part of the picture. Plans stay plain Markdown.

Cross-device path claims are a separate question with a separate document:
`2026-11-23-cross-device-claims-assessment.md`.

## Why

### The review gate filters nothing

Measured on the maintainer's machine on 2026-11-13, across every enrolled store:

| Proposed entries | Count | Share |
|---|---|---|
| One per commit, written by the progress hook | 480 | 86% |
| One per finished task, written by the progress hook | 34 | 6% |
| Written by an agent (`remember`, hand-off, tasks) | 47 | 8% |
| Total | 561 | |

Entries had been accepted in two stores only: this repository's own and the
machine store. In the other 26 stores with proposals, nothing had ever been
reviewed.

Proposed entries are already served to agents. `memory_context` and
`memory_search` include every entry that is not rejected, superseded or
resolved; the status only changes a label, a ranking tie-break and a warning.
The one hard difference is `relationship` entries, which are used only once
accepted.

So the gate costs a queue nobody works, and warnings on every session start,
and buys a label.

### Memory is split by folder

A project's id is its folder name plus a hash of its path, and its store lives
inside that folder. Two consequences:

- Every git worktree is a separate project with an empty store. On the measured
  machine about 25 of 59 registered projects were worktrees of one repository.
  Removing a worktree removes its memory.
- A second device has a different path, so a different id, so nothing shared.
  ADR 0004 records this as intended ("a second machine starts blank").

Task state does not have the first problem: `work.Repository.id` is derived from
the common git directory, so all worktrees of a repository share one task store.
Memory should behave the same way.

### Obsidian is not used by any code path

Nothing in `harness/` or `hooks/` reads an Obsidian vault's configuration. The
only reference is one sentence in the generated `docs/vault/README.md`.
Blueprints are ordinary Markdown files that agents read and write as files.

## Part A. Trust without review

### What can be checked and what cannot

| Entry | Evidence a program can check | Treatment |
|---|---|---|
| Document pointer | file digest | verified; already automatic (ADR 0003) |
| Relationship | recorded digest of the shared evidence | verified while the digest matches |
| Commit or finished-task pointer | the SHA exists | verifiable, but it restates `git log`; stop storing it |
| Decision, discovery, lesson, blocker | none; it is the agent's conclusion | accept as an agent note; attribute, age, and allow correction |

### Design

- **Agent notes are accepted when written.** They keep `confidence: inferred`,
  render with a flag so a reader can tell them from verified or human-reviewed
  entries, and rank below both.
- **Verified entries are re-checked.** Curation already re-hashes document
  pointers. An entry whose evidence no longer matches leaves the packet and is
  reported once.
- **Agent notes age.** A note that nobody has re-confirmed leaves the injected
  packet after 90 days and is archived after 180. It stays searchable until
  archived. Both numbers are starting values to tune against real stores.
- **Correction replaces review.** A `memory_retract` tool lets an agent or the
  operator withdraw or supersede an entry, with a required reason. The original
  is kept and points at its replacement.
- **Progress pointers stop.** The Stop hook no longer appends commit and
  finished-task entries. `memory_context` shows a "Recent work" section built at
  read time from `git log` and the task store, so it is always current and
  stores nothing. The hook keeps its session marker.
- **Human review stays available** as an override (`review reject`,
  `review supersede`). It stops being required.

### Relation to the acceptance-policy change (PR #16)

PR #16 adds an operator setting, `memory_acceptance: auto`, that lets curation
accept plain agent notes and marks them `(auto)`. That is the first slice of
this part. What it leaves, by design, and what this plan adds:

| PR #16 | This plan |
|---|---|
| Default stays `review`; `auto` is opt-in per machine | Make `auto` the default once aging and retraction exist |
| Progress pointers still wait for review | Stop writing them |
| Auto-accepted notes never expire | 90 / 180-day aging |
| No agent-side correction | `memory_retract` |
| Auto-accepted notes are not promoted into `durable.md` | Unchanged |

### Audit trail

- Every entry keeps its id, timestamp, project and evidence. Add
  `source.session`, `source.agent` and `source.device`, so a wrong memory can be
  traced to what wrote it.
- Every status change is recorded with a time and a reason and is never applied
  by deleting the original.
- Anything removed from a live store is written to the archive first. This is
  already true of compaction.
- The write-time secret screen is unchanged.

### Existing backlog

One migration command, with a dry run that prints counts before anything moves:

- Commit and finished-task pointers: move to the archive with the note
  "retired: restates git".
- Agent-written decisions, discoveries and lessons: accept as agent notes with
  their original timestamps, so aging counts from when they were written.
- Agent-written open tasks: list for the operator once. They are work items, not
  knowledge.

## Part B. One synced store per project

### Requirements

- The same project shows the same memory on every device the operator uses.
- Works offline, and with no server, account or always-on machine.
- Nothing leaves storage the operator already controls.
- Two devices, or two agents on one device, can write at the same time without
  losing either write.
- Someone else can install it with no sync at all and lose nothing.

### Options considered

| Option | Verdict |
|---|---|
| Per-device append-only logs in a synced folder, local index | **Chosen** |
| A server on a private network that every device calls | Rejected as the base: needs an always-on machine, and offline use would need the local store anyway. Kept as a possible transport later |
| A hosted database or memory service | Rejected: private memory leaves the operator's machines, and it adds an account and a network dependency |
| Sync today's single `inbox.jsonl` with a file-sync tool | Rejected: every review, claim and compaction rewrites the file in place, so two devices produce conflict copies or lose a write |

The chosen option is the roadmap's standing rule applied to a second machine: a
plain-text store of record, with everything else derived and rebuildable.

### Layout

```
<store-root>/
  projects/<project-key>/
    log/<device-id>.jsonl      entries and events written by one device
    archive/<device-id>/...    what that device compacted away
    scope.md                   project purpose and boundaries
  user/
    log/<device-id>.jsonl      cross-project memory (today's "machine" scope)

~/.agent-harness/
  config.json                  store_root, device id
  index/                       derived: search index, semantic index
  work/                        task state and path claims (not synced)
```

- `store_root` defaults to `~/.agent-harness/store`, which is not synced. An
  operator who wants sync points it at a folder that a file-sync tool already
  carries. The harness never talks to a sync service itself.
- Nothing is stored inside the repository any more, so memory cannot enter git,
  and ADR 0004's exclude-file handling is no longer needed for new stores.

### One writer per file

A device appends only to its own log. No file has two writers, so a file-sync
tool never has to merge, and a conflict copy cannot be produced by the harness.
Appending is the only write to a live log; a reader skips a torn final line, as
`iter_entries` already does for malformed lines.

### Entries are immutable; changes are events

Today a review, a claim, a hand-off refresh or a compaction rewrites a line in
`inbox.jsonl`. With one writer per file, a device cannot rewrite an entry
another device wrote. So:

- An entry line is never edited after it is written.
- A change is a new line in the changing device's own log:
  `{"event": "status", "target": "<entry id>", "status": "superseded", "reason": ..., "ts": ...}`.
- The current state of a store is the fold of every log: entries, with the
  events that target them applied in timestamp order, ties broken by device id.
- Two devices changing the same entry both keep their event. The fold picks one
  winner deterministically and the loser stays readable in the log.

The call sites that splice lines today and move to events: `memory_review`,
`memory_curation`, `memory_claims`, `memory_tasks`, `handoff` and
`memory_compact`.

### Ids

`mem-YYYY-MM-DD-NNN` is allocated by scanning one file, so two devices would
mint the same id on the same day. New ids carry the device:
`mem-YYYY-MM-DD-<device>-NNN`. Existing ids stay valid and are never rewritten.
The device id is a short random string created once per machine and stored in
`config.json`; it is not the hostname.

### Project identity

In order:

1. An explicit key in the project's `scope.md` front matter, for the cases
   below that need an override.
2. The repository's root commit SHA. It is the same in every clone, on every
   device and in every worktree, needs no remote and no network.
3. For a folder that is not a git repository: today's folder-name-and-path hash,
   which stays local to that machine until an explicit key is set.

A repository with more than one root commit uses the oldest. A fork shares its
parent's key, which is harmless because stores belong to one operator.

### Compaction

Each device compacts only its own log, writing what it removes to its own
archive first. It may drop its own closed entries, and its own events once the
entry they target has been absent from every log for a grace period. An event
whose target is missing is ignored by the fold, so the order in which devices
compact does not matter.

### Derived index

A local SQLite database with full-text search holds the folded state and is
rebuilt from the logs whenever a log's size or modification time changes. It
uses the standard library's `sqlite3`, so it adds no dependency. It is never
placed in the synced folder: a database file under a file-sync tool is the
classic way to corrupt one.

The index is for growth, not for today. The largest measured store had 114
entries, and folding that in memory on every read is fast enough. The index
lands after the log format, and only with a benchmark that shows it paying.

### What does not sync

- Task state and path claims. They describe one checkout on one machine. See
  the claims assessment for the cross-device question.
- The search and semantic indexes, and telemetry. All are derived or local by
  nature.

### Migration

A one-time, repeatable import per machine: each existing `inbox.jsonl` is
appended to this device's log for the project key its folder resolves to, ids
unchanged. Stores that belonged to worktrees of one repository merge into one.
The old files are left in place and no longer read.

### Known risks

- **Sync delay.** Another device sees a new entry when the file-sync tool
  delivers it, typically seconds to minutes. Acceptable for knowledge.
- **Clock skew** affects only the order of two events on the same entry.
- **Placeholder files.** Some sync tools leave files as online-only stubs. The
  store folder must be kept fully on disk; `doctor` should check and say so.
- **Private text in a synced folder.** The store holds whatever agents recorded.
  It goes only where the operator pointed `store_root`.

## Part C. Plans stay Markdown; Obsidian is optional

- `docs/vault/blueprints/` keeps its path and its human approval step. It is a
  folder of Markdown files and needs nothing else.
- The generated vault README loses its Obsidian sentence. The roadmap's
  "Optional add-on -- Obsidian visualization" entry already states the rule:
  nothing in the kit may require it.
- Facts go to the memory store through `memory_remember`; documents a person
  approves go to Markdown files. A project should not need a third place.

## Build order

Each step is its own pull request, tests first, under the repository's review
policy.

1. Land the acceptance policy (PR #16).
2. Stop the progress hook writing entries; add the read-time "Recent work"
   section.
3. Backlog migration command with a dry run.
4. Storage layer: device id, per-device logs, event fold, root-commit project
   key, import of existing stores. Readers switch to the fold; behaviour is
   otherwise unchanged.
5. Move every in-place writer to events (one pull request per module).
6. `store_root` setting, `doctor` checks for the store folder, and a documented
   two-device walkthrough.
7. Aging, `memory_retract`, `source.session` / `source.agent` / `source.device`.
8. Make automatic acceptance the default; supersede ADR 0003 and ADR 0004 with
   new records; update the templates and installed agent instructions.
9. SQLite index, behind a benchmark.
10. Remove the Obsidian sentence from the vault README template.

Steps 2 and 3 do not depend on the storage layer and can ship first. Step 5
depends on 4. Step 8 depends on 7.

## Open questions

- Aging numbers: 90 and 180 days are guesses until measured against real use.
- Whether `scope.md` and `active.md` move to the store root with the logs, or
  whether `active.md` becomes fully derived. This document assumes the first.
- Whether the semantic index is rebuilt per device or its embeddings are shared.
  Per device is assumed, because it is derived.

## Not verified

- The backlog counts were read from each store's `inbox.jsonl` on one machine on
  2026-11-13 and have not been re-counted since.
- No file-sync tool has been tested against the per-device layout yet. Step 6
  exists to do that before the design is called working.
