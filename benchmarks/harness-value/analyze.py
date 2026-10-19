"""Ranged scores for the harness-value benchmark (method: DESIGN.md, "Score").

  python analyze.py                      # reads ahbench/hv-results.jsonl
  python analyze.py --results FILE --json OUT

For each family and comparison (A+/B, A/B, A/A+, and A+/M for H): per-task
median ratio of each metric, the family's geometric mean over tasks with a
95% bootstrap interval (runs resampled within task x setup), a verdict band on
the primary metric (total tokens), and the pass-rate quality gate.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks_common import BENCH  # noqa: E402

METRICS = ("total_tokens", "cost_usd", "ctx_per_call", "api_calls", "wall_s")
COMPARISONS = (("A+", "B"), ("A", "B"), ("A", "A+"), ("A+", "M"), ("B", "M"))
FAMILIES = ("H", "R", "D", "G", "C")
BOOT = 5000
WIN, COST = 0.90, 1.10


def load(path: Path) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r.get("complete") and not r.get("pilot")]
    # Two matrix processes can overlap after a restart and finish the same
    # (task, setup, rep); keep the first, so every cell has its planned reps.
    first: dict = {}
    for r in rows:
        first.setdefault((r["task"], r["setup"], r["rep"]), r)
    rows = list(first.values())
    for r in rows:
        if "api_calls" not in r and r["setup"] != "M":
            backfill_api_calls(r)
    return rows


def backfill_api_calls(r: dict) -> None:
    """Records written before api_calls existed: count model calls from each
    measured step's own session log (one session per step outside setup M)."""
    from run import session_profile
    calls = sum(session_profile(s["session_id"])["api_calls"]
                for s in r["steps"] if s.get("measured") and s.get("session_id"))
    total_input = r["input_tokens"] + r["cache_write_tokens"] + r["cache_read_tokens"]
    r["api_calls"] = calls
    r["ctx_per_call"] = round(total_input / calls) if calls else None


def gmean(xs: list[float]) -> float:
    return math.exp(statistics.fmean(math.log(x) for x in xs))


def band(lo: float, hi: float, num: str, den: str) -> str:
    """Verdict on the CI. "harness wins/costs" only where the comparison is a
    harness setup against the no-harness control; otherwise name the cheaper side."""
    harness_vs_control = num in ("A", "A+") and den == "B"
    if hi <= WIN:
        return "harness wins" if harness_vs_control else f"{num} cheaper"
    if lo >= COST:
        return "harness costs" if harness_vs_control else f"{num} costs more"
    return "no clear effect"


def family_ratio(by: dict, tasks: list[str], num: str, den: str, metric: str,
                 rng: random.Random | None) -> float | None:
    ratios = []
    for t in tasks:
        a = [r[metric] for r in by[(t, num)] if r.get(metric)]
        b = [r[metric] for r in by[(t, den)] if r.get(metric)]
        if not a or not b:
            continue
        if rng:
            a = [rng.choice(a) for _ in a]
            b = [rng.choice(b) for _ in b]
        ratios.append(statistics.median(a) / statistics.median(b))
    return gmean(ratios) if ratios else None


def analyze(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[(r["task"], r["setup"])].append(r)
    out = {}
    for fam in FAMILIES:
        fam_tasks = sorted({r["task"] for r in rows if r["family"] == fam})
        fam_out = {}
        for num, den in COMPARISONS:
            tasks = [t for t in fam_tasks if by[(t, num)] and by[(t, den)]]
            if not tasks:
                continue
            res = {"tasks": len(tasks)}
            for metric in METRICS:
                point = family_ratio(by, tasks, num, den, metric, None)
                if point is None:
                    continue
                rng = random.Random(20261015)
                boots = sorted(x for x in (family_ratio(by, tasks, num, den, metric, rng)
                                           for _ in range(BOOT)) if x)
                res[metric] = {"ratio": point, "lo": boots[int(0.025 * len(boots))],
                               "hi": boots[int(0.975 * len(boots)) - 1]}
            runs = lambda s: [r for t in tasks for r in by[(t, s)]]
            steps = lambda s: (sum(r["steps_passed"] for r in runs(s)),
                               sum(r["steps_measured"] for r in runs(s)))
            res["pass_num"], res["pass_den"] = steps(num), steps(den)
            rate = lambda p: p[0] / p[1] if p[1] else 0
            res["gate_ok"] = rate(res["pass_num"]) >= rate(res["pass_den"])
            tt = res.get("total_tokens")
            res["verdict"] = ("invalid (pass rate below comparison)" if not res["gate_ok"]
                              else band(tt["lo"], tt["hi"], num, den) if tt else "n/a")
            res["harness_calls_per_run"] = {s: statistics.fmean([r["harness_calls"] for r in runs(s)])
                                            for s in (num, den)}
            fam_out[f"{num}/{den}"] = res
        if fam_out:
            out[fam] = fam_out
    # Overall: geometric mean of the family ratios (equal weight per family).
    for comp in ("A+/B", "A/B"):
        fams = [f for f in out if comp in out[f] and "total_tokens" in out[f][comp]]
        if fams:
            pick = lambda key: gmean([out[f][comp]["total_tokens"][key] for f in fams])
            out.setdefault("overall", {})[comp] = {
                "ratio": pick("ratio"), "lo": pick("lo"), "hi": pick("hi"), "families": fams}
    return out


def _ci(r: dict, metric: str) -> str:
    m = r.get(metric)
    return f"{m['ratio']:.2f} ({m['lo']:.2f}-{m['hi']:.2f})" if m else "-"


def _pt(r: dict, metric: str) -> str:
    m = r.get(metric)
    return f"{m['ratio']:.2f}" if m else "-"


def render(out: dict) -> str:
    lines = [f"{'family':<7} {'compare':<8} {'total tokens (95% CI)':<22} {'cost':<6} "
             f"{'ctx/call':<9} {'pass':<16} verdict"]
    for fam in FAMILIES:
        for comp, r in (out.get(fam) or {}).items():
            passes = f"{r['pass_num'][0]}/{r['pass_num'][1]} vs {r['pass_den'][0]}/{r['pass_den'][1]}"
            lines.append(f"{fam:<7} {comp:<8} {_ci(r, 'total_tokens'):<22} {_pt(r, 'cost_usd'):<6} "
                         f"{_pt(r, 'ctx_per_call'):<9} {passes:<16} {r['verdict']}")
    for comp, r in (out.get("overall") or {}).items():
        lines.append(f"overall {comp:<8} {r['ratio']:.2f} ({r['lo']:.2f}-{r['hi']:.2f})  "
                     f"families {', '.join(r['families'])}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(BENCH / "hv-results.jsonl"))
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    out = analyze(load(Path(a.results)))
    print(render(out))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
