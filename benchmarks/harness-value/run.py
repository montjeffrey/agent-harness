"""Runner for the harness-value benchmark (design: DESIGN.md).

  python run.py probe                         # isolation check, one cheap session per setup
  python run.py one TASK SETUP REP [--pilot]  # one run (SETUP: B, A, A+, M)
  python run.py matrix --reps 5 --parallel 2  # everything, resumable, spend-capped
  python run.py spent                         # list-price spend so far

Setups
  B   no harness; ~/.claude/CLAUDE.md excluded.
  A   per-run harness home + enroll, SessionStart/Read hooks, MCP server with
      deferred tools, ~/.claude/CLAUDE.md (the installed memory protocol).
  A+  A, plus the MCP server loaded eagerly (alwaysLoad) and WORKFLOW appended
      to the system prompt.
  M   as B, but a chain runs as ONE conversation (--resume); H family only.

Chains (H, R): one working tree for the whole chain. Each step is a fresh
`claude -p` (M: resumed). After a step its check runs; a failed step is repaired
to the reference state before the next step, so every setup starts every step
from the same code. Harness memory (A, A+) persists across steps.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from tasks_common import (APP_CONTROL_BLOCK, BENCH, REPOS, CheckBlocked, git,  # noqa: E402
                          repair_to)

OLD = HERE.parent / "token-efficiency"

RUNS = BENCH / "hv-runs"
RESULTS = BENCH / "hv-results.jsonl"
MODEL_CACHE = BENCH / "ahhome" / ".cache"
PIPX = Path(os.environ.get("AGENT_HARNESS_VENV")
            or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pipx" / "pipx" / "venvs" / "agent-harness-kit")
PY = PIPX / "Scripts" / "python.exe"
SITE = PIPX / "Lib" / "site-packages"
CLAUDE = shutil.which("claude") or "claude"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
MODEL = "claude-sonnet-5-5"
STEP_TIMEOUT_S = 2400
MAX_BUDGET_USD = "10"
SPEND_CAP_USD = float(os.environ.get("HV_SPEND_CAP", "150"))
SETUPS = ("B", "A", "A+", "M")
FAMILY_ORDER = ("H", "R", "D", "G", "C")

WORKFLOW = """\
This project uses agent-harness for memory and code navigation; its MCP tools
(mcp__agent-harness__*) are loaded. Work this way:
1. Before planning, call memory_context once with your task as the query. For
   questions about who calls what or what a change would break, use graph_find,
   graph_callers and graph_impact before falling back to grep.
2. When you settle a decision, find a non-obvious fact about the code, or finish
   a step, record it with memory_remember in one durable sentence. A later
   session will rely on it instead of rediscovering it.
