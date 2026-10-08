# Portable Hierarchical Memory Design

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  
**Project:** Agent Harness

## Purpose

Agent Harness will provide a portable, production-grade memory layer for any
software project that uses AI-assisted development. It will maintain concise
short-term project context, compact that context weekly, preserve durable
knowledge at both project and machine scope, and identify scope drift without
requiring large prompts or continuous AI summarization.

The core product must remain useful without Obsidian, a specific AI agent, or
an AI API. Obsidian is an optional visualization adapter for ordinary Markdown
artifacts; it is not a runtime dependency and the harness will not manage an
Obsidian installation, plugins, or `.obsidian` configuration.

## Goals

- Give an AI agent enough current context to resume work efficiently.
- Track objectives, decisions, progress, blockers, and completion evidence.
- Compact weekly activity into useful durable knowledge.
- Preserve reusable knowledge across projects on the same machine.
- Discover possible relationships between projects without treating guesses
  as trusted facts.
- Detect scope drift by comparing proposed work with recorded boundaries and
  acceptance criteria.
- Keep context and summarization costs bounded as project history grows.
- Work across Claude Code, Codex, Gemini, IDE agents, and future runtimes
  through one stable context contract.
- Remain inspectable, reversible, Git-friendly, and safe by default.

## Non-goals

- Capturing complete prompts, responses, source files, or unrestricted agent
  session logs.
- Replacing Git, issue trackers, or project documentation.
- Automatically accepting inferred decisions or cross-project relationships.
- Installing or configuring Obsidian or its community plugins.
- Requiring embeddings, a vector database, SQLite, or a hosted service for the
  first implementation.
- Performing expensive AI summarization in agent hot paths.

## Evidence and Existing-System Constraints

The existing Claude hooks are not the cause of the reported Obsidian failure.
The `PreToolUse` graph guard reads hook input and may deny a direct read of the
large graph JSON. The `SessionStart` graph check reads graph and Git health and
prints guidance. Neither hook writes to Obsidian, `.obsidian`, AppData, or an
Obsidian executable.

The repository currently contains a nested Obsidian vault whose ignored
`.obsidian` state enables Obsidian Sync and multiple community plugins while
the checkout is also stored in OneDrive. That creates competing synchronization
and plugin-runtime risks which persist outside Git. The new design therefore
separates portable Markdown memory from optional visualization and adds
diagnostics for unsafe sync combinations.

Existing uncommitted Phase 2 telemetry changes are outside this design's
implementation scope and must be preserved.

## Chosen Architecture

The system uses a Markdown-first event ledger with deterministic weekly
compaction and two durable-memory scopes:

```text
Explicit agent entries + metadata-only Git evidence + optional collectors
                              |
                              v
                    Project proposal inbox
                              |
                       review / accept
                              v
                  Weekly active project memory
                              |
                    deterministic compaction
                              v
                    Project durable memory
                              |
                  reviewed promotion proposals
                              v
                    Machine durable memory
                              |
                  relevance-bounded retrieval
                              v
                     Current agent context
```

Markdown is the human-readable source of truth for accepted project memory.
JSONL is used only for compact append-oriented proposal records. Retrieval is
based initially on explicit project relationships, tags, technologies,
recency, state, and source attribution. This avoids a mandatory database or
embedding model while keeping future indexing implementations possible behind
the same interface.

## Storage Model

### Project-owned storage

Each initialized repository contains:

```text
.agent-harness/
  config.json
  scope.md
  memory/
    active.md
    durable.md
    inbox.jsonl
    archive/
      YYYY-Www.md
```

- `scope.md` records the project purpose, current boundaries, explicit
  exclusions, acceptance criteria, and active objective.
- `active.md` is a bounded weekly context containing current phase, open work,
  blockers, recent accepted decisions, and the last compaction timestamp.
- `durable.md` contains stable project knowledge: architecture, conventions,
  confirmed decisions, milestones, recurring lessons, and unresolved risks.
