"""A/B runner for the agent-harness token-efficiency benchmark.

  python run.py selftest                       # checks fail untouched, pass on reference fixes
  python run.py one TASK ARM REP               # one run (ARM is A or B)
  python run.py matrix --reps 3 --parallel 2   # full matrix, resumable

Arm A: fresh clone + agent-harness-enroll, harness SessionStart/Read hooks,
       harness MCP server, and ~/.claude/CLAUDE.md (which carries the memory
       protocol that agent-harness-setup installs).
Arm B: fresh clone, no hooks, no MCP, ~/.claude/CLAUDE.md excluded.
Both:  --setting-sources project (no user plugins/hooks), --strict-mcp-config,
       same model, same prompt, bypassPermissions inside a throwaway clone.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks import PINNED_COMMIT, TASKS  # noqa: E402

# Scratch area for clones, per-run homes and results. Keep it outside
# AGENT_HARNESS_WORKSPACE so auto-enroll can never fire in a control-arm clone.
BENCH = Path(os.environ.get("AHBENCH_DIR")
             or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Temp" / "ahbench")
PRISTINE = BENCH / "pristine-landfirst"
RUNS = BENCH / "runs"
RESULTS = BENCH / "results.jsonl"
TEMPLATE_CACHE = BENCH / "ahhome" / ".cache"
TOOLS = Path(__file__).parent
# The pipx venv bootstrap.ps1 installs; override for a different install.
PIPX = Path(os.environ.get("AGENT_HARNESS_VENV")
            or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pipx" / "pipx" / "venvs" / "agent-harness-kit")
PY = PIPX / "Scripts" / "python.exe"
SITE = PIPX / "Lib" / "site-packages"
ENROLL = PIPX / "Scripts" / "agent-harness-enroll.exe"
CLAUDE = shutil.which("claude") or "claude"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
MODEL = "claude-sonnet-5-5"
RUN_TIMEOUT_S = 1800
MAX_BUDGET_USD = "8"

# Host-session variables inherited from the desktop app that would change the
# child's behaviour (tool-use summaries, attended-session UI, etc.). Auth
# variables are left alone.
STRIP_ENV = {
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES", "CLAUDE_CODE_REPORT_FINDINGS",
    "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_EAGER_FLUSH", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_DISABLE_CRON",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "CLAUDE_CODE_TERMINAL_MCP_TOOLS",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_CODE_SIMPLE",
    "AGENT_HARNESS_AUTO_ENROLL", "AGENT_HARNESS_WORKSPACE",
}

_results_lock = threading.Lock()


def rmtree_force(path: Path) -> None:
    """Delete a benchmark dir: unlink junctions first (never follow them), then
    clear the read-only bit git sets on object files, which defeats rmtree on Windows."""
    for j in path.rglob(".cache"):
        if j.is_junction():
            subprocess.run(["cmd", "/c", "rmdir", str(j)], capture_output=True)

    def onexc(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)

    shutil.rmtree(path, onexc=onexc)


def fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def fresh_clone(repo: Path, task: dict) -> str:
    """Clone the pinned commit; fold any injected bug into HEAD."""
    subprocess.run(["git", "clone", "-q", str(PRISTINE), str(repo)], check=True)
    git(repo, "checkout", "-q", "-B", "main", PINNED_COMMIT)
    if task["setup"]:
        task["setup"](repo)
        name, email, date = git(repo, "log", "-1", "--format=%an%n%ae%n%cI").splitlines()
        env = dict(os.environ, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
                   GIT_COMMITTER_DATE=date)
        git(repo, "-c", f"user.name={name}", "-c", f"user.email={email}",
            "commit", "-q", "-a", "--amend", "--no-edit", "--no-verify", env=env)
    return git(repo, "rev-parse", "HEAD")


def arm_a_setup(run_dir: Path, repo: Path) -> tuple[list[str], float]:
    home = run_dir / "home"
    home.mkdir()
    # Shared ONNX model cache: the first enroll downloads into it through the junction.
    TEMPLATE_CACHE.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(home / ".cache"), str(TEMPLATE_CACHE)],
                   check=True, capture_output=True)
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home))
    for k in STRIP_ENV:
        env.pop(k, None)
    t0 = time.time()
    r = subprocess.run([str(ENROLL), str(repo)], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    enroll_s = time.time() - t0
    (run_dir / "enroll.log").write_text(r.stdout + "\n--stderr--\n" + r.stderr, encoding="utf-8")
    if "[1/4] memory" not in r.stdout or "[3/4] graph      built" not in r.stdout:
        raise RuntimeError(f"enroll incomplete: {r.stdout[-500:]} {r.stderr[-500:]}")
    hook = lambda script: f"{fwd(PY)} {fwd(TOOLS / 'hookwrap.py')} {fwd(home)} {fwd(SITE / script)}"
    settings = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
                                     "command": hook("hooks/claude/memory_context_hook.py")}]}],
        "PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command",
                                     "command": hook("hooks/claude/graph_read_guard.py")}]}],
    }}
    mcp = {"mcpServers": {"agent-harness": {
        "command": fwd(PY), "args": ["-m", "harness.mcp_server"],
        "env": {"PYTHONPATH": fwd(SITE), "USERPROFILE": str(home), "HOME": fwd(home)},
    }}}
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    (run_dir / "mcp.json").write_text(json.dumps(mcp, indent=1), encoding="utf-8")
    return ["--settings", str(run_dir / "settings.json"),
            "--mcp-config", str(run_dir / "mcp.json")], enroll_s


def arm_b_setup(run_dir: Path) -> list[str]:
    settings = {"claudeMdExcludes": [fwd(Path.home() / ".claude" / "CLAUDE.md"),
                                     "**/.claude/CLAUDE.md"]}
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    return ["--settings", str(run_dir / "settings.json")]


def child_env() -> dict:
    env = dict(os.environ)
    for k in STRIP_ENV:
        env.pop(k, None)
    return env


def session_files(session_id: str) -> list[Path]:
    hits = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    for h in list(hits):
        sub = h.parent / session_id
        if sub.is_dir():
            hits += sorted(sub.rglob("*.jsonl"))
    return hits


def tool_profile(session_id: str) -> dict:
    """Tool calls by name and characters of tool output returned into context."""
    calls, result_chars = Counter(), Counter()
    id_to_name = {}
    files = session_files(session_id)
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls[block.get("name")] += 1
                    id_to_name[block.get("id")] = block.get("name")
                elif block.get("type") == "tool_result":
                    name = id_to_name.get(block.get("tool_use_id"), "?")
                    content = block.get("content")
                    if isinstance(content, list):
                        size = sum(len(c.get("text", "")) for c in content if isinstance(c, dict))
                    else:
                        size = len(str(content or ""))
                    result_chars[name] += size
    return {"tool_calls": dict(calls), "tool_result_chars": dict(result_chars),
            "session_files": [str(f) for f in files]}


def modified_files(repo: Path) -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                         capture_output=True, text=True).stdout
    files = [l[3:] for l in out.splitlines() if l.strip()]
    return [f for f in files if not f.startswith((".agent-harness", "graphify-out"))]


def run_one(task_id: str, arm: str, rep: int, pilot: bool = False) -> dict:
    task = TASKS[task_id]
    run_id = f"{task_id}_{arm}_r{rep}_{datetime.now():%m%d%H%M%S}"
    run_dir = RUNS / run_id
    repo = run_dir / "repo"
    run_dir.mkdir(parents=True)
    head = fresh_clone(repo, task)
    enroll_s = None
    if arm == "A":
        extra, enroll_s = arm_a_setup(run_dir, repo)
    else:
        extra = arm_b_setup(run_dir)
    cmd = [CLAUDE, "-p", task["prompt"], "--output-format", "json", "--model", MODEL,
           "--setting-sources", "project", "--strict-mcp-config",
           "--permission-mode", "bypassPermissions", "--max-budget-usd", MAX_BUDGET_USD, *extra]
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=RUN_TIMEOUT_S, env=child_env())
        stdout, stderr, rc, timed_out = p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr, rc, timed_out = "timeout", -1, True
    wall = time.time() - t0
    (run_dir / "claude.stdout.json").write_text(stdout or "", encoding="utf-8")
    (run_dir / "claude.stderr.txt").write_text(stderr or "", encoding="utf-8")
    out = {}
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    result_text = out.get("result") or ""
    mod = modified_files(repo)
    try:
        passed, detail = task["check"](repo, result_text)
    except Exception as e:  # a crashing check is a failed run, recorded as such
        passed, detail = False, f"check crashed: {e!r}"
    usage = out.get("usage") or {}
    model_usage = out.get("modelUsage") or {}
    tot = lambda key: sum(int(m.get(key) or 0) for m in model_usage.values())
    rec = {
        "run_id": run_id, "task": task_id, "category": task["category"], "arm": arm, "rep": rep,
        "pilot": pilot, "model_requested": MODEL, "models_used": sorted(model_usage),
        "started": started, "wall_s": round(wall, 1), "enroll_s": enroll_s and round(enroll_s, 1),
        "head": head, "cwd": str(repo), "exit_code": rc, "timed_out": timed_out,
        "is_error": out.get("is_error"), "subtype": out.get("subtype"),
        "num_turns": out.get("num_turns"), "session_id": out.get("session_id"),
        "input_tokens": tot("inputTokens"), "output_tokens": tot("outputTokens"),
        "cache_creation_tokens": tot("cacheCreationInputTokens"),
        "cache_read_tokens": tot("cacheReadInputTokens"),
        "usage_top": {k: usage.get(k) for k in ("input_tokens", "output_tokens",
                      "cache_creation_input_tokens", "cache_read_input_tokens")},
        "model_usage": model_usage,
        "cost_usd": out.get("total_cost_usd"),
        "passed": passed, "check_detail": detail[:2000], "modified_files": mod,
        "result_tail": result_text[-1500:],
    }
    rec["total_input_tokens"] = (rec["input_tokens"] + rec["cache_creation_tokens"]
                                 + rec["cache_read_tokens"])
    rec["total_tokens"] = rec["total_input_tokens"] + rec["output_tokens"]
    if rec["session_id"]:
        rec.update(tool_profile(rec["session_id"]))
    with _results_lock, RESULTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    # The run home's .cache is a junction to the shared model cache; remove the
    # junction itself (never its target) so a later cleanup cannot follow it.
    junction = run_dir / "home" / ".cache"
    if junction.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(junction)], capture_output=True)
    return rec


def summary_line(r: dict) -> str:
    return (f"{r['task']:<22} {r['arm']} r{r['rep']}  pass={r['passed']!s:<5} "
            f"tok={r['total_tokens']:>9,} (in {r['total_input_tokens']:,} / out {r['output_tokens']:,}) "
            f"cost=${(r['cost_usd'] or 0):.3f} turns={r['num_turns']} wall={r['wall_s']}s "
            f"{'' if r['passed'] else r['check_detail'][:120]}")


def done_keys() -> set:
    keys = set()
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("subtype") and not r.get("timed_out") and not r.get("pilot"):
                keys.add((r["task"], r["arm"], r["rep"]))
    return keys


def matrix(reps: int, parallel: int, tasks: list[str], seed: int) -> None:
    rng = random.Random(seed)
    sched = []
    for rep in range(1, reps + 1):
        order = list(tasks)
        rng.shuffle(order)
        for t in order:
            arms = ["A", "B"]
            rng.shuffle(arms)
            sched += [(t, a, rep) for a in arms]
    done = done_keys()
    todo = [s for s in sched if s not in done]
    print(f"{len(todo)} runs to do ({len(sched) - len(todo)} already done)", flush=True)
    with ThreadPoolExecutor(parallel) as ex:
        for rec in ex.map(lambda s: _safe_run(*s), todo):
            if rec:
                print(summary_line(rec), flush=True)


SPEND_CAP_USD = 25.0


def spent_usd() -> float:
    if not RESULTS.exists():
        return 0.0
    return sum(json.loads(l).get("cost_usd") or 0
               for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip())


def _safe_run(t, a, rep, pilot=False):
    if spent_usd() >= SPEND_CAP_USD:
        print(f"SKIPPED {t} {a} r{rep}: spend cap ${SPEND_CAP_USD} reached", flush=True)
        return None
    try:
        return run_one(t, a, rep, pilot)
    except Exception as e:
        print(f"RUN FAILED {t} {a} r{rep}: {e!r}", flush=True)
        return None


# ---------------------------------------------------------------- selftest

def _ref_f1(repo: Path) -> None:
    p = repo / "internal/tui/plain.go"
    t = p.read_text(encoding="utf-8")
    t = t.replace("\treturn b.String()\n}",
                  "\tif len(s.Plan) == 0 && len(s.Units) > 0 {\n"
                  "\t\tb.WriteString(\"\\nMERGE ORDER\\n  nothing to merge\\n\")\n\t}\n"
                  "\treturn b.String()\n}")
    p.write_text(t, encoding="utf-8", newline="\n")


def _ref_f2(repo: Path) -> None:
    (repo / "internal/tui/filter.go").write_text('''package tui

import (
	"strings"

	"github.com/montjeffrey/landfirst/internal/protocol"
)

func ParseVerdictList(s string) []string {
	var out []string
	for _, v := range strings.Split(s, ",") {
		if v = strings.TrimSpace(v); v != "" {
			out = append(out, v)
		}
	}
	return out
}

func FilterByVerdict(s *protocol.Snapshot, keep ...string) *protocol.Snapshot {
	if len(keep) == 0 {
		return s
	}
	want := map[string]bool{}
	for _, k := range keep {
		want[k] = true
	}
	c := *s
	c.Units = nil
	kept := map[string]bool{}
	for _, u := range s.Units {
		if want[u.Verdict] {
			c.Units = append(c.Units, u)
			kept[u.Branch] = true
		}
	}
	c.Plan = nil
	for _, p := range s.Plan {
		if kept[p.Branch] {
			c.Plan = append(c.Plan, p)
		}
	}
	return &c
}
''', encoding="utf-8", newline="\n")
    p = repo / "cmd/landfirst/main.go"
    t = p.read_text(encoding="utf-8")
    t = t.replace('\t\tall      = flag.Bool(', '\t\tonly     = flag.String("only", "", "comma-separated verdicts to show")\n\t\tall      = flag.Bool(')
    t = t.replace("\t\tif *asJSON {\n\t\t\tenc", "\t\tsnap = tui.FilterByVerdict(snap, tui.ParseVerdictList(*only)...)\n\t\tif *asJSON {\n\t\t\tenc")
    p.write_text(t, encoding="utf-8", newline="\n")


def _ref_r1(repo: Path) -> None:
    for p in repo.rglob("*.go"):
        t = p.read_text(encoding="utf-8")
        n = re.sub(r"(?<![A-Za-z0-9_])Lines\(", "NonEmptyLines(", t)
        if n != t:
            p.write_text(n, encoding="utf-8", newline="\n")


def _ref_r2(repo: Path) -> None:
    p = repo / "internal/engine/state/derive.go"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "func IsStale(fetchHeadAt time.Time, threshold time.Duration) bool {\n\treturn time.Since(fetchHeadAt) > threshold",
        "func IsStale(fetchHeadAt, now time.Time, threshold time.Duration) bool {\n\treturn now.Sub(fetchHeadAt) > threshold"),
        encoding="utf-8", newline="\n")
    p = repo / "internal/model/build.go"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "state.IsStale(m.Freshness.FetchHeadAt, staleThreshold)",
        "state.IsStale(m.Freshness.FetchHeadAt, time.Now(), staleThreshold)"), encoding="utf-8", newline="\n")
    p = repo / "internal/engine/state/derive_test.go"
    p.write_text(re.sub(r"IsStale\((.*?), time\.Hour\)", r"IsStale(\1, time.Now(), time.Hour)",
                        p.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")


def _ref_restore(rel: str):
    def f(repo: Path) -> None:
        content = subprocess.run(["git", "show", f"{PINNED_COMMIT}:{rel}"], cwd=repo,
                                 capture_output=True, check=True).stdout
        (repo / rel).write_bytes(content)
    return f


REFS = {
    "B1-derive-bug": (_ref_restore("internal/engine/state/derive.go"), ""),
    "B2-severity-bug": (_ref_restore("internal/model/types.go"), ""),
    "F1-nothing-to-merge": (_ref_f1, ""),
    "F2-only-flag": (_ref_f2, ""),
    "R1-rename-lines": (_ref_r1, ""),
    "R2-isstale-now": (_ref_r2, ""),
    "Q1-try-callers": (None, "ANSWER: engine.baseRefOf, engine.refsRevision, engine.resolveGitPath, "
                       "engine.worktreeService.Add, git.Lines, git.candidateRemote, git.resolveBase, "
                       "git.resolveRemote, model.baseRef, model.buildForks, model.buildGraph, "
                       "model.buildUnits, model.collectCommits, model.collectWorktrees, model.dirtyState, "
                       "model.gitCapability, model.parseLog, model.probeMerge, model.rawDistance, "
                       "model.resolveGitPath"),
    "Q2-noprobe-conflicts": (None, "ANSWER: -1, set in buildPlan, printed as `not probed`"),
}


def selftest() -> None:
    root = BENCH / "selftest"
    if root.exists():
        rmtree_force(root)
    root.mkdir(parents=True)
    ok_all = True
    for tid, task in TASKS.items():
        neg = root / f"{tid}-neg"
        fresh_clone(neg, task)
        n_pass, _ = task["check"](neg, "")
        pos = root / f"{tid}-pos"
        fresh_clone(pos, task)
        fix, answer = REFS[tid]
        if fix:
            fix(pos)
        p_pass, p_detail = task["check"](pos, answer)
        good = (not n_pass) and p_pass
        ok_all &= good
        print(f"{tid:<22} untouched={'PASS' if n_pass else 'FAIL'} reference={'PASS' if p_pass else 'FAIL'}"
              f"  {'ok' if good else 'BROKEN'}  {'' if p_pass else p_detail[:300]}", flush=True)
    print("SELFTEST", "OK" if ok_all else "BROKEN")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    o = sub.add_parser("one")
    o.add_argument("task")
    o.add_argument("arm", choices=["A", "B"])
    o.add_argument("rep", type=int)
    o.add_argument("--pilot", action="store_true")
    m = sub.add_parser("matrix")
    m.add_argument("--reps", type=int, default=3)
    m.add_argument("--parallel", type=int, default=2)
    m.add_argument("--tasks", nargs="*", default=list(TASKS))
    m.add_argument("--seed", type=int, default=20261012)
    a = ap.parse_args()
    if a.cmd == "selftest":
        selftest()
    elif a.cmd == "one":
        print(summary_line(run_one(a.task, a.arm, a.rep, a.pilot)))
    else:
        matrix(a.reps, a.parallel, a.tasks, a.seed)


if __name__ == "__main__":
    main()
