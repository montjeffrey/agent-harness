# Harness-value benchmark

A/B/A+ measurement of whether agent-harness lowers token use on the work the
real-usage baseline says is expensive. Method: [DESIGN.md](DESIGN.md). Results:
[docs/benchmarks/2026-10-16-harness-value.md](../../docs/benchmarks/2026-10-16-harness-value.md).

Not part of the package (`pyproject.toml` packages only `harness*`, `templates*`, `hooks*`).

## Files

| File | Purpose |
|---|---|
| `tasks_common.py` | Task contract, Go hidden-test helper (Application Control retries), reference repair |
| `tasks_h.py` | H: two six-step feature chains on landfirst |
| `tasks_r.py` | R: resume after a break (session 2 scored) |
| `tasks_d.py`, `corpus_adr/` | D: decision recall from a 30-ADR history |
| `tasks_g.py` | G: callers / change impact on Hugo v0.128.0 |
| `selftest.py` | No-cost validation; builds the reference branches in `refs-<repo>.git` |
| `run.py` | Setups B / A / A+ / M, `probe`, `one`, `matrix` (resumable, `HV_SPEND_CAP`, default $150) |
| `analyze.py` | Ranged scores, verdict bands, pass-rate gate |
| `results/2026-10-16/` | Scored records and analysis behind the report |

Family C reuses `../token-efficiency/tasks.py` and its `hookwrap.py`.

The archived `results.jsonl` contains local paths and session ids from the
operator's machine. Strip them before publishing it anywhere.

## Running it

Windows; needs Go, git, Python 3.12+, the `claude` CLI logged in, and agent-harness
installed in the pipx venv (`AGENT_HARNESS_VENV` overrides it). Scratch data goes to
`%LOCALAPPDATA%\Temp\ahbench` (`AHBENCH_DIR` overrides). Pristine clones expected
there: `pristine-landfirst` (github.com/montjeffrey/landfirst) and `pristine-hugo`
(`git clone --depth 1 --branch v0.128.0 https://github.com/gohugoio/hugo.git`).

```bash
python selftest.py tasks_h
```

```bash
python run.py probe
```

```bash
python run.py matrix --reps 5 --m-reps 3 --parallel 2
```

```bash
python analyze.py
```

Run `selftest.py` for every family module before a matrix: it also (re)builds the
reference branches the chain repairs fetch. Never run two `matrix` processes over the
same families at once; `analyze.py` keeps only the first record per task, setup and rep.
