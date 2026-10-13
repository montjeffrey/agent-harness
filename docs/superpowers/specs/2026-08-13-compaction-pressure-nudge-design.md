# Compaction pressure nudge

Date: 2026-08-13
Status: approved for implementation

## Problem

`agent-harness-memory compact` exists, is wired into the CLI, and has never
run in this repository. The evidence is in the store itself:
`.agent-harness/memory/archive/` is empty, and `active.md` still carries its
untouched template header rather than the `Last compacted: <ts> (<week>)` line
that `memory_compact.render_active` writes.

Nothing is wrong with compaction. Nothing invokes it. Every other memory
command adds -- `remember` appends, `review` transitions a status, `index`
mirrors -- so an inbox in long-term use grows without bound, and the growth
degrades exactly what the store exists to provide: the selector ranks over
more candidates and the context packet spends its budget on settled work.

The same absence shows at machine scope, differently. The machine inbox holds
6 entries and every one of them is `proposed` -- none has ever been reviewed.
Machine memory is the half shared across every registered project (4 today),
so enrolling more projects multiplies the writers without adding a reader.

## What this design does, and does not, do

It makes not-compacting **visible**. It does not make compaction run.

That boundary is inherited, not chosen.
`2026-07-22-portable-hierarchical-memory-design.md` states it twice:

> Compaction is explicit in the first implementation. A scheduler may remind
> the operator when memory is stale, but it must not perform unreviewed
> machine-memory promotion. (line 231)

> Hooks must not invoke AI providers, weekly compaction, machine promotion,
> or Obsidian. (line 320)

Hooks *may* report stale memory (line 316). A reminder is therefore the
largest intervention available without amending the governing design, and
amending it was considered and rejected: automatic removal would take entries
out of the store of record with no human in the loop, and the operator would
discover it by reading an archive rather than by deciding.

Out of scope, deliberately:

- Machine-scope `durable.md`, `archive/`, or eviction rules. Machine memory
  is 6 entries. Writing archival rules for it now would be inventing policy
  ahead of evidence; revisit when there is volume to reason about.
- A `source.project` field on machine entries. Machine entries currently
  carry no source attribution at all, which will matter for pruning by
  origin -- but it is a schema change with a backfill, and it is not what
  this design needs.
- Scheduled or hook-driven invocation of `compact` itself. Forbidden above.

## Approach

Three were considered.

**A. Read the timestamp `compact` already writes.** Parse `Last compacted:`
out of `active.md`; warn when it is older than N days or absent. No new
state. Rejected: it parses prose that a human is explicitly invited to hand
edit ("Hand edits are preserved until the next compaction"), and it keys on
elapsed time, so it nags a clean store and stays silent on a dirty one that
happens to have been compacted recently.

**B. A dedicated state file** written by `compact()`, read by the packet.
Structured and unambiguous. Rejected: it duplicates a fact `active.md`
already records, and adds a file whose tracked-or-ignored status is a further
decision, to solve a problem C solves with no new state at all.

**C. Derive pressure from the entries themselves.** Chosen. Count what
compaction would actually act on, and speak only when those counts are
non-zero.

C measures the condition instead of a proxy for it. It stays quiet the week
nothing closed and speaks the day something does. It also dissolves the
"never compacted" edge case entirely: a store that has never been compacted
is not a special state, it simply has whatever pressure it has. And the
second thing this design owes -- surfacing proposals nobody has reviewed --
falls out of the same pass rather than needing separate machinery.

`active.md`'s `Last compacted:` header stays as human-readable provenance.
Nothing parses it, so hand edits cannot corrupt the signal.

## Components

### `memory_compact.pressure(entries, now) -> CompactionPressure`

A pure function beside the rules it describes. `CompactionPressure` is a
frozen dataclass:

| field | meaning |
|---|---|
| `closed_in_inbox` | entries satisfying `is_terminal` -- work that is over |
| `duplicate_proposals` | entries `find_duplicates` would drop |
| `unreviewed` | entries with status `proposed` |
| `oldest_unreviewed_days` | age of the oldest such entry, or `None` |
| `near_duplicate_proposals` | proposals whose wording one another contains |

It reuses `is_terminal` and `find_duplicates` unchanged. This is the
load-bearing decision in the design. A reimplementation of "what is
compactable" would drift from the removal rules the first time those rules
change, and a nudge that overstates what compaction will do is worse than no
nudge: the operator runs it expecting twelve entries to clear, three clear,
and the line stops being believed. Keeping the definition next to compaction
means the signal changes with the behaviour it advertises.

`duplicate_proposals` counts entries that would be *removed*, not duplicate
groups. `find_duplicates` keeps one member of each group, so a group of
three contributes two. Counting groups would overstate the payoff.

`near_duplicate_proposals` is added later and is a different *kind* of
number, which is why it is a separate field with a separate note. The two
above count what `compact` will remove; this one counts what only a human
can decide about, because containment is not equivalence -- the longer entry
states a strictly more specific claim. Removing on containment would be the
automatic removal this design rejected in "What this design does, and does
not, do"; reporting it is squarely inside the "hooks may report stale
memory" permission. Its note therefore points at `review`, never `compact`,
and it fires at one rather than a threshold: a backlog of unreviewed
proposals is normal in small numbers, but a single contained pair is already
worth a look.

