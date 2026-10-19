# Does the harness pay off on the work that is actually expensive?

*Harness-value benchmark, run 2026-10-15/16. Internal report; not published.
Method: [benchmarks/harness-value/DESIGN.md](../../benchmarks/harness-value/DESIGN.md).*

## Headline

**No.** On five task families aimed at where the [2026-10-14 usage
baseline](2026-10-14-usage-baseline.md) says tokens go, agent-harness as installed is
**token-neutral** (A/B overall 1.09, 95% CI 0.83–1.41), and the harness made "easy to use" (tools
loaded up front plus a workflow prompt) **costs about 30% more** (A+/B 1.30, CI 1.06–1.61) and lowered
the pass rate on two families. No family showed a harness win.

The mechanism is one finding, repeated in every family: **agents never write to
memory and never use the graph.** In 261 scored runs there was not a single
`memory_remember`, `work_*` or `graph_*` call — not even in A+, where the tools were
loaded eagerly and the system prompt told the agent to record decisions and to use
`graph_callers` before grep. A+ agents did *read*: 45 `memory_context` lookups (all
empty, because nothing had been written) and 43 `memory_search` calls in the recall
family, which found the right ADRs and still cost 31% more than grep.

## Results

Total-token ratio, geometric mean of per-task medians, 95% bootstrap CI (5,000
resamples). Verdict bands: CI ≤ 0.90 harness wins, ≥ 1.10 harness costs, otherwise
no clear effect; a family whose pass rate is below the control's is invalid.

| Family | What it tests | A/B (as installed) | A+/B (eager + told to use it) | Pass A / A+ / B |
|---|---|---|---|---|
| H | two 6-step feature chains, a fresh session per step | 1.01 (0.87–1.21) no clear effect | 1.20 (0.98–1.42) no clear effect | 60/60 · 60/60 · 60/60 |
| R | resume after a break (session 2 scored) | 1.19 (0.47–2.16) **invalid** | 1.34 (0.76–2.43) **invalid** | 3/5 · 3/5 · 5/5 |
| D | recall decisions from a 30-ADR history | 1.09 (1.05–1.29) no clear effect | **1.31 (1.28–1.35) harness costs** | 10/10 · 10/10 · 10/10 |
| G | callers / change impact on Hugo (104k lines) | 1.08 (0.97–1.37) no clear effect | 1.29 (1.12–1.47) **invalid** | 20/20 · 19/20 · 20/20 |
| C | the 8 tasks of 2026-10-12 (control) | 1.07 (0.95–1.21) no clear effect | **1.36 (1.23–1.60) harness costs** | 40/40 · 40/40 · 39/40 |
| **Overall** | equal weight per family | **1.09 (0.83–1.41)** | **1.30 (1.06–1.61)** | |

Median per run (B = no harness):

| Family | B tokens | A tokens | A+ tokens | B cost | A cost | A+ cost |
|---|---|---|---|---|---|---|
| H (whole chain) | 1.30 M | 1.37 M | 1.53 M | $0.64 | $0.70 | $0.73 |
| R (session 2) | 390 k | 464 k | 522 k | $0.18 | $0.19 | $0.18 |
| D | 81 k | 90 k | 108 k | $0.08 | $0.09 | $0.11 |
| G | 72 k | 78 k | 93 k | $0.05 | $0.06 | $0.06 |
| C | 127 k | 134 k | 162 k | $0.07 | $0.08 | $0.08 |

### Harness tool calls (all scored runs)

| Setup | memory_context | memory_search | memory_remember | graph_* | work_* |
|---|---|---|---|---|---|
| A (as installed, deferred tools) | 0 | 0 | 0 | 0 | 0 |
| A+ (eager tools + workflow prompt) | 45 | 43 (all in D) | **0** | **0** | 0 |

In A the agent loaded deferred harness tools with ToolSearch twice (both in R) and
then did not call them.

### Session strategy: one long conversation vs a fresh session per step (H only)

| Setup | Tokens | Cost | Cache writes | Context per model call |
|---|---|---|---|---|
| M — one resumed conversation | 1.61 M | **$0.51** | 52 k | 48 k |
| B — fresh session per step, no harness | 1.30 M | $0.64 | 95 k | 30 k |
| A+ — fresh session per step, harness | 1.53 M | $0.73 | 106 k | 37 k |

B/M: tokens 0.83 (0.64–1.00), cost **1.27**. Fresh sessions read less context per
call, but each one pays the cache write for its whole starting context again, so at
this size the long conversation is cheaper. The chains only reach ~60 k context per
call by step 6 — a quarter to a tenth of the 250–465 k per call of the operator's real
marathon sessions, so this benchmark does not reach the regime where hand-off can
pay.

