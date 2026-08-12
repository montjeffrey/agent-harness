# Roadmap

## What this kit is for

Four objectives, all of them first-class. They mostly reinforce each other,
but where they pull apart the tension is named rather than papered over.

1. **Daily driver.** It has to work, unattended, on the machines actually in
   use -- Windows first, cloud-synced paths, no setup ceremony.
2. **Benchmark harness.** It has to *measure* which models and retrieval
   strategies do better on real work, not assert it.
3. **Publishable product.** Other people install it and it works, with no
   private data in the artifact.
4. **Shared memory across agents.** One durable memory that Claude Code,
   Codex, and Antigravity/Gemini all read and write, surviving a switch of
   tool or machine.

Where they conflict: (1) wants whatever works here now, (3) wants nothing
machine-specific to leak. (2) wants pluggable backends and stable
measurement, (4) wants a fixed contract every tool can rely on. The resolution
throughout is the same one: **a plain-text store of record, with everything
else derived and rebuildable.** Measurement, portability, and multi-tool
access all follow from that; none of them survive an opaque store.

---

## Phase 1 -- Portable harness (done)

- Generalized code-relations query CLI (`harness/graph_query.py`) with
  auto-discovery of `graphify-out/graph.json` from any working directory.
- Cross-platform graph refresh (`harness/graph_refresh.py`) replacing the
  PowerShell-only refresh flow, with pluggable backend + API-key sourcing.
- Idempotent installer (`install.py`) that drops the blueprint gate and the
  compact agent instruction block into any project without clobbering
  existing `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` content.
- Optional Claude Code PreToolUse hook that blocks direct reads of
  `graph.json` and redirects agents to the query CLI.
- No hardcoded paths, company names, or secrets -- safe to open-source as-is.

## Phase 2 -- Unified telemetry (done)

- Per-agent session/cost/limit collectors: Claude Code, Codex CLI,
  Antigravity/Gemini CLI.
- One JSONL schema (timestamp, agent, session id, tokens/cost, rate-limit
  remaining, event type), stored under `~/.agent-harness/telemetry/`, never
  transmitted off-machine.
- `harness/telemetry.py` to tail/aggregate per agent and per project.
- Collectors are best-effort and non-blocking: a malformed session log must
  not break the agent's actual work.

## Portable hierarchical memory (core -- done)

Design: `docs/superpowers/specs/2026-07-22-portable-hierarchical-memory-design.md`.

- Storage model and schema-1 entry contract, project + machine scopes, stable
  entry ids, credential rejection, and
  `agent-harness-memory init | remember | status`.
- `review` (accept/reject/resolve/supersede, atomic line-level splice,
  resolution evidence, supersession history) and `context` (token-budgeted
  packet: fixed section order, per-section and whole-packet character budgets,
  unit-atomic omission with counts, Markdown and JSON).
- ChromaDB-backed semantic selection as the **default** strategy.
  `context --strategy auto` prefers it and falls back to deterministic ranking
  with a visible note when the `semantic` extra or the index is absent;
  `--strategy semantic|deterministic` pins one for benchmarking. The index is
  built by the explicit `index` command only and lives machine-side at
  `~/.agent-harness/memory/index/<project-id>/`.
- Machine-scope memory has its own shared index rather than being copied into
  every project's.
- In-session approval: `memory_confirm` records a plan the human approved in
  chat as an accepted decision plus one tracked `task` per step, storing their
  actual words as `source.confirmation` -- the audit trail that replaces a
  blueprint file and a checkbox. `memory_task_done` closes a task with
  evidence; `memory_tasks` lists open work in priority order. Bottlenecks are
  deterministic tag clustering over open work (count, blocker weight, age past
  a staleness threshold) surfaced in the packet's `priorities` section. These
  are ordinary schema-1 entries -- no new store, no schema bump.
- **Both directions of the memory/graph join.** `memory_graph` takes an entry
  and returns its blast radius (memory in, graph out). `graph_evidence` takes
  graph nodes and returns the memory that mentions them (graph in, memory
  out), so `find handler` ranks the handler a decision was recorded against
  ahead of eight equally-named ones. Resolution requires symbol *shape* and an
  exactly-unique label match; ambiguous names are dropped, never guessed.
  `tests/test_cross_checking.py` forces the two directions to agree -- an
  agent must not get two different stories about one symbol depending on which
  tool it reached for.
