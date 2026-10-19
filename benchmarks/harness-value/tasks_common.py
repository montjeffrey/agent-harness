"""Shared contract and helpers for the harness-value task families.

Every family module (tasks_h.py, tasks_r.py, tasks_d.py, tasks_g.py) exposes
``TASKS: dict[str, dict]``. A task is one of two shapes.

Single-session task::

    dict(
        family="D" | "G",
        repo="landfirst" | "hugo",          # key of REPOS below
        setup=callable(clone) | None,       # edits folded into HEAD before the run
        prompt=str,
        check=callable(clone, result_text) -> (passed: bool, detail: str),
        reference=callable(clone) -> str,   # selftest: applies the fix (if any) and
                                            # returns the result text a correct agent
                                            # would print (e.g. its ANSWER: line)
    )

Multi-session task (a chain)::

    dict(
        family="H" | "R",
        repo="landfirst",
        setup=callable(clone) | None,
        steps=[dict(prompt=str, check=callable, reference=callable), ...],
        measure_from=0,                     # index of the first scored step; earlier
                                            # steps are setup (R scores session 2 only)
    )

Rules every check must follow:

- It runs after the agent exits, in the agent's working tree.
- Hidden tests are written with ``hidden_go_test`` and are always removed again,
  pass or fail, so a later step never sees an earlier step's hidden test.
- It never trusts the agent to have run anything; it runs what it needs.
- It is deterministic and must fail on the untouched tree (selftest checks this).

Repairs: a chain step that fails is repaired by ``restore_reference`` before the
next step, so every setup starts every step from identical code. Reference
states are commits ``ref/<task>/<n>`` (state *after* step n) built by
``build_reference_branches`` from each step's ``reference`` callable.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from pathlib import Path

BENCH = Path(os.environ.get("AHBENCH_DIR")
             or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Temp" / "ahbench")

REPOS = {
    "landfirst": dict(pristine=BENCH / "pristine-landfirst",
                      commit="4eb6172d08cb421e1f9d49d440b400ce89b82850"),
    "hugo": dict(pristine=BENCH / "pristine-hugo",
                 commit="e6d2712ee062321dc2fc49e963597dd5a6157660"),
}

GO_TIMEOUT = 900

# Windows Application Control intermittently refuses to run freshly built Go
# test binaries. That says nothing about the agent's work, so it must never be
# scored as a failed check.
APP_CONTROL_BLOCK = "Application Control policy has blocked"


class CheckBlocked(RuntimeError):
    """A check could not run (environment block); the step needs a re-check."""


def sh(cwd: Path, *args: str, timeout: int = GO_TIMEOUT, env: dict | None = None
       ) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace", env=env)


def git(cwd: Path, *args: str) -> str:
    r = sh(cwd, "git", *args, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"anchor not unique/missing in {path}: {old[:60]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def write_file(clone: Path, rel: str, body: str) -> None:
    p = clone / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8", newline="\n")


def build_and_vet(clone: Path) -> tuple[bool, str]:
    for args in (("build", "./..."), ("vet", "./...")):
        r = sh(clone, "go", *args)
        if r.returncode != 0:
            return False, f"go {args[0]} failed: " + (r.stdout + r.stderr)[-1500:]
    return True, ""


def hidden_go_test(clone: Path, pkg_dir: str, package_clause: str, body: str,
                   run: str = "^TestHidden") -> tuple[bool, str]:
    """Drop a hidden test into ``pkg_dir``, run only it, always remove it.

    ``body`` is the file content after the package clause (imports + tests).
    Test functions must be named ``TestHidden...``.
    """
    name = f"zz_hidden_{uuid.uuid4().hex[:8]}_test.go"
    path = clone / pkg_dir / name
    path.write_text(f"package {package_clause}\n\n{body}", encoding="utf-8", newline="\n")
    try:
        for attempt in range(4):
            # Application Control has refused stripped and unstripped test
            # binaries at different times, so alternate between them and back
            # off; a block that persists is never scored as a failure.
            ldflags = ["-ldflags=-s -w"] if attempt % 2 == 0 else []
            r = sh(clone, "go", "test", *ldflags, f"./{pkg_dir}/", "-run", run, "-count=1")
            out = (r.stdout + r.stderr)[-2000:]
            if APP_CONTROL_BLOCK not in out:
                break
            time.sleep(5 * (attempt + 1))
        else:
            raise CheckBlocked(out)
        if r.returncode != 0:
            return False, out
        if "no tests to run" in out:
            return False, "hidden test did not run: " + out
        return True, out
    finally:
        path.unlink(missing_ok=True)


def answer_line(text: str) -> str:
    """The last ``ANSWER:`` line of the agent's result (markdown decoration ignored)."""
    lines = [l for l in (text or "").splitlines()
             if l.strip().strip("*`").upper().startswith("ANSWER:")]
    return lines[-1].split(":", 1)[1].strip() if lines else ""


def norm_set(text: str) -> set[str]:
    """Comma/space/newline-separated identifiers, backticks stripped, lower-cased."""
    return {t.strip("`*. ").lower() for t in re.split(r"[,\s]+", text or "") if t.strip("`*. ")}


def read(clone: Path, rel: str) -> str:
    p = clone / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def commit_all(clone: Path, message: str) -> str:
    """Commit the working tree as the fixed benchmark identity (reference builds only)."""
    git(clone, "add", "-A")
    git(clone, "-c", "user.name=bench", "-c", "user.email=bench@example.invalid",
        "commit", "-q", "--allow-empty", "--no-verify", "-m", message)
    return git(clone, "rev-parse", "HEAD")


def refs_repo(repo_key: str) -> Path:
    """Bare repo holding ``ref/<task>/<n>`` branches. Kept apart from the
    pristine clone so a run's clone never contains reference solutions."""
    return BENCH / f"refs-{repo_key}.git"


def repair_to(clone: Path, repo_key: str, task_id: str, step: int) -> str:
    """Fetch only the reference state after ``step`` and make the tree match it."""
    git(clone, "fetch", "-q", str(refs_repo(repo_key)), f"ref/{task_id}/{step}")
    sha = git(clone, "rev-parse", "FETCH_HEAD")
    restore_reference(clone, sha)
    return sha


def restore_reference(clone: Path, ref_commit: str) -> None:
    """Make the working tree match ``ref_commit`` exactly, keeping HEAD and any
    untracked harness state (.agent-harness/, graphify-out/) in place."""
    git(clone, "checkout", ref_commit, "--", ".")
    tracked_now = set(git(clone, "ls-files").splitlines())
    tracked_ref = set(git(clone, "ls-tree", "-r", "--name-only", ref_commit).splitlines())
    for rel in tracked_now - tracked_ref:
        git(clone, "rm", "-q", "--cached", rel)
        (clone / rel).unlink(missing_ok=True)
    for line in git(clone, "status", "--porcelain", "--untracked-files=all").splitlines():
        rel = line[3:]
        if line.startswith("??") and not rel.startswith((".agent-harness", "graphify-out")):
            (clone / rel).unlink(missing_ok=True)
    git(clone, "reset", "-q")
