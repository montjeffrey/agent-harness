"""Family R - resume after a break (targets K3).

One chain on landfirst (pinned 4eb6172), "accurate unlanded counts", split in
two sessions:

- Session 1 (setup, not measured): investigate why ``Unit.Unlanded`` is 0 for
  most branch verdicts (C1, written to ANSWER.md), then make wrong-upstream
  branches report real counts so they reach AtRisk (C2).
- Session 2 (measured): "continue where we left off" - make the merge plan's
  Ahead the true unlanded count for every plan candidate (C3) and make its
  BehindN / "N behind main" mean behind the base, not the upstream (C4).

Facts session 1 discovers and session 2 needs - where Unlanded is computed
(judge() in internal/model/landing.go, gated on op/Gone/Upstream), which
verdicts skip git cherry, and that Unit.Behind is upstream-relative (from
collectBranches' for-each-ref %(upstream:track)) - are deliberately NOT in the
session-2 prompt. A/A+ can carry them over in memory; B has to rediscover them.

Truth, verified against 4eb6172:

- judge() (landing.go ~line 102) runs unlanded() (git cherry) only when
  ``op == OpNone && (b.Gone || b.Upstream == "")``. So InProgress,
  WrongUpstream, Diverged, Unpushed, Behind and Landed branch units always
  carry Unlanded/Equivalent 0 (Orphaned/NeverPushed/SafeToDelete get real
  counts; detached worktree units always run cherry in buildUnits).
- Build() (build.go) counts a unit toward AtRisk/StrandedCommits only when
  ``Severity() == SeverityBad && Unlanded > 0``: WrongUpstream and InProgress
  are SeverityBad but never counted.
- buildPlan() (plan.go) sets ``PlanItem.Ahead = u.Unlanded`` - 0 for every
  Unpushed/Diverged/WrongUpstream candidate - and ``BehindN = u.Behind``,
  which is behind the upstream, while the Why text says "N behind <base>".
"""

from __future__ import annotations

import re
from pathlib import Path

from tasks_common import build_and_vet, hidden_go_test, read, replace_once, write_file

MODEL = "internal/model"


def _all(clone: Path, checks) -> tuple[bool, str]:
    """Run every check (so a failure report is complete), pass only if all pass."""
    ok, details = True, []
    for name, fn in checks:
        good, detail = fn()
        ok &= good
        if not good:
            details.append(f"[{name}] {detail}")
    return ok, "\n".join(details)


# ---------------------------------------------------------------- session 1

PROMPT_1 = """\
I want landfirst's unlanded counts to be trustworthy. This session has two parts; \
the merge-plan side of it is for another session, so stop after part 2.

Part 1 - investigate. For which verdicts is model.Unit.Unlanded always 0 for a \
branch unit, whatever the branch actually contains? What does that do to the \
at-risk headline (Summary.AtRisk) and to PlanItem.Ahead in the merge plan? \
Write your findings to ANSWER.md at the repository root.

Part 2 - fix. Make wrong-upstream branches report real Unlanded/Equivalent \
counts so they count toward AtRisk, without adding a git cherry spawn for any \
other verdict. Keep `go build ./...` and `go vet ./...` clean.
"""

