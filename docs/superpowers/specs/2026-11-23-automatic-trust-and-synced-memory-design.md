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

One file per device is not one writer per device: several agents and hooks run
on the same machine. Id allocation and the append that uses it happen under one
local lock per log, held for that single write. The lock file lives under
`~/.agent-harness/`, never in the synced folder, so a sync tool cannot carry a
stale lock to another device. A lock older than its timeout is recoverable, as
the task store's lock already is.

### Entries are immutable; changes are events

Today a review, a claim, a hand-off refresh or a compaction rewrites a line in
`inbox.jsonl`. With one writer per file, a device cannot rewrite an entry
another device wrote. So:

- An entry line is never edited after it is written.
- A change is a new line in the changing device's own log:
  `{"event": "status", "target": "<entry id>", "status": "superseded", "reason": ..., "ts": ...}`.
- The current state of a store is the fold of every log: entries, with the
  events that target them applied in order.
- Order comes from what each writer had seen, not from wall clocks. Every event
  has an id, `<device>:<n>`, where `n` counts the lines that device has written
  to that log. Every event also carries `seen`: for each device, the highest
  `n` among the events on the same entry that the writer had folded when it
  wrote. A log is append-only and a sync tool delivers whole files, so a device
  always holds a prefix of every other log and `seen` is well defined.
- Event A is *before* event B only when B's `seen` covers A, directly or
  through an event B had seen. Otherwise the two are concurrent. A single
  counter cannot say this: an agent that writes twice offline would otherwise
  sort its second event after a review it never saw.
- The fold works one field at a time. An event sets named fields only: a
  review sets `status`, a claim sets `claim`, and so on. For each field the
  fold takes the events that set it and drops every one that another event
  setting the same field is after. A later claim therefore never removes an
  earlier review, because it does not set `status`.
- If one event remains for a field, it gives that field's value. If several
  remain they are concurrent, and an operator's review outranks an agent's
  event; then the timestamp decides, then the device id, then the event's
  own counter `n`, higher winning. Two agents on one device can write
  concurrent events in the same second; the counter is unique within a log,
  so the order is always total.
- An agent's event therefore replaces an operator's review only when the agent
  had that review in `seen`. No clock setting, and no number of offline writes,
  moves an agent's change past a human correction it had not seen.
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

1. An explicit key in the clone's own git configuration
   (`git config agent-harness.projectKey <key>`), for the cases below that need
   an override. It is read before any project folder is opened, is never
   committed, and is shared by every worktree of that clone.
2. The repository's root commit SHA. It is the same in every full clone, on
   every device and in every worktree, needs no remote and no network.
3. For a folder that is not a git repository: today's folder-name-and-path hash,
   which stays local to that machine until an explicit key is set.

**More than one root commit.** A new project takes the lexicographically
smallest root SHA as its key. The rule reads commit ids only, never author or
committer dates, which can be skewed or forged. Each project folder also
records every root SHA it has been seen with, and every remote URL.

One shared root is not enough evidence to join: a repository that merged
another repository's history (`--allow-unrelated-histories`, a subtree import)
holds that other project's root too. A checkout joins an existing project
automatically only on one of these:

- every one of its roots is recorded in that project, and in no other; a
  clone that fetched only some branches holds a subset and still qualifies;
- its smallest root is that project's key and one of its remote URLs is
  recorded there; the checkout's other roots are then added to the project.

Anything else that matches a project selects no project: some roots matched
but not all, roots matching two projects, or a smallest root that names a
project the checkout did not qualify for. The checkout then gets a key local
to the machine, as a shallow clone without a match does, and `doctor` names
every partial match. The operator joins one of them, which carries the
local-key memory in by the Joining rule below, or sets an explicit key. A
checkout whose roots match nothing recorded starts a new project under its
smallest root.

**Shallow clones.** A shallow clone does not contain the root commit; git
reports its oldest fetched commit as parentless, which would give a different
key. The harness detects a shallow repository
(`git rev-parse --is-shallow-repository`) and does not derive a key from it.
Each project folder records the remote URLs it has been seen with, so a shallow
clone whose remote matches exactly one known project joins that project. A
remote can match more than one, because a fork's folder may have recorded its
parent's URL. Then, as with roots, no project is selected: the checkout gets a
key local to the machine and `doctor` names every match. With no match it also
gets a local key. In both cases `doctor` says how to join: set the explicit
key, or deepen the clone.

