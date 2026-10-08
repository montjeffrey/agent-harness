<!-- agent-harness:start -->
## Agent Harness

### Memory protocol

For feature-scale work, bug investigation, or a resumed task, retrieve one
bounded project packet before planning: use the `memory_context` MCP tool when
available, otherwise run `agent-harness-memory context --max-chars 2000`.
Do not retrieve memory for a trivial one-shot question. Record a concise
decision, discovery, blocker, or lesson with `memory_remember` only when it
will help a later session; those entries are proposed until a human reviews
them, except for document pointers automatically verified by source digest
during context retrieval. Never record secrets, prompts, or source dumps. Do
not promote your own conclusions.

Before non-trivial implementation that the user has authorized, call
`work_status` then `work_start` with the session id printed at session start
and exact repository-relative paths. Work only in the returned branch/worktree.
In Claude Code a claim is enforced: editing a path claimed by another active
session is refused until that task finishes; your own claims never block you.
`work_start` also returns a task-scoped memory packet. `work_finish` records
the branch, base, and final commit and releases the claim only. Commits and
finished tasks are proposed to memory automatically at the end of each turn.
Never rebase, merge, or remove another task's worktree.

Code relations (never Read graphify-out/graph.json directly -- several MB,
wastes a full context window):
```
{QUERY_CMD} find <term>
{QUERY_CMD} node <id>
{QUERY_CMD} callers <id> [depth]
{QUERY_CMD} callees <id> [depth]
{QUERY_CMD} impact <id>
{QUERY_CMD} file <path-fragment>
{QUERY_CMD} community <name-fragment>
```

Refresh the graph after merges:
`{REFRESH_CMD} <repo-root>`

Check staleness by comparing graph.json's `built_at_commit` to `git rev-parse HEAD`.

Feature-scale work (3+ files, migrations, cross-module changes) gets a
blueprint before code changes: copy `docs/vault/templates/feature-blueprint.md`
into `docs/vault/blueprints/<date>-<slug>.md`, fill it out, then STOP and wait
for a human to set the blueprint's `Status:` field to `Approved`. This is an
advisory convention, not a technical control -- nothing in the harness
prevents an agent from writing code before that happens; the gate only works
if the agent chooses to honor it. Re-read the blueprint immediately before
executing -- do not trust a stale in-context copy.
<!-- agent-harness:end -->