- Semantic **diagnosis**, not just detection: `chromadb_status()` returns
  (available, reason), `probe_index()` opens the index rather than checking a
  path exists, and doctor answers the three questions separately (is chromadb
  importable / is the graph index usable / is the memory index usable).

---

## Objective 1 -- Daily driver

Shipped: `bootstrap.ps1` + `agent-harness-enroll` (one command per machine,
one per project); SessionStart hook injecting the packet; auto-enrollment
gated on both `AGENT_HARNESS_AUTO_ENROLL` and the project living under
`AGENT_HARNESS_WORKSPACE`; `agent-harness-doctor`.

Also shipped: **`compact`** (`harness/memory_compact.py`) -- the deterministic
weekly pass. Only closed work and duplicate *proposals* leave the inbox, always
into `archive/YYYY-Www.md` first, so every removal is recoverable and no
unreviewed proposal is ever discarded. Accepted knowledge is promoted into
`durable.md` as a **copy**, not a move: retrieval reads durable knowledge out
of the inbox, so moving it would delete it from the packet and the index.
`active.md` is rewritten within entry and character budgets with unit-atomic,
counted omission. Writes are ordered archive -> durable -> active -> inbox, so
a crash loses only derived work; the inbox splice is line-level, preserving
lines this schema version cannot parse. A project lockfile excludes concurrent
compactions and self-heals when stale or corrupt. Because `update_index` only
drops departed ids when re-run, compaction reports when the semantic index
needs rebuilding -- without that, compaction would freshen the files while
retrieval kept serving archived entries.

Deferred out of that increment, deliberately, since each is its own item
below or in Objective 4: scope-drift detection (design step 6), cross-project
relationship proposals (step 9), contradiction detection (step 5, which exists
nowhere yet), and the optional AI summarization pass. Design step 4 (reconcile
task and blocker states) needed no work -- `memory_review` and `memory_tasks`
already enforce those transitions at write time.

Open:

- **Machine-scope compaction.** `compact` is project-scope only. The machine
  inbox that `remember --scope machine` feeds is read by every project's
  context packet and served by the shared machine index, and it still grows
  without bound -- so half the store has no freshness story. It needs its own
  rules rather than the project ones aimed at a different directory: machine
  memory has no repository to be reviewed in, no per-project week to archive
  against, and "promotion" there is the cross-machine question rather than the
  project-durable one.

- **Scope drift** -- detect entries recorded in project scope that are really
  machine-local (and the reverse). Wrong scope means either a machine detail
  committed to a shared repo or a project decision stranded on one machine.
- **Known local breakage:** `key_command` in `~/.agent-harness/config.json`
  produces no output, so the configured graph backend cannot authenticate.
  Diagnosed as a probable `$HOME` resolution difference between Windows
  PowerShell 5.1 and pwsh; left untouched pending a decision, since it reads a
  credential file.
- **Test runs pollute the real registry.** Not one stale id, as previously
  recorded: 22 of the 28 projects in `~/.agent-harness/memory/registry.json`
  are pytest temp paths, so some test writes the operator's real registry on
  essentially every suite run. Two things are needed and the first is the
  urgent one -- find the test that escapes isolation (the memory-compaction
  tests monkeypatch both `machine_memory_dir` and `_registry_path`, which is
  the pattern that holds), then add a prune command for the entries already
  there. Until the leak is closed, pruning only buys one clean run.
- **Cloud-synced paths** -- recursive search across the whole workspace tree
  times out under the sync layer. The harness already avoids this internally;
  it should be a documented constraint rather than folklore.

## Objective 2 -- Benchmark harness

This is the least-built objective and the one that most changes how the others
are judged. "Semantic is the default" is currently a design preference. It
should be a measured result.

Shipped: `--strategy semantic|deterministic` pinning (the control); Phase 2
telemetry (cost/token/limit per agent); `memory_index.top_k_agreement()`, an
opt-in diagnostic that runs both selectors over the same candidates for **one**
query and returns each ranking plus their overlap count. It observes ranking
only -- semantic hits never become a source of graph symbols or evidence.

Open:

- **A runnable benchmark, not just a comparator.** `top_k_agreement` reports
  raw overlap for a single query. Two gaps sit between that and a benchmark:
  it does not iterate a query set, and overlap measures *whether two
  strategies agree*, not *which one is right*. Needs a fixture query set with
  known-good expected entries so agreement becomes precision/recall against
  ground truth.
- **Per-model comparison.** The stated goal is choosing among the models being
  run against. That needs a run format that pins model, strategy, corpus, and
  query set, and emits a comparable record.
