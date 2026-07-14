# agent-harness

A portable, stdlib-only toolkit that gives any coding agent -- Claude Code,
Codex CLI, Antigravity/Gemini CLI, or others that read `AGENTS.md` -- the same
four capabilities inside any project:

1. **Queryable code relations** -- search, inspect, and traverse a
   [graphify](https://pypi.org/project/graphify/) `graph.json` without ever
   reading the whole file into context.
2. **A human-in-the-loop blueprint gate** -- feature-scale changes get a
   Markdown blueprint (viewable natively in Obsidian if the project root is a
   vault) that a human must approve before an agent writes code.
3. **Agent catch-up** -- a compact, marker-delimited instruction block dropped
   into `AGENTS.md` / `CLAUDE.md` / `GEMINI.md` so any agent picks up the
   conventions on its first read of the project.
4. **Token-efficiency conventions** -- a PreToolUse hook (Claude Code) that
   blocks direct reads of `graph.json` and redirects to the query CLI, plus
   guidance baked into the instruction block.

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
`--agents claude,codex` / `-Agents claude,codex` limits the pointer files.

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
```

By default the query CLI walks up from the current working directory looking
for `graphify-out/graph.json`. Pass `--graph PATH` to point at a specific
file instead.

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

## The blueprint gate

Before feature-scale work (3+ files, migrations, cross-module changes), an
agent should:

1. Copy `docs/vault/templates/feature-blueprint.md` into
   `docs/vault/blueprints/<date>-<slug>.md` and fill it out, using
   `graph_query.py impact <id>` to populate Impacted Files / Blast Radius.
2. Stop and wait. A human reviews the blueprint (in Obsidian, or any Markdown
   editor) and sets `Status: Approved` (or ticks the Approved checkbox).
3. The agent re-reads the blueprint before executing -- never trusts a stale
   in-context copy -- and checks off steps as they land.

## Per-agent support matrix

| Agent | Reads `AGENTS.md`? | Pointer file | PreToolUse hook support |
|---|---|---|---|
| Claude Code | Yes | `CLAUDE.md` (3-line pointer) | Yes -- `--hooks` installs the fragment |
| Codex CLI | Yes (native convention) | -- (uses `AGENTS.md` directly) | No hook mechanism generalized yet |
| Antigravity / Gemini CLI | Varies by version | `GEMINI.md` (3-line pointer) | No hook mechanism generalized yet |

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

## Tests

```bash
python -m pytest tests/ -q
```

## License

Choose and add a LICENSE file appropriate for your use before publishing.