- `inbox.jsonl` contains proposed observations from explicit agent actions,
  Git metadata, or optional collectors.
- `archive/YYYY-Www.md` is an immutable human-readable weekly record produced
  by compaction.
- `config.json` contains schema version, identity, capture settings, context
  budgets, privacy controls, and optional integrations.

> **Superseded on 2026-09-11 by ADR 0004.** Project memory is never committed.
> It stays on disk beside the project, excluded through the repository's local
> `.git/info/exclude`, so it never appears in anyone's git work. The original
> paragraph is kept below for the record.

~~Project memory is committed with the repository by default so it travels with
the code and can be reviewed through Git. Teams may override that policy in
configuration, but the harness will report when durable project memory is not
versioned.~~

### Machine-owned storage

Machine-wide memory lives outside repositories under the platform-appropriate
user data directory. The logical default is `~/.agent-harness/memory/`, with a
configuration override for managed environments.

```text
~/.agent-harness/memory/
  registry.json
  durable.md
  inbox.jsonl
  relationships.jsonl
  archive/
    YYYY-Www.md
```

- `registry.json` assigns stable project identifiers and records repository
  locations, technologies, aliases, and last-seen metadata.
- `durable.md` contains accepted reusable practices, developer preferences,
  environment knowledge, and cross-project lessons.
- `relationships.jsonl` records accepted and proposed links between projects,
  including evidence and confidence.
- Machine archives preserve promotion and supersession history.

Machine memory is local by default and must never be silently committed into a
project. Export and synchronization are separate, explicit operations that are
not required for the first implementation.

## Memory Entry Contract

Every logical entry contains:

```yaml
id: mem-2026-07-22-001
type: decision | discovery | task | blocker | lesson | relationship
scope: project | machine
status: proposed | accepted | superseded | resolved
confidence: confirmed | inferred
source:
  project: stable-project-id
  commit: optional-commit-sha
  file: optional/relative/path
tags: [example, tags]
summary: One concise, durable statement.
```

Additional rules:

- IDs are stable and unique within the machine registry.
- Accepted entries must have a source or explicitly state that they were
  manually confirmed by the operator.
- Inferred entries remain proposed until accepted.
- Existing accepted entries are superseded, never silently rewritten.
- Resolved tasks and blockers retain their resolution evidence.
- Summaries have a strict configured length limit.
- Paths stored in project memory are repository-relative whenever possible.

## Capture Model

Capture is hybrid, with explicit entries authoritative:

1. Agents use `harness remember` to record decisions, discoveries, tasks,
   blockers, lessons, and confirmed relationships.
2. Git collection may propose metadata-only observations from commits, branch
   state, and changed paths. It does not read or store source bodies.
3. Optional agent-log collectors may use strict field allowlists. They must not
   traverse prompt content, tool arguments, source bodies, secrets, or arbitrary
   map keys.
4. Automatically collected observations enter `inbox.jsonl` as `proposed` and
   never become authoritative solely because they were observed repeatedly.
5. Review accepts, edits, rejects, resolves, or supersedes proposals.

Hooks may suggest that an important outcome be recorded, but they do not run
summarization or write inferred durable memory automatically.

## Weekly Compaction

`harness compact` performs a deterministic pass before any optional AI work:

1. Validate schemas and acquire a project-scoped write lock.
2. Snapshot the input files needed for rollback.
3. Deduplicate exact and normalized-equivalent proposals.
4. Reconcile task and blocker states using explicit identifiers.
5. Detect contradictions and leave them unresolved for human review.
6. Compare accepted and proposed work with `scope.md`; report possible drift.
7. Create the immutable weekly archive.
8. Promote confirmed stable project knowledge into `durable.md`.
9. Create machine-promotion proposals for reusable lessons and relationships.
10. Rewrite `active.md` within its configured entry and character budgets,
    carrying forward only unresolved or currently relevant work.
11. Commit all file replacements atomically or restore the snapshot on error.

