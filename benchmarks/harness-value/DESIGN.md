# Harness-value benchmark — design

*Drafted 2026-10-15. Successor to `benchmarks/token-efficiency` (2026-10-12).*

## Question

Does agent-harness make a coding agent cheaper **on the kinds of work where the
real-usage baseline says tokens go** (long multi-step work, resumed work, recall
of earlier decisions, code-relation questions on a large codebase), without
lowering the pass rate? Answered per task family as a ranged score.

The 2026-10-12 A/B could not answer this: its 8 tasks were single-session and
short on a 10k-line repo, and the agent never called a harness tool.

## Setups

| Setup | Harness | Instructions | MCP tools |
|---|---|---|---|
| **B** — control | none | `~/.claude/CLAUDE.md` excluded | none |
| **A** — as installed | enroll + SessionStart/Read hooks + MCP | the installed memory protocol (`~/.claude/CLAUDE.md`) | deferred (as today) |
| **A+** — as intended | same as A | A's protocol plus an appended workflow: retrieve at start, record decisions/discoveries, hand off at the end | loaded eagerly, no ToolSearch round trip |
| **M** — marathon reference (family H only) | none | as B | none |

Every setup: `claude -p … --model claude-sonnet-5-5 --setting-sources project
--strict-mcp-config --permission-mode bypassPermissions`, a per-run harness home
for A/A+ (no leakage between runs or into the operator's store), host-session
env vars stripped. `A+ ÷ B` is what the harness *can* do; `A ÷ A+` is what is lost
to non-adoption; `A+ ÷ M` (H only) is harness hand-off against the status-quo
marathon session.

## Task families

### H — long-run hand-off (targets K1, K2)

Two six-step feature chains on landfirst (pinned `4eb6172`), each touching 4–6
packages. Later steps depend on facts found in earlier ones that the final code
alone does not reveal.

- **H-sugg** — surface each unit's suggested fix: investigate why `-plain` never
  prints it → extract a pure `buildTree` → populate `protocol.Unit.Suggested`
  before `revisionOf` → plain-report suffixes → TUI `s` key → `-json` + README.
- **H-stale** — repository staleness: investigate why `Unit.Stale` is always true
  in fixtures → configurable `StaleAfter` → `Freshness.Stale` in protocol → keep
  it out of `Snapshot.Revision` → TUI/plain warning → `-stale-after` flag.

B/A/A+ run each step as a fresh `claude -p` in the same working tree (memory
persists across steps for A/A+). M runs all six steps in one conversation
(`--resume`). After each step its hidden check runs; on failure the step's
reference solution is applied before the next step ("repair"), so every step
starts from the same code in every setup and failures do not cascade. The
repair is recorded; a chain's score counts only its own steps' usage.

### R — resume after a break (targets K3)

Chain C (accurate unlanded counts) split in two sessions. Session 1: investigate
C1 and implement C2, then stop. Session 2 ("continue the work"): C3 + C4. Session
2's usage and pass are the measurement; session 1 is setup (also repaired on
failure). A/A+ keep the session-1 memory; B starts cold.

### D — decision recall (targets K4)

A synthetic but realistic project history committed as `docs/adr/0001…0030-*.md`
(30 ADRs, ~16.8k words; 8 load-bearing decisions, the rest plausible noise,
including two superseded decisions). Questions whose answers are decisions, not
code ("which threshold did we settle on for X and what superseded the first
choice"). Both setups see the same files; A/A+ also get them through enroll's
docs ingest and memory search. Scored by an `ANSWER:` line with exact
ground-truth tokens. 2 tasks × 4 questions.

### G — code relations at scale

Hugo `v0.128.0` (`e6d2712`, 104k non-test Go lines, 816 files). Callers of
uniquely named functions and change-impact questions ("which packages break if
X's signature changes"), ground truth from a `go/ast` walk (`gocallers`) and
`go list`. Exact-set scoring. 4 tasks. Enroll here takes ~250 s per A/A+ run;
it is wall time only and excluded from the score.

### C — control (regression, K7)

The 8 tasks of 2026-10-12, unchanged, setups B/A/A+.

## Metrics

Per run, from the `--output-format json` result (summed over all sessions of a
multi-session run): input, cache-write, cache-read, output tokens; total
tokens; list-price cost (`total_cost_usd`); turns; context per turn (total
input ÷ turns); wall time; harness tool calls and ToolSearch calls from the
session logs; pass/fail per step.

## Score

- Per task: median over reps of each metric, ratio vs B (and A+ vs M for H).
- Per family: geometric mean of per-task ratios, 95% bootstrap CI (5,000
  resamples of runs within task × setup).
- **Primary score: total-token ratio. Secondary: cost ratio, context/turn ratio.**
- Bands, applied to the CI of the primary score:
  - whole CI ≤ 0.90 → **harness wins**
  - CI inside 0.90–1.10, or straddling a band edge → **no clear effect**
  - whole CI ≥ 1.10 → **harness costs**
- Quality gate: a setup whose pass rate on a family is below B's has that
  family's score marked **invalid** regardless of tokens.
- Overall: geometric mean of the family ratios (equal weight), with its CI.

Output: one line per family and setup, e.g.
`H  A+/B  0.58 (0.47–0.71)  harness wins   pass 30/30 vs 29/30`.

## Size and budget

| Family | Tasks | Setups | Reps | Runs |
|---|---|---|---|---|
| H | 2 chains (12 sessions each) | B, A, A+, M | 5 (M: 3) | 36 chain runs |
| R | 1 | B, A, A+ | 5 | 15 |
| D | 2 | B, A, A+ | 5 | 30 |
| G | 4 | B, A, A+ | 5 | 60 |
| C | 8 | B, A, A+ | 5 | 120 |

Hard spend cap **$150** (list price, summed from `total_cost_usd`), enforced
before every run. Families run in the order H, R, D, G, C so the cap, if hit,
cuts the cheapest-to-skip family. A two-run pilot per family precedes the
matrix and is excluded from the statistics.

## Validity checks before any model run

- `selftest`: every hidden check fails on the untouched tree and passes on the
  reference solution (each chain step, in order, from the previous step's
  reference).
- Isolation probes per setup: B sees no CLAUDE.md, hooks or harness tools; A
  sees deferred harness tools; A+ sees them loaded and the appended workflow.
