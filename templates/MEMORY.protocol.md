<!-- agent-harness:start -->
## Agent Harness memory protocol

The `agent-harness` MCP tools are registered for every project on this machine.
They read and write a local, per-project memory store; nothing leaves the
machine and no model is called on your behalf.

**Retrieve before planning.** For feature-scale work, a bug investigation, or a
resumed task, call `memory_context` once before planning. Skip it for a trivial
one-shot question -- the packet spends context, and a one-liner does not earn
it. One call is enough: the packet is already budgeted and ranked.

**Record what a later session would otherwise have to rediscover.** Call
`memory_remember` for a decision and its reason, a non-obvious discovery, a
blocker, or a lesson learned the hard way. Prefer recording at the moment the
thing is settled, not in a batch at the end.

**Do not record what the repository already states.** Code structure, git
history, file contents, and anything a README or ADR already says are all
retrievable without memory. An entry earns its place only by holding something
the repository does not.

**Entries are proposals, not truth.** Everything recorded lands as `proposed`
and stays that way until a human accepts it with `memory review accept`. Never
promote your own proposals. Treat any `(proposed)` entry in a packet as a claim
to verify, not a fact to build on.

**Never record** secrets, credentials, tokens, prompts, or source dumps.

**Coordinate before implementation.** After the user has authorized a
non-trivial code change, call `work_status` and then `work_start` before
editing. Give `work_start` this chat's unique session id and one exact
repository-relative path per line. It refuses files already claimed by an
active chat, records the immutable diff base, and returns the branch/worktree
to use. Call `work_diff` before handoff and `work_finish` only when the task
is complete. Do not use these tools for read-only investigation, and never
rebase, merge, or delete a worktree on another task's behalf.

If a project has no memory store yet, `memory_context` returns an empty packet
with a warning rather than an error -- that is normal, not a failure.
<!-- agent-harness:end -->
