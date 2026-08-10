# Roadmap

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

## Phase 2 -- Unified telemetry

- Per-agent session/cost/limit collectors: Claude Code (usage API or local
  session logs), Codex CLI (its own session/usage output), Antigravity/Gemini
  CLI (quota + session logs).
- Normalize all collectors to one JSONL schema (timestamp, agent, session id,
  tokens/cost if available, rate-limit remaining, event type).
- Store telemetry locally under a user-config directory (e.g.
  `~/.agent-harness/telemetry/`), never transmitted off-machine.
- Provide a small CLI (`harness/telemetry.py`) to tail/aggregate the JSONL
  stream per agent and per project.
- Keep collectors best-effort and non-blocking: a missing or malformed
  session log must not break the agent's actual work.

## Portable hierarchical memory

Design: `docs/superpowers/specs/2026-07-22-portable-hierarchical-memory-design.md`.

- Shipped: storage model and schema-1 entry contract, project + machine
  scopes, stable entry ids, credential rejection, and the
  `agent-harness-memory init | remember | status` command surface.
- Shipped: `review` (accept/reject/resolve/supersede, atomic line-level
  splice, resolution evidence, supersession history) and `context` (the
  token-budgeted packet contract -- fixed section order, per-section and
  whole-packet character budgets, unit-atomic omission with counts, Markdown
  and JSON).
- Shipped: ChromaDB-backed semantic selection as the **default** strategy.
  `context --strategy auto` prefers it and falls back to deterministic
  ranking with a visible note when the `semantic` extra or the index is
  absent; `--strategy semantic|deterministic` pins one for benchmarking. The
  index is built by the explicit `index` command only and lives machine-side
  at `~/.agent-harness/memory/index/<project-id>/`.
- Shipped: automatic surfaces. A SessionStart hook
  (`hooks/claude/memory_context_hook.py`) injects the packet at session
  start -- deterministic and small-budget by design, because each hook run
  is a fresh process and `auto` would import chromadb every time
  (`AGENT_HARNESS_HOOK_STRATEGY=auto` opts in). Three MCP tools
  (`memory_context`, `memory_status`, `memory_remember`) expose memory to
  agents; the long-lived server is where semantic selection belongs. There
  is deliberately no MCP review/accept tool, and `memory_remember` hardcodes
  `proposed`/`inferred` -- an agent must not be able to promote its own
  guesses.
- Shipped: machine-scope memory has its own shared index rather than being
  copied into every project's, and `bootstrap.ps1` +
  `agent-harness-enroll` reduce setup to one command per machine and one
  per project.
- Remaining: `compact` (weekly deterministic archive + durable promotion),
  scope-drift detection, cross-project relationship proposals, and the
  optional Obsidian visualization add-on.
- Shipped: in-session approval. `memory_confirm` records a plan the human
  approved in chat as an accepted decision plus one tracked `task` per step,
  storing their actual words as `source.confirmation` -- the audit trail
  that replaces a blueprint file and a checkbox. `memory_task_done` closes a
  task with evidence, and `memory_tasks` lists open work in priority order.
  Bottlenecks are deterministic tag clustering over open work (count,
  blocker weight, age past a staleness threshold) and surface in the context
  packet's `priorities` section, so a session opens knowing what keeps
  stalling. No new store or schema version -- these are ordinary schema-1
  entries, so they index, review, supersede, and archive like everything
  else.
- Shipped: auto-enrollment. The SessionStart hook creates `.agent-harness/`
  for a project that has none, gated on BOTH `AGENT_HARNESS_AUTO_ENROLL` and
  the project living under `AGENT_HARNESS_WORKSPACE`. Memory init only --
  building a graph or index there would put a multi-second job in front of
  every session start.
- Shipped: the distribution is `agent-harness-kit` (the plain
  `agent-harness` name on PyPI belongs to an unrelated project). Console
  scripts and the `harness` import package are unchanged. `bootstrap.ps1`
  installs from the local checkout and registers the SessionStart hook at
  *user* scope, which is what lets `agent-harness-enroll` be memory + graph
  + index only and write no instruction files.
- Shipped: the memory/graph join (`harness/memory_graph.py`). Until now the
  two halves of this toolkit answered adjacent questions independently --
  memory said "here is what we decided", the graph said "here is what calls
  this", and nothing said "the decision you recorded last week touches these
  symbols". `context` now resolves symbol-shaped names out of an entry's
  summary and tags, matches them against graph node labels, and renders each
  match's transitive dependents inside that entry's own unit (so budgeting
  stays unit-atomic and a blast radius can never outlive the entry claiming
  it). The traversal is `graph_query`'s, so the packet and `impact` cannot
  disagree; files are ordered nearest-first so the direct call sites are the
  ones that fit on the line. Resolution requires symbol *shape* and an
  exactly-unique label match -- ambiguous labels are dropped, never guessed,
  because a wrong blast radius is worse than none. On by default across CLI,
  MCP, and the SessionStart hook; silent where no graph exists; `--no-graph`
  and `AGENT_HARNESS_HOOK_GRAPH=0` opt out.
- Remaining on that join: the other direction -- ranking graph query results
  by memory relevance, so `find` surfaces the symbols a recent decision
  touched ahead of equally-named ones nobody has discussed.
- Note: the design doc lists a vector database as a non-goal "for the first
  implementation". That still holds for the *core* package -- `chromadb`
  stays an optional extra and every command works without it -- but semantic
  selection is now the preferred path when available, rather than an
  afterthought behind a flag.
- The command surface is exposed as `agent-harness-memory <subcommand>` to
  match this project's existing console-script convention, rather than the
  bare `harness <subcommand>` used in the design prose.

## Phase 3 -- Question router

- CLI that takes a prompt/task description and classifies it (e.g. quick
  lookup, refactor, architecture decision, long-running batch job).
- Route the classified task to the best available agent based on capability
  fit, per-token/session cost, and remaining rate limits pulled from Phase 2
  telemetry.
- Support a dry-run mode that only prints the recommended agent + reasoning,
  and an execute mode that hands the prompt off directly.
- Make routing rules user-editable (a simple rules/config file, not a hidden
  heuristic) so routing stays auditable.

## Phase 4 -- Multi-screen dashboard ("fleet view")

- Local web UI (stdlib HTTP server or a minimal, explicitly-approved
  dependency) that reads the Phase 2 telemetry stream.
- Show live sessions across Claude Code, Codex CLI, and Antigravity/Gemini
  CLI side by side: current task, elapsed time, token/cost burn, remaining
  limits.
- Support multiple concurrent projects/repos in one view, not just one
  working directory.
- No cloud component by default -- runs entirely on localhost, opt-in only.