3. Do not re-derive what memory already states unless you need to verify it.
"""

STRIP_ENV = {
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES", "CLAUDE_CODE_REPORT_FINDINGS",
    "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_EAGER_FLUSH", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_DISABLE_CRON",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "CLAUDE_CODE_TERMINAL_MCP_TOOLS",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_CODE_SIMPLE",
    "AGENT_HARNESS_AUTO_ENROLL", "AGENT_HARNESS_WORKSPACE", "ENABLE_TOOL_SEARCH",
}

_lock = threading.Lock()


# ------------------------------------------------------------------ tasks

def _old_tasks() -> dict:
    """The 2026-10-12 tasks as family C (already validated by that selftest)."""
    sys.path.insert(0, str(OLD))
    old = importlib.import_module("tasks")
    if old.PINNED_COMMIT != REPOS["landfirst"]["commit"]:
        raise RuntimeError("token-efficiency tasks are pinned to a different commit")
    return {f"C-{tid}": dict(family="C", repo="landfirst", setup=t["setup"],
                             prompt=t["prompt"], check=t["check"])
            for tid, t in old.TASKS.items()}


def all_tasks() -> dict:
    tasks = {}
    for name in ("tasks_h", "tasks_r", "tasks_d", "tasks_g"):
        try:
            tasks.update(importlib.import_module(name).TASKS)
        except ModuleNotFoundError:
            print(f"note: {name}.py not present; its family is skipped", file=sys.stderr)
    tasks.update(_old_tasks())
    return tasks


def setups_for(task: dict) -> tuple[str, ...]:
    return ("B", "A", "A+", "M") if task["family"] == "H" else ("B", "A", "A+")


# ------------------------------------------------------------------ setup

def fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


def rmtree_force(path: Path) -> None:
    for j in path.rglob(".cache"):
        if j.is_junction():
            subprocess.run(["cmd", "/c", "rmdir", str(j)], capture_output=True)

    def onexc(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, onexc=onexc)


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env.update(extra or {})
    return env


def fresh_clone(task: dict, repo: Path) -> str:
    spec = REPOS[task["repo"]]
    subprocess.run(["git", "clone", "-q", str(spec["pristine"]), str(repo)], check=True)
    git(repo, "checkout", "-q", "-B", "main", spec["commit"])
    if task.get("setup"):
        task["setup"](repo)
        name, email, date = git(repo, "log", "-1", "--format=%an%n%ae%n%cI").splitlines()
        env = dict(os.environ, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
                   GIT_COMMITTER_DATE=date)
        subprocess.run(["git", "-c", f"user.name={name}", "-c", f"user.email={email}",
                        "add", "-A"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "-c", f"user.name={name}", "-c", f"user.email={email}",
                        "commit", "-q", "--amend", "--no-edit", "--no-verify"],
                       cwd=repo, check=True, env=env)
    return git(repo, "rev-parse", "HEAD")


def harness_setup(run_dir: Path, repo: Path, eager: bool) -> tuple[list[str], float]:
    home = run_dir / "home"
    home.mkdir()
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(home / ".cache"), str(MODEL_CACHE)],
                   check=True, capture_output=True)
    env = child_env({"USERPROFILE": str(home), "HOME": str(home)})
    t0 = time.time()
    r = subprocess.run([str(PY), "-m", "harness.enroll", str(repo)], env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=1500)
    enroll_s = time.time() - t0
    (run_dir / "enroll.log").write_text(r.stdout + "\n--stderr--\n" + r.stderr, encoding="utf-8")
    if "[1/4] memory" not in r.stdout or "[3/4] graph      built" not in r.stdout:
        raise RuntimeError(f"enroll incomplete: {r.stdout[-500:]} {r.stderr[-500:]}")
    hook = lambda script: f"{fwd(PY)} {fwd(OLD / 'hookwrap.py')} {fwd(home)} {fwd(SITE / script)}"
    settings = {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
                                     "command": hook("hooks/claude/memory_context_hook.py")}]}],
        "PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command",
                                     "command": hook("hooks/claude/graph_read_guard.py")}]}],
    }}
    server = {"command": fwd(PY), "args": ["-m", "harness.mcp_server"],
              "env": {"PYTHONPATH": fwd(SITE), "USERPROFILE": str(home), "HOME": fwd(home)}}
    if eager:
        server["alwaysLoad"] = True
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    (run_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"agent-harness": server}},
                                                 indent=1), encoding="utf-8")
    extra = ["--settings", str(run_dir / "settings.json"),
             "--mcp-config", str(run_dir / "mcp.json")]
    if eager:
        (run_dir / "workflow.txt").write_text(WORKFLOW, encoding="utf-8")
        extra += ["--append-system-prompt-file", str(run_dir / "workflow.txt")]
    return extra, enroll_s


def control_setup(run_dir: Path) -> list[str]:
    settings = {"claudeMdExcludes": [fwd(Path.home() / ".claude" / "CLAUDE.md"),
                                     "**/.claude/CLAUDE.md"]}
    (run_dir / "settings.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    return ["--settings", str(run_dir / "settings.json")]


# ------------------------------------------------------------------ one session

USAGE_KEYS = ("inputTokens", "outputTokens", "cacheCreationInputTokens", "cacheReadInputTokens")


def session_profile(session_id: str) -> dict:
    """Tool calls by name and model API calls, from the session log and its
    subagent transcripts. ``api_calls`` counts distinct assistant message ids:
    the result JSON's ``num_turns`` counts conversation turns, not model calls,
    so it cannot give context per call."""
    calls: Counter = Counter()
    message_ids: set = set()
    files = list(CLAUDE_PROJECTS.glob(f"*/{session_id}.jsonl"))
    for f in list(files):
        sub = f.parent / session_id
        if sub.is_dir():
            files += sorted(sub.rglob("*.jsonl"))
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                msg = json.loads(line).get("message")
            except ValueError:
                continue
            if (isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("id")
                    and msg.get("model") != "<synthetic>"):
                message_ids.add(msg["id"])
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                for b in msg["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        calls[b.get("name")] += 1
    harness = {k.removeprefix("mcp__agent-harness__"): v for k, v in calls.items()
               if str(k).startswith("mcp__agent-harness__")}
    return {"tool_calls": dict(calls), "harness_calls": harness,
            "toolsearch_calls": calls.get("ToolSearch", 0), "api_calls": len(message_ids)}


def run_session(repo: Path, prompt: str, extra: list[str], resume: str | None,
                log_prefix: Path) -> dict:
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json", "--model", MODEL,
           "--setting-sources", "project", "--strict-mcp-config",
           "--permission-mode", "bypassPermissions", "--max-budget-usd", MAX_BUDGET_USD, *extra]
    if resume:
        cmd += ["--resume", resume]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=STEP_TIMEOUT_S, env=child_env())
        stdout, stderr, rc, timed_out = p.stdout, p.stderr, p.returncode, False
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr, rc, timed_out = "timeout", -1, True
    log_prefix.with_suffix(".stdout.json").write_text(stdout or "", encoding="utf-8")
    log_prefix.with_suffix(".stderr.txt").write_text(stderr or "", encoding="utf-8")
    out = {}
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    mu = out.get("modelUsage") or {}
    usage = {k: sum(int(m.get(k) or 0) for m in mu.values()) for k in USAGE_KEYS}
    rec = {"wall_s": round(time.time() - t0, 1), "exit_code": rc, "timed_out": timed_out,
           "subtype": out.get("subtype"), "is_error": out.get("is_error"),
           "num_turns": out.get("num_turns") or 0, "session_id": out.get("session_id"),
           "cost_usd": out.get("total_cost_usd") or 0.0, "models": sorted(mu), **usage,
           "result_text": out.get("result") or ""}
    if rec["session_id"]:
        rec.update(session_profile(rec["session_id"]))
    return rec


def _delta(cur: dict, prev: dict | None) -> dict:
    """A resumed conversation reports cumulative modelUsage and total_cost_usd;
    keep this step's share. num_turns is already per invocation (verified on a
    six-step pilot: 11, 11, 11, 4, 10, 7), so it is left as reported."""
    if not prev:
        return cur
    out = dict(cur)
    # The resumed session's log also accumulates, so api_calls is cumulative too.
    for k in (*USAGE_KEYS, "cost_usd", "api_calls"):
        if (cur.get(k) or 0) >= (prev.get(k) or 0):
            out[k] = (cur.get(k) or 0) - (prev.get(k) or 0)
    return out


def run_check(check, repo: Path, result_text: str) -> tuple[bool, str, bool]:
    """Run a step's check; an Application Control block is retried, and one that
    persists is reported as blocked (the run is then incomplete and redone),
    never as a failed step."""
    for attempt in range(3):
        try:
            passed, detail = check(repo, result_text)
        except CheckBlocked as e:
            passed, detail = False, f"{APP_CONTROL_BLOCK}: {e}"
        except Exception as e:  # a crashing check is a failed step
            return False, f"check crashed: {e!r}", False
        if APP_CONTROL_BLOCK not in detail:
            return passed, detail, False
        time.sleep(5 * (attempt + 1))
    return False, detail, True


# ------------------------------------------------------------------ one run

def run_one(task_id: str, setup: str, rep: int, pilot: bool = False) -> dict:
    task = all_tasks()[task_id]
    if setup not in setups_for(task):
        raise ValueError(f"setup {setup} does not apply to {task_id}")
    run_id = f"{task_id}_{setup.replace('+', 'p')}_r{rep}_{datetime.now():%m%d%H%M%S}"
    run_dir = RUNS / run_id
    repo = run_dir / "repo"
    run_dir.mkdir(parents=True)
    head = fresh_clone(task, repo)
    enroll_s = None
    if setup in ("A", "A+"):
        extra, enroll_s = harness_setup(run_dir, repo, eager=(setup == "A+"))
    else:
        extra = control_setup(run_dir)

    steps = task.get("steps") or [dict(prompt=task["prompt"], check=task["check"])]
    measure_from = task.get("measure_from", 0)
    started = datetime.now(timezone.utc).isoformat()
    step_recs, resume, prev_cum = [], None, None
    for n, step in enumerate(steps, start=1):
        s = run_session(repo, step["prompt"], extra, resume if setup == "M" else None,
                        run_dir / f"step{n}")
        if setup == "M":
            cum, s = s, _delta(s, prev_cum)
            prev_cum, resume = cum, cum.get("session_id") or resume
        passed, detail, blocked = run_check(step["check"], repo, s.pop("result_text"))
        s.update(step=n, measured=n - 1 >= measure_from, passed=passed, check_detail=detail[:1500],
                 repaired=False, check_blocked=blocked)
        if not passed and n < len(steps):
            try:
                repair_to(repo, task["repo"], task_id, n)
                s["repaired"] = True
            except Exception as e:
                s["repair_error"] = repr(e)
        step_recs.append(s)

    meas = [s for s in step_recs if s["measured"]]
    tot = lambda k: sum(s.get(k) or 0 for s in meas)
    total_input = tot("inputTokens") + tot("cacheCreationInputTokens") + tot("cacheReadInputTokens")
    rec = {
        "run_id": run_id, "task": task_id, "family": task["family"], "setup": setup, "rep": rep,
        "pilot": pilot, "model": MODEL, "started": started, "head": head,
        "enroll_s": enroll_s and round(enroll_s, 1),
        "steps": step_recs,
        "passed": all(s["passed"] for s in meas), "steps_passed": sum(s["passed"] for s in meas),
        "steps_measured": len(meas),
        "input_tokens": tot("inputTokens"), "cache_write_tokens": tot("cacheCreationInputTokens"),
        "cache_read_tokens": tot("cacheReadInputTokens"), "output_tokens": tot("outputTokens"),
        "total_tokens": total_input + tot("outputTokens"),
        "turns": tot("num_turns"), "wall_s": round(tot("wall_s"), 1),
        "ctx_per_turn": round(total_input / tot("num_turns")) if tot("num_turns") else None,
        "api_calls": tot("api_calls"),
        "ctx_per_call": round(total_input / tot("api_calls")) if tot("api_calls") else None,
        "cost_usd": round(tot("cost_usd"), 4),
        "cost_usd_all_steps": round(sum(s.get("cost_usd") or 0 for s in step_recs), 4),
        "harness_calls": sum(sum((s.get("harness_calls") or {}).values()) for s in meas),
        "toolsearch_calls": tot("toolsearch_calls"),
        "complete": all(s.get("subtype") and not s.get("timed_out") and not s.get("check_blocked")
                        for s in step_recs),
    }
    with _lock, RESULTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    junction = run_dir / "home" / ".cache"
    if junction.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(junction)], capture_output=True)
    return rec


def summary_line(r: dict) -> str:
    return (f"{r['task']:<24} {r['setup']:<2} r{r['rep']}  pass={r['steps_passed']}/{r['steps_measured']} "
            f"tok={r['total_tokens']:>11,} ctx/call={r.get('ctx_per_call') or 0:>7,} "
            f"cost=${r['cost_usd']:.3f} calls={r.get('api_calls')} harness={r['harness_calls']} "
            f"wall={r['wall_s']}s")


# ------------------------------------------------------------------ matrix

def _records() -> list[dict]:
    if not RESULTS.exists():
        return []
    return [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]


def spent_usd() -> float:
    return sum(r.get("cost_usd_all_steps") or 0 for r in _records())


def done_keys() -> set:
    return {(r["task"], r["setup"], r["rep"]) for r in _records()
            if r.get("complete") and not r.get("pilot")}


def _safe_run(t, s, rep, pilot=False):
    if spent_usd() >= SPEND_CAP_USD:
        print(f"SKIPPED {t} {s} r{rep}: spend cap ${SPEND_CAP_USD:.0f} reached", flush=True)
        return None
    try:
        return run_one(t, s, rep, pilot)
    except Exception as e:
        print(f"RUN FAILED {t} {s} r{rep}: {e!r}", flush=True)
        return None


def matrix(reps: int, m_reps: int, parallel: int, families: list[str], seed: int) -> None:
    tasks = all_tasks()
    rng = random.Random(seed)
    sched = []
    for fam in [f for f in FAMILY_ORDER if f in families]:
        fam_tasks = [t for t, v in tasks.items() if v["family"] == fam]
        for rep in range(1, reps + 1):
            order = list(fam_tasks)
            rng.shuffle(order)
            for t in order:
                setups = [s for s in setups_for(tasks[t]) if s != "M" or rep <= m_reps]
                rng.shuffle(setups)
                sched += [(t, s, rep) for s in setups]
    done = done_keys()
    todo = [x for x in sched if x not in done]
    print(f"{len(todo)} runs to do ({len(sched) - len(todo)} done); spent ${spent_usd():.2f} "
          f"of ${SPEND_CAP_USD:.0f}", flush=True)
    with ThreadPoolExecutor(parallel) as ex:
        for rec in ex.map(lambda x: _safe_run(*x), todo):
            if rec:
                print(summary_line(rec), flush=True)


# ------------------------------------------------------------------ probe

PROBE_PROMPT = ("Reply with three lines and do nothing else. Line 1: 'TOOLS:' then a comma list of "
                "every tool name you can call whose name starts with mcp__ (or 'none'). Line 2: "
                "'DEFERRED:' then yes if any tool is listed only by name and needs ToolSearch, else no. "
                "Line 3: 'INSTR:' then yes if your instructions mention agent-harness memory_context, else no.")


def probe() -> None:
    task = dict(family="P", repo="landfirst", setup=None)
    for setup in ("B", "A", "A+"):
        run_dir = RUNS / f"probe_{setup.replace('+', 'p')}_{datetime.now():%m%d%H%M%S}"
        repo = run_dir / "repo"
        run_dir.mkdir(parents=True)
        fresh_clone(task, repo)
        extra = (harness_setup(run_dir, repo, eager=(setup == "A+"))[0]
                 if setup in ("A", "A+") else control_setup(run_dir))
        s = run_session(repo, PROBE_PROMPT, extra, None, run_dir / "probe")
        print(f"--- {setup}: cost ${s['cost_usd']:.3f}\n{s['result_text'].strip()}", flush=True)
        junction = run_dir / "home" / ".cache"
        if junction.exists():
            subprocess.run(["cmd", "/c", "rmdir", str(junction)], capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("spent")
    sub.add_parser("list")
    o = sub.add_parser("one")
    o.add_argument("task")
    o.add_argument("setup", choices=SETUPS)
    o.add_argument("rep", type=int)
    o.add_argument("--pilot", action="store_true")
    m = sub.add_parser("matrix")
    m.add_argument("--reps", type=int, default=5)
    m.add_argument("--m-reps", type=int, default=3)
    m.add_argument("--parallel", type=int, default=2)
    m.add_argument("--families", nargs="*", default=list(FAMILY_ORDER))
    m.add_argument("--seed", type=int, default=20261015)
    a = ap.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    if a.cmd == "probe":
        probe()
    elif a.cmd == "spent":
        print(f"${spent_usd():.2f} of ${SPEND_CAP_USD:.0f}")
    elif a.cmd == "list":
        for tid, t in all_tasks().items():
            print(f"{t['family']}  {tid:<26} steps={len(t.get('steps') or [1])}  setups={','.join(setups_for(t))}")
    elif a.cmd == "one":
        print(summary_line(run_one(a.task, a.setup, a.rep, a.pilot)))
    else:
        matrix(a.reps, a.m_reps, a.parallel, a.families, a.seed)


if __name__ == "__main__":
    main()
