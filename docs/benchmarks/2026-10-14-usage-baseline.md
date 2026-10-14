# Usage baseline and efficiency targets

*Baseline taken 2026-10-14 on one machine (machine A, Windows 11). Internal; not published.*

This is the first point in a cross-machine benchmark series. It records where real
day-to-day usage stands, which numbers matter, and the targets the harness should move.
The [2026-10-12 A/B benchmark](2026-10-12-token-efficiency.md) answered "does the harness
save tokens on small scripted tasks" (no). This baseline asks where tokens actually go
in everyday use.

## Source and method

- Read-only scan of `~/.claude/projects/**` (891 transcripts, 1.4 GB), main and
  subagent transcripts, usage counted once per `message.id`, `<synthetic>` skipped.
  Nothing was written to `~/.agent-harness/telemetry`. That store is empty on this
  machine: `agent-harness-telemetry collect` has never run here.
- Window: the 30 days to 2026-10-14. All-time on this machine is 766 sessions and
  6.56 B tokens since 2026-08-16.
- "Interactive" means 3 or more main-loop turns. 615 of 718 sessions in the window are
  1–2-turn automated runs (520 of them in `ECC-gateguard-full`). They carry 0.6% of the
  tokens and would otherwise swamp every median.
- Tokens, not dollars: the toolkit never prices usage. Relative weights at list
  price are noted only where they change the conclusion.
- Codex and Gemini were not scanned. This is Claude Code only.

## Baseline — 30 days, interactive sessions (n = 103)

| Metric | Value |
|---|---|
| Total tokens | 4.94 B (cache read 97.6%, cache write 2.1%, output 0.3%, uncached input ≈0) |
| Main-loop turns / subagent turns | 12,832 / 1,786 (subagents 12% of turns) |
| Tokens per session | p50 325 k · p90 110 M |
| Turns per session | p50 9 · p90 315 |
| **Context per turn** | **p50 30 k · p90 346 k** |
| Context at first turn (fixed overhead) | p50 23 k · p90 97 k |
| Cache-hit ratio (read ÷ all input) | 97.9% |
| **Concentration** | **top 1 session 23% · top 5 59% · top 10 77% · top 20 94.5%** |
| Harness tool use | 22 / 103 sessions (21%); 640 calls, of which 620 `memory_remember`, 8 `memory_context`, 6 `memory_search`, 4 `memory_status`, 1 `graph_find`, 1 `memory_tasks` |
| Models (by API responses) | opus-5 7.8 k · opus-5-5 6.2 k · sonnet-5-5 0.5 k · sonnet-5 0.2 k |

The heaviest sessions:

| Start | Project | Turns (main + sub) | Tokens | Context / turn | Harness calls |
|---|---|---|---|---|---|
| 2026-10-02 | project A | 2,690 | 1,125 M | 418 k | 279 |
| 2026-09-27 | project B | 1,925 | 871 M | 453 k | 234 |
| 2026-09-25 | project C | 448 + 914 | 348 M | 255 k | 27 |
| 2026-10-10 | scratch-…582b07 | 818 + 25 | 314 M | 372 k | 10 |
| 2026-09-30 | project C | 554 + 33 | 273 M | 465 k | 16 |

## What the baseline says

1. **The cost is marathon sessions, not per-task overhead.** Twenty sessions carry 94.5%
   of all tokens. Each turn in them re-reads 250–465 k tokens of context. The harness's
   fixed overhead (~1.6 k tokens, measured 2026-10-12) is noise next to that. A single
   turn of a marathon session costs as much as about 18 typical first turns.
2. **Cache reads are the bill.** 97.6% of tokens are cache reads. Even at one tenth of the
   input price they are roughly 60% of the list-price-weighted spend, ahead of cache
   writes and output. The lever is *context size × turn count*, not input or output.
3. **The harness is used as a write-only store.** 97% of harness calls are
   `memory_remember`, and almost all of them come from the two largest sessions.
   Retrieval (`memory_context`, `memory_search`, `graph_*`) ran 15 times in 30 days.
   Memory that is never read back cannot shorten a session. That is the same failure
   the 2026-10-12 benchmark found ("0 of 24 runs called a harness tool").
4. **"Sessions with harness calls are bigger" is confounded.** The median is 84 M tokens
   with harness calls vs 172 k without. The marathon sessions are where people remember
   to call the harness, so this is not an effect of the harness.
5. **Caching already works.** The 97.9% hit ratio leaves little to win there; it only needs
   a guard against regression.

## Target efficiency

These are the metrics to track on every machine, with targets set from this baseline.
"Context per turn" and "concentration" are the two that move cost.

| # | KPI | Baseline (machine A) | Target | Harness lever |
|---|---|---|---|---|
| K1 | Context per turn, p90 (interactive) | 346 k | ≤ 150 k | Hand off long work to fresh sessions seeded by `work_start` / `memory_context` instead of continuing past ~150 k |
| K2 | Token share of top-20 sessions | 94.5% | ≤ 60% | Same as K1; a context-size nudge in the SessionStart / Stop hook |
| K3 | Retrieval calls per interactive session | 0.15 | ≥ 1 in feature-scale sessions | Make `memory_context` cheaper to reach (non-deferred, or injected by hook) and phase-1 latency fix |
| K4 | Read:write ratio of harness memory calls | 15 : 620 | ≥ 1 : 1 | Same as K3, and batch `memory_remember` |
| K5 | First-turn context, p50 | 23 k | Harness share ≤ 1.6 k and not growing | Keep the SessionStart packet budgeted |
| K6 | Cache-hit ratio | 97.9% | ≥ 95% (guard) | Don't put volatile text early in context |
| K7 | Pass rate on the A/B task set | 24 / 24 | No regression | Rerun `benchmarks/token-efficiency` when K1–K4 levers ship |