**Joining.** A checkout that recorded memory under a local key and later
resolves to a shared project (the clone was deepened, a remote was added, an
explicit key was set, or `doctor` found two folders for one repository) does
not leave that memory behind. The device carries its own log from the local-key
folder into its own log in the resolved project, by the same rules as the
migration below: new ids with the old one kept as `legacy_id`, references
rewritten in the same pass, and a record of what was taken so that a second run
adds nothing. Only the device that wrote a log carries it, so one writer per
file still holds. The old folder is left in place with a marker naming where
its entries went.

**Forks.** A fork shares its parent's roots, and so its parent's key. That is
right only while the fork is the same project to the operator. To keep a fork's
memory separate, set `agent-harness.projectKey` in the fork's clone before
recording anything in it. The setting lives in the clone, so it cannot change
the parent's project, and each device with a clone of the fork sets it once.
`doctor` warns when a checkout's remote URL is not one the resolved project has
been seen with, which is how an unmarked fork shows up.

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

A repeatable import per machine, kept current by the reconcile pass of build
step 4 until cut-over: each existing `inbox.jsonl` is
appended to this device's log for the project key its folder resolves to.
Stores that belonged to worktrees of one repository merge into one. The old
files are left in place.

Migration and sync never overlap. A machine migrates alone, into a local
shadow, and may join a shared `store_root` only after it has cut over (build
steps 4 to 6). The old single-file store and another device's events are
therefore never live on one machine at the same time.

Ids are not carried over unchanged. Every old store numbered its entries from
001 each day, so two worktree stores of one repository routinely hold different
entries under the same id. On import:

- each entry gets a new device-qualified id and keeps the old one as
  `legacy_id`, with the old store's project id as `legacy_store`;
- references inside one old store (`supersedes`, `superseded_by`, task and
  claim targets) are rewritten to the new ids in the same pass, using that
  store's own old-to-new map, so a reference never crosses into another store's
  entry that happened to share the id;
- a lookup by an old id still works when it matches one entry, and lists the
  candidates when it matches several;
- the import records which stores it has taken, so running it again adds
  nothing.

### Known risks

- **Sync delay.** Another device sees a new entry when the file-sync tool
  delivers it, typically seconds to minutes. Acceptable for knowledge.
- **Clock skew** can only decide between two events on the same entry that
  were written without seeing each other and by the same kind of writer.
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

### Review gate for every step

A step merges only when all of these hold:

- CI is green on the head commit.
- Every finding from the automated reviewers on the pull request has an answer
  on its thread: fixed, with the commit, or declined, with the reason. This
  includes findings that arrive after the last push; the threads are re-read
  immediately before merging.
- A finding about data integrity or security is fixed in that pull request, or
  the step does not merge. It is not carried forward as a follow-up.
- A step that touches a trust decision, the store's write path, a hook, or
  configuration loading also gets the maintainer's own security review, run
  beside the automated one. Neither replaces the other.

Design documents are held to the same gate as code.

### Steps

