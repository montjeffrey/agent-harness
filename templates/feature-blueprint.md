# FEATURE: {{Feature Name}}

- **Status:** Pending Human Review  <!-- Human changes to: Approved | Rejected | Needs Changes -->
- [ ] **Approved** <!-- Human ticks this box to release the agent -->
- **Date:** {{YYYY-MM-DD}}
- **Author agent/session:** {{session or agent id}}
- **Branch:** {{branch the work will land on}}

## Impacted Files
<!-- Mapped from graphify-out/graph.json (harness/graph_query.py) and any symbol-reference
     tooling available. List every file the change touches, with the relation that pulls it in. -->
- [ ] `path/to/file.ext` -- {{why it is impacted}}

## Blast Radius
<!-- Modules/communities from the code graph that depend on the impacted files.
     Anything here that is NOT edited must be verified unaffected (tests named below). -->
- {{community/module}} -> {{dependency path}}

## Proposed Changes
1. [ ] **Step 1:** {{architectural change, with the file(s) it lands in}}
2. [ ] **Step 2:** {{dependency impacts and how they are handled}}
3. [ ] **Step 3:** {{tests: which suites prove the blast radius is safe (unit tests / DB tests / E2E tests)}}

## Open Questions for Human
- {{anything the agent could not decide from code or memory}}

## Execution Log
<!-- Agent appends here as steps complete: timestamp, commit hash, test evidence. -->
