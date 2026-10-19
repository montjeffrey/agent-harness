# Does agent-harness reduce the tokens a coding agent spends?

*Benchmark run 2026-10-12.*

## Headline

**No meaningful difference.** Across 8 tasks × 3 repetitions per arm (48 runs) on a
10k-line Go repository, enabling agent-harness changed total tokens per task by
**−0.7%** (bootstrap 95% CI −17% to +23%) and **raised** cost per task by **+7.5%**
(95% CI −4% to +28%). Pass rates were identical: **24/24 with the harness, 24/24
without**.

The reason is visible in the tool logs: **in 24 of 24 harness-enabled runs the agent
never called a single agent-harness tool.** Every agent navigated with Grep, Read and
Bash, exactly as the control arm did. What the harness did contribute was a fixed
**+1,622 tokens of context at session start** (memory protocol in `~/.claude/CLAUDE.md`,
the SessionStart table of contents, and the deferred MCP tool names), which shows up as
**+16% cache-write tokens** (95% CI +6% to +38%, the only metric whose interval
excludes zero).

This data does **not** support a "reduces tokens by X%" claim on the GitHub profile.

## Method

### Arms

| | Arm A — harness | Arm B — control |
|---|---|---|
| Clone | fresh clone of the pinned commit, then `agent-harness-enroll` (memory + docs ingest + graphify code graph + semantic index; ~15 s, no LLM calls) | fresh clone of the pinned commit |
| Hooks | SessionStart `memory_context_hook.py`, PreToolUse(Read) `graph_read_guard.py` — the two hooks `bootstrap.ps1` installs in user settings | none |
| MCP | `agent-harness` server (`python -m harness.mcp_server`), 21 tools | none |
| Instructions | `~/.claude/CLAUDE.md` loaded — it holds the memory protocol `agent-setup` installs (plus a ~10-line git-attribution note) | `~/.claude/CLAUDE.md` excluded via `claudeMdExcludes` |

Common to both arms:

- `claude -p "<prompt>" --output-format json --model claude-sonnet-5-5`
  `--setting-sources project --strict-mcp-config --permission-mode bypassPermissions`
  `--max-budget-usd 8`. This suppresses the user's other plugins, hooks and MCP servers
  (ECC, Serena, context7 …) so the only difference between arms is the harness.
- Identical prompt text, model, effort (CLI default) and repository state.
- A fresh clone per run, outside `AGENT_HARNESS_WORKSPACE`, so auto-enroll cannot fire.
- Host-session environment variables inherited from the desktop app (tool-use
  summaries, attended-session flags, etc.) stripped from the child process.
- Arm A ran each harness process (hooks, MCP server, enroll) with a **per-run harness
  home** (`USERPROFILE` overridden for those processes only) so machine-level memory,
  the project registry and work markers could not leak between runs or into the operator's
  real `~/.agent-harness`. The ONNX embedding model cache was shared via a junction.
- Isolation was verified before the matrix with probe sessions: arm B reported no
  CLAUDE.md text, no hook output and no `mcp__agent-harness__*` tools; arm A reported
  all three, and a probe call to `memory_status` and `graph_find` succeeded.

### Schedule

3 repetitions × 8 tasks × 2 arms = 48 runs, two at a time. Task order was shuffled per
repetition and arm order shuffled per task (seed 20261012). A one-task pilot (R1, one
run per arm) preceded the matrix and is excluded from the statistics.

### Target repository

`github.com/montjeffrey/landfirst` at `4eb6172d08cb421e1f9d49d440b400ce89b82850`
(2026-10-11): Go 1.23, 43 `.go` files, 10,353 lines, 18 test files, 7 packages.
graphify built a 556-node / 1,483-edge graph for it.

For the two bug-fix tasks the bug was injected and folded into HEAD with
`git commit --amend` (author and committer preserved), so it is not visible as a diff
or a separate commit.

### Tasks

Every check was validated before any model ran (`run.py selftest`): each fails on the
untouched clone and passes on a hand-written reference solution. Hidden tests are
copied in only after the agent exits. All code tasks also require `go build ./...` and
`go vet ./...` to pass.