HIDDEN_1 = r'''import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/montjeffrey/landfirst/internal/git"
)

func TestHiddenWrongUpstreamCountsTowardAtRisk(t *testing.T) {
	f := newFixture(t)
	f.run("checkout", "-q", "-b", "feat/w")
	f.commit("w.txt", "work on the wrong upstream", "work tracking origin/main")
	f.run("branch", "--set-upstream-to=origin/main", "feat/w")
	f.run("checkout", "-q", "main")

	m := f.build()
	var u *Unit
	for i := range m.Units {
		if m.Units[i].Branch == "feat/w" {
			u = &m.Units[i]
		}
	}
	if u == nil {
		t.Fatal("feat/w not found in units")
	}
	if u.Verdict != WrongUpstream {
		t.Fatalf("feat/w verdict = %q, want %q", u.Verdict, WrongUpstream)
	}
	if u.Unlanded != 1 {
		t.Fatalf("feat/w Unlanded = %d, want 1 (one commit absent from the base)", u.Unlanded)
	}
	if m.AtRisk < 1 {
		t.Fatalf("AtRisk = %d, want >= 1: a wrong-upstream branch with unlanded work is at risk", m.AtRisk)
	}
}

// One branch per verdict, plus a detached worktree. git cherry may run only
// for the units whose answer needs it: no-upstream, gone, wrong-upstream
// branches and detached worktrees.
func TestHiddenCherryRunsOnlyWhereTheVerdictNeedsIt(t *testing.T) {
	f := newFixture(t)
	branch := func(name string) {
		f.run("checkout", "-q", "main")
		f.run("checkout", "-q", "-b", name)
	}
	slug := func(s string) string { return strings.ReplaceAll(s, "/", "_") }

	branch("feat/n") // never pushed
	f.commit("n.txt", "n", "never pushed")

	branch("feat/g") // upstream gone
	f.commit("g.txt", "g", "gone upstream")
	f.run("push", "-q", "-u", "origin", "feat/g")
	f.run("push", "-q", "origin", "--delete", "feat/g")
	f.run("fetch", "-q", "--prune")

	branch("feat/w") // wrong upstream
	f.commit("w.txt", "w", "wrong upstream")
	f.run("branch", "--set-upstream-to=origin/main", "feat/w")

	branch("feat/sync") // landed (in sync with its remote)
	f.commit("s.txt", "s", "in sync")
	f.run("push", "-q", "-u", "origin", "feat/sync")

	branch("feat/u") // unpushed
	f.commit("u1.txt", "u1", "pushed")
	f.run("push", "-q", "-u", "origin", "feat/u")
	f.commit("u2.txt", "u2", "not pushed")

	branch("feat/dv") // diverged
	f.commit("dv1.txt", "dv1", "dv1")
	f.commit("dv2.txt", "dv2", "dv2")
	f.run("push", "-q", "-u", "origin", "feat/dv")
	f.run("reset", "-q", "--hard", "HEAD~1")
	f.commit("dv3.txt", "dv3", "dv3")

	branch("feat/lag") // behind
	f.commit("l1.txt", "l1", "l1")
	f.commit("l2.txt", "l2", "l2")
	f.run("push", "-q", "-u", "origin", "feat/lag")
	f.run("reset", "-q", "--hard", "HEAD~1")

	f.run("checkout", "-q", "main")
	f.run("worktree", "add", "-q", "--detach", filepath.Join(filepath.Dir(f.dir), "wt-detached"), "HEAD")

	repo, err := git.Open(f.dir)
	if err != nil {
		t.Fatal(err)
	}
	var m *Model
	trace := captureGitTrace(t, func() { m = Build(repo, Options{Probe: false}) })

	want := map[string]Verdict{
		"feat/n": NeverPushed, "feat/g": Orphaned, "feat/w": WrongUpstream,
		"feat/sync": Landed, "feat/u": Unpushed, "feat/dv": Diverged, "feat/lag": Behind,
	}
	detached := 0
	for _, u := range m.Units {
		if u.Branch == "" && u.Verdict == Detached {
			detached++
		}
		if v, ok := want[u.Branch]; ok && u.Verdict != v {
			t.Fatalf("fixture: %s verdict = %q, want %q (%s)", u.Branch, u.Verdict, v, slug(u.Detail))
		}
	}
	if detached != 1 {
		t.Fatalf("fixture: want 1 detached unit, got %d", detached)
	}

	var cherry []string
	for _, line := range strings.Split(trace, "\n") {
		if strings.Contains(line, " git cherry ") {
			cherry = append(cherry, line)
		}
	}
	// feat/n + feat/g + feat/w + the detached worktree.
	if len(cherry) != 4 {
		t.Fatalf("want exactly 4 git cherry spawns (no-upstream, gone, wrong-upstream, detached), got %d:\n%s",
			len(cherry), strings.Join(cherry, "\n"))
	}
}
'''


