"""Benchmark task definitions for the agent-harness token-efficiency study.

Target: github.com/montjeffrey/landfirst pinned at PINNED_COMMIT.

Each task has:
  prompt  -- identical text for both arms
  setup   -- optional edit applied to the fresh clone, then folded into HEAD
             with `git commit --amend` so the injected bug is not visible as
             an uncommitted diff or a separate commit
  check   -- objective pass/fail run after the agent exits; hidden tests are
             copied in only at check time
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PINNED_COMMIT = "4eb6172d08cb421e1f9d49d440b400ce89b82850"

GO_TIMEOUT = 900


def sh(cwd: Path, *args: str, timeout: int = GO_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"setup anchor not unique/missing in {path}: {old[:60]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def write_hidden(clone: Path, rel: str, body: str) -> None:
    (clone / rel).write_text(body, encoding="utf-8", newline="\n")


def go_ok(clone: Path, *args: str) -> tuple[bool, str]:
    r = sh(clone, "go", *args)
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def build_and_vet(clone: Path) -> tuple[bool, str]:
    ok, out = go_ok(clone, "build", "./...")
    if not ok:
        return False, "go build failed: " + out
    ok, out = go_ok(clone, "vet", "./...")
    if not ok:
        return False, "go vet failed: " + out
    return True, ""


def answer_line(text: str) -> str:
    lines = [l for l in (text or "").splitlines() if l.strip().strip("*`").upper().startswith("ANSWER:")]
    return lines[-1].split(":", 1)[1].strip() if lines else ""


# --------------------------------------------------------------------------
# B1  bug fix, covered by existing tests
# --------------------------------------------------------------------------

def setup_b1(clone: Path) -> None:
    replace_once(clone / "internal/engine/state/derive.go",
        "\tcase o.UpstreamGone && o.Unlanded > 0:\n\t\treturn Orphaned\n\tcase o.UpstreamGone:\n\t\treturn SafeToDelete\n",
        "\tcase o.UpstreamGone:\n\t\treturn SafeToDelete\n\tcase o.UpstreamGone && o.Unlanded > 0:\n\t\treturn Orphaned\n")


HIDDEN_B1 = '''package state

import "testing"

func TestBenchHiddenOrphanedPrecedence(t *testing.T) {
	cases := []struct {
		o    Observation
		want Verdict
	}{
		{Observation{Op: OpNone, UpstreamConfigured: true, UpstreamGone: true, Unlanded: 3}, Orphaned},
		{Observation{Op: OpNone, UpstreamConfigured: true, UpstreamGone: true, Unlanded: 0}, SafeToDelete},
		{Observation{Op: OpRebase, UpstreamConfigured: true, UpstreamGone: true, Unlanded: 3}, InProgress},
		{Observation{Op: OpNone, UpstreamConfigured: true, UpstreamGone: true, UpstreamMismatch: true, Unlanded: 1}, Orphaned},
		{Observation{Op: OpNone, UpstreamConfigured: true, Ahead: 1, Behind: 1}, Diverged},
		{Observation{Op: OpNone, UpstreamConfigured: false, Unlanded: 2}, NeverPushed},
	}
	for i, c := range cases {
		if got := Derive(c.o); got != c.want {
			t.Fatalf("case %d: Derive(%+v) = %s, want %s", i, c.o, got, c.want)
		}
	}
}
'''


def check_b1(clone: Path, result_text: str) -> tuple[bool, str]:
    write_hidden(clone, "internal/engine/state/zz_bench_hidden_test.go", HIDDEN_B1)
    ok, out = build_and_vet(clone)
    if not ok:
        return False, out
    ok, out = go_ok(clone, "test", "-count=1", "./internal/engine/state/")
    return ok, "" if ok else "state tests failed: " + out


# --------------------------------------------------------------------------
# B2  bug fix, NOT covered by existing tests (symptom only)
# --------------------------------------------------------------------------

def setup_b2(clone: Path) -> None:
    replace_once(clone / "internal/model/types.go",
        "\tcase 4, 5, 6: // Diverged, Unpushed, Detached\n\t\treturn SeverityWarn\n\tcase 7: // SafeToDelete\n",
        "\tcase 4, 5: // Diverged, Unpushed\n\t\treturn SeverityWarn\n\tcase 6, 7: // Detached, SafeToDelete\n")


HIDDEN_B2 = '''package model

import "testing"

func TestBenchHiddenSeverityTable(t *testing.T) {
	want := map[Verdict]Severity{
		InProgress: SeverityBad, Orphaned: SeverityBad, WrongUpstream: SeverityBad, NeverPushed: SeverityBad,
		Diverged: SeverityWarn, Unpushed: SeverityWarn, Detached: SeverityWarn,
		SafeToDelete: SeverityInfo, Behind: SeverityDim, Landed: SeverityOK,
	}
	for v, s := range want {
		if got := v.Severity(); got != s {
			t.Fatalf("%s.Severity() = %d, want %d", v, got, s)
		}
	}
}
'''


def check_b2(clone: Path, result_text: str) -> tuple[bool, str]:
    write_hidden(clone, "internal/model/zz_bench_hidden_test.go", HIDDEN_B2)
    ok, out = build_and_vet(clone)
    if not ok:
        return False, out
    ok, out = go_ok(clone, "test", "-count=1", "-run", "TestBenchHidden", "./internal/model/")
    return ok, "" if ok else "severity test failed: " + out


# --------------------------------------------------------------------------
# F1  small feature in the plain report
# --------------------------------------------------------------------------

HIDDEN_F1 = '''package tui

import (
	"strings"
	"testing"

	"github.com/montjeffrey/landfirst/internal/protocol"
)

func TestBenchHiddenNothingToMerge(t *testing.T) {
	snap := &protocol.Snapshot{
		Repo:  protocol.RepoInfo{Name: "r", Base: "main"},
		Units: []protocol.Unit{{Branch: "feat/a", Verdict: "landed", Detail: "in sync"}},
	}
	out := PlainReport(snap)
	if !strings.Contains(out, "MERGE ORDER\\n  nothing to merge\\n") {
		t.Fatalf("want MERGE ORDER + nothing to merge when units exist but plan is empty:\\n%q", out)
	}
	if strings.Count(out, "MERGE ORDER") != 1 {
		t.Fatalf("MERGE ORDER printed more than once:\\n%s", out)
	}
	empty := PlainReport(&protocol.Snapshot{Repo: protocol.RepoInfo{Name: "r", Base: "main"}})
	if strings.Contains(empty, "MERGE ORDER") || strings.Contains(empty, "nothing to merge") {
		t.Fatalf("no units: the MERGE ORDER section must be omitted:\\n%s", empty)
	}
	withPlan := PlainReport(&protocol.Snapshot{
		Repo:  protocol.RepoInfo{Name: "r", Base: "main"},
		Units: []protocol.Unit{{Branch: "feat/a", Verdict: "unpushed"}},
		Plan:  []protocol.PlanItem{{Order: 1, Branch: "feat/a", Conflicts: 0}},
	})
	if strings.Contains(withPlan, "nothing to merge") {
		t.Fatalf("plan present: must not print nothing to merge:\\n%s", withPlan)
	}
}
'''


def check_f1(clone: Path, result_text: str) -> tuple[bool, str]:
    write_hidden(clone, "internal/tui/zz_bench_hidden_test.go", HIDDEN_F1)
    ok, out = build_and_vet(clone)
    if not ok:
        return False, out
    ok, out = go_ok(clone, "test", "-count=1", "-run", "PlainReport|TestBenchHidden", "./internal/tui/")
    return ok, "" if ok else "tui tests failed: " + out


# --------------------------------------------------------------------------
# F2  feature spanning the CLI and the tui package
# --------------------------------------------------------------------------

HIDDEN_F2 = '''package tui

import (
	"testing"

	"github.com/montjeffrey/landfirst/internal/protocol"
)

func TestBenchHiddenFilterByVerdict(t *testing.T) {
	in := &protocol.Snapshot{
		Repo: protocol.RepoInfo{Name: "r", Base: "main"},
		Units: []protocol.Unit{
			{Branch: "a", Verdict: "never-pushed"},
			{Branch: "b", Verdict: "landed"},
			{Branch: "c", Verdict: "diverged"},
		},
		Plan: []protocol.PlanItem{{Order: 1, Branch: "a"}, {Order: 2, Branch: "c"}},
	}
	out := FilterByVerdict(in, "never-pushed", "landed")
	if len(out.Units) != 2 || out.Units[0].Branch != "a" || out.Units[1].Branch != "b" {
		t.Fatalf("units = %+v", out.Units)
	}
	if len(out.Plan) != 1 || out.Plan[0].Branch != "a" {
		t.Fatalf("plan should drop filtered-out branches, got %+v", out.Plan)
	}
	if len(in.Units) != 3 || len(in.Plan) != 2 || in.Units[2].Branch != "c" {
		t.Fatalf("input snapshot was mutated")
	}
	if all := FilterByVerdict(in); len(all.Units) != 3 {
		t.Fatalf("no verdicts given: want unfiltered, got %d units", len(all.Units))
	}
}
'''


def check_f2(clone: Path, result_text: str) -> tuple[bool, str]:
    main = (clone / "cmd/landfirst/main.go").read_text(encoding="utf-8")
    if not re.search(r'flag\.String\(\s*"only"', main):
        return False, "no -only string flag in cmd/landfirst/main.go"
    if "FilterByVerdict" not in main:
        return False, "main.go does not call FilterByVerdict"
    write_hidden(clone, "internal/tui/zz_bench_hidden_test.go", HIDDEN_F2)
    ok, out = build_and_vet(clone)
    if not ok:
        return False, out
    ok, out = go_ok(clone, "test", "-count=1", "-run", "PlainReport|TestBenchHidden", "./internal/tui/")
    return ok, "" if ok else "tui tests failed: " + out


# --------------------------------------------------------------------------
# R1  rename touching every caller
# --------------------------------------------------------------------------

HIDDEN_R1 = '''package git

import "testing"

func TestBenchHiddenNonEmptyLines(t *testing.T) {
	if got := NonEmptyLines(".", "this-is-not-a-git-subcommand"); len(got) != 0 {
		t.Fatalf("failed command should yield no lines, got %v", got)
	}
}
'''


def check_r1(clone: Path, result_text: str) -> tuple[bool, str]:
    for p in clone.rglob("*.go"):
        if "graphify-out" in p.parts or ".agent-harness" in p.parts:
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?<![A-Za-z0-9_])Lines\(", txt):
            return False, f"old name Lines( still present in {p.relative_to(clone)}"
    if not re.search(r"func NonEmptyLines\(", (clone / "internal/git/git.go").read_text(encoding="utf-8")):
        return False, "func NonEmptyLines not defined in internal/git/git.go"
    write_hidden(clone, "internal/git/zz_bench_hidden_test.go", HIDDEN_R1)
    ok, out = build_and_vet(clone)
    if not ok:
        return False, out
    ok, out = go_ok(clone, "test", "-count=1", "./internal/git/")
    return ok, "" if ok else "git tests failed: " + out


# --------------------------------------------------------------------------
# R2  signature change touching callers and tests
# --------------------------------------------------------------------------

HIDDEN_R2 = '''package state

import (
	"testing"
	"time"
)

func TestBenchHiddenIsStaleNow(t *testing.T) {
	t0 := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
	if !IsStale(t0, t0.Add(2*time.Hour), time.Hour) {
		t.Fatal("2h old fetch with 1h threshold must be stale")
	}
	if IsStale(t0, t0.Add(2*time.Minute), time.Hour) {
		t.Fatal("2m old fetch with 1h threshold must not be stale")
	}
	if !IsStale(time.Time{}, t0, time.Hour) {
		t.Fatal("never fetched must be stale")
	}
}
'''


def check_r2(clone: Path, result_text: str) -> tuple[bool, str]:
    write_hidden(clone, "internal/engine/state/zz_bench_hidden_test.go", HIDDEN_R2)
    ok, out = build_and_vet(clone)
    if not ok:
        return False, out
    ok, out = go_ok(clone, "test", "-count=1", "./internal/engine/state/")
    return ok, "" if ok else "state tests failed: " + out


# --------------------------------------------------------------------------
# Q1  codebase question: callers (ground truth from a go/ast walk, gocallers/)
# --------------------------------------------------------------------------

Q1_TRUTH = {
    "baserefof", "refsrevision", "resolvegitpath", "add", "lines", "candidateremote",
    "resolvebase", "resolveremote", "baseref", "buildforks", "buildgraph", "buildunits",
    "collectcommits", "collectworktrees", "dirtystate", "gitcapability", "parselog",
    "probemerge", "rawdistance",
}


def check_q1(clone: Path, result_text: str) -> tuple[bool, str]:
    ans = answer_line(result_text)
    if not ans:
        return False, "no ANSWER: line"
    names = set()
    for tok in re.split(r"[,\s]+", ans):
        tok = tok.strip("`'\"()[]{}*;.")
        if tok:
            names.add(tok.split(".")[-1].lower())
    missing, extra = Q1_TRUTH - names, names - Q1_TRUTH
    if missing or extra:
        return False, f"missing={sorted(missing)} extra={sorted(extra)}"
    return True, ""


# --------------------------------------------------------------------------
# Q2  codebase question: data flow
# --------------------------------------------------------------------------

def check_q2(clone: Path, result_text: str) -> tuple[bool, str]:
    ans = answer_line(result_text)
    if not ans:
        return False, "no ANSWER: line"
    low = ans.lower()
    need = {"-1": "-1" in low, "buildPlan": "buildplan" in low, "not probed": "not probed" in low}
    miss = [k for k, v in need.items() if not v]
    return (not miss), ("missing " + ", ".join(miss)) if miss else ""


QUESTION_SUFFIX = (
    "\n\nThis is a question only: do not modify any files. "
    "End your reply with a single line that starts with `ANSWER:`."
)

TASKS = {
    "B1-derive-bug": dict(
        category="bug fix (test-covered)",
        prompt=(
            "Users report that landfirst labels a branch \"safe to delete\" when its "
            "remote-tracking branch was deleted, even if the branch still has commits that "
            "never reached the base branch. That is exactly how people lose work. "
            "Find the cause and fix it. Make sure the existing tests pass."
        ),
        setup=setup_b1, check=check_b1),
    "B2-severity-bug": dict(
        category="bug fix (no test coverage)",
        prompt=(
            "In landfirst's panel, branches with a detached worktree are styled with the same "
            "tone as \"safe to delete\" (routine cleanup) instead of as a warning like "
            "\"diverged\" and \"unpushed\". Detached worktrees can lose commits, so they must be "
            "a warning. Find the cause and fix it."
        ),
        setup=setup_b2, check=check_b2),
    "F1-nothing-to-merge": dict(
        category="small feature",
        prompt=(
            "Change landfirst's plain text report (`landfirst -plain`): when the snapshot has at "
            "least one unit but the merge plan is empty, still print the MERGE ORDER section, "
            "containing exactly one line `  nothing to merge` (two leading spaces). When there are "
            "no units at all, keep omitting the section. Add a test for it."
        ),
        setup=None, check=check_f1),
    "F2-only-flag": dict(
        category="feature (CLI + package)",
        prompt=(
            "Add a `-only` flag to the landfirst command. It takes a comma-separated list of "
            "verdicts (for example `-only never-pushed,diverged`) and limits the units shown by "
            "`-plain` and `-json` to those verdicts. Implement the filtering as an exported "
            "function in package internal/tui with this exact signature:\n\n"
            "    func FilterByVerdict(s *protocol.Snapshot, keep ...string) *protocol.Snapshot\n\n"
            "It returns a filtered copy and must not mutate its input. Merge-plan entries whose "
            "branch was filtered out are dropped as well. With no verdicts given it returns the "
            "snapshot unfiltered. An empty -only flag means no filtering."
        ),
        setup=None, check=check_f2),
    "R1-rename-lines": dict(
        category="refactor touching callers",
        prompt=(
            "Rename the function `Lines` in package internal/git to `NonEmptyLines` and update "
            "every caller in the repository. Behaviour must not change; the build, `go vet` and "
            "the git package tests must pass."
        ),
        setup=None, check=check_r1),
    "R2-isstale-now": dict(
        category="refactor (signature change)",
        prompt=(
            "Make `IsStale` in internal/engine/state deterministic: change its signature to "
            "`IsStale(fetchHeadAt, now time.Time, threshold time.Duration) bool` so it no longer "
            "reads the clock itself, and update every caller and test accordingly (production "
            "callers should pass the current time). The build, `go vet` and the state package "
            "tests must pass."
        ),
        setup=None, check=check_r2),
    "Q1-try-callers": dict(
        category="codebase question (callers)",
        prompt=(
            "Which functions and methods in this repository's non-test Go code call the function "
            "`Try` from package internal/git directly (including calls from inside package git "
            "itself)? List every one. In the ANSWER line, give them comma-separated as "
            "`package.Function` or `package.Type.Method`." + QUESTION_SUFFIX
        ),
        setup=None, check=check_q1),
    "Q2-noprobe-conflicts": dict(
        category="codebase question (data flow)",
        prompt=(
            "When landfirst runs with `-no-probe`, what value does each merge-plan item's "
            "`Conflicts` field hold, which function assigns it, and what does the `-plain` "
            "report print for such an item? In the ANSWER line give the value, the function "
            "name, and the exact printed text." + QUESTION_SUFFIX
        ),
        setup=None, check=check_q2),
}