## What this says to do next

1. **Make memory capture and retrieval automatic (highest leverage).** Agents do not
   call `memory_remember` on their own, with or without instructions, so the
   hand-off loop never closes. Write the hand-off entry from a session-end hook
   (changed files, final message, decisions found deterministically in the
   transcript — no model call) and inject it from the SessionStart hook instead of
   asking the agent to call `memory_context`.
   *Gate:* H or R with the hooks-only setup, total-token CI ≤ 0.90 vs B, pass rate
   not lower. (Proposed as its own task; in progress.)
2. **Stop paying for unused tools.** Eager loading cost +20–34% tokens for nothing;
   keep the tools deferred, remove the "call memory_context" instruction from the
   installed protocol (it only produces empty lookups), and cut the default surface
   from 21 tools to the 2–3 that would be used.
   *Gate:* A's context per call within ~1.6 k tokens of B's.
3. **Aim hand-off at real marathons, not at short chains.** Below ~60 k context a
   single conversation is cheaper than fresh sessions. Hand off only above a context
   threshold (start at the baseline's 150 k), and add a long-chain family (12–15
   steps, 200 k+ per call) — the only place hand-off can win.
   *Gate:* on that family, hand-off at 150 k beats M on cost.
4. **Pause investment in graph tools.** On 104 k lines, grep answered every caller and
   impact question in 2–5 model calls (B 20/20). Test the graph only where grep fails
   (interface implementations, multi-hop impact) or drop it from the default surface.
5. **Treat retrieval as overhead until it replaces work.** In D, 43 `memory_search`
   calls found the right ADRs and still cost 31% more than grep: retrieval that adds a
   step instead of removing several does not pay. A retrieval feature is worth
   keeping only when it removes reads.
6. **Use this benchmark as the gate.** A full rerun costs about $47 at list
   price and runs unattended (`python run.py matrix`); a harness change ships when its
   family's CI moves below 0.90 without a pass-rate drop.

**Stop doing:** adding protocol text that asks agents to use the harness; loading tools
eagerly; counting recorded memory entries as value.

## Method summary

- Model `claude-sonnet-5-5`, `claude -p --setting-sources project --strict-mcp-config
  --permission-mode bypassPermissions`, host-session env stripped. Setups:
  B no harness (`~/.claude/CLAUDE.md` excluded); A enroll + hooks + MCP (deferred)
  + installed protocol, per-run harness home; A+ as A plus `alwaysLoad` on the MCP
  server and an appended workflow prompt; M as B with the chain in one `--resume`d
  conversation. Isolation probe: B sees no harness tools or instructions; A and A+
  see all 21 tools; A+ starts with ~5 k more context (30.2 k vs 25.3 k).
- 5 reps per task and setup (M: 3), randomized order, 2–3 runs in parallel. Every
  check fails on the untouched tree and passes on a reference solution (no-cost
  `selftest.py`). Chains repair a failed step to the reference state so steps start
  equal; no repair was needed in any H run.
- Usage from the result JSON (`modelUsage`, `total_cost_usd` = list price). Resumed
  runs report cumulative usage and cost, so M steps are differenced; `num_turns` is
  per invocation and counts conversation turns, not model calls, so context per call
  uses distinct assistant message ids from the session logs.
- Windows Application Control intermittently blocked freshly built Go test binaries;
  checks retry with stripped and unstripped builds and a still-blocked check marks
  the run incomplete (re-run), never failed.

## Limitations

- **Small task sets per family** (1–8 tasks) and 5 reps; R has a single task, so its
  CI is very wide (0.47–2.16).
- **Chains are short.** H tops out near 60 k context per call; the real expensive
  sessions are 4–8× larger. The H result says nothing about hand-off at that size.
- **One model** (Sonnet 5.5). The operator's real usage is mostly Opus; adoption and
  costs may differ.
- **R failures are real but narrow:** both harness setups missed the "N behind main"
  wording on unpushed branches in 2 of 5 runs where B did not; with n = 5 this may be
  noise.
- **Synthetic recall corpus** (D) was written for the benchmark; real ADR histories may
  be messier.

## Cost

| Item | List price |
|---|---|
| Scored matrix (261 runs) | $46.76 |
| Duplicate runs after an app restart (excluded from scoring) | ≈ $1.0 |
| Probes and pilots | ≈ $0.7 |
| Cap | $150 |
