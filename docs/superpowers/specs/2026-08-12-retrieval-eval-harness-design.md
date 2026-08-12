# Retrieval eval harness — design

**Date:** 2026-08-12
**Status:** Approved, implemented
**Increment:** 0 of the semantic-routing sequence

## Why this exists

The goal behind this work is to make an agent's memory search cheaper and more
accurate by partitioning memory semantically and routing an incoming prompt to
the right partition before any search runs. Four failure modes motivated it,
all of which the user reports hitting:

1. Wrong entries win the context budget.
2. Choosing what to include costs too much.
3. The budget is too small for what is genuinely relevant.
4. Another project's memory cannot be reached at all.

The chosen shape (**Approach A**) is a router layer *above* the existing
selectors, leaving `deterministic_selector` and `semantic_selector` untouched.
Partitions are content-derived and topic-shaped; a router maps an incoming
prompt to a small set of them; selection then happens within the routed
partitions. The router is an interface with a cheap deterministic
implementation first, so a NIM-backed classifier can drop in behind the same
interface later without retrieval changing.

The full sequence, each increment its own spec:

| # | Increment | Fixes |
|---|---|---|
| **0** | **Measurement harness** | *(makes 1–4 checkable)* |
| 1 | Partitions + deterministic router | 1, 2 |
| 2 | Tiered expansion | 3 |
| 3 | Cross-project routing over the registry | 4 |
| 4 | NIM worker behind the router interface | cost |

This spec covers **increment 0 only**.

### An honest scoping note

`memory_context.DEFAULT_MAX_CHARS = 8000` already hard-caps every packet. So
semantic partitioning is a **precision-and-cost** win, not a token-volume one:
the packet was never going to get bigger. What improves is *which* 8000
characters an agent gets, and how much work it took to choose them. The
harness measures exactly those two things and nothing else.

## Architecture and boundaries

A retrieval measurement harness answering one question: for a given prompt,
did selection put the right memory entries in the packet, and what did
deciding cost?

`harness/memory_eval.py` sits *beside* retrieval, never inside it. It calls
the selectors as a consumer through the existing `Selector` contract. Neither
`memory_context` nor `memory_index` changes behaviour. This keeps
`memory_store`'s stated rule intact — no sockets, no model summarization in
the storage layer — because the harness only observes.

**Explicitly out of scope:** it does not change ranking, does not add
partitions, does not call NIM, and does not write memory. It is read-only
against memory and write-only into the working store. Every later increment is
measured *by* it rather than modifying it.

## The working-store tier

Eval sets are real prompts paired with local entry ids — machine-specific, not
something to commit into a shared repository. That makes increment 0 the
natural place to introduce the third storage tier the wider design needs.

```
<project>/.agent-harness/          committed, as today
  scope.md, memory/…
<project>/.agent-harness/work/     NEW — never committed
  .gitignore                       contains "*", so it ignores itself
  eval/<name>.jsonl                eval cases
  eval/results/<stamp>.json        scored baselines
```

Three properties are load-bearing:

- **Self-ignoring.** A `.gitignore` containing a single `*` inside `work/`
  ignores the whole directory including itself. Enabling the tier in any
  project therefore never requires editing that project's root `.gitignore`.
  (`install_cli.ensure_gitignore_entry` takes the other approach for
  `graphify-out/`; that path predates this tier.)
- **Invisible to memory.** `memory_store.iter_entries` resolves exactly one
  file, `<directory>/inbox.jsonl`, so a sibling `work/` is unseen by `status`,
  `compact`, `review`, and `index` without any of them knowing it exists.
- **Lazy.** Nothing is created by resolving a path. A project that never runs
  eval never grows the directory.

`work_path` refuses to resolve outside the tier: outside it, nothing is
ignored, so an escaping `..` is a leak rather than a bug.

## Components

| Component | Responsibility |
|---|---|
| `work_store.work_dir` / `work_path` | Pure path resolution, containment-checked |
| `work_store.ensure_work_dir` / `ensure_work_path` | Create the tier and its ignore file; idempotent, never clobbers |
| `memory_eval.load_cases` / `parse_case` | Read and validate a JSONL case set |
| `memory_eval.candidate_pool` | The pool production ranks, via `memory_context.durable_pools` |
| `memory_eval.resolve_strategies` | Resolve each strategy explicitly; report the unavailable ones |
| `memory_eval.run_case` / `score` | Time one selection and score what it returned |
| `memory_eval.evaluate` | Cases × strategies → `EvalReport` |
| `memory_eval.render_markdown` / `render_json` | The two halves of the report contract |
| `memory.cmd_eval` | CLI surface |

## Data flow

```
cases.jsonl ─┐
             ├─► evaluate ─► run_case ─► selector(candidates, query, limit)
memory ──────┘                  │                    ▲
  └─ durable_pools ─► candidates┘        deterministic│semantic
                                                      │
                              score(index_keys, relevant) ─► EvalReport
                                                              ├─► Markdown (stdout)
                                                              └─► JSON (stdout, --save)
```

### Case format

