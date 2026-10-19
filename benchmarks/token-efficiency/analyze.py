"""Summarise results.jsonl: per-task medians per arm, overall A-vs-B ratio with
a bootstrap interval, pass rates, harness-tool usage, and benchmark cost.

  python analyze.py [--include-pilot] [--json out.json] [--results results.jsonl]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run import RESULTS  # noqa: E402
METRICS = ["total_tokens", "cost_usd", "output_tokens", "cache_creation_tokens",
           "cache_read_tokens", "input_tokens", "num_turns", "wall_s"]


def load(include_pilot: bool, path: Path = RESULTS) -> list[dict]:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if include_pilot or not r.get("pilot")]


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def geo_mean_ratio(by_task_arm: dict, tasks: list[str], metric: str, rng=None) -> float:
    logs = []
    for t in tasks:
        a = by_task_arm[(t, "A")]
        b = by_task_arm[(t, "B")]
        if rng:
            a = [rng.choice(a) for _ in a]
            b = [rng.choice(b) for _ in b]
        ma, mb = med([r[metric] for r in a]), med([r[metric] for r in b])
        if ma and mb:
            logs.append(math.log(ma / mb))
    return math.exp(sum(logs) / len(logs)) if logs else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-pilot", action="store_true")
    ap.add_argument("--json")
    ap.add_argument("--results", type=Path, default=RESULTS,
                    help="results.jsonl to analyse (default: the live benchmark dir)")
    a = ap.parse_args()
    rows = load(a.include_pilot, a.results)
    by = defaultdict(list)
    for r in rows:
        by[(r["task"], r["arm"])].append(r)
    tasks = sorted({r["task"] for r in rows if (r["task"], "A") in by and (r["task"], "B") in by})

    out = {"n_runs": len(rows), "tasks": {}, "overall": {}}
    print(f"runs: {len(rows)}  tasks with both arms: {len(tasks)}\n")
    print(f"{'task':<22} {'nA':>2} {'nB':>2} {'tok A':>10} {'tok B':>10} {'A/B':>6} {'$A':>6} {'$B':>6} "
          f"{'passA':>6} {'passB':>6} {'turnsA':>6} {'turnsB':>6}")
    for t in tasks:
        A, B = by[(t, "A")], by[(t, "B")]
        row = {"nA": len(A), "nB": len(B)}
        for m in METRICS:
            row[f"{m}_A"] = med([r[m] for r in A])
            row[f"{m}_B"] = med([r[m] for r in B])
            row[f"{m}_A_all"] = [r[m] for r in A]
            row[f"{m}_B_all"] = [r[m] for r in B]
        row["pass_A"] = sum(r["passed"] for r in A)
        row["pass_B"] = sum(r["passed"] for r in B)
        row["ratio_tokens"] = row["total_tokens_A"] / row["total_tokens_B"]
        row["ratio_cost"] = row["cost_usd_A"] / row["cost_usd_B"]
        out["tasks"][t] = row
        print(f"{t:<22} {len(A):>2} {len(B):>2} {row['total_tokens_A']:>10,.0f} {row['total_tokens_B']:>10,.0f} "
              f"{row['ratio_tokens']:>6.2f} {row['cost_usd_A']:>6.2f} {row['cost_usd_B']:>6.2f} "
              f"{row['pass_A']:>3}/{len(A):<2} {row['pass_B']:>3}/{len(B):<2} "
              f"{row['num_turns_A']:>6} {row['num_turns_B']:>6}")

    rng = random.Random(7)
    for metric in ("total_tokens", "cost_usd", "output_tokens", "cache_creation_tokens",
                   "cache_read_tokens", "num_turns", "wall_s"):
        point = geo_mean_ratio(by, tasks, metric)
        boots = sorted(geo_mean_ratio(by, tasks, metric, rng) for _ in range(5000))
        lo, hi = boots[int(0.025 * len(boots))], boots[int(0.975 * len(boots))]
        per_task = sorted(out["tasks"][t][f"{metric}_A"] / out["tasks"][t][f"{metric}_B"] for t in tasks
                          if out["tasks"][t][f"{metric}_B"])
        out["overall"][metric] = {"geo_mean_ratio": point, "ci95": [lo, hi],
                                  "median_task_ratio": med(per_task),
                                  "task_ratio_min": per_task[0], "task_ratio_max": per_task[-1]}
        print(f"\n{metric}: A/B geometric mean of per-task median ratios = {point:.3f} "
              f"({(point - 1) * 100:+.1f}%), bootstrap 95% CI [{lo:.3f}, {hi:.3f}]; "
              f"median per-task ratio {med(per_task):.3f}, range {per_task[0]:.2f}-{per_task[-1]:.2f}")

    for arm in "AB":
        rs = [r for r in rows if r["arm"] == arm]
        out["overall"][f"pass_{arm}"] = [sum(r["passed"] for r in rs), len(rs)]
        print(f"\npass rate {arm}: {sum(r['passed'] for r in rs)}/{len(rs)}")
    a_runs = [r for r in rows if r["arm"] == "A"]
    used = [r for r in a_runs if any(k.startswith("mcp__agent-harness") for k in (r.get("tool_calls") or {}))]
    calls = defaultdict(int)
    for r in a_runs:
        for k, v in (r.get("tool_calls") or {}).items():
            if k.startswith("mcp__agent-harness"):
                calls[k.split("__")[-1]] += v
    out["overall"]["harness_tool_use"] = {"runs_using": len(used), "runs": len(a_runs), "calls": dict(calls)}
    print(f"\narm A runs that called any agent-harness MCP tool: {len(used)}/{len(a_runs)}  calls: {dict(calls)}")
    for arm in "AB":
        tc = defaultdict(int)
        for r in rows:
            if r["arm"] == arm:
                for k, v in (r.get("tool_calls") or {}).items():
                    tc[k] += v
        out["overall"][f"tool_calls_{arm}"] = dict(tc)
        print(f"tool calls {arm}: {dict(sorted(tc.items(), key=lambda kv: -kv[1]))}")
    total_cost = sum(r["cost_usd"] or 0 for r in load(True, a.results))
    out["overall"]["benchmark_cost_usd_all_runs_incl_pilot"] = total_cost
    print(f"\nbenchmark cost, all recorded runs incl. pilot: ${total_cost:.2f}")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
