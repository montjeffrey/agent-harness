# agent-harness

A portable, stdlib-only toolkit that gives any coding agent -- Claude Code,
Codex CLI, Antigravity/Gemini CLI, or others that read `AGENTS.md` -- the same
four capabilities inside any project:

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
4. **Token-efficiency conventions** -- a PreToolUse hook (Claude Code) that
   blocks direct reads of `graph.json` and redirects to the query CLI, plus
   guidance baked into the instruction block.
5. **Self-healing graph checks** -- a SessionStart hook (Claude Code) that
   detects a missing or stale graph at the start of every session and hands
   the agent the exact build/refresh command.

This project only *generalizes* tooling -- it does not replace project-specific
CI, git hooks, or CRM/business logic. Install it into any repo with one
command; nothing here is specific to any one company, user, or codebase.

## Quickstart

One command, any shell. Both setup scripts locate a Python 3 interpreter,
run the preflight **doctor** (which tells you exactly what is missing), and
install only if nothing FAILs:

```bash
# bash / sh (macOS, Linux, Git Bash)
git clone <this-repo> agent-harness
./agent-harness/setup.sh /path/to/your/project
```

```powershell
# Windows PowerShell / pwsh
git clone <this-repo> agent-harness
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
| Python >= 3.10 | FAIL | harness code won't run |
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
- `.gitignore` (adds `graphify-out/` once)
- `AGENTS.md` (full harness instruction block, marker-delimited)
- `CLAUDE.md` / `GEMINI.md` (3-line pointer to the AGENTS.md block, if selected)
- `.claude/settings.json` (PreToolUse hook, only with `--hooks`)
- `.mcp.json` (agent-harness MCP server registration, only with `--mcp`)

Re-running `install.py` is idempotent: content outside the
`<!-- agent-harness:start -->` / `<!-- agent-harness:end -->` markers is never
touched, and unchanged content is left alone.

## Using the query CLI directly

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
pip install "agent-harness[semantic]"
python harness/graph_refresh.py <repo-root>       # builds/updates the semantic index too
python harness/graph_query.py search "sends a welcome email"
```

Notes:

- The index lives at `graphify-out/semantic-index/`, a sibling of
  `graph.json` inside the directory the harness already gitignores.
- `search` never builds the index implicitly. If it's missing, the error
  message points you at `graph_refresh.py`; a normal graph refresh keeps the
  semantic index in sync as a best-effort last step (it never fails the
  refresh if the index update itself fails).
- Without the extra installed, `search` fails fast with an actionable
  `pip install "agent-harness[semantic]"` message instead of a traceback.
- Embedding uses Chroma's default local model (ONNX all-MiniLM-L6-v2) --
  fully local, no API key required. The first time it runs, Chroma downloads
  that model (~80MB) to a local cache; after that, embedding is offline.
- Output mirrors `find`: a count header plus one row per match, each row
  prefixed with a 2-decimal relevance score (higher is more relevant).

## Refreshing the graph

```bash
python harness/graph_refresh.py <repo-root>          # fast: AST update + community re-naming
python harness/graph_refresh.py <repo-root> --full   # full LLM re-documentation
```

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

**Codex / Gemini**: documentation-only for now -- their MCP config formats
differ from Claude Code's `.mcp.json`, so `install.py --mcp` only writes
Claude Code's file. To wire the server into Codex or Gemini CLI manually,
point their MCP server config at `python -m harness.mcp_server` (with
`--graph PATH` if you want to skip auto-discovery) the same way you would
register any other stdio MCP server for that client -- consult each CLI's
own MCP documentation for its config file location and schema.

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

| Agent | Reads `AGENTS.md`? | Pointer file | PreToolUse hook support | MCP tools |
|---|---|---|---|---|
| Claude Code | Yes | `CLAUDE.md` (3-line pointer) | Yes -- `--hooks` installs the fragment | Yes -- `--mcp` registers `.mcp.json` |
| Codex CLI | Yes (native convention) | -- (uses `AGENTS.md` directly) | No hook mechanism generalized yet | Manual only (see MCP server section) |
| Antigravity / Gemini CLI | Varies by version | `GEMINI.md` (3-line pointer) | No hook mechanism generalized yet | Manual only (see MCP server section) |

`AGENTS.md` is written unconditionally -- it is the common-denominator file
most agent runtimes already read. `CLAUDE.md` and `GEMINI.md` only get the
short pointer so the harness content lives in one place.

## Requirements

- Python 3.10+
- Standard library only for `harness/` and `install.py`
- `pytest` for running the test suite (`pip install pytest`, or
  `pip install -e ".[dev]"`)
- [graphify](https://pypi.org/project/graphify/) installed separately and
  available as `python -m graphify ...` if you want to build/refresh graphs
- `chromadb>=0.5` (`pip install "agent-harness[semantic]"`) only if you want
  the `search` subcommand -- everything else works without it

## Tests

```bash
python -m pytest tests/ -q
```

## Security model

The only external command this toolkit ever executes on your behalf is the
`key_command` you write into your own `~/.agent-harness/config.json` -- it is
run as your user, its output is exported into the named env var for the
duration of a graph refresh, and it is never printed or logged. Nothing else
reads credentials, and no telemetry leaves your machine.

The optional `[semantic]` extra follows the same rule: it uses ChromaDB's
default **local** embedding model (ONNX all-MiniLM-L6-v2), so node text
(labels, metadata summaries/docstrings, community names) is embedded and
indexed entirely on your machine -- no API key, no per-query network call,
nothing sent anywhere. The only network activity is a one-time ~80MB
download of the local model itself the first time Chroma needs it, cached
locally afterward. `harness/graph_index.py` never imports `chromadb` at
module scope, so the core package -- and every other subcommand -- keeps
working with zero dependencies even if the extra is never installed.

## License

[MIT](LICENSE)
