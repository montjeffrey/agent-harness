# agent-harness

A portable, stdlib-only toolkit that gives any coding agent -- Claude Code,
Codex CLI, Antigravity/Gemini CLI, or others that read `AGENTS.md` -- the same
portable capabilities inside any project:

1. **Queryable code relations** -- search, inspect, and traverse a
   [graphify](https://pypi.org/project/graphify/) `graph.json` without ever
   reading the whole file into context.
2. **An advisory, human-in-the-loop blueprint gate** -- feature-scale changes
   get a Markdown blueprint (viewable natively in Obsidian if the project
   root is a vault) that a human is expected to approve before an agent
   writes code. This is a convention, not a technical control -- the harness
   does not stop an agent from writing code without approval.
3. **Agent catch-up** -- a compact, marker-delimited instruction block dropped
   into `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` so any agent picks up the
   conventions on its first read of the project.
4. **Bounded project memory** -- reviewed Markdown/JSONL facts, compact
   context packets, and optional local semantic ranking; no hosted memory
   service or automatic durable promotion is required.
5. **Token-efficiency conventions** -- a PreToolUse hook (Claude Code) that
   blocks direct reads of `graph.json` and redirects to the query CLI, plus
   guidance baked into the instruction block.
6. **Self-healing graph checks** -- a SessionStart hook (Claude Code) that
   detects a missing or stale graph at the start of every session and hands
   the agent the exact build/refresh command.

This project only *generalizes* tooling -- it does not replace project-specific
CI, git hooks, or CRM/business logic. Install it into any repo with one
command; nothing here is specific to any one company, user, or codebase.

## Quickstart

Two steps: set up the machine once, then enroll each project you work in.

> [!IMPORTANT]
> **Do not run `pipx install agent-harness`.** The name `agent-harness` on
> PyPI belongs to an unrelated project, so that command installs a stranger's
> package. This project is not published yet; install it from a clone until
> it ships under a name it owns.

**Once per machine** — clone, then run the bootstrapper. It checks your
Python, installs [pipx](https://pypa.github.io/pipx/) if missing, and
installs this checkout as an isolated pipx app so the `agent-harness-*`
commands work from any directory:

```powershell
.\bootstrap.ps1
```

Pass `-NoSemantic` to skip chromadb + onnxruntime (a few hundred MB);
everything still works, with deterministic context selection. Windows
PowerShell 5.1 is enough — `pwsh` is not required.

Bootstrap registers the selected agents at **user** scope, so they apply to
every project you open. The default configures Claude Code, Codex, and
Antigravity; pass `-Agents claude`, `-Agents codex`, or
`-Agents antigravity` when a machine hosts only one client. This keeps one
memory store and MCP server rather than three incompatible agent memories.
Restart installed agent CLIs afterwards so they pick up their configuration.

On macOS or Linux, do the same two steps by hand:

```bash
python -m pip install --user pipx && python -m pipx ensurepath
python -m pipx install --force "/path/to/agent-harness-kit[mcp,graph,semantic]"

# same user-scope registration bootstrap.ps1 performs, and equally idempotent
"$(python -m pipx environment --value PIPX_LOCAL_VENVS)/agent-harness-kit/bin/python" \
    -m harness.agent_setup --agents claude,codex,antigravity \
    --harness-root /path/to/agent-harness-kit
```

Add `--dry-run` to `agent-harness-setup` to see adapter changes first.

**Once per project** — one idempotent command, re-runnable any time:

```bash
agent-harness-enroll /path/to/your/project
```

That creates `.agent-harness/` (memory), builds the code graph if graphify
is available, and builds the semantic index. It **writes no instruction
files** — no AGENTS.md, no CLAUDE.md, no project-local settings. Memory is
set up first and independently, so a project without a graph still gets
working memory.

Memory **never enters git.** Enroll lists `.agent-harness/` in the
repository's private `.git/info/exclude` — not in `.gitignore`, which would
itself show up as a diff in other people's work. Re-running enroll adds the
exclusion to a project enrolled before this rule existed. If git already
tracks the directory, enroll prints the `git rm -r --cached .agent-harness`
command and leaves that history change to you (ADR 0004).

**Or never run it at all.** Set these two once and any project under your
workspace gets its memory created the first time you open a session in it:

```powershell
setx AGENT_HARNESS_AUTO_ENROLL 1
setx AGENT_HARNESS_WORKSPACE "C:\path\to\your\workspace"
```

Both are required. The flag is the intent; the workspace is the blast
radius. Without the workspace check, opening a repo you cloned to read for
five minutes would scaffold committed files into it.

If you want a project-local instruction block anyway, that is still its own
command:

```bash
agent-harness-install /path/to/your/project --hooks --mcp
agent-harness-memory init /path/to/your/project
```

The installer runs the preflight **doctor** first (which tells you exactly
what is missing) and installs only if nothing FAILs. `--agents
claude,codex,gemini` (default: all three) limits which pointer files get
written; see `agent-harness-install --help` for the full list.

### Agent setup: one client or all clients on one machine

`agent-harness-setup` is the once-per-machine adapter command. It never
creates project memory; it only configures the installed client to use the
same local server and memory contract. Re-run it after moving the harness or
reinstalling Python so adapter paths stay current.

```powershell
# Inspect all planned changes without modifying client configuration.
agent-harness-setup --dry-run

# Configure every supported client on a shared workstation.
agent-harness-setup --agents claude,codex,antigravity

# Configure only the client installed on a focused machine.
agent-harness-setup --agents codex
```

| Client | What setup installs | Retrieval policy |
|---|---|---|
| Claude Code | User-scope SessionStart memory hook, graph-read guard, and MCP server. | An adaptively-selected, bounded packet is injected once at session start -- semantic ranking only when it could change the packet, deterministic otherwise, always stated; MCP provides focused follow-up reads and proposed writes. |
| Codex | User-scope stdio MCP registration using `codex mcp add`. | MCP is on-demand. The managed `AGENTS.md` block asks for one `memory_context` call only for resumed or non-trivial work. |
| Antigravity | Global `agent-harness` plugin containing `mcp_config.json` and a `PreInvocation` hook. | The hook injects a packet only at `invocationNum == 0`; later invocations return no context. MCP handles focused reads and proposed writes. |

The Antigravity plugin is placed under
`~/.gemini/config/plugins/agent-harness/`, its documented global plugin
location. Its CLI need not be installed for the plugin files to be prepared;
after installing Antigravity, rerun setup and restart the client. Setup leaves
an existing Codex MCP server named `agent-harness` unchanged rather than
replacing a configuration it cannot prove it owns.

### Install from source

Prefer a git checkout (e.g. to track `main`, or to hack on the harness
itself)? Both setup scripts locate a Python 3 interpreter, run the preflight
**doctor**, and install only if nothing FAILs:

```bash
# bash / sh (macOS, Linux, Git Bash)
git clone https://github.com/montjeffrey/agent-harness.git
./agent-harness/setup.sh /path/to/your/project
```

```powershell
# Windows PowerShell / pwsh
git clone https://github.com/montjeffrey/agent-harness.git
.\agent-harness\setup.ps1 C:\path\to\your\project
```

Useful flags (both scripts): `--check` / `-Check` reports the environment and
installs nothing; `--no-hooks` / `-NoHooks` skips the Claude Code hook merge;
`--build-graph` / `-BuildGraph` builds the code graph immediately after
install if none exists; `--agents claude,codex` / `-Agents claude,codex`
limits the pointer files.

Prefer plain Python? The setup scripts are thin wrappers around:

```bash
python install.py /path/to/your/project --agents claude,codex,gemini --hooks
python install.py /path/to/your/project --check          # preflight report only
python harness/doctor.py /path/to/your/project            # same report, standalone
```

### What the doctor checks

| Check | Level if bad | Meaning |
|---|---|---|
| Python >= 3.11 | FAIL | harness code won't run |
| Harness files complete | FAIL | incomplete clone |
| Target `.claude/settings.json` valid JSON | FAIL | `--hooks` merge would corrupt it |
| Target dir writable / is a dir | FAIL | install can't proceed |
| Broken `key_command` in `~/.agent-harness/config.json` | FAIL | graph refresh would fail |
| graphify importable | WARN | queries work; refresh needs `pip install graphifyy` |
| LLM backend configured (`HARNESS_GRAPH_BACKEND`) | WARN | community names silently degrade without it |
| git on PATH | WARN | no graph staleness detection |
| Any agent CLI detected | WARN | harness installs, but nobody's reading it yet |
| Existing graph / git repo / AGENTS.md state | INFO | facts, never blocking |

`install.py` runs these automatically and refuses to install on any FAIL
(`--skip-checks` overrides). Exit codes: 0 = ok, 1 = failures found.

The install writes/updates, inside the target project:

- `docs/vault/blueprints/` and `docs/vault/templates/feature-blueprint.md`
- `docs/vault/README.md`
- `.git/info/exclude` (adds `graphify-out/` once, in a git repository;
  `.gitignore` is never touched)
- `AGENTS.md` (full harness instruction block, marker-delimited)
- `CLAUDE.md` / `GEMINI.md` (3-line pointer to the AGENTS.md block, if selected)
- `.claude/settings.json` (PreToolUse hook, only with `--hooks`)
- `.mcp.json` (agent-harness MCP server registration, only with `--mcp`)

Re-running `install.py` is idempotent: content outside the
`<!-- agent-harness:start -->` / `<!-- agent-harness:end -->` markers is never
touched, and unchanged content is left alone.

## Using the query CLI directly

```bash
agent-harness-query find <term>
agent-harness-query node <id>
agent-harness-query callers <id> [depth]
agent-harness-query callees <id> [depth]
agent-harness-query impact <id>
agent-harness-query file <path-fragment>
agent-harness-query community <name-fragment>
```

From a source checkout, the equivalent is:

```bash
python harness/graph_query.py find <term>
python harness/graph_query.py node <id>
python harness/graph_query.py callers <id> [depth]
python harness/graph_query.py callees <id> [depth]
python harness/graph_query.py impact <id>
python harness/graph_query.py file <path-fragment>
python harness/graph_query.py community <name-fragment>
python harness/graph_query.py search <text>            # requires the [semantic] extra
```

By default the query CLI walks up from the current working directory looking
for `graphify-out/graph.json`. Pass `--graph PATH` to point at a specific
file instead.

### Semantic search (`[semantic]` extra)

`search` adds a ChromaDB-backed semantic query over node text (label,
metadata summary/docstring, community name) on top of the exact/substring
matching the other subcommands do. It's an opt-in extra so the core harness
stays stdlib-only:

```bash
python -m pip install -e ".[semantic]"   # run from this checkout
python harness/graph_refresh.py <repo-root>       # builds/updates the semantic index too
python harness/graph_query.py search "sends a welcome email"
```

Notes:

- The index lives at `graphify-out/semantic-index/`, a sibling of
  `graph.json` inside the directory enroll and install exclude from git.
- `search` never builds the index implicitly. If it's missing, the error
  message points you at `graph_refresh.py`; a normal graph refresh keeps the
  semantic index in sync as a best-effort last step (it never fails the
  refresh if the index update itself fails).
- Without the extra installed, `search` fails fast with an actionable
  `python -m pip install -e ".[semantic]"` message instead of a traceback.
- Embedding uses Chroma's default local model (ONNX all-MiniLM-L6-v2) --
  fully local, no API key required. The first time it runs, Chroma downloads
  that model (~80MB) to a local cache; after that, embedding is offline.
- Output mirrors `find`: a count header plus one row per match, each row
  prefixed with a 2-decimal relevance score (higher is more relevant).

## Refreshing the graph

```bash
agent-harness-refresh <repo-root>          # fast: AST update + community re-naming
agent-harness-refresh <repo-root> --full   # full LLM re-documentation
```

From a source checkout, the equivalent is:

```bash
python harness/graph_refresh.py <repo-root>          # fast: AST update + community re-naming
python harness/graph_refresh.py <repo-root> --full   # full LLM re-documentation
```

For a new project, use the repairable one-command route instead of running
individual graphify steps yourself:

```bash
agent-harness-enroll /path/to/project
```

It initializes memory first, then attempts a local AST graph build, then
builds the local semantic index when available. A missing `graphifyy` or
unavailable semantic dependency makes the command return `1` with an
actionable diagnostic, but leaves memory usable. After installing the missing
dependency, rerun the exact same command; it is idempotent.

The default graph build does not call an LLM. Opt into Graphify's more
expensive semantic extraction only after explicitly configuring a backend:

```powershell
$env:HARNESS_GRAPH_BACKEND = "openai"
$env:OPENAI_API_KEY = "<read from your secret manager>"
agent-harness-enroll C:\path\to\project --semantic-graph
```

On macOS/Linux, export the same two variables in the shell before running the
enrollment command.

### Large-project operating mode

The graph is an infrastructure index, not text sent wholesale to an agent.
Agents query a bounded neighborhood (`find`, `node`, `impact`, or `file`), and
memory packets annotate only remembered symbols that resolve unambiguously.
This keeps prompt growth bounded as a repository grows.

For a very large repository, build the graph once outside an interactive
session, keep the startup packet at 1.5–2k characters, and turn off startup
graph annotation while retaining on-demand MCP queries:

```powershell
setx AGENT_HARNESS_HOOK_GRAPH 0
agent-harness-enroll C:\path\to\large-project
agent-harness-memory context --project C:\path\to\large-project --max-chars 2000
```

Measure graph-build duration, graph size, context length, and task completion
quality before increasing budgets or adding a hosted retrieval system. The
local semantic index ranks remembered facts; it is not a source-code dump or
a replacement for structural graph queries.

Backend resolution: `--backend` flag, then `HARNESS_GRAPH_BACKEND` env var,
then `"auto"`. API keys are read from the environment; if a backend's key
isn't set, `graph_refresh.py` will look for `~/.agent-harness/config.json`:

```json
{
  "key_command": "op read op://vault/nvidia/api_key",
  "key_env": "NVIDIA_API_KEY"
}
```

The command's stdout is sourced into the named env var for the duration of
the refresh. The key value is never printed or logged.

## MCP server (optional)

The same 7 query-CLI commands are also available as typed MCP tools over
stdio, so Claude Code (and, with manual setup, other MCP-capable clients)
can call them directly instead of shelling out.

```bash
pip install -e ".[mcp]"                       # installs the optional `mcp` SDK
python install.py /path/to/your/project --mcp # registers .mcp.json (Claude Code)
```

`--mcp` writes/merges a top-level `.mcp.json` in the target project (Claude
Code's project-scoped MCP server config -- **not** `.claude/settings.json`,
which stays hooks/permissions only) with an `"agent-harness"` entry that runs
`python -m harness.mcp_server`. It is idempotent (unchanged/created/updated,
same as `--hooks`) and refuses to overwrite an existing `"agent-harness"`
entry that doesn't match, or a `.mcp.json` that isn't valid JSON. It does
**not** set `enableAllProjectMcpServers` -- Claude Code will still prompt to
approve the project's MCP servers on first use.

For user-scope setup across Claude, Codex, and Antigravity, prefer
`agent-harness-setup`; it uses each client's configuration surface rather
than copying Claude's `.mcp.json` format into another client.

Run the server directly with the console script or as a module:

```bash
agent-harness-mcp [--graph PATH]
python -m harness.mcp_server [--graph PATH]
```

Without `--graph`, the server does the same repo-bound discovery as the CLI
(walks up from the working directory to `graphify-out/graph.json`, bounded
to the nearest `.git` repo root).

The 7 tools, each a thin wrapper over `harness/graph_query.py`'s CLI
commands and capped at the same `MAX_ROWS` the CLI already enforces:

| Tool | Maps to CLI command |
|---|---|
| `graph_find` | `find` |
| `graph_node` | `node` |
| `graph_callers` | `callers` |
| `graph_callees` | `callees` |
| `graph_impact` | `impact` |
| `graph_file` | `file` |
| `graph_community` | `community` |

**Codex / Antigravity**: use `agent-harness-setup`, not the project-local
`.mcp.json`. Codex is registered with its own `codex mcp add` command.
Antigravity receives a standards-shaped `mcp_config.json` inside its global
plugin together with the guarded context hook. Gemini CLI is not configured
by this release; use `GEMINI.md` / `AGENTS.md` as its portable fallback.

## The blueprint gate

This is an **advisory convention**, not a technical control -- nothing in
the harness prevents an agent from writing code before a blueprint is
approved; the gate only works if the agent chooses to honor it. Before
feature-scale work (3+ files, migrations, cross-module changes), an agent
should:

1. Copy `docs/vault/templates/feature-blueprint.md` into
   `docs/vault/blueprints/<date>-<slug>.md` and fill it out, using
   `graph_query.py impact <id>` to populate Impacted Files / Blast Radius.
2. Stop and wait. A human reviews the blueprint (in Obsidian, or any Markdown
   editor) and sets its `Status:` field to `Approved`.
3. The agent re-reads the blueprint before executing -- never trusts a stale
   in-context copy -- and checks off steps as they land.

## Per-agent support matrix

| Agent | Reads `AGENTS.md`? | Adapter | MCP tools |
|---|---|---|---|
| Claude Code | Yes | `CLAUDE.md` pointer plus SessionStart / PreToolUse hooks | Yes |
| Codex CLI | Yes | Native `AGENTS.md` plus on-demand MCP | Yes |
| Antigravity CLI | Yes through project rules/instructions | First-invocation plugin hook plus MCP | Yes |
| Gemini CLI | Varies by version | `GEMINI.md` pointer | Manual only |

`AGENTS.md` is written unconditionally -- it is the common-denominator file
most agent runtimes already read. `CLAUDE.md` and `GEMINI.md` only get the
short pointer so the harness content lives in one place.

## Requirements

- Python 3.11+
- Standard library only for `harness/` and `install.py`
- A `pipx install agent-harness` / `pip install agent-harness` install needs
  no local git clone at all -- the source-checkout path above is only for
  running from `main` or developing the harness itself
- `pytest` for running the test suite (`pip install pytest`, or
  `pip install -e ".[dev]"`)
- [graphify](https://pypi.org/project/graphify/) installed separately and
  available as `python -m graphify ...` if you want to build/refresh graphs
- `chromadb>=0.5` (`python -m pip install -e ".[semantic]"` from this checkout) only if you want
  the `search` subcommand -- everything else works without it

## Tests

```bash
python -m pytest tests/ -q
```

## Telemetry

An entirely local, opt-in-by-running-it usage log for Claude Code, Codex
CLI, and Gemini CLI / Antigravity session history -- nothing here runs
automatically (no hook, no background process); it only collects when you
invoke the CLI below, and it never opens a network socket. Events are
appended as JSONL to `~/.agent-harness/telemetry/events-YYYYMM.jsonl`
(month-sharded, never auto-deleted in this phase).

| Field | Type | Notes |
|---|---|---|
| `schema` | int | always `1` for this phase |
| `ts` | string | ISO 8601 UTC, `Z` suffix |
| `agent` | string | `claude-code` \| `codex` \| `gemini` |
| `event` | string | `session_start` \| `session_end` \| `usage` \| `rate_limit` \| `collector_error` |
| `project` | string \| null | optional |
| `session_id` | string \| null | optional |
| `input_tokens` / `output_tokens` | int | optional, omitted (not `0`) when unknown |
| `cost_usd` | float | optional, only when the source reports it directly -- no price-estimation table |
| `model` | string | optional |
| `limit_remaining` / `limit_kind` | int/float / string | optional pair (`requests`\|`tokens`\|`percent`); `limit_kind` required whenever `limit_remaining` is present |
| `source` | string | optional, local file path |

```bash
python harness/telemetry.py collect [--agent claude-code|codex|gemini]
python harness/telemetry.py tail [-n N] [--agent AGENT] [--project P]
python harness/telemetry.py aggregate [--by agent|project] [--since ISO8601]
```

Collectors are best-effort: a missing or unrecognized log format degrades
to zero events (or a single `collector_error` record) rather than crashing
`collect`. See `harness/collectors/*.py` for what each collector actually
reads on-disk (module docstrings document exact discovery findings).

## Memory

A portable, local-first project memory: short-term working context plus
durable knowledge, in ordinary Markdown and JSONL. Nothing here calls an AI
model or opens a network socket, and capture stays a decision rather than a
side effect -- every entry is something you, or an agent acting on the
protocol installed into your user-scope rules, chose to record. Curation is
the narrow automatic exception: a hook or context request can promote an
ingested document pointer only after its source file still matches the digest
recorded at ingestion; it cannot promote an agent conclusion.

Two things do run on their own once configured. `agent-harness-setup
--workspace <dir>` creates a memory store for any project under `<dir>` on
first sight, so a new repository is enrolled without being asked; and that
same setup installs the memory protocol into `~/.claude/CLAUDE.md`, which is
what tells an agent to retrieve before planning and to record what a later
session would otherwise rediscover. Without `--workspace` a project never
gets a store, the SessionStart hook stays silent, and every packet comes
back empty -- the usual reason this harness looks like it is doing nothing.
Everything an agent records is still `proposed` until a human accepts it.

```bash
agent-harness-setup --agents claude --workspace ~/code   # enable automation
agent-harness-memory init [PATH]
agent-harness-memory remember --type decision --summary "Chose X over Y."
agent-harness-memory status
agent-harness-memory curate                    # deterministic review pipeline

agent-harness-memory review list                  # what is awaiting review
agent-harness-memory review accept mem-2026-08-07-001
agent-harness-memory review resolve mem-2026-08-07-002 --commit a5f6f70
agent-harness-memory review supersede mem-2026-08-07-001 --summary "Chose Z."

agent-harness-memory context                      # budgeted packet, Markdown
agent-harness-memory context --json --query "rate limiting"
agent-harness-memory index                        # build the semantic index
agent-harness-memory compact                      # archive closed work
agent-harness-memory eval                         # score retrieval on a case set
agent-harness-memory eval --strategy deterministic --strategy semantic --save

# Source-checkout/module fallback
python -m harness.memory init
```

Project storage is created beside the code, on this machine only. It is kept
out of git through `.git/info/exclude` and never appears in anyone's status,
diff, or commit (ADR 0004):

```text
.agent-harness/
  scope.md                  purpose, boundaries, exclusions, acceptance criteria
  memory/active.md          bounded working context
  memory/durable.md         stable project knowledge
  memory/inbox.jsonl        proposed entries awaiting review
  memory/archive/           immutable weekly records
```

Machine-scope memory (`~/.agent-harness/memory/`) holds the project registry
and reusable cross-project knowledge. It is local and is never committed into
a project. The registry, not git, is what links projects to each other; a
second machine starts with blank memory for both scopes.

### Parallel task workspaces

For a user-authorized implementation task, start it before editing. This
records a unique chat/session owner, exact repository-relative path claims,
the immutable base SHA, a branch, and a private plan outside the repository.
It refuses active path overlap. When another task is active, or the primary
checkout is dirty, it creates an external worktree automatically; otherwise it
switches a clean primary checkout to the task branch.

```bash
agent-harness-work list
agent-harness-work start --summary "Add rate-limit headers" --session codex-<chat-id> \
  --path src/api/headers.py --path tests/test_headers.py
agent-harness-work diff work-YYYYMMDD-XXXXXXXX
agent-harness-work finish work-YYYYMMDD-XXXXXXXX --session codex-<chat-id>
```

Plans, task state, and external worktrees live under
`~/.agent-harness/work/<repository-id>/`; the project `.gitignore` is never
modified. Finishing releases path claims but deliberately does not rebase,
merge, delete branches, or delete worktrees. Use the matching MCP tools
(`work_start`, `work_status`, `work_diff`, `work_finish`) when the client has
the harness server registered.

| Entry field | Notes |
|---|---|
| `id` | `mem-YYYY-MM-DD-NNN`, unique per store |
| `type` | `decision` \| `discovery` \| `task` \| `blocker` \| `lesson` \| `relationship` |
| `status` | `proposed` \| `accepted` \| `rejected` \| `superseded` \| `resolved` |
| `confidence` | `confirmed` \| `inferred` |
| `source` | `{commit, file, project, manual}` |

Two trust rules are enforced in the store, so every writer inherits them: an
**inferred** entry can never be recorded as accepted (review is what promotes
it), and an **accepted** entry needs real evidence -- a commit, a file, or an
explicit `--manual` attestation. The project id attached to every entry is
attribution, not evidence, and does not satisfy that rule. Anything that
looks like a credential is rejected before it can reach an inbox.

Routine review is automated: SessionStart and `memory_context` run the same
deterministic curation pass. It accepts only unchanged document pointers with
a matching source digest and rejects exact duplicate proposals. Use
`agent-harness-memory curate` or MCP `memory_curate` to inspect the bounded
exception queue; that is the only memory review that needs human judgment.

### Approving work in-session

You do not have to open a file to approve a plan. An agent presents it in
chat; you agree (or object) in chat; the agent calls `memory_confirm`, which
records the plan as an accepted `decision` plus one tracked `task` per step
— and stores **your actual words** as `source.confirmation`. That text is
the audit trail: it is what makes the entry accepted, and what you can read
back later to disagree with.

```bash
agent-harness-memory review list         # what an agent proposed on its own
```

Tasks are closed with evidence (`memory_task_done`, or `review resolve`),
never silently. `memory_tasks` lists open work in priority order along with
**bottlenecks** — topics that work keeps piling up on, scored by how many
items are open, how many are blocking, and how long they have sat. Those
surface automatically in the context packet's *Priorities and bottlenecks*
section, so the next session opens already knowing what keeps stalling.

The honest caveat: an agent that called `memory_confirm` without you having
agreed would be recording an approval that never happened, and nothing can
detect that mechanically. Storing your words verbatim is what makes it
reviewable after the fact.

`review` remains the path from `proposed` to `accepted` for anything an
agent recorded on its own initiative via `memory_remember`. Accepting stamps
`confidence: confirmed` and, with no commit or file supplied, records
`source.manual` as your attestation. `supersede` never rewrites an accepted
entry: the original is kept with a `superseded_by` pointer and a replacement
is appended, so what was believed and when survives. Writes splice one line
and replace the file atomically, carrying any unparseable line across
untouched.

### Context packets and selection

`context` emits one budget-bounded packet -- the same contract for every
agent -- in Markdown or JSON:

| Section | Contents |
|---|---|
| `scope` | purpose, boundaries, exclusions, acceptance criteria |
| `active` | current phase, plus unresolved tasks and blockers |
| `project_durable` | live project-scope entries, relevance-ranked |
| `machine_durable` | live machine-scope entries |
| `relationships` | accepted cross-project links |
| warnings | staleness, unreviewed proposals, untouched scope |

Each section has its own character budget and the packet has a hard ceiling
(`--max-chars`, default 8000). Entries are included whole or omitted whole
with a reported count -- a record is never cut in half, because a truncated
record reads to a model as a complete one. `--query` sets what to select for
and defaults to the active objective in `scope.md`; `--id` pins specific
entries to the front of their section.

**Selection is ChromaDB-backed by default.** `--strategy auto` (the CLI
default) uses semantic ranking whenever `chromadb` is installed *and* an index
exists, and otherwise falls back to deterministic tag/keyword/recency ranking
with a note printed on the packet, so a fallback is never silent. Pass
`--strategy semantic` to make an unavailable index a hard error (exit 1)
rather than a fallback, and `--strategy deterministic` to force the baseline
-- that pairing is what makes the two comparable on your own models.

`--strategy adaptive` adds a cheap gate in front of `auto`, and is what the
SessionStart hook and the MCP server use. Before importing chromadb at all, it
asks whether semantic ranking could change *this* packet: it cannot when there
is no active objective to rank against (the semantic selector is defined to
return deterministic order without a query), and effectively cannot when the
store is small enough that every section already fits its budget. Either way
it declines, costs nothing, and says so on the packet. A project with a real
objective and a real store gets semantic ranking without opting in.
`AGENT_HARNESS_ADAPTIVE_MIN_ENTRIES` moves the size threshold;
`AGENT_HARNESS_HOOK_STRATEGY` overrides the hook's choice entirely.

```bash
python -m pip install -e ".[semantic]"   # run from this checkout
agent-harness-memory index
```

The index is built only by that explicit command, never by `context`:
Chroma's first embedding call downloads an ~80MB local model, and the agent
hot path must stay free of network access. (Running `index` first is what
makes that true in practice -- it warms the model cache, so the later
`context` query embedding is purely local.) It lives machine-side under
`~/.agent-harness/memory/index/<project-id>/` rather than in the repo, since
project memory is committed and a binary index should not be. Embedding is
local (all-MiniLM-L6-v2, ONNX) -- no API key, and summaries never leave the
machine. Without the extra, everything above still works deterministically.

### Blast radius: memory joined to the code graph

Memory answers *what we decided*. The code graph answers *what calls this*.
On their own those are two unrelated replies to the same question. `context`
joins them: an entry that names a symbol the graph knows gets that symbol's
blast radius attached to it.

```text
- `mem-2026-08-10-002` [decision] Context packets are assembled by build_packet;
  a new section has to be added to SECTION_ORDER as well.
    graph: `build_packet` -- 4 dependents in 3 files
    (harness/mcp_server.py, harness/memory.py, hooks/claude/memory_context_hook.py)
```

The count is the same transitive closure `graph_query.py impact` reports, from
the same traversal, so the packet and the query CLI can never disagree. Files
are listed nearest-first -- fewest hops to a dependent -- so the direct call
sites are the ones that make the line and the transitive tail collapses into
`+N more`.

Resolution is deliberately conservative, because a wrong blast radius is worse
than none:

- A name is looked up only if it is **symbol-shaped**: backticked,
  `snake_case`, `camelCase`, `call()`, or a filename. Ordinary prose is never
  matched, so an entry mentioning "the auth flow" does not light up every node
  labelled `auth`.
- It must resolve to **exactly one** node. An ambiguous label -- `page.tsx` is
  shared by dozens of nodes in any sizeable Next.js app -- is dropped, never
  guessed at.
- Backticks override the shape rule: writing `` `auth` `` is an explicit
  statement that you meant the symbol.

Annotation is on by default and degrades to silence: a project with no graph
gets exactly the packet it got before this existed, with no error and no note.
An unreadable graph, or one git reports as stale or dirty, says so as a packet
note rather than being quietly trusted. `--no-graph` turns the join off;
`--graph PATH` points at a specific `graph.json` instead of discovering one.
In the SessionStart hook the same join runs unless
`AGENT_HARNESS_HOOK_GRAPH=0`. **It is not free:** on a 6,300-node graph it
adds roughly a second to session start (~2.1s with it, ~1.0s without), almost
all of it importing the module and parsing `graph.json`. Annotation itself is
under a millisecond, because blast radii are memoized per symbol. Cost scales
with graph size, so a smaller project pays far less; there is no model, no
`chromadb`, and no network in that path. If you want the packet without the
wait, set the variable -- the CLI and MCP tools still do the join on demand.

The hook deliberately skips the git freshness check that the CLI and MCP
paths run. Those are two `git` calls, ~880ms on a large repo on network-backed
storage -- more than everything else in the hook combined -- and they buy
nothing here, because `graph_check.py` runs as a SessionStart hook alongside
this one and already reports a stale or dirty graph.

### Compaction: keeping retrieval fresh

Every other memory command adds. `remember` appends, `review` transitions a
status, `index` mirrors whatever is there -- so an inbox in long-term use grows
without bound, and a growing inbox degrades the very thing memory is for: the
selector ranks over more candidates, the context packet spends its budget on
settled work, and the semantic index carries entries nobody needs again.

```bash
agent-harness-memory compact
```

Compaction is the only operation that removes entries, so its rules are narrow:

- **Only closed work leaves.** Entries in a `rejected`, `superseded`, or
  `resolved` status, plus duplicate *proposals* (same type, scope, and summary
  after normalizing case, spacing, and punctuation). An unreviewed proposal is
  never discarded -- that decision belongs to a human. Accepted entries are
  never deduplicated either, because two similar ones may cite different
  commits, and collapsing them would destroy the audit trail.
- **Everything removed is archived first**, into
  `.agent-harness/memory/archive/YYYY-Www.md`. The file is appended to, never
  replaced, so a second run in the same ISO week cannot erase the first.
- **Promotion is a copy, not a move.** Accepted decisions, discoveries, and
  lessons are written into `durable.md` for human and Git review but *stay* in
  the inbox, because retrieval reads durable knowledge out of the inbox.
  Moving them would delete them from the context packet and the index.
  Promotion is idempotent by entry id.
- **`active.md` is rewritten** with open work and recent decisions, bounded by
  entry and character budgets. Omission is unit-atomic and counted, matching
  the context packet's own rule.
- **The inbox is spliced last** -- archive, then durable, then active, then
  inbox. The inbox is the store of record and everything else derives from it,
  so a crash partway through loses only derived work the next run redoes.
  Lines this schema version cannot parse are carried across untouched.

A project-scoped lockfile stops two compactions running at once; a stale or
corrupt lock is broken rather than wedging the command forever.

Because removals only reach ChromaDB when the index is rebuilt, compaction
tells you when to do it:

```text
entries were removed -- the semantic index still serves them.
run `agent-harness-memory index` to drop them from retrieval.
```

`compact` is **project scope only.** The machine inbox that
`remember --scope machine` feeds has no compaction pass yet, so that half of
the store still grows without bound. It needs its own rules rather than these
pointed at another directory -- machine memory has no repository to be
reviewed in and no per-project week to archive against.

Also not yet included, each tracked separately in `ROADMAP.md`: scope-drift
detection, cross-project relationship proposals, contradiction detection, and
the optional AI summarization pass. Task and blocker state needs no
reconciliation here -- `review` and the task tools already enforce those
transitions at write time.

Exit codes: `0` success, `1` nothing to do (e.g. `status` before `init`),
`2` input rejected.

## Security model

The only external command this toolkit ever executes on your behalf is the
`key_command` you write into your own `~/.agent-harness/config.json` -- it is
run as your user, its output is exported into the named env var for the
duration of a graph refresh, and it is never printed or logged. Nothing else
reads credentials, and no telemetry leaves your machine -- the telemetry
store above is written, read, and aggregated entirely locally.

The optional `[semantic]` extra follows the same rule: it uses ChromaDB's
default **local** embedding model (ONNX all-MiniLM-L6-v2), so node text
(labels, metadata summaries/docstrings, community names) is embedded and
indexed entirely on your machine -- no API key, no per-query network call,
nothing sent anywhere. The only network activity is a one-time ~80MB
download of the local model itself the first time Chroma needs it, cached
locally afterward. `harness/graph_index.py`, `harness/memory_index.py`, and
`harness/embedding.py` never import `chromadb` at module scope, so the core
package -- and every other subcommand -- keeps working with zero dependencies
even if the extra is never installed.

## License

[MIT](LICENSE)
