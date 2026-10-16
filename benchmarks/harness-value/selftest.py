"""Validate task definitions without any model call; build reference branches.

  python selftest.py tasks_h            # every task in the module
  python selftest.py tasks_h H-sugg     # one task

Single-session task: fresh clone -> setup -> check must FAIL on the untouched
tree (with an empty result text) -> reference -> check must PASS on the
reference's result text.

Chain: fresh clone -> setup -> for each step n: check FAILS, reference applies,
check PASSES, the tree is committed and pushed to refs-<repo>.git as
``ref/<task>/<n>`` (the repair target used by the runner).
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks_common import REPOS, commit_all, git, refs_repo  # noqa: E402


def fresh_clone(repo_key: str, dest: Path, setup) -> None:
    spec = REPOS[repo_key]
    subprocess.run(["git", "clone", "-q", str(spec["pristine"]), str(dest)], check=True)
    git(dest, "checkout", "-q", "-B", "main", spec["commit"])
    if setup:
        setup(dest)
        commit_all(dest, "setup")


def ensure_refs_repo(repo_key: str) -> Path:
    bare = refs_repo(repo_key)
    if not bare.exists():
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


def _rm(path: Path) -> None:
    def onexc(func, p, _exc):
        Path(p).chmod(0o700)
        func(p)
    shutil.rmtree(path, onexc=onexc)


def check_single(tid: str, task: dict, work: Path) -> bool:
    clone = work / "repo"
    fresh_clone(task["repo"], clone, task.get("setup"))
    ok_before, detail_before = task["check"](clone, "")
    answer = task["reference"](clone)
    ok_after, detail_after = task["check"](clone, answer or "")
    good = (not ok_before) and ok_after
    print(f"{'OK  ' if good else 'FAIL'} {tid}: untouched={'pass' if ok_before else 'fail'} "
          f"reference={'pass' if ok_after else 'fail'}"
          + ("" if good else f"\n  before: {detail_before[:300]}\n  after: {detail_after[:600]}"),
          flush=True)
    return good


def check_chain(tid: str, task: dict, work: Path) -> bool:
    clone = work / "repo"
    fresh_clone(task["repo"], clone, task.get("setup"))
    bare = ensure_refs_repo(task["repo"])
    all_good = True
    for n, step in enumerate(task["steps"], start=1):
        t0 = time.time()
        ok_before, detail_before = step["check"](clone, "")
        answer = step["reference"](clone)
        ok_after, detail_after = step["check"](clone, answer or "")
        good = (not ok_before) and ok_after
        all_good &= good
        commit_all(clone, f"reference {tid} step {n}")
        git(clone, "push", "-q", "--force", str(bare), f"HEAD:refs/heads/ref/{tid}/{n}")
        print(f"{'OK  ' if good else 'FAIL'} {tid} step {n}: "
              f"untouched={'pass' if ok_before else 'fail'} "
              f"reference={'pass' if ok_after else 'fail'} ({time.time() - t0:.0f}s)"
              + ("" if good else f"\n  before: {detail_before[:300]}\n  after: {detail_after[:600]}"),
              flush=True)
    return all_good


def main() -> int:
    module = importlib.import_module(sys.argv[1])
    only = set(sys.argv[2:])
    tasks = {k: v for k, v in module.TASKS.items() if not only or k in only}
    results = []
    for tid, task in tasks.items():
        work = Path(tempfile.mkdtemp(prefix=f"hv-selftest-{tid}-"))
        try:
            fn = check_chain if "steps" in task else check_single
            results.append(fn(tid, task, work))
        except Exception as exc:  # a crashing task definition is a failed selftest
            print(f"FAIL {tid}: crashed: {exc!r}", flush=True)
            results.append(False)
        finally:
            _rm(work)
    print(f"{sum(results)}/{len(results)} task(s) valid", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
