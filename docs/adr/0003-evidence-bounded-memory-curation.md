# ADR 0003: Evidence-bounded automatic memory curation

Date: 2026-08-24

## Context

Manual review of every proposed memory record does not scale. Treating every
agent-written observation as automatically true would scale only by corrupting
the durable knowledge base. The harness already records documentation pointers
with a repository-relative file path and content digest, which can be verified
without a model or external service.

## Decision

- `memory_curation` runs before Claude SessionStart context and MCP
  `memory_context` retrieval, and is available explicitly as
  `agent-harness-memory curate` / `memory_curate`.
- It accepts only `memory_ingest` document pointers whose current normalized
  file content matches the recorded digest.
- It rejects only exact duplicate proposals, retaining the earliest record and
  preserving the duplicate's audit note.
- All other agent conclusions remain `proposed` and appear in a bounded
  exception queue with a reason.
- Curation does not compact, rebuild semantic indexes, summarize with a model,
  rebase, or alter human-approved records.

## Consequences

- Routine document maps become durable automatically and are available on the
  first context read after ingestion.
- Human review is reserved for semantic judgments rather than provenance
  checks and duplicate cleanup.
- A changed or missing source document stops automatic promotion instead of
  creating a stale durable assertion.