| ID | Category | Prompt (abridged — full text in `tasks.py`) | Pass check |
|---|---|---|---|
| B1-derive-bug | bug fix, covered by existing tests | Branches whose remote was deleted are labelled "safe to delete" even with unlanded commits; fix it. (Injected: Orphaned/SafeToDelete precedence swapped in `state.Derive`.) | state package tests + hidden precedence table |
| B2-severity-bug | bug fix, no test coverage | Detached worktrees are styled as routine cleanup instead of a warning; fix it. (Injected: rank 6 moved from Warn to Info in `Verdict.Severity`.) | hidden test of all 10 verdict severities |
| F1-nothing-to-merge | small feature | `-plain` report prints `MERGE ORDER` / `  nothing to merge` when units exist but the plan is empty; add a test. | hidden test (3 cases) + existing PlainReport tests |
| F2-only-flag | feature, CLI + package | Add `-only v1,v2` flag and `tui.FilterByVerdict(s, keep ...string)` (copy, no mutation, plan pruned). | flag + call present in `main.go`; hidden test of filter semantics |
| R1-rename-lines | refactor touching callers | Rename `git.Lines` → `git.NonEmptyLines` and update all callers (6 call sites, 5 files). | no `Lines(` call left; func defined; git tests + hidden test |
| R2-isstale-now | refactor, signature change | `IsStale(fetchHeadAt, now time.Time, threshold)`; update callers and tests. | state tests + hidden deterministic test |
| Q1-try-callers | codebase question (callers) | List every non-test function calling `git.Try`. | exact set match against a `go/ast` walk (20 functions, 19 distinct names) |
| Q2-noprobe-conflicts | codebase question (data flow) | With `-no-probe`, what is `PlanItem.Conflicts`, who sets it, what does `-plain` print? | `ANSWER:` line contains `-1`, `buildPlan`, `not probed` |

### Software versions

| Component | Version |
|---|---|
| Model | `claude-sonnet-5-5` (main loop); Claude Code also made small `claude-haiku-4-5-20251001` side calls in both arms ($0.05 total) |
| Claude Code | 2.1.287 |
| agent-harness | `agent-harness-kit` 0.1.0, pipx install; key files byte-identical to repo HEAD `8c82246` |
| graphify | graphifyy 0.9.58 |
| Go | 1.27.0 windows/amd64 |
| OS | Windows 11 Home 10.0.26300 |

### Metrics

From each run's `--output-format json` result: `modelUsage` summed across models gives
input, cache-write, cache-read and output tokens; `total_cost_usd` gives cost (Anthropic
**list price**; on a subscription this is quota consumption, not a charge); `num_turns`;
wall time measured by the runner. "Total tokens" = input + cache write + cache read +
output. Tool calls and tool-output sizes come from each session's JSONL log.

Overall effect = geometric mean over the 8 tasks of (median A / median B), with a
5,000-sample bootstrap that resamples runs within each task × arm. The median of the
per-task ratios and its range are reported alongside as the spread.

## Results

### Per task (medians of 3 runs; per-run values in parentheses)

| Task | Total tokens A | Total tokens B | A/B | Cost A | Cost B | A/B | Output A / B | Turns A / B | Wall s A / B | Pass A | Pass B |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B1-derive-bug | 130,071 (130k/130k/363k) | 151,039 (149k/151k/357k) | 0.86 | $0.082 | $0.087 | 0.94 | 1,069 / 1,226 | 6 / 7 | 279 / 300 | 3/3 | 3/3 |
| B2-severity-bug | 236,092 (173k/236k/349k) | 195,410 (158k/195k/219k) | 1.21 | $0.132 | $0.116 | 1.14 | 1,916 / 1,620 | 10 / 9 | 243 / 290 | 3/3 | 3/3 |
| F1-nothing-to-merge | 103,873 (104k/104k/104k) | 119,650 (97k/120k/124k) | 0.87 | $0.083 | $0.078 | 1.06 | 1,376 / 1,403 | 7 / 7 | 24 / 21 | 3/3 | 3/3 |
| F2-only-flag | 215,640 (213k/216k/219k) | 201,546 (178k/202k/326k) | 1.07 | $0.154 | $0.143 | 1.08 | 3,958 / 3,826 | 12 / 13 | 40 / 52 | 3/3 | 3/3 |
| Q1-try-callers | 81,032 (80k/81k/132k) | 98,859 (98k/99k/124k) | 0.82 | $0.087 | $0.089 | 0.98 | 1,201 / 1,209 | 3 / 4 | 13 / 15 | 3/3 | 3/3 |
| Q2-noprobe-conflicts | 75,088 (75k/75k/130k) | 70,404 (70k/70k/98k) | 1.07 | $0.063 | $0.057 | 1.11 | 671 / 737 | 4 / 4 | 11 / 10 | 3/3 | 3/3 |
| R1-rename-lines | 72,497 (72k/72k/73k) | 67,545 (67k/68k/68k) | 1.07 | $0.056 | $0.047 | 1.19 | 625 / 598 | 3 / 3 | 19 / 20 | 3/3 | 3/3 |
| R2-isstale-now | 126,752 (104k/127k/132k) | 121,208 (119k/121k/122k) | 1.05 | $0.093 | $0.082 | 1.13 | 2,108 / 2,081 | 9 / 9 | 24 / 21 | 3/3 | 3/3 |

