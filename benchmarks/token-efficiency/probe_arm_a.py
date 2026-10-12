"""Wiring probe for arm A: one short, cheap session (~$0.11) that confirms the
SessionStart hook context arrives and the agent-harness MCP tools answer from
the per-run harness home. Run it once before a matrix.

  python probe_arm_a.py
"""

import json
import subprocess

import run
from tasks import TASKS

PROMPT = (
    "Do three things and report briefly: (1) quote the first 3 lines of any SessionStart hook "
    "context you received; (2) call the agent-harness MCP tool memory_status and report whether it "
    "succeeded and the project id it names; (3) call graph_find for 'Lines' and report how many "
    "results. Do not do anything else."
)


def main() -> None:
    d = run.RUNS / "probe-armA"
    if d.exists():
        run.rmtree_force(d)
    d.mkdir(parents=True)
    repo = d / "repo"
    run.fresh_clone(repo, TASKS["R1-rename-lines"])
    extra, enroll_s = run.arm_a_setup(d, repo)
    cmd = [run.CLAUDE, "-p", PROMPT, "--output-format", "json", "--model", run.MODEL,
           "--setting-sources", "project", "--strict-mcp-config",
           "--permission-mode", "bypassPermissions", *extra]
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                       env=run.child_env(), timeout=600)
    out = json.loads(p.stdout.strip().splitlines()[-1])
    print(out["result"])
    print("cost", out["total_cost_usd"], "enroll_s", round(enroll_s, 1))
    print(json.dumps(run.tool_profile(out["session_id"])["tool_calls"]))
    subprocess.run(["cmd", "/c", "rmdir", str(d / "home" / ".cache")], capture_output=True)


if __name__ == "__main__":
    main()
