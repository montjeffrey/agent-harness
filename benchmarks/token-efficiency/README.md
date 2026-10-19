# Token-efficiency benchmark

A/B measurement of whether agent-harness reduces the tokens Claude Code spends.
Results and method: [docs/benchmarks/2026-10-12-token-efficiency.md](../../docs/benchmarks/2026-10-12-token-efficiency.md).

Not part of the package: `pyproject.toml` only packages `harness*`, `templates*` and `hooks*`.

## Files

| File | Purpose |
|---|---|
| `tasks.py` | The 8 tasks: prompts, injected bugs, hidden tests, pass checks |
| `run.py` | `selftest`, `one TASK ARM REP`, `matrix` (resumable, spend-capped at $25) |
| `hookwrap.py` | Runs a harness hook with a per-run harness home |
| `probe_arm_a.py` | One cheap session that proves arm A's hook and MCP wiring |
| `analyze.py` | Per-task medians, bootstrap ratios, pass rates, tool usage |
| `crosscheck.py` | Compares result-JSON usage against `agent-harness-telemetry` |
| `gocallers/` | `go/ast` walk that produced Q1's ground truth |
| `results/2026-10-12/` | Raw per-run records and the analysis/cross-check output behind the report |

The archived `results.jsonl` has local paths and session ids removed; it keeps
every field `analyze.py` reads.

## Running it

Windows; needs Go, git, Python 3.12+, the `claude` CLI logged in, and agent-harness
installed with `bootstrap.ps1`. Scratch data goes to `%LOCALAPPDATA%\Temp\ahbench`
(override with `AHBENCH_DIR`; keep it outside `AGENT_HARNESS_WORKSPACE`). A non-default
harness venv can be set with `AGENT_HARNESS_VENV`.

```bash
git clone https://github.com/montjeffrey/landfirst.git "$LOCALAPPDATA/Temp/ahbench/pristine-landfirst"
```

```bash
python run.py selftest
```

```bash
python probe_arm_a.py
```

```bash
python run.py one R1-rename-lines A 1 --pilot
```

```bash
python run.py matrix --reps 3 --parallel 2
```

```bash
python analyze.py --json "$LOCALAPPDATA/Temp/ahbench/analysis.json"
```

```bash
python crosscheck.py
```

`selftest` makes no model calls. `crosscheck.py` expects
`ahbench\telhome\.claude\projects` to be a junction to `%USERPROFILE%\.claude\projects`
(`mklink /J`). Remove that junction with `rmdir` afterwards, never with a recursive
delete, which would follow it into the real session logs.

To re-derive the published numbers from the archived data without running anything:

```bash
python analyze.py --results results/2026-10-12/results.jsonl
```