1. Land the acceptance policy (PR #16).
2. Stop the progress hook writing entries; add the read-time "Recent work"
   section.
3. Backlog migration command with a dry run.
4. Storage layer in shadow: device id, per-device logs, event fold, root-commit
   project key, import of existing stores. `inbox.jsonl` stays the store of
   record and every reader keeps reading it. Nothing an agent sees changes.

   The shadow is local and single-device by rule. It is written under
   `~/.agent-harness/shadow/`, never under `store_root`, and nothing syncs it.
   Until this machine has cut over (5a), the harness refuses a `store_root`
   that already holds another device's log. So during steps 4 and 5 the shadow
   holds only what this machine derived from its own `inbox.jsonl` files:
   there are no events from another device to order against, protect or
   overwrite. Nothing reads the shadow except the parity check.

   A *reconcile* pass keeps the shadow current. It runs once per old store,
   under that store's writer lock, and compares that store's `inbox.jsonl`
   with the shadow entries whose `legacy_store` names it. For every
   difference it appends what brings the shadow up to date, marked
   `"origin": "reconcile"`: the entry, when `inbox.jsonl` has one the shadow
   lacks, or an event, when a status, claim or text differs. An entry the old
   store dropped in compaction becomes a closing event. `inbox.jsonl` is the
   only source of truth in this phase, so a reconcile event is always right
   to win. The shadow may lag between passes; the parity check runs reconcile
   first, in tests and on demand from `doctor`.
5. Move every in-place writer to events, one pull request per module. During
   this step each moved writer writes both the old in-place change and the
   event, in that order and under the one writer lock. If the in-place write
   fails, no event is written. If the process stops between the two,
   `inbox.jsonl` is ahead and the next reconcile pass writes the missing
   event. Every line in the shadow comes from a local dual write or from
   reconcile, so the shadow never holds a change that `inbox.jsonl` lacks.

   A module's pull request is done when, in its tests, reconcile writes
   nothing for that module's kind of change: every such change already reached
   the shadow through the module's own event.
5a. Cut over: readers switch to the fold and the in-place writes stop. The
   code ships in one pull request after all six writers have moved. The
   switch happens at run time, once per machine, for every store on it
   together, so no reader is ever on the fold while some writer is still on
   `inbox.jsonl`. The harness takes the writer lock of every known old store,
   in a fixed order; runs reconcile on each and repeats until a full round
   writes nothing; writes a cut-over journal; moves the shadow logs into
   `store_root` as this device's logs; and writes one machine marker. It
   releases no lock until the marker is written. Every writer and reader
   checks the marker under its store's lock. Without it, a writer writes both
   forms and a reader reads `inbox.jsonl`; with it, a writer writes the event
   only and a reader reads the fold. If any store cannot come clean, nothing
   is moved, no journal or marker is written, the machine stays on
   `inbox.jsonl`, and `doctor` names the store.

   The journal makes an interrupted cut-over safe to finish. It lives under
   `~/.agent-harness/`, is written and flushed before the first log moves,
   and lists every shadow log with its size and destination. Each log moves
   by copying to a temporary name in `store_root`, flushing, renaming into
   place, and only then removing the shadow copy. Any harness process that
   finds a journal without a marker writes nothing, in either form, until it
   has finished the cut-over itself: it takes the same locks (the dead
   process's locks are recoverable after their timeout), and for each listed
   log keeps the copy in `store_root` if it is complete and moves the shadow
   copy otherwise, then writes the marker and removes the journal. No writer
   can create a second shadow log in between, so nothing is lost or
   duplicated.

   Reconcile never runs after the marker. An old store found later, such as a
   checkout restored from a backup, is static, because no code writes
   `inbox.jsonl` any more. It is imported once, as new entries.
6. `store_root` setting, `doctor` checks for the store folder, and a documented
   two-device walkthrough. A machine may point `store_root` at a shared folder
   only after its own cut-over. Each device therefore migrates alone and joins
   with finished logs, and the ordering rules above apply only between devices
   that are all on the fold.

   Changing `store_root` is a command, not a bare edit of `config.json`,
   because the device's logs must arrive in the new folder before it becomes
   active. The command takes this device's log locks and writes a
   root-change journal under `~/.agent-harness/`, naming the old root, the
   new root, and every file it will copy. It then copies this device's own
   logs and archives into the new folder by the same copy-flush-rename steps
   as cut-over, verifies each copy's size, switches `config.json`, leaves a
   marker in the old folder naming where the logs went, and removes the
   journal. It copies only files this device wrote, so one writer per file
   still holds.

   While a root-change journal exists, no writer on this device writes
   anything, so the old root stays the only current copy and nothing can be
   written after a log was copied. Any harness process that finds the
   journal finishes the change before doing anything else: it copies every
   listed file again from the old root, replacing whatever copy the new
   folder already holds, then switches `config.json` and removes the
   journal. A peer reading the new folder sees either a temporary name,
   which no reader opens, or a complete and current copy. If the new folder
   cannot be reached, the operator can cancel with the same command: it
   deletes this device's copies in the new folder, by their device-named
   file names, and removes the journal, leaving the old root in use.

   `config.json` records the device's last active root beside `store_root`.
   Writers compare the two. If they differ, because the setting was edited
   by hand, the harness writes nothing, whatever logs the configured folder
   holds: an old root still has this device's earlier logs, so finding them
   there proves nothing. `doctor` says to run the command, which moves the
   logs from the last active root to the configured one.
7. Aging, `memory_retract`, `source.session` / `source.agent` / `source.device`.
8. Make automatic acceptance the default; supersede ADR 0003 and ADR 0004 with
   new records; update the templates and installed agent instructions.
9. SQLite index, behind a benchmark.
10. Remove the Obsidian sentence from the vault README template.

Steps 2 and 3 do not depend on the storage layer and can ship first. Step 5
depends on 4, and 5a on 5. Step 6 depends on 5a. Step 8 depends on 7.

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
