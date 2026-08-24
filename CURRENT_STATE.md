# Current State

## Now

- Local task-workspace coordination is implemented. `agent-harness-work start`
  records a session-level task, private plan, immutable base SHA, branch, and
  exact claimed paths under `~/.agent-harness/work/`; it never modifies a
  project's `.gitignore`. Overlapping active paths are refused even when both
  sessions use Codex. A clean idle primary checkout uses the task branch;
  parallel work or a dirty checkout receives an external worktree. MCP exposes
  the same flow as `work_start`, `work_status`, `work_diff`, and `work_finish`.
  Completion only releases claims: rebase, merge, and worktree cleanup remain
  explicit integration actions.
- Local-first memory/graph lifecycle is implemented and installed on the
  primary development machine.
- `agent-harness-setup` configures Claude Code, Codex, and the Antigravity
  plugin from one command; use `--agents <name>` for a single-client machine.
- The current repository has a verified local AST graph. Full semantic graph
  extraction remains opt-in and requires an explicit backend.
- Semantic memory selection works on this Python 3.14 Windows machine. The
  earlier "imports and builds but cannot reopen its persistent store" entry
  was a misdiagnosis and is withdrawn: the persistent store was readable the
  whole time, on both the rebuilt index and the backed-up one.
- Root cause: Chroma 1.5.9 refuses a query-time `embedding_function` whose
  name differs from the persisted one ("Embedding function conflict: new:
  onnx_mini_lm_l6_v2 vs persisted: default"). `_existing_collection` reported
  that as "no collection named ...", which reads as a missing store and
  invites a rebuild that can never fix it. `harness.embedding` (b532ad7)
  already avoids the conflict by passing a precomputed vector; the
  collection-open helpers in `memory_index` and `graph_index` now also carry
  the underlying reason in the exception message, so this cannot be
  misread again.
- The pipx-installed wheel had drifted behind the checkout and still ran the
  pre-fix code, which is why the failure appeared to survive a rebuild.
  Reinstalling from this working tree is what made the fix live; a
  source-vs-install diff is the check worth running after any bootstrap.
- The automation chain is closed end to end. It was broken in two places: no
  project was ever enrolled (both `AGENT_HARNESS_AUTO_ENROLL` and
  `AGENT_HARNESS_WORKSPACE` were unset, so the hook stayed silent and every
  packet came back empty), and nothing ever wrote memory (both shipped hooks
  are read-only; the capture protocol existed in `templates/AGENTS.harness.md`
  but was only ever installed per project, never at user scope).
- `agent-harness-setup --workspace <dir>` now enables auto-enrollment for
  projects under `<dir>` and installs a new user-scope memory protocol
  (`templates/MEMORY.protocol.md`) into `~/.claude/CLAUDE.md`. That template
  deliberately carries no graph or blueprint instructions, so it is true in
  any repository; the per-project block keeps those, because they only make
  sense where a graph and a vault exist.
- Division of labour, verified: hooks read and enroll, rules decide what is
  worth recording, MCP performs the reads and the proposed writes. No hook
  writes an entry, and everything an agent records is still `proposed`.
- Configured on this machine for the `Coding` workspace root. Verified: a
  fresh repository under it auto-creates `.agent-harness/`; a repository
  outside it is correctly refused; an entry recorded through MCP lands as
  `proposed` and appears in the next session's packet.
- Selection is now a two-strategy system that decides per call. `adaptive`
  (the SessionStart hook's default, and the MCP server's) asks in arithmetic
  alone whether semantic ranking could change *this* packet -- there must be
  an active objective to rank against, and enough entries that a section has
  to drop some -- and only then behaves like `auto`. The gate runs before any
  chromadb import, so a declined call costs nothing, and every decline is
  reported as a packet note. `cross_check` still forces `auto`, because a
  comparison against semantic ranking must not be handed a strategy that can
  decline. `AGENT_HARNESS_ADAPTIVE_MIN_ENTRIES` moves the size threshold.
- Verification: full test suite `722 passed, 3 skipped`. Both branches were
  verified end to end through the deployed hook: this repository (no
  objective, one entry) selects deterministic and says why; a ten-entry
  project whose objective is "Rewrite the authentication layer" selects
  semantic and ranks "Rewrote the login flow to issue short-lived tokens"
  first.
- `tests/test_packaging.py::test_installed_wheel_console_scripts_run_outside_source_tree`
  is intermittently blocked by Windows Application Control, which rejects a
  freshly built, never-before-seen console-script executable with
  `OSError: [WinError 4551]`. It failed four consecutive runs mid-session and
  passes now. It is an OS policy result rather than a packaging defect: a
  one-line no-op comment on an otherwise clean tree reproduced it exactly, and
  reverting that comment cleared it. Expect it to reappear after source
  changes, and expect the same policy to affect real release builds here.

## Next

- Install Antigravity CLI on a machine that will use it, then perform its
  runtime MCP/hook smoke test.
- Fill this repository's `.agent-harness/scope.md` and review the existing
  proposed memory entry before compaction.
- Establish a reproducible large-repository benchmark: graph build duration,
  graph size, packet length, task completion rate, retries, and cost per
  successful task.
- Populate `.agent-harness/work/eval/default.jsonl` so `agent-harness-memory
  eval` scores semantic against deterministic on real cases; the store holds
  one entry today, which is too few for the comparison to mean anything.
- Decide how to handle Windows Application Control for the packaging test:
  either sign the built console scripts, or exempt the build directory, or
  mark the test as expected-to-fail on this machine with the reason recorded.
  Leaving it red hides real packaging regressions behind a known-noisy test.
- Fill `.agent-harness/scope.md`'s active objective for this repository. Until
  it is set, `adaptive` correctly declines semantic ranking here, so this
  project exercises only the cheap half of its own selection system.

## Later

- Add explicit export/import of accepted machine-scope durable memory for
  cross-machine synchronization. Do not synchronize live inbox/registry files
  through OneDrive.
- Consider optional semantic graph extraction only after the benchmark shows
  the local AST graph lacks needed recall.