This count is deliberately indifferent to *which* member survives, and that
indifference is what let the survivor rule change without touching the
design. `find_duplicates` kept the earliest of each group when this was
written; it now keeps the most detailed, breaking ties on the earliest id.
Group sizes are identical either way, so the number this nudge advertises is
unchanged and the reuse argument above still holds.

### `memory_context.build_packet`

Calls `pressure()` **once per pool** -- once over project entries and once
over machine entries, never over the two combined -- and appends notes beside
the existing `_freshness_warnings`, same shape, same position, same
precedent. Per-pool computation is what the scope rules below depend on: a
combined count could not tell the operator which store to act on, and only
one of the two stores has a working `compact`.

No new packet field: `ContextPacket.notes` is already a tuple rendered by
both `render_markdown` and `render_json`, so the CLI, the MCP tool, and the
session-start hook pick the line up without further work.

`memory_compact` imports only `memory_store`, so `memory_context` importing
it introduces no cycle. Cost on the hot path is pure Python over entries
`build_packet` has already loaded and is already iterating: no second read,
no I/O.

## The notes

Two sentences, never merged into one, because they ask for different actions
and only one of them is mechanical.

```
[memory] 12 closed, 3 duplicate proposals -- run `agent-harness-memory compact`
[memory] 6 proposals unreviewed, oldest 23d -- run `agent-harness-memory review`
```

Merging them would produce a lie in the state the machine inbox is in right
now. All 6 of its entries are `proposed`, and **compaction cannot remove a
proposal** -- an unreviewed proposal is never discarded automatically,
because the decision to throw away an observation belongs to a human. A note
reading "6 entries, run compact" would send the operator to a command that
clears nothing.

### Firing rules

- **Compact note:** `closed_in_inbox > 0 or duplicate_proposals > 0`. No
  threshold. If compaction would remove something, say so.
- **Review note:** `unreviewed >= 5 or oldest_unreviewed_days >= 7`. A
  proposal recorded this morning is the system working; one sitting a
  fortnight is the observed failure. Age is the sharper trigger; the count
  catches a sudden pile-up.

Both are silent at zero. A clean store adds nothing to the packet, which is
what keeps the line credible when it does appear.

Thresholds are named constants in `memory_compact`, beside the predicates --
not configuration. There is no evidence yet about what the right numbers are,
and a config surface for two untuned integers would be speculative.

### Scope asymmetry

`review` accepts `--scope` and can act on machine memory. `compact` operates
on `project_memory_dir` only. Therefore:

- Project pressure may produce either note.
- Machine pressure produces **only** the review note, and it must name the
  scope explicitly, because the flag defaults to `project`:

```
[memory] 6 machine proposals unreviewed, oldest 23d -- run `agent-harness-memory review --scope machine`
```

Emitting a compact note against machine pressure would print a command that
silently does nothing -- the precise failure that trains an operator to
ignore the line.

## Failure modes

**Totality instead of exception handling.** `pressure()` is total over
malformed input rather than wrapped in a catch:

- an unrecognised status counts as neither terminal nor proposed;
- an entry with a missing or unparseable `ts` still counts toward
  `unreviewed`, but is excluded from `oldest_unreviewed_days`.

This keeps the hot path free of exception handling and satisfies the
project's no-silent-swallowing rule by leaving nothing to swallow. Should
something genuinely raise, the session-start hook's existing fail-open
behaviour covers it: hooks are opt-in and fail open so a harness error cannot
prevent ordinary development work.

**Budget.** At most two notes, one line each. No meaningful draw on the
packet's character budget.

## Testing

Tests are written first. The first is the centrepiece.

1. **Pressure equals reality.** Build a store, compute `pressure()`, run
   `compact()`, and assert `closed_in_inbox + duplicate_proposals` equals the
   number of ids actually removed. This protects the entire premise and fails
   the moment the predicates and the removal rules diverge.
2. **Silence at zero.** A clean store produces no note.
3. **Both review triggers, independently.** Age fires with a low count;
   count fires with fresh entries. Testing only one would let the other ship
   dead.
4. **Malformed entries.** Missing status, absent `ts`, and unparseable `ts`
   neither raise nor inflate counts.
5. **Scope.** No compact note is ever emitted for machine pressure, and the
   machine review note carries `--scope machine`.
6. **Both renderers.** The note survives into `render_markdown` and
   `render_json`, since the CLI, MCP tool, and hook consume different ones.

## Consequences

The operator gets a truthful, self-silencing signal at session start, and
retains the trigger. Machine memory's 6 unreviewed proposals become visible
for the first time without any machine-scope archival policy being invented.

If the nudge is shown and ignored over a sustained period, that is evidence
for revisiting the governing design's "explicit in the first implementation"
stance -- with data about what was ignored, rather than in advance of it.