def check_1(clone: Path, _result: str) -> tuple[bool, str]:
    def answer():
        text = read(clone, "ANSWER.md")
        if not text.strip():
            return False, "ANSWER.md missing or empty"
        missing = [label for label, pat in (("wrong-upstream", r"wrong[-_ ]?upstream"),
                                            ("AtRisk", r"at[-_ ]?risk"))
                   if not re.search(pat, text, re.I)]
        return (not missing), ("ANSWER.md does not mention " + ", ".join(missing)) if missing else ""

    return _all(clone, [
        ("answer", answer),
        ("build", lambda: build_and_vet(clone)),
        ("hidden", lambda: hidden_go_test(clone, MODEL, "model", HIDDEN_1)),
    ])


ANSWER_1 = """\
# Where Unit.Unlanded is always 0

`judge()` in `internal/model/landing.go` only calls `unlanded()` (the `git cherry`
spawn) when `op == OpNone && (b.Gone || b.Upstream == "")`. Every other branch
unit keeps a zero `cherryResult`, so `Unlanded`/`Equivalent` are 0 regardless of
the branch's contents for these verdicts:

- in-progress (a worktree has an unfinished op - op != OpNone)
- wrong-upstream (upstream configured, not gone)
- diverged, unpushed, behind, landed (upstream configured, not gone)

Only orphaned / never-pushed / safe-to-delete branch units get real counts
(detached-worktree units always run cherry in `buildUnits`).

## Effect on AtRisk

`Build()` (`build.go`) counts a unit toward `AtRisk` and `StrandedCommits` only
when `Severity() == SeverityBad && Unlanded > 0`. WrongUpstream and InProgress
are SeverityBad but their Unlanded is always 0, so they never count toward
Summary.AtRisk / StrandedCommits - a wrong-upstream branch full of unpublished
work leaves the at-risk headline at 0.

## Effect on PlanItem.Ahead

`buildPlan()` (`plan.go`) sets `PlanItem.Ahead = u.Unlanded`. Plan candidates
include wrong-upstream, diverged and unpushed branches, and for all of them
Ahead is 0 - the "smallest diff first" tie-break sorts on a meaningless zero.
(Separately, `BehindN = u.Behind` is behind the upstream, not behind the base,
although the Why text reads "N behind main".)
"""


def reference_1(clone: Path) -> str:
    write_file(clone, "ANSWER.md", ANSWER_1)
    replace_once(clone / MODEL / "landing.go",
                 'if op == OpNone && (b.Gone || b.Upstream == "") {',
                 'if op == OpNone && (b.Gone || b.Upstream == "" || upstreamIsWrong(r, b)) {')
    return "Findings written to ANSWER.md; wrong-upstream branches now run git cherry."


# ---------------------------------------------------------------- session 2

PROMPT_2 = """\
Let's pick up the unlanded-counts work in landfirst where we left off last time. \
The investigation and the wrong-upstream fix are done; what remains is the merge plan:

1. PlanItem.Ahead must be the true unlanded count - changes genuinely absent \
from the base, same meaning as Unit.Unlanded - for every plan candidate, \
including unpushed and diverged branches. Compute it only for plan candidates; \
branches that never reach the plan must not cost an extra git spawn.
2. PlanItem.BehindN, and the "N behind main" entry in the plan's Why list, must \
mean commits on the base that the branch does not have \
(`git rev-list --count <branch>..<base>`), not commits behind the branch's upstream.

Keep `go build ./...` and `go vet ./...` clean.
"""