Rough size of the prize: had the five heaviest sessions stayed at 150 k context per turn,
they would have used about 1.1 B tokens instead of 2.9 B. That is a 62% cut, and over a
third of this machine's 30-day total.

## What is missing to benchmark across machines

The current telemetry pipeline cannot produce this table, and it cannot combine machines.

| Gap | Where | Effect |
|---|---|---|
| No machine identity on records | `harness/telemetry_store.py` schema 1 | Stores from two machines can't be merged or compared |
| Store is per-machine and has no export | `~/.agent-harness/telemetry`, `harness/telemetry.py` | No way to bring machines together, even through a synced folder |
| Cache write and cache read folded into `input_tokens` | `harness/collectors/claude_code.py` `_collect_from_file` | Loses the split that drives cost (finding 2) |
| No tool-call counts on records | Claude Code collector | Can't measure harness adoption (K3, K4) |
| No per-session rollups (turns, context per turn, first-turn context) | `aggregate` groups only by agent/project | K1, K2 and K5 aren't computable |
| `project` is a raw local path, `source` is a local file path | Collector records | Machine-specific and private; project ids may differ per machine |
| Collection never runs automatically | No hook or schedule calls `collect` | Store stays empty unless someone remembers to run it |

## Proposed design (within the scope boundaries)

*Implemented 2026-10-14; every gap in the table above is closed. How to run it is under
[Reproduce](#reproduce).*

- **Schema 2, additive.** Add `machine` (a random id created once in
  `~/.agent-harness/machine.json`, plus an optional user label; never the hostname),
  `cache_write_tokens`, `cache_read_tokens`, and `tool_calls` / `harness_tool_calls` counts.
  Keep `input_tokens` as the sum so schema-1 readers still agree.
- **`agent-harness-telemetry export --to <dir>`.** Writes
  `<dir>/<machine>/events-YYYYMM.jsonl`, with `source` dropped and `project` replaced by
  a stable key (the git remote URL hash, else the directory basename hash). Point `<dir>`
  at a OneDrive folder and the sync client moves the data. The harness itself opens
  no socket.
- **`agent-harness-telemetry baseline [--from <dir>] [--days N] [--by machine|project|month]`.**
  Computes K1–K6 from the merged exports and writes a dated snapshot JSON, so each machine
  and each month becomes a comparable point in this series.
- **Collect on a cadence.** Run `collect` from the existing SessionStart hook at most once a
  day, in the background, so the store fills without anyone remembering to.

## Reproduce

On one machine:

```bash
agent-harness-telemetry collect
agent-harness-telemetry baseline --days 30 --min-turns 3 --json baseline-2026-10-14.json
```

`collect` fills the local store (the first run reads all history; later runs read only
transcripts changed since the last one). After that, the SessionStart hook runs it in the
background at most once a day; set `AGENT_HARNESS_TELEMETRY_AUTO=0` to turn that off.
`baseline` measures Claude Code sessions by default, because only Claude Code records carry
tool and subagent counts; `--agent all` merges Codex and Gemini. `--json` writes the full
report, including the heaviest sessions, as a dated snapshot.

Across machines, give each machine a label and export it to a shared synced folder. Then
read the merged tree from any of them:

```bash
agent-harness-telemetry machine --label desktop
agent-harness-telemetry export --to "$HOME/OneDrive/agent-harness-telemetry"
agent-harness-telemetry baseline --from "$HOME/OneDrive/agent-harness-telemetry" --days 30 --by machine
```

The export drops local paths. `project` becomes a hash of the git remote, or of the
directory name when there is no remote, so the same repository has the same key on every
machine.

### Reproduction check (2026-10-14, machine A)

The figures above came from a one-off session scan script that is not in the repository.
Re-running that script and `baseline --days 30 --min-turns 3` at the same moment, a few
hours after the original scan:

| Metric | This report | Scan script, rerun | `baseline` |
|---|---|---|---|
| Interactive sessions | 103 | 105 | 105 |
| Total tokens | 4.94 B | 4.95 B | **4.80 B** |
| Token split (read / write / output / uncached) | 97.6 / 2.1 / 0.3 / ≈0% | same | 97.6 / 2.1 / 0.3 / ≈0% |
| Main-loop / subagent turns | 12,832 / 1,786 | 12,908 / 1,786 | **12,432** / 1,785 |
| K1 context per turn, p50 · p90 | 30 k · 346 k | 30.7 k · 344 k | 30.7 k · **351 k** |
| K2 top-20 share (top 1 · 5 · 10) | 94.5% (23 · 59 · 77) | — | 94.5% (23.4 · 61.0 · 78.3) |
| K3 retrieval calls per session | 0.15 | 0.14 | 0.14 |
| K4 retrieval : `memory_remember` | 15 : 620 | 15 : 622 | 15 : 622 |
| K5 first-turn context, p50 · p90 | 23 k · 97 k | 23.2 k · 96.6 k | 23.2 k · **102 k** |
| K6 cache-hit ratio | 97.9% | 97.9% | 97.9% |
| Five heaviest sessions | table above | — | identical |

The 30-day window had moved a few hours since the original scan, which added two sessions
and a few hundred turns to both reruns. The bold differences between the two reruns come
from one method correction. A resumed session's transcript starts with a copy of its
parent's history, still tagged with the parent's session id. The scan deduplicated per
file, so it counted that history once for every resume. On this machine that was one
resume chain of three project B sessions: 483 API messages and about 150 M tokens
counted twice. `baseline` counts them once, under the parent session. Every other headline
figure reproduces within rounding.
