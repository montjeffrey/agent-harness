<!-- agent-harness:start -->
## Agent Harness memory

A local, per-project memory store is available through the `agent-harness` MCP
tools. Nothing leaves the machine and no model is called on your behalf.

**It is already in your context.** The SessionStart hook injects this project's
memory, and hooks record commits and finished tasks for you. Do not call
`memory_context` just to start a task.

**Search only to save reading.** Call `memory_search` for a specific question the
injected context does not answer, when it would replace reading several files.

**Record only what hooks cannot see:** the reason behind a decision, a
non-obvious discovery, or a blocker, via `memory_remember` in one sentence. Do
not record what the repository already states, and never record secrets,
credentials, tokens, or prompts. Recorded entries stay `proposed` until a human
accepts them; treat `(proposed)` entries as claims to verify.

**Parallel edits:** before an authorized non-trivial change, `work_start` claims
its paths (pass the `_Session ..._` id and one path per line); `work_finish`
releases them. Not for read-only work.
<!-- agent-harness:end -->
