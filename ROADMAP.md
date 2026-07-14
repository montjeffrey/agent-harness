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