```json
{"name": "auth-rewrite",
 "query": "how did we redo login",
 "relevant": ["project:mem-2026-08-12-001", "machine:mem-2026-08-11-004"]}
```

`relevant` holds `memory_index.index_key` values — `scope:id`, **not** bare
entry ids. Ids are unique per store, not globally: the project and machine
inboxes routinely mint the same `mem-YYYY-MM-DD-001` on the same day, so a
case keyed on a bare id would score a machine hit as a project hit and quietly
inflate precision. Bare ids are rejected at parse time.

### Candidate pool

`memory_context.durable_pools` was factored out of `build_packet` and made
public so eval ranks exactly the pool production ranks. Scoring a
plausible-looking substitute — "every live entry", say — yields numbers that
read as retrieval quality while describing a pool no packet is ever built
from. Eval uses `project_durable + machine_durable`; relationships are
excluded, since that section is accepted-only and tiny, and folding it in
would let a change in link count move a score meant to track knowledge
retrieval.

### Metrics

All at the requested `limit` (`@k`):

- **precision** — of what was returned, how much was wanted. Falls when
  irrelevant entries win budget relevant ones needed. Divides by entries
  actually returned, not by `limit`, so a small store is not scored as though
  selection failed to fill a quota it could never fill.
- **recall** — of what was wanted, how much was returned. Falls when the right
  entry exists but ranks below the cut.
- **reciprocal rank** — 1/(position of the first relevant hit). Falls when the
  right entry is returned but buried, which costs real budget even when
  precision and recall look fine.
- **elapsed_ms** — the wall-clock cost of deciding, via `time.perf_counter`.
  Ranking quality bought with unacceptable latency is not an improvement.

Default `limit` is `memory_context.CANDIDATE_LIMIT` (40) — the depth
production actually requests. Scoring at another depth would measure a cut the
packet builder never makes.

## Error handling

The governing distinction: a *store* that silently drops a malformed line
still works; a *benchmark* that silently drops a case reports a score for a
question it never asked.

| Situation | Behaviour |
|---|---|
| Malformed case line | `EvalCaseError` naming file and line number; CLI exit **2** |
| Bare id in `relevant` | `EvalCaseError` explaining the cross-store collision; exit **2** |
| Case file absent | Message plus a copy-pasteable example line; exit **1** |
| Case file with no cases | `EvalCaseError`; exit **2** |
| ChromaDB missing, or no index built | Strategy skipped, recorded as `semantic: unavailable (<reason>)` in `notes`; deterministic still scores; exit **0** |
| No strategy resolvable | Explicit note; nothing scored; exit **1** — nothing was measured, so the run is not a baseline |
| Empty candidate pool | Explicit note that every score is zero by construction |
| `limit <= 0` | `ValueError`; exit **2**. Selectors slice with `[:limit]`, so `-1` would silently drop the last ranked entry and score a cut nobody asked for |

This deliberately diverges from `memory_store.iter_entries`, which skips
malformed lines by design.

Each strategy is requested **explicitly**, never through `auto`, honouring
`resolve_selector`'s own contract that an explicit strategy raises rather than
substituting another. A benchmark that silently ran deterministic selection
under a `semantic` heading would be worse than no benchmark. Correspondingly,
`EvalReport.aggregate` omits a strategy that never ran rather than averaging
in an unmeasured zero — that is how an absent backend comes to look like a bad
one.

Exit codes follow the module's existing contract: `0` success, `1` nothing to
do or capability unavailable, `2` input rejected.

One known limit, inherited rather than introduced: `memory.main` has no
top-level `except`, so an unanticipated exception (a Chroma index that passes
`probe_index` but fails mid-query, say) surfaces as a traceback rather than a
clean exit code. `cmd_context` and `cmd_index` behave the same way. Worth
fixing once, for all of them, rather than papering over it in `eval` alone.

## Testing

`tests/test_work_store.py` and `tests/test_memory_eval.py`, both under the
autouse `isolated_home` fixture in `tests/conftest.py`, so nothing touches the
real `~/.agent-harness`.

Two claims are verified against reality rather than asserted:

- **Git really ignores the tier.** The test `git init`s a temp project, writes
  a file under `work/`, and asserts `git check-ignore` returns 0 and
  `git status --untracked-files=all` is silent — while the project's root
  `.gitignore` does not exist at all. Skipped when git is unavailable.
- **Memory readers really cannot see it.** A decoy `inbox.jsonl` is planted
  inside `work/`; `iter_entries` returns only the real entry.

Other coverage: path containment (`..` and deep escapes rejected), idempotent
non-clobbering `ensure`, bare-id rejection, malformed-case rejection with line
numbers, precision's denominator, MRR falling with depth, pool parity with
`build_packet`'s filters, semantic reported unavailable rather than swapped,
aggregate omitting a strategy that never ran, filename/report clock agreement,
and all four CLI exit paths.

## What increment 1 inherits

- A gitignored per-project tier that later per-task working stores can live in
  without further design.
- `durable_pools` as the single definition of what gets ranked.
- A baseline command: any partition or router change must move precision,
  recall, MRR, or elapsed_ms on a real case set, or it is not an improvement.