Wall times for B1/B2 are dominated by the repository's own slow test packages
(`internal/engine` ~77 s, `internal/model` ~92 s), not by the model.

### Overall (harness ÷ control)

| Metric | Geometric mean of per-task ratios | Bootstrap 95% CI | Median per-task ratio | Per-task range |
|---|---|---|---|---|
| **Total tokens** | **0.993 (−0.7%)** | 0.83 – 1.23 | 1.056 | 0.82 – 1.21 |
| **Cost (list price)** | **1.075 (+7.5%)** | 0.96 – 1.28 | 1.092 | 0.94 – 1.19 |
| Output tokens | 1.000 (0.0%) | 0.85 – 1.19 | 1.003 | 0.87 – 1.18 |
| Cache-write tokens | 1.164 (+16.4%) | 1.06 – 1.38 | 1.165 | 1.02 – 1.29 |
| Cache-read tokens | 0.972 (−2.8%) | 0.81 – 1.22 | 1.050 | 0.76 – 1.22 |
| Turns | 0.949 (−5.1%) | 0.83 – 1.15 | 1.000 | 0.75 – 1.11 |
| Wall time | 0.953 (−4.7%) | 0.77 – 1.33 | 0.946 | 0.76 – 1.14 |

Pooled over all 24 runs per arm: 3.49 M vs 3.40 M total tokens (+2.7%) and
$2.415 vs $2.188 (+10.4%).

**Pass rate: 24/24 (A) vs 24/24 (B).** No task got worse with the harness, and none
got better.

### Mechanism

- **Harness tool calls: 0 of 24 arm-A runs** — not `memory_context`, not
  `graph_callers` (even for Q1, the callers question), not `graph_find`. The MCP tools
  are registered as *deferred* tools, so using one first costs a ToolSearch round trip,
  and the installed protocol asks for `memory_context` only on "feature-scale work, a
  bug investigation, or a resumed task". Sonnet 5.5 judged none of these tasks worth it.
- Tool calls summed over 24 runs per arm — A: Grep 47, Read 35, Edit 34, Bash 28,
  Write 5, Glob 3; B: Read 42, Bash 40, Grep 35, Edit 30, Write 3, Glob 1. Same toolset,
  same navigation style.
- **Fixed overhead:** context at the first model turn was 23,081 tokens (A) vs 21,459
  (B), median over 24 runs each, with a spread of about ±80 tokens. Those ~1,600 tokens are
  written to the prompt cache once (1-hour cache write, 2× the base input price) and
  re-read on every later turn. That is why cost moves up while total tokens stay flat:
  on short tasks (R1: 3 turns) the overhead is a visible +7% of tokens and +19% of cost;
  on longer ones it disappears into run-to-run noise.
- Run-to-run noise is large compared with any harness effect: one extra exploratory
  turn or test cycle swung individual runs by 50–230k tokens in both arms (B1 rep 3 used
  ~360k in both arms against ~140k typical).

### Telemetry cross-check

`agent-harness-telemetry collect --agent claude-code` followed by
`aggregate --by project --since 2026-10-12T13:00:00Z` (run against a separate telemetry
home so the benchmark did not touch the operator's real store) matched all 50 runs (48 plus
2 pilot) by working directory — and **disagreed with the result JSON**:

| | `claude -p` result JSON (Sonnet only) | telemetry aggregate | ratio | same logs, counted once per `message.id` |
|---|---|---|---|---|
| Input (incl. cache) | 6,904,690 | 12,244,313 | 1.77× | 6,904,690 (1.000×) |
| Output | 79,785 | 191,078 | 2.39× | 79,785 (1.000×) |