- **Join telemetry to memory quality.** Cost per task is already collected and
  retrieval quality is becoming measurable; neither is useful alone. The
  question worth answering is whether a better packet reduces tokens spent to
  complete a task.
- **Reproducibility.** Benchmark runs must be re-runnable from a recorded
  spec, or results cannot be compared across weeks.

## Objective 3 -- Publishable product

Shipped: distribution named `agent-harness-kit` (plain `agent-harness` on PyPI
belongs to an unrelated project); packaging tests covering console scripts,
packaged resources, and wheel/sdist build; a leak scan asserting no private
identifiers ship in either artifact, with the denylist assembled from
fragments so the test file is not itself the leak; an install-into-clean-venv
test that runs console scripts from outside the source tree.

Open:

- **Not actually published.** Install guidance now correctly points at the
  local checkout precisely *because* the PyPI name is unclaimed. Publishing is
  the step that makes `pip install "agent-harness-kit[semantic]"` true, and
  `tests/test_semantic_install_guidance.py` is the test to invert when it is.
- **No CI.** Every result in this repo is from one Windows machine. The two
  skipped tests are POSIX file-mode checks that have therefore *never run*.
  Cross-platform CI is the gap between "works here" and "works".
- **License and contribution surface** unverified for a public release.

## Objective 4 -- Shared memory across agents

Shipped: an MCP server exposing `memory_context`, `memory_status`,
`memory_remember` (plus confirm/tasks) to any MCP-speaking client; a
SessionStart hook for Claude Code; settings/hooks sync into the
Gemini/Antigravity config.

Deliberate limits, not gaps: there is no MCP review/accept tool, and
`memory_remember` hardcodes `proposed`/`inferred`. An agent must not be able to
promote its own guesses to accepted fact.

Open:

- **Codex has no read path.** It is named as a target agent but has neither a
  session hook nor a verified MCP registration.
- **Machine-scope sync.** Project scope travels by git. Machine scope travels
  by nothing -- which is correct by design, but means a second machine starts
  blank with no documented way to seed it.
- **Concurrent writes.** Advisory locking was designed and never built. Two
  agents appending to one inbox is an ordinary case in this setup, not an edge
  case.
- **Contract stability.** The moment more than one tool depends on the packet
  format, it stops being freely changeable. It should be versioned before that
  happens, not after.
- **Cross-project relationship proposals -- half shipped.** `relationship` is
  a schema-1 entry type, and the context packet already gives accepted
  relationship entries their own section (pulled out of the live sections, so
  they cannot be double-counted). What does not exist is anything that
  *proposes* them: a relationship between two projects has to be written by
  hand today. Detecting candidates -- shared symbols, shared tags, one
  project's decision referencing another's -- is the missing half, and it
  belongs here rather than under memory, because a cross-project link is only
  useful to an agent that works across more than one of them.

## Phase 3 -- Question router

- CLI that takes a prompt/task description and classifies it (quick lookup,
  refactor, architecture decision, long-running batch job).
- Route to the best available agent by capability fit, cost, and remaining
  rate limits from Phase 2 telemetry.
- Dry-run mode printing the recommended agent + reasoning; execute mode that
  hands the prompt off.
- User-editable rules file, not a hidden heuristic, so routing stays auditable.

Depends on Objective 2: routing by "best available agent" is guesswork until
agent performance is actually measured.

## Phase 4 -- Multi-screen dashboard ("fleet view")

- Local web UI reading the Phase 2 telemetry stream.
- Live sessions across Claude Code, Codex CLI, and Antigravity/Gemini side by
  side: current task, elapsed time, token/cost burn, remaining limits.
- Multiple concurrent projects in one view.
- No cloud component by default -- localhost, opt-in only.

## Optional add-on -- Obsidian visualization

Memory is Markdown on disk, so a vault view is nearly free. Kept explicitly
optional: nothing in the kit may require it.

---

## Notes on deviations from the design doc

- The design lists a vector database as a non-goal "for the first
  implementation". That still holds for the *core* package -- `chromadb` is an
  optional extra and every command works without it -- but semantic selection
  is now the preferred path when available rather than an afterthought behind
  a flag. The design's own provision for this is the sentence that a future
  search backend may replace the indexing strategy without changing the
  context output contract, which is the seam that was used.
- The command surface is `agent-harness-memory <subcommand>`, matching this
  project's console-script convention, rather than the bare
  `harness <subcommand>` used in the design prose.