HIDDEN_2 = r'''import (
	"strings"
	"testing"

	"github.com/montjeffrey/landfirst/internal/git"
)

func hiddenPlanItem(t *testing.T, m *Model, branch string) PlanItem {
	t.Helper()
	for _, p := range m.Plan {
		if p.Branch == branch {
			return p
		}
	}
	t.Fatalf("%s missing from the merge plan: %+v", branch, m.Plan)
	return PlanItem{}
}

func hiddenVerdicts(t *testing.T, m *Model, want map[string]Verdict) {
	t.Helper()
	for b, v := range want {
		if got := verdictOf(m, b); got != v {
			t.Fatalf("fixture: %s verdict = %q, want %q", b, got, v)
		}
	}
}

func TestHiddenPlanAheadIsTrueUnlandedForEveryCandidate(t *testing.T) {
	f := newFixture(t)
	branch := func(name string) {
		f.run("checkout", "-q", "main")
		f.run("checkout", "-q", "-b", name)
	}

	branch("feat/u") // unpushed: 1 pushed + 2 local commits, all absent from main
	f.commit("u1.txt", "u1", "u1 pushed")
	f.run("push", "-q", "-u", "origin", "feat/u")
	f.commit("u2.txt", "u2", "u2 local")
	f.commit("u3.txt", "u3", "u3 local")

	branch("feat/dv") // diverged: local A,C vs remote A,B
	f.commit("dva.txt", "a", "dv a")
	f.commit("dvb.txt", "b", "dv b")
	f.run("push", "-q", "-u", "origin", "feat/dv")
	f.run("reset", "-q", "--hard", "HEAD~1")
	f.commit("dvc.txt", "c", "dv c")

	branch("feat/w") // wrong upstream, 1 commit
	f.commit("w.txt", "w", "wrong upstream")
	f.run("branch", "--set-upstream-to=origin/main", "feat/w")

	branch("feat/n") // never pushed, 2 commits
	f.commit("n1.txt", "n1", "n1")
	f.commit("n2.txt", "n2", "n2")

	branch("feat/sync") // landed: not a plan candidate
	f.commit("s.txt", "s", "in sync")
	f.run("push", "-q", "-u", "origin", "feat/sync")

	branch("feat/lag") // behind: not a plan candidate
	f.commit("l1.txt", "l1", "l1")
	f.commit("l2.txt", "l2", "l2")
	f.run("push", "-q", "-u", "origin", "feat/lag")
	f.run("reset", "-q", "--hard", "HEAD~1")
	f.run("checkout", "-q", "main")

	repo, err := git.Open(f.dir)
	if err != nil {
		t.Fatal(err)
	}
	var m *Model
	trace := captureGitTrace(t, func() { m = Build(repo, Options{Probe: false}) })
	hiddenVerdicts(t, m, map[string]Verdict{
		"feat/u": Unpushed, "feat/dv": Diverged, "feat/w": WrongUpstream,
		"feat/n": NeverPushed, "feat/sync": Landed, "feat/lag": Behind,
	})

	for b, want := range map[string]int{"feat/u": 3, "feat/dv": 2, "feat/w": 1, "feat/n": 2} {
		if got := hiddenPlanItem(t, m, b).Ahead; got != want {
			t.Fatalf("PlanItem.Ahead for %s = %d, want %d (changes absent from main)", b, got, want)
		}
	}

	for _, line := range strings.Split(trace, "\n") {
		if !strings.Contains(line, " git cherry ") {
			continue
		}
		for _, tok := range strings.Fields(line) {
			if tok == "feat/sync" || tok == "feat/lag" {
				t.Fatalf("git cherry ran for a branch that never reaches the plan: %s", line)
			}
		}
	}
}

func TestHiddenPlanBehindCountsAgainstTheBase(t *testing.T) {
	f := newFixture(t)
	f.run("checkout", "-q", "-b", "feat/b") // in sync with its remote apart from one local commit
	f.commit("b1.txt", "b1", "b1 pushed")
	f.run("push", "-q", "-u", "origin", "feat/b")
	f.commit("b2.txt", "b2", "b2 local")

	f.run("checkout", "-q", "main")
	f.run("checkout", "-q", "-b", "feat/dv") // 1 behind its upstream
	f.commit("dva.txt", "a", "dv a")
	f.commit("dvb.txt", "b", "dv b")
	f.run("push", "-q", "-u", "origin", "feat/dv")
	f.run("reset", "-q", "--hard", "HEAD~1")
	f.commit("dvc.txt", "c", "dv c")

	f.run("checkout", "-q", "main") // main moves on by 3
	f.commit("m1.txt", "m1", "main 1")
	f.commit("m2.txt", "m2", "main 2")
	f.commit("m3.txt", "m3", "main 3")
	f.run("push", "-q", "origin", "main")

	m := f.build()
	hiddenVerdicts(t, m, map[string]Verdict{"feat/b": Unpushed, "feat/dv": Diverged})
	for _, b := range []string{"feat/b", "feat/dv"} {
		p := hiddenPlanItem(t, m, b)
		if p.BehindN != 3 {
			t.Fatalf("PlanItem.BehindN for %s = %d, want 3 (main moved 3 commits past it)", b, p.BehindN)
		}
		var behind []string
		for _, w := range p.Why {
			if strings.Contains(w, "behind") {
				behind = append(behind, w)
			}
		}
		if len(behind) != 1 || !strings.Contains(behind[0], "3 behind main") {
			t.Fatalf("Why for %s should say \"3 behind main\" once, got %q", b, p.Why)
		}
	}
}
'''


