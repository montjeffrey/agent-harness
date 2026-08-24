# ADR 0001: Local-first memory and AST graph by default

Date: 2026-08-22

## Context

The harness must work across Claude Code, Codex, and Antigravity without
repeating large project context or requiring a hosted memory service. Graphify
supports both local AST extraction and LLM semantic extraction. The latter
requires a configured provider and may incur API cost.

## Decision

- Markdown/JSONL project memory is the durable source of truth.
- The shared MCP server is the typed retrieval/capture interface.
- Claude uses its SessionStart hook; Codex uses on-demand MCP plus `AGENTS.md`;
  Antigravity uses a plugin that injects context only at invocation zero.
- `agent-harness-enroll` builds Graphify's local AST graph by default.
- LLM semantic graph extraction is explicit: `--semantic-graph` plus a
  configured backend and key.

## Consequences

- A new project gets structural code retrieval without an API key or model
  cost.
- Community labels may remain local placeholders until a backend is configured.
- Memory packets stay character-bounded; code is queried by relation rather
  than copied wholesale into prompts.
- A missing client or optional dependency degrades with a diagnostic instead
  of disabling project memory.
