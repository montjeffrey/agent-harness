<!-- agent-harness:start -->
## Agent Harness

Code relations (never Read graphify-out/graph.json directly -- several MB,
wastes a full context window):
```
python {HARNESS_PATH}/harness/graph_query.py find <term>
python {HARNESS_PATH}/harness/graph_query.py node <id>
python {HARNESS_PATH}/harness/graph_query.py callers <id> [depth]
python {HARNESS_PATH}/harness/graph_query.py callees <id> [depth]
python {HARNESS_PATH}/harness/graph_query.py impact <id>
python {HARNESS_PATH}/harness/graph_query.py file <path-fragment>
python {HARNESS_PATH}/harness/graph_query.py community <name-fragment>
```

Refresh the graph after merges:
`python {HARNESS_PATH}/harness/graph_refresh.py <repo-root>`

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