An optional AI summarization provider may improve wording after deterministic
selection. Its input and output budgets are fixed by configuration. Failure or
absence of the provider leaves the deterministic compaction result usable.

Compaction is explicit in the first implementation. A scheduler may remind the
operator when memory is stale, but it must not perform unreviewed machine-memory
promotion.

## Cross-project Relationships

The harness proposes relationships from low-cost evidence such as:

- shared declared dependencies or technologies;
- explicit repository references;
- common service, package, or workspace identifiers;
- accepted entries that name another registered project;
- user-supplied parent, child, predecessor, or dependency relationships.

Each proposal includes evidence, direction, relationship type, confidence, and
source project. Relationships become durable only after explicit acceptance.
Future retrieval can use accepted relationships to surface relevant memory from
another project, but project-specific details are not copied into machine memory
unless separately promoted.

## Context Retrieval and Token Budgets

`harness context` returns the same logical packet for every supported agent:

1. Current project scope and objective.
2. Current phase, open work, and blockers from `active.md`.
3. Relevant project durable entries.
4. Relevant machine durable entries.
5. Relevant accepted cross-project relationships.
6. Memory freshness and unresolved contradiction warnings.

Selection order is deterministic: required scope fields, unresolved work,
explicitly referenced entry IDs, accepted project entries, accepted related
machine entries, then recent lower-priority items. Each section has an
independent character budget and the whole packet has a hard maximum. When the
budget is exhausted, lower-priority entries are omitted with an explicit count;
entries are not cut mid-record.

The first implementation uses tags and structured metadata rather than sending
the entire memory corpus to an AI model. A future search backend may replace the
indexing strategy without changing the context output contract.

## Scope-drift Detection

`harness status`, `harness review`, and `harness compact` compare proposed work
with the active objective, boundaries, exclusions, and acceptance criteria in
`scope.md`. They report, but do not automatically reject:

- work unrelated to the active objective;
- changes touching an explicit exclusion;
- new dependencies or subsystems without a recorded decision;
- tasks carried across multiple compactions without progress evidence;
- completed claims without a verification source;
- contradictory accepted decisions.

Warnings cite the relevant scope statement and memory entries. Overrides require
an explicit new or superseding decision so the reason becomes durable.

## Command Interface

The stable command surface is:

- `harness init` registers the project and creates storage without touching
  Obsidian.
- `harness remember` appends an explicit structured entry.
- `harness status` reports objective, progress, blockers, freshness, integrity,
  completion evidence, and scope warnings.
- `harness review` operates on proposals, contradictions, stale work, promotion
  candidates, and relationship candidates.
- `harness compact` creates the weekly archive and bounded active memory.
- `harness context` emits a token-budgeted Markdown or JSON context packet.
- `harness doctor` validates configuration, storage, locks, hooks, privacy
  settings, memory integrity, project registration, and optional integrations.
- `harness obsidian enable` creates only an explicit visualization link or
  documented vault-opening workflow selected by the user.

Interactive and non-interactive flags must use the same underlying operations.
Automation commands return stable exit codes and machine-readable output.

## Agent Integration

Agent integrations are thin adapters:

- Instruction files reference the stable `harness context` and `harness
  remember` behavior through managed marker blocks.
- Session-start hooks may request bounded context or report stale memory.
- Completion hooks may propose that a verified outcome be recorded.
- Hooks are opt-in and fail open so a harness error cannot prevent ordinary
  development work.
- Hooks must not invoke AI providers, weekly compaction, machine promotion, or
  Obsidian.

The context schema, not an agent-specific hook format, is the compatibility
boundary.

## Obsidian Add-on

Obsidian is an optional read-oriented visual layer over Markdown memory.

- Core initialization never creates `.obsidian` or registers a project as a
  vault.
- The add-on never installs community plugins or edits Obsidian application
  state.
- Users choose either to open the project memory directory as a vault or expose
  it inside an existing user-owned vault through an explicit, reversible link
  or generated index.