Per run, the telemetry over-count ranged from 1.00× to 2.65×. Cause, confirmed in the
logs: Claude Code writes one API response as several JSONL lines (for example a
`thinking` line, then a `tool_use` line), each repeating the full `usage` block with the
same `message.id`, and `harness/collectors/claude_code.py` counts every line. The
benchmark figures above come from the result JSON, which this de-duplicated recount
reproduces exactly. **Any token figure previously derived from `telemetry.py` for Claude
Code is inflated by roughly 1.5–2.5×.**

## Cost of running the benchmark

| Item | List-price cost |
|---|---|
| 48 matrix runs | $4.60 |
| 2 pilot runs | $0.10 |
| 6 wiring/isolation probe sessions | ≈ $0.35 |
| **Agent runs total** | **≈ $5.05** (approved budget $5–15, cap $25) |

The orchestrating Claude Code session that designed, ran and analysed the benchmark is
not included above; it cost considerably more than the runs themselves.

## Limitations

- **Small N.** Three runs per task per arm. Per-task medians move by 20–40% on one
  unusual run, so the confidence intervals are wide: the data rules out a large saving
  (more than about 17% on total tokens) but cannot distinguish a small one from a small cost.
- **One repository, and a small one.** At 10k lines, Grep finds anything in one or two
  calls, which is the regime where a code graph or memory store has the least to offer.
  A 100k+ line repository, or one with ambiguous names, could come out differently.
- **One model, one agent.** Sonnet 5.5 in Claude Code 2.1.287 only. A model that reaches
  for MCP tools more readily, or Codex/Gemini with the harness's other adapters, was not
  tested.
- **Cold memory.** Every arm-A run started from a freshly enrolled store: three
  doc-ingest entries and an empty work history. The harness's memory value accumulates
  across sessions on the same project. This benchmark measures the first session in a
  project, not the hundredth.
- **Fresh graph reported DIRTY.** Enroll excludes `.agent-harness/` from git but not
  `graphify-out/`, so a freshly built graph reads as dirty (`git status` shows
  `?? graphify-out/`), which disables graph evidence in memory packets. This was
  measured as shipped. It did not matter here, because no agent called a memory tool.
- **Budgets are characters, not tokens.** `memory_context` and the SessionStart hook
  budget per-section `max_chars` (the hook targets 1,500 chars; this run used 534). Its
  real token cost was only measured indirectly, as part of the +1,622-token difference.
- **Arm A carried a ~10-line git-attribution note** in `~/.claude/CLAUDE.md` that arm B
  did not, because the file was loaded or excluded as a whole. That inflates A's overhead by
  roughly 100–150 tokens, a few percent of the measured difference.
- **No model routing exists** in agent-harness, so none was tested. Nothing here should be
  read as a claim about routing easy work to a cheaper model.
- Tasks were written by the benchmark author, who had read the code; pass checks are
  objective, but task selection is not a random sample of real work.

## What would change the answer

1. Re-run on a substantially larger repository (and on the operator's real day-to-day
   projects) with **warm memory**: several prior sessions' accepted entries.
2. Make the tools cheaper to reach for: non-deferred registration of the 2–3 graph tools
   most useful for navigation, and a protocol line that names concrete triggers
   ("finding callers → `graph_callers`"). Then test whether usage, and savings, follow.
3. Fix the telemetry double count before using it for any public number.
4. Increase to 5+ repetitions if a claim of a few percent is ever wanted.

## Reproducing

The tooling is in [`benchmarks/token-efficiency/`](../../benchmarks/token-efficiency/README.md):
`tasks.py` (prompts, bug injections, hidden tests, checks), `run.py` (`selftest`,
`one`, `matrix`), `hookwrap.py` (per-run harness home), `probe_arm_a.py`, `analyze.py`,
`crosscheck.py`, and `gocallers/` (go/ast ground truth for Q1). The raw per-run records,
`analysis.json` and `crosscheck.json` behind this report are archived in
`benchmarks/token-efficiency/results/2026-10-12/`;
`python analyze.py --results results/2026-10-12/results.jsonl` reproduces every number
above. Each run's clone, settings, MCP config and Claude JSON remain under
`%LOCALAPPDATA%\Temp\ahbench\runs\` until that folder is cleaned up.