def check_2(clone: Path, _result: str) -> tuple[bool, str]:
    return _all(clone, [
        ("build", lambda: build_and_vet(clone)),
        ("hidden", lambda: hidden_go_test(clone, MODEL, "model", HIDDEN_2)),
    ])


def reference_2(clone: Path) -> str:
    plan = clone / MODEL / "plan.go"
    replace_once(plan, 'import (\n\t"sort"\n\t"strconv"\n',
                 'import (\n\t"sort"\n\t"strconv"\n\t"strings"\n\t"sync"\n')
    replace_once(plan,
                 "func buildPlan(r *git.Repo, units []Unit, contentions []Contention, probed bool) []PlanItem {",
                 "// behindBase counts commits on the base that branch does not have - what\n"
                 "// \"N behind main\" means. Unit.Behind is relative to the upstream instead.\n"
                 "func behindBase(r *git.Repo, branch string) int {\n"
                 "\tn, _ := strconv.Atoi(strings.TrimSpace(git.Try(r.Root, \"rev-list\", \"--count\", branch+\"..\"+baseRef(r))))\n"
                 "\treturn n\n"
                 "}\n\n"
                 "func buildPlan(r *git.Repo, units []Unit, contentions []Contention, probed bool, cache map[string]cherryResult) []PlanItem {")
    replace_once(plan, "\titems := make([]PlanItem, 0, len(cands))\n",
                 "\t// judge() skips git cherry for verdicts that do not need it, so Unit.Unlanded\n"
                 "\t// is 0 for unpushed/diverged branches. Plan candidates need the real count;\n"
                 "\t// the shared cache makes this free for the ones judge() already counted.\n"
                 "\tvar mu sync.Mutex\n"
                 "\titems := make([]PlanItem, 0, len(cands))\n")
    replace_once(plan, "\t\tvar why []string\n\t\tconf := -1\n",
                 "\t\tahead := unlanded(r, c.u.Branch, cache, &mu).Unlanded\n"
                 "\t\tbehind := behindBase(r, c.u.Branch)\n"
                 "\t\tvar why []string\n\t\tconf := -1\n")
    replace_once(plan,
                 "\t\tif c.u.Behind > 0 {\n\t\t\twhy = append(why, strconv.Itoa(c.u.Behind)+\" behind \"+r.Base)\n\t\t}\n",
                 "\t\tif behind > 0 {\n\t\t\twhy = append(why, strconv.Itoa(behind)+\" behind \"+r.Base)\n\t\t}\n")
    replace_once(plan, "Branch: c.u.Branch, Ahead: c.u.Unlanded, BehindN: c.u.Behind,",
                 "Branch: c.u.Branch, Ahead: ahead, BehindN: behind,")
    replace_once(clone / MODEL / "build.go",
                 "m.Plan = buildPlan(r, units, m.Contentions, opt.Probe)",
                 "m.Plan = buildPlan(r, units, m.Contentions, opt.Probe, cache)")
    return "PlanItem.Ahead is the patch-id unlanded count for every candidate; BehindN counts against the base."


TASKS = {
    "R-unlanded": dict(
        family="R",
        repo="landfirst",
        setup=None,
        measure_from=1,
        steps=[
            dict(prompt=PROMPT_1, check=check_1, reference=reference_1),
            dict(prompt=PROMPT_2, check=check_2, reference=reference_2),
        ],
    ),
}
