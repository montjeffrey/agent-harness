<!-- agent-harness:start -->
## Agent Harness

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