- Generated navigation files are ordinary Markdown and remain useful in any
  editor.
- `doctor` warns when the same vault is controlled by competing sync systems,
  when enabled plugins require a newer Obsidian version, or when a link target
  is missing.
- Disabling the add-on removes only harness-created links or generated indexes
  after confirming their exact targets. It never deletes memory or vault data.

## Privacy and Security

- Collectors use key-name allowlists and metadata-only reads.
- Secrets, environment values, prompt text, response text, source bodies, and
  arbitrary tool input/output are excluded.
- Potential secrets are rejected before an entry reaches an inbox.
- Machine-memory promotion requires explicit acceptance.
- Machine storage uses user-only permissions where the platform supports them.
- External links and project paths are validated before writes.
- Project-scoped locks and atomic replacement prevent partial compaction.
- Corrupt input is quarantined with a diagnostic; valid memory remains
  readable.
- No network transmission occurs unless the operator explicitly configures an
  optional AI provider or future synchronization adapter.

## Failure Handling

- Missing Git, hooks, Obsidian, agent logs, or an AI provider results in a
  warning and reduced functionality, not data loss.
- Invalid configuration prevents mutation and reports the exact field.
- Compaction failures restore the pre-operation snapshot.
- Concurrent writers either acquire the lock or exit with a clear retryable
  status; they never interleave JSONL fragments.
- Contradictions remain visible and block promotion of the affected fact, not
  unrelated compaction.
- Context generation falls back from machine and related-project memory to
  project active memory when higher layers are unavailable.
- Schema migrations are explicit, versioned, backed up, and reversible within
  the supported version window.

## Testing Strategy

The implementation requires tests for:

- initialization idempotency and uninstall preservation;
- explicit capture and proposal-state transitions;
- schema validation and version migration;
- secret and content-bearing field rejection;
- deterministic deduplication, reconciliation, and compaction;
- immutable archives and supersession history;
- atomic rollback after injected failures;
- concurrent write and lock behavior on supported operating systems;
- strict context budgets and deterministic selection order;
- scope-drift and missing-verification warnings;
- machine promotion and source attribution;
- proposed and accepted cross-project relationships;
- retrieval with related, unrelated, stale, and unavailable projects;
- operation without Git, hooks, Obsidian, logs, network, or an AI API;
- Obsidian integration that does not modify `.obsidian` or install plugins;
- OneDrive plus Obsidian Sync conflict diagnostics;
- Windows, macOS, and Linux path and permission behavior;
- installed-package execution outside the source checkout.

Synthetic fixtures must be used for agent logs and private project data.

## Rollout Boundaries

Implementation planning should divide delivery into independently verifiable
phases:

1. Storage schemas, project identity, explicit capture, and integrity checks.
2. Bounded project context, status, and deterministic weekly compaction.
3. Machine registry, promotion review, and cross-project relationships.
4. Thin agent adapters and opt-in hooks.
5. Optional Obsidian visualization and sync-conflict diagnostics.

Each phase must preserve the existing graph, governance, installer, and
telemetry capabilities. No phase may make Obsidian or an AI provider mandatory.

## Success Criteria

The design is successfully implemented when:

- A new or existing repository can initialize and use memory without Obsidian.
- An agent can resume from a bounded context packet without reading full
  project history.
- Explicit decisions and completion evidence survive weekly compaction.
- Weekly context size remains within configured limits as archives grow.
- Reusable knowledge can be reviewed and promoted to machine memory with source
  attribution.
- A later project can retrieve relevant accepted knowledge through tags or an
  accepted project relationship.
- Scope drift, stale tasks, contradictions, and unsupported completion claims
  are visible to the operator.
- No automatic collector can make inferred memory authoritative.
- The system operates deterministically without network access or an AI key.
- Enabling or disabling the Obsidian add-on does not modify the Obsidian
  installation, plugin set, or `.obsidian` configuration.
- Existing tests and installed-package smoke checks remain green across the
  supported operating-system and Python matrix.
