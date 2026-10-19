"""Family H: long-run hand-off chains on landfirst (pinned 4eb6172).

Two six-step feature chains. Each step is one session; later steps depend on
facts an earlier step had to dig out of the code (which function builds the
wire snapshot, what feeds the revision hash, where staleness comes from), and
the prompts deliberately do not restate them.

- H-sugg: surface each unit's suggested fix (protocol.Unit.Suggested).
- H-stale: repository staleness (StaleAfter, Freshness.Stale, revision hygiene).

See tasks_common.py for the task contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from tasks_common import (APP_CONTROL_BLOCK, CheckBlocked, build_and_vet,
                          hidden_go_test, read, replace_once, sh, write_file)

# ---------------------------------------------------------------- helpers ---


def _tabs(code: str) -> str:
    """Turn 4-space indentation into tabs so inserted Go stays gofmt-shaped."""
    out = []
    for line in code.split("\n"):
        stripped = line.lstrip(" ")
        n = (len(line) - len(stripped)) // 4
        out.append("\t" * n + stripped)
    return "\n".join(out)


def _replace_span(path: Path, start: str, end: str, new: str) -> None:
    """Replace text from the unique ``start`` marker through the first ``end``
    after it (inclusive) with ``new``."""
    text = path.read_text(encoding="utf-8")
    if text.count(start) != 1:
        raise RuntimeError(f"start anchor not unique/missing in {path}: {start[:60]!r}")
    i = text.index(start)
    j = text.find(end, i)
    if j < 0:
        raise RuntimeError(f"end anchor missing in {path}: {end[:60]!r}")
    path.write_text(text[:i] + new + text[j + len(end):], encoding="utf-8", newline="\n")


def _answer_has(clone: Path, *tokens: str, nocase: tuple[str, ...] = ()) -> tuple[bool, str]:
    text = read(clone, "ANSWER.md")
    if not text.strip():
        return False, "ANSWER.md missing or empty"
    missing = [t for t in tokens if t not in text]
    missing += [t for t in nocase if t.lower() not in text.lower()]
    if missing:
        return False, f"ANSWER.md lacks {missing}"
    return True, "ANSWER.md ok"


def _all(*results: tuple[bool, str]) -> tuple[bool, str]:
    for ok, detail in results:
        if not ok:
            return False, detail
    return True, " | ".join(d[-200:] for _, d in results)


def _rmtree(path: Path) -> None:
    def onexc(func, p, _exc):
        try:
            Path(p).chmod(0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=onexc)


def _git_env(home: Path) -> dict:
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=str(home / ".noglobal"),
               GIT_CONFIG_SYSTEM=str(home / ".nosystem"),
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="t@example.invalid")
    return env


def _fixture_repo(tmp: Path, *, branch: str | None, fetch: bool) -> Path:
    """A real repo + bare origin, main pushed; optional unpushed branch and fetch."""
    env = _git_env(tmp)
    remote, repo = tmp / "remote.git", tmp / "repo"
    repo.mkdir(parents=True)

    def g(cwd: Path, *args: str) -> None:
        r = sh(cwd, "git", *args, timeout=120, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"fixture git {args}: {r.stderr.strip()}")

    g(tmp, "init", "-q", "--bare", str(remote))
    g(repo, "init", "-q", "-b", "main")
    g(repo, "remote", "add", "origin", str(remote))
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "commit", "-q", "--no-gpg-sign", "-m", "init")
    g(repo, "push", "-q", "-u", "origin", "main")
    if branch:
        g(repo, "checkout", "-q", "-b", branch)
        (repo / "x.txt").write_text("work\n", encoding="utf-8")
        g(repo, "add", "-A")
        g(repo, "commit", "-q", "--no-gpg-sign", "-m", "unpushed work")
        g(repo, "checkout", "-q", "main")
    if fetch:
        g(repo, "fetch", "-q", "origin")
    return repo


def _build_binary(clone: Path, tmp: Path, attempt: int) -> Path:
    exe = tmp / f"landfirst{attempt}{'.exe' if os.name == 'nt' else ''}"
    # Application Control blocks stripped and unstripped binaries at different
    # times, so retries alternate between the two.
    flags = ["-ldflags=-s -w"] if attempt % 2 == 0 else []
    r = sh(clone, "go", "build", *flags, "-o", str(exe), "./cmd/landfirst")
    if r.returncode != 0:
        # The build itself is part of the check (agent code must compile).
        raise _BuildFailed("go build ./cmd/landfirst: " + (r.stdout + r.stderr)[-800:])
    return exe


class _BuildFailed(RuntimeError):
    pass


def _run_binary(clone: Path, tmp: Path, repo: Path, *args: str,
                merge_stderr: bool = False) -> tuple[int, str]:
    """Build (once) and run the landfirst command in ``repo``. A Windows
    Application Control block is retried with a fresh build and, if it
    persists, raised as CheckBlocked - never scored as a failed check."""
    blocked = ""
    for attempt in range(3):
        exe = tmp / f"landfirst{attempt}{'.exe' if os.name == 'nt' else ''}"
        if not exe.exists():
            try:
                exe = _build_binary(clone, tmp, attempt)
            except _BuildFailed as exc:
                return -1, str(exc)
        try:
            r = sh(repo, str(exe), *args, timeout=300, env=_git_env(tmp))
        except OSError as exc:  # CreateProcess refused by policy
            if "Application Control" in str(exc):
                blocked = str(exc)
                exe.unlink(missing_ok=True)
                continue
            raise
        if APP_CONTROL_BLOCK in r.stdout + r.stderr:
            blocked = r.stderr
            exe.unlink(missing_ok=True)
            continue
        tail = r.stderr if merge_stderr else ("\nSTDERR: " + r.stderr if r.returncode else "")
        return r.returncode, r.stdout + tail
    raise CheckBlocked(blocked)


def _json_run(clone: Path, tmp: Path, repo: Path, *args: str) -> tuple[dict | None, str]:
    code, out = _run_binary(clone, tmp, repo, "-json", "-no-probe", *args)
    if code != 0:
        return None, f"landfirst -json {' '.join(args)} exited {code}: {out[-600:]}"
    try:
        return json.loads(out), ""
    except ValueError as exc:
        return None, f"-json output is not JSON ({exc}): {out[:300]}"


def _readme_section(clone: Path, heading: str) -> str:
    text = read(clone, "README.md")
    i = text.find("## " + heading)
    if i < 0:
        return ""
    j = text.find("\n## ", i + 3)
    return text[i:] if j < 0 else text[i:j]


# ================================================================ H-sugg ===

SUGG_PROMPTS = [
    # 1 investigate
    "`landfirst -plain` has code that prints an indented `git ...` fix line under "
    "each unit, but on a real repository that line never appears. Find the exact "
    "cause: which field is involved, and which function would have to change for "
    "the line to print. Do not change any code. Write your findings to ANSWER.md "
    "at the repository root, naming that function and that field.",
    # 2 refactor
    "Refactor internal/engine: pull the per-verdict decision-tree construction out "
    "of the engine's Decide method into a pure package-level function\n\n"
    "    func buildTree(snap *protocol.Snapshot, unit *protocol.Unit) (protocol.DecisionNode, string, error)\n\n"
    "returning the tree's root, the ID of the action the engine would suggest "
    "(\"\" when there is none), and an error for a verdict it does not recognise. "
    "It must not touch the engine, its locks, git or the filesystem. Decide must "
    "delegate to it and keep its exact current behaviour (behind and landed units "
    "still get an empty root and no suggestion). `go build ./...` and "
    "`go vet ./...` must pass.",
    # 3 populate
    "Make every Snapshot the engine returns carry each unit's suggested action in "
    "protocol.Unit.Suggested: the same leaf that Decide marks as suggested for that "
    "unit, or nil when there is none (a landed or behind unit, for example). The "
    "snapshot's Revision must account for the suggestion, and two Snapshots of an "
    "unchanged repository must still have equal Revisions. `go build ./...` and "
    "`go vet ./...` must pass.",
    # 4 plain suffixes
    "In `landfirst -plain`, the fix line printed under a unit must say when the "
    "tool will not run it for you. If the unit's suggested action is manual-class, "
    "append ` (do it by hand)` to that line. If it has blocked preconditions, "
    "append ` (blocked: <reasons>)` with the blocked reasons joined by `; ` in "
    "their existing order, for example\n\n"
    "          git merge --no-edit origin/feat/c (blocked: worktree has no uncommitted changes)\n\n"
    "A suggestion that is neither keeps its line exactly as it is today.",
    # 5 TUI key
    "Add a key `s` to the TUI's topology tab that runs the selected unit's "
    "suggested action without going through the decision tree. If the selected "
    "unit has a suggestion that is not manual-class and has no blocked "
    "preconditions, open the same y/n confirmation bar an action from the decision "
    "tree opens (confirming runs it exactly as a tree action is run). It acts on "
    "the suggestion the snapshot already carries for that unit, immediately, "
    "without a Decide call. Otherwise "
    "open nothing and set the status line to `no runnable suggestion`. Add `s` to "
    "the footer key help with the label `run suggested`.",
    # 6 JSON + README
    "Scripts that read `landfirst -json` need every unit to carry a `suggested` "
    "key: the action object when the unit has a suggestion, and `null` when it has "
    "none (today the key is missing entirely for such units). Make that so, and "
    "document the `s` key in the README's \"Acting on it\" section. "
    "`go build ./...` and `go vet ./...` must pass.",
]

# ---- step 1 ---------------------------------------------------------------


def sugg1_check(clone: Path, result: str) -> tuple[bool, str]:
    return _answer_has(clone, "toSnapshot", "Suggested")


def sugg1_ref(clone: Path) -> str:
    write_file(clone, "ANSWER.md", """# Why `-plain` never prints the fix line

`internal/tui/plain.go` `PlainReport` prints `      git <argv>` only when
`u.Suggested != nil`. Nothing ever sets `protocol.Unit.Suggested`:
`internal/engine/engine.go` `toSnapshot` builds each `protocol.Unit` from the
model unit and never fills `Suggested` (decision trees are only built on demand
by `Decide`). So the field is always nil and the line never prints.

Fix: `toSnapshot` (the snapshot conversion behind `engine.Snapshot`) has to
populate `Suggested` from each unit's decision tree, before `revisionOf` runs.
""")
    return ""


# ---- step 2 ---------------------------------------------------------------

SUGG2_TEST = r'''import (
    "context"
    "testing"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

var _ func(*protocol.Snapshot, *protocol.Unit) (protocol.DecisionNode, string, error) = buildTree

func hiddenSnap(remote string, units ...protocol.Unit) *protocol.Snapshot {
    return &protocol.Snapshot{
        Revision: "rev-h",
        Repo: protocol.RepoInfo{Root: "/no/such/root", Name: "r", Base: "main",
            BaseRef: "origin/main", Remote: remote},
        Units: units,
    }
}

func TestHiddenBuildTreeNeverPushed(t *testing.T) {
    s := hiddenSnap("origin", protocol.Unit{ID: "branch:feat/x", Branch: "feat/x", Verdict: "never-pushed", Unlanded: 1})
    root, sugg, err := buildTree(s, &s.Units[0])
    if err != nil {
        t.Fatalf("buildTree: %v", err)
    }
    if sugg != "branch:feat/x/push" {
        t.Fatalf("suggested = %q, want branch:feat/x/push", sugg)
    }
    a := root.Action
    if a == nil || a.ID != sugg || a.Class != protocol.ClassSafe {
        t.Fatalf("root = %+v, want a single safe push leaf", root)
    }
    want := []string{"push", "-u", "origin", "feat/x:feat/x"}
    if len(a.Argv) != len(want) {
        t.Fatalf("argv = %v, want %v", a.Argv, want)
    }
    for i := range want {
        if a.Argv[i] != want[i] {
            t.Fatalf("argv = %v, want %v", a.Argv, want)
        }
    }
    if a.Cwd != "/no/such/root" {
        t.Fatalf("cwd = %q, want the snapshot's repo root", a.Cwd)
    }
}

func TestHiddenBuildTreeNoRemoteAndDiverged(t *testing.T) {
    s := hiddenSnap("", protocol.Unit{ID: "branch:feat/x", Branch: "feat/x", Verdict: "never-pushed", Unlanded: 1})
    root, sugg, err := buildTree(s, &s.Units[0])
    if err != nil || sugg != "" || root.Action == nil || root.Action.Kind != "no-remote" {
        t.Fatalf("no remote: root=%+v sugg=%q err=%v", root, sugg, err)
    }
    s = hiddenSnap("origin", protocol.Unit{ID: "branch:feat/d", Branch: "feat/d", Upstream: "origin/feat/d",
        Verdict: "diverged", Ahead: 1, Behind: 1})
    root, sugg, err = buildTree(s, &s.Units[0])
    if err != nil {
        t.Fatalf("diverged: %v", err)
    }
    if root.Question == "" || len(root.Options) != 3 {
        t.Fatalf("diverged root = %+v, want a 3-option question", root)
    }
    if sugg != "branch:feat/d/merge-upstream" {
        t.Fatalf("diverged suggested = %q", sugg)
    }
}

func TestHiddenBuildTreeEmptyAndUnknown(t *testing.T) {
    for _, v := range []string{"landed", "behind"} {
        s := hiddenSnap("origin", protocol.Unit{ID: "branch:b", Branch: "b", Verdict: v})
        root, sugg, err := buildTree(s, &s.Units[0])
        if err != nil || sugg != "" || root.Action != nil || root.Question != "" || len(root.Options) != 0 {
            t.Fatalf("%s: root=%+v sugg=%q err=%v, want empty", v, root, sugg, err)
        }
    }
    s := hiddenSnap("origin", protocol.Unit{ID: "branch:b", Branch: "b", Verdict: "bogus-verdict"})
    if _, _, err := buildTree(s, &s.Units[0]); err == nil {
        t.Fatal("an unrecognized verdict must return an error")
    }
}

func TestHiddenDecideAgreesWithBuildTree(t *testing.T) {
    f := newEfixture(t)
    f.run("checkout", "-q", "-b", "feat/agree")
    f.commit("g.txt", "work", "unpushed")
    f.run("checkout", "-q", "main")
    eng, err := Open(f.dir)
    if err != nil {
        t.Fatal(err)
    }
    defer eng.Close()
    ctx := context.Background()
    snap, err := eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast})
    if err != nil {
        t.Fatal(err)
    }
    for i := range snap.Units {
        u := &snap.Units[i]
        tree, err := eng.Decide(ctx, u.ID)
        if err != nil {
            t.Fatalf("Decide(%s): %v", u.ID, err)
        }
        _, sugg, err := buildTree(snap, u)
        if err != nil || sugg != tree.Suggested {
            t.Fatalf("%s: buildTree suggested %q (err %v), Decide %q", u.ID, sugg, err, tree.Suggested)
        }
    }
}
'''


def sugg2_check(clone: Path, result: str) -> tuple[bool, str]:
    ok, d = hidden_go_test(clone, "internal/engine", "engine", SUGG2_TEST)
    if not ok:
        return False, d
    return _all((True, d), build_and_vet(clone))


SUGG2_DECIDE = _tabs('''    root, suggested, err := buildTree(snap, unit)
    if err != nil {
        return nil, err
    }
    return &protocol.DecisionTree{UnitID: unitID, Revision: snap.Revision, Suggested: suggested, Root: root}, nil
}

// buildTree is Decide's pure core: the decision tree for one unit of snap and
// the ID of the leaf the engine would suggest ("" if none). It reads only its
// arguments - no engine state, no git, no filesystem - so anything holding a
// snapshot can build a unit's tree without going through an Engine.
func buildTree(snap *protocol.Snapshot, unit *protocol.Unit) (protocol.DecisionNode, string, error) {
    dc := &decideCtx{snap: snap, unit: unit}
    var root protocol.DecisionNode
    var suggested string

    // One case per model.Verdict - see internal/model/landing.go's remedy()
    // for the precedent this replaces (one command per verdict); this
    // mirrors that same verdict switch but grows a tree per verdict, not a
    // single command.
    switch unit.Verdict {
    case string(model.InProgress):
        root, suggested = dc.inProgress()
    case string(model.Orphaned):
        root, suggested = dc.orphaned()
    case string(model.WrongUpstream):
        root, suggested = dc.wrongUpstream()
    case string(model.NeverPushed):
        root, suggested = dc.neverPushed()
    case string(model.Unpushed):
        root, suggested = dc.unpushed()
    case string(model.SafeToDelete):
        root, suggested = dc.safeToDelete()
    case string(model.Detached):
        root, suggested = dc.detached()
    case string(model.Diverged):
        root, suggested = dc.diverged()
    case string(model.Behind), string(model.Landed):
        // Nothing to suggest for either - root stays the zero DecisionNode,
        // matching that the old Remedy system never offered anything here.
    default:
        return protocol.DecisionNode{}, "", fmt.Errorf("engine: unit %q has unrecognized verdict %q", unit.ID, unit.Verdict)
    }
    return root, suggested, nil
}
''')


def sugg2_ref(clone: Path) -> str:
    _replace_span(clone / "internal/engine/decide.go",
                  "\tdc := &decideCtx{snap: snap, unit: unit}\n\ttree := &protocol.DecisionTree{",
                  "\treturn tree, nil\n}\n", SUGG2_DECIDE)
    return ""


# ---- step 3 ---------------------------------------------------------------

SUGG3_TEST = r'''import (
    "context"
    "testing"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

func hiddenUnit(s *protocol.Snapshot, branch string) *protocol.Unit {
    for i := range s.Units {
        if s.Units[i].Branch == branch {
            return &s.Units[i]
        }
    }
    return nil
}

func hiddenCopy(s *protocol.Snapshot) *protocol.Snapshot {
    c := *s
    c.Units = append([]protocol.Unit(nil), s.Units...)
    return &c
}

func TestHiddenSnapshotCarriesSuggested(t *testing.T) {
    f := newEfixture(t)
    f.run("checkout", "-q", "-b", "feat/x")
    f.commit("x.txt", "work", "unpushed work")
    f.run("checkout", "-q", "main")

    eng, err := Open(f.dir)
    if err != nil {
        t.Fatal(err)
    }
    defer eng.Close()
    ctx := context.Background()
    snap, err := eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast})
    if err != nil {
        t.Fatal(err)
    }

    feat := hiddenUnit(snap, "feat/x")
    if feat == nil || feat.Verdict != "never-pushed" {
        t.Fatalf("feat/x = %+v, want a never-pushed unit", feat)
    }
    a := feat.Suggested
    if a == nil {
        t.Fatal("feat/x.Suggested is nil, want its push action")
    }
    want := []string{"push", "-u", "origin", "feat/x:feat/x"}
    if a.ID != "branch:feat/x/push" || a.Class != protocol.ClassSafe || len(a.Argv) != len(want) {
        t.Fatalf("feat/x.Suggested = %+v", a)
    }
    for i := range want {
        if a.Argv[i] != want[i] {
            t.Fatalf("argv = %v, want %v", a.Argv, want)
        }
    }
    main := hiddenUnit(snap, "main")
    if main == nil {
        t.Fatal("main unit missing")
    }
    if main.Verdict != "landed" || main.Suggested != nil {
        t.Fatalf("main = verdict %q suggested %+v, want landed with nil Suggested", main.Verdict, main.Suggested)
    }

    tree, err := eng.Decide(ctx, "branch:feat/x")
    if err != nil || tree.Suggested != a.ID {
        t.Fatalf("Decide suggested %q (err %v), snapshot says %q", tree.Suggested, err, a.ID)
    }

    snap2, err := eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast})
    if err != nil {
        t.Fatal(err)
    }
    if snap2.Revision != snap.Revision {
        t.Fatalf("Revision changed across two snapshots of the same state: %s vs %s", snap.Revision, snap2.Revision)
    }

    // The revision must be computed over the populated suggestion.
    same := hiddenCopy(snap)
    noBaseRef := hiddenCopy(snap)
    noBaseRef.Repo.BaseRef = ""
    if revisionOf(same) != snap.Revision && revisionOf(noBaseRef) != snap.Revision {
        t.Fatal("snap.Revision does not match its own contents - was Suggested set after the revision was computed?")
    }
    cleared := hiddenCopy(snap)
    for i := range cleared.Units {
        cleared.Units[i].Suggested = nil
    }
    if revisionOf(cleared) == revisionOf(same) {
        t.Fatal("the revision hash ignores Unit.Suggested")
    }
}
'''


def sugg3_check(clone: Path, result: str) -> tuple[bool, str]:
    ok, d = hidden_go_test(clone, "internal/engine", "engine", SUGG3_TEST)
    if not ok:
        return False, d
    return _all((True, d), build_and_vet(clone))


def sugg3_ref(clone: Path) -> str:
    replace_once(clone / "internal/engine/engine.go",
                 "\ts.Revision = revisionOf(s)\n\treturn s\n}",
                 _tabs('''    // Each unit carries the engine's own pick from its decision tree, set
    // before revisionOf so the revision covers it.
    for i := range s.Units {
        root, suggested, err := buildTree(s, &s.Units[i])
        if err != nil || suggested == "" {
            continue
        }
        s.Units[i].Suggested = findAction(root, suggested)
    }
    s.Revision = revisionOf(s)
    return s
}'''))
    return ""


# ---- step 4 ---------------------------------------------------------------

SUGG4_TEST = r'''import (
    "strings"
    "testing"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

func TestHiddenPlainSuggestedSuffixes(t *testing.T) {
    snap := &protocol.Snapshot{
        Repo: protocol.RepoInfo{Name: "myrepo", Base: "main"},
        Units: []protocol.Unit{
            {Branch: "feat/a", Verdict: "never-pushed", Detail: "1 change never pushed",
                Suggested: &protocol.Action{ID: "branch:feat/a/push", Argv: []string{"push", "-u", "origin", "feat/a:feat/a"}, Class: protocol.ClassSafe}},
            {Branch: "feat/b", Verdict: "diverged", Detail: "both moved",
                Suggested: &protocol.Action{ID: "branch:feat/b/rebase-onto-base", Argv: []string{"rebase", "origin/main"}, Class: protocol.ClassManual}},
            {Branch: "feat/c", Verdict: "diverged", Detail: "both moved",
                Suggested: &protocol.Action{ID: "branch:feat/c/merge-upstream", Argv: []string{"merge", "--no-edit", "origin/feat/c"},
                    Class: protocol.ClassGuarded,
                    Blocked: []string{"worktree has no uncommitted changes", "no rebase/merge/cherry-pick in progress"}}},
        },
    }
    out := PlainReport(snap)
    for _, want := range []string{
        "      git push -u origin feat/a:feat/a\n",
        "      git rebase origin/main (do it by hand)\n",
        "      git merge --no-edit origin/feat/c (blocked: worktree has no uncommitted changes; no rebase/merge/cherry-pick in progress)\n",
    } {
        if !strings.Contains(out, want) {
            t.Fatalf("missing line %q in:\n%s", want, out)
        }
    }
    if strings.Contains(out, "feat/a:feat/a (") {
        t.Fatalf("a safe, unblocked suggestion must keep its plain line:\n%s", out)
    }
}
'''


def sugg4_check(clone: Path, result: str) -> tuple[bool, str]:
    return hidden_go_test(clone, "internal/tui", "tui", SUGG4_TEST)


def sugg4_ref(clone: Path) -> str:
    replace_once(clone / "internal/tui/plain.go",
                 '\t\t\tfmt.Fprintf(&b, "      git %s\\n", strings.Join(u.Suggested.Argv, " "))\n',
                 _tabs('''            note := ""
            if u.Suggested.Class == protocol.ClassManual {
                note += " (do it by hand)"
            }
            if len(u.Suggested.Blocked) > 0 {
                note += " (blocked: " + strings.Join(u.Suggested.Blocked, "; ") + ")"
            }
            fmt.Fprintf(&b, "      git %s%s\\n", strings.Join(u.Suggested.Argv, " "), note)
'''))
    return ""


# ---- step 5 ---------------------------------------------------------------

SUGG5_TEST = r'''import (
    "strings"
    "testing"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

func hiddenSuggSnap(sugg *protocol.Action) *protocol.Snapshot {
    return &protocol.Snapshot{
        Revision: "rev1",
        Units:    []protocol.Unit{{ID: "branch:feat/x", Branch: "feat/x", Verdict: "never-pushed", Suggested: sugg}},
        Forks:    []protocol.ForkPoint{{SHA: "abc", Branches: []string{"feat/x"}}},
    }
}

func TestHiddenSuggestKeyConfirmsAndRuns(t *testing.T) {
    act := &protocol.Action{ID: "branch:feat/x/push", Kind: "push",
        Argv: []string{"push", "-u", "origin", "feat/x:feat/x"}, Class: protocol.ClassSafe}
    eng := &fakeEngine{executeResult: &protocol.ExecuteResult{Ran: true, Revision: "rev2"}}
    a := New(eng)
    a.snap = hiddenSuggSnap(act)
    a.tab, a.cursor = tabTopology, 1

    a.onKey(keyRune('s'))
    if a.confirm == nil || a.confirm.kind != confirmAction || a.confirm.action == nil || a.confirm.action.ID != act.ID {
        t.Fatalf("s must open the action confirm for the suggestion, got %+v", a.confirm)
    }
    _, cmd := a.onKey(keyRune('y'))
    if cmd == nil {
        t.Fatal("confirming must return a Cmd that runs the action")
    }
    cmd()
    if eng.executeReq.ActionID != act.ID || eng.executeReq.Revision != "rev1" || eng.executeReq.Confirmation != confirmationOf(act.Argv) {
        t.Fatalf("Execute request = %+v", eng.executeReq)
    }
}

func TestHiddenSuggestKeyRefusesWhatItCannotRun(t *testing.T) {
    cases := map[string]*protocol.Action{
        "nil":     nil,
        "manual":  {ID: "branch:feat/x/rebase", Argv: []string{"rebase", "origin/main"}, Class: protocol.ClassManual},
        "blocked": {ID: "branch:feat/x/merge", Argv: []string{"merge", "x"}, Class: protocol.ClassGuarded, Blocked: []string{"worktree has no uncommitted changes"}},
    }
    for name, sugg := range cases {
        eng := &fakeEngine{}
        a := New(eng)
        a.snap = hiddenSuggSnap(sugg)
        a.tab, a.cursor = tabTopology, 1
        a.onKey(keyRune('s'))
        if a.confirm != nil {
            t.Fatalf("%s: s must not open a confirm, got %+v", name, a.confirm)
        }
        if a.status != "no runnable suggestion" {
            t.Fatalf("%s: status = %q, want %q", name, a.status, "no runnable suggestion")
        }
        if eng.executeReq.ActionID != "" {
            t.Fatalf("%s: nothing may execute", name)
        }
    }
}

func TestHiddenFooterListsSuggestKey(t *testing.T) {
    a := New(&fakeEngine{})
    a.snap = hiddenSuggSnap(nil)
    if f := a.footer(160); !strings.Contains(f, "run suggested") {
        t.Fatalf("footer help lacks the s key (\"run suggested\"):\n%s", f)
    }
}
'''


def sugg5_check(clone: Path, result: str) -> tuple[bool, str]:
    return hidden_go_test(clone, "internal/tui", "tui", SUGG5_TEST)


def sugg5_ref(clone: Path) -> str:
    model = clone / "internal/tui/model.go"
    replace_once(model, '\tcase "w":\n\t\ta.wtAdd()\n',
                 '\tcase "s":\n\t\ta.runSuggested()\n\tcase "w":\n\t\ta.wtAdd()\n')
    replace_once(model, "// wtAdd stages a confirm for creating a worktree",
                 _tabs('''// runSuggested stages the y/n confirm for the selected unit's own suggested
// action - the same confirm a decision-tree leaf opens - when that action is
// one the engine may run (not manual, nothing blocking it).
func (a *App) runSuggested() {
    u, ok := a.selectedUnit()
    if !ok || u.Suggested == nil || u.Suggested.Class == protocol.ClassManual || len(u.Suggested.Blocked) > 0 {
        a.status = "no runnable suggestion"
        return
    }
    a.confirm = &pendingConfirm{kind: confirmAction, action: u.Suggested}
}

''') + "// wtAdd stages a confirm for creating a worktree")
    replace_once(clone / "internal/tui/view.go",
                 '\t\tstyKey.Render("p") + styFaint.Render(" decide  ") +\n',
                 '\t\tstyKey.Render("p") + styFaint.Render(" decide  ") +\n'
                 '\t\tstyKey.Render("s") + styFaint.Render(" run suggested  ") +\n')
    return ""


# ---- step 6 ---------------------------------------------------------------


def sugg6_check(clone: Path, result: str) -> tuple[bool, str]:
    section = _readme_section(clone, "Acting on it")
    if "`s`" not in section:
        return False, "README 'Acting on it' does not document the `s` key"
    ok, d = build_and_vet(clone)
    if not ok:
        return False, d
    tmp = Path(tempfile.mkdtemp(prefix="hv-h-sugg6-"))
    try:
        repo = _fixture_repo(tmp, branch="feat/x", fetch=False)
        snap, err = _json_run(clone, tmp, repo)
        if snap is None:
            return False, err
        units = snap.get("units") or []
        lacking = [u.get("branch") for u in units if "suggested" not in u]
        if lacking:
            return False, f"units without a 'suggested' key in -json: {lacking}"
        by = {u.get("branch"): u for u in units}
        feat, main = by.get("feat/x"), by.get("main")
        if not feat or not isinstance(feat["suggested"], dict) or \
                feat["suggested"].get("argv") != ["push", "-u", "origin", "feat/x:feat/x"]:
            return False, f"feat/x suggested = {feat and feat.get('suggested')!r}"
        if not main or main["suggested"] is not None:
            return False, f"main suggested = {main and main.get('suggested')!r}, want null"
        return True, "json ok"
    finally:
        _rmtree(tmp)


def sugg6_ref(clone: Path) -> str:
    replace_once(clone / "internal/protocol/protocol.go",
                 '`json:"suggested,omitempty"` // the engine\'s own pick',
                 '`json:"suggested"`           // null when none; the engine\'s own pick')
    replace_once(clone / "README.md",
                 "one level at a time; at the top level it leaves the tree entirely.\n",
                 "one level at a time; at the top level it leaves the tree entirely.\n\n"
                 "`s` skips the tree: it runs the selected branch's suggested action (the\n"
                 "same fix `-plain` prints under it) behind the same confirmation bar, and\n"
                 "says `no runnable suggestion` when that action is manual or blocked.\n")
    return ""


# =============================================================== H-stale ===

STALE_PROMPTS = [
    # 1 investigate
    "For a repository built by the `newFixture` test helper "
    "(internal/model/landing_test.go) and then given a local branch or two, what is "
    "`Unit.Stale` for each unit after `Build`, and exactly why? Trace the value back "
    "to its source. Do not change any code. Write your findings to ANSWER.md at the "
    "repository root.",
    # 2 StaleAfter
    "The staleness threshold behind `Unit.Stale` is hard-coded. Add "
    "`StaleAfter time.Duration` to `model.Options` and to `engine.SnapshotOptions`, "
    "wired through so the engine's Snapshot uses it for every unit's `Stale`. Zero "
    "keeps today's default. `go build ./...` and `go vet ./...` must pass.",
    # 3 Freshness.Stale
    "Staleness is a fact about the repository, not about any one branch, but the "
    "protocol only exposes it per unit. Add a `Stale bool` field with JSON key "
    "`stale` to `protocol.Freshness` and set it in every Snapshot, using the same "
    "threshold (including a configured StaleAfter) that decides the units' Stale. "
    "`go build ./...` and `go vet ./...` must pass.",
    # 4 revision hygiene
    "A Snapshot's Revision is supposed to change only when the repository's state "
    "changes. Today, merely crossing the staleness threshold (time passing, or a "
    "different StaleAfter) changes it even though nothing in the repository "
    "changed. Fix that: staleness derived from the clock must not feed the "
    "Revision, while everything else that feeds it today still must. Explain the "
    "exclusion in revisionOf's doc comment.",
    # 5 warnings
    "Warn about a stale remote view. When the snapshot's freshness is stale, the "
    "TUI header must show a warning containing the word `stale`, and "
    "`landfirst -plain` must print the line `  remote view is stale - run git fetch` "
    "right after its at-risk/all-clear banner line, before the first verdict group. "
    "Neither appears when the view is fresh.",
    # 6 flag
    "Add a `-stale-after` flag to the landfirst command: a duration, default `1h`, "
    "that sets the staleness threshold for every Snapshot the command takes "
    "(`-plain`, `-json` and the interactive panel alike). `go build ./...` and "
    "`go vet ./...` must pass.",
]

# ---- step 1 ---------------------------------------------------------------


def stale1_check(clone: Path, result: str) -> tuple[bool, str]:
    return _answer_has(clone, "FETCH_HEAD", nocase=("true",))


def stale1_ref(clone: Path) -> str:
    write_file(clone, "ANSWER.md", """# Unit.Stale in a newFixture repository

Every unit has `Stale == true`.

Why: `newFixture` only inits, commits and `push -u`s; nothing ever fetches, so
`.git/FETCH_HEAD` does not exist. `collectFreshness` (internal/model/collect.go)
stats `rev-parse --git-path FETCH_HEAD`, the stat fails and it returns a zero
`Freshness{}` (zero `FetchHeadAt`). `Build` (internal/model/build.go) then sets
every unit's `Stale = state.IsStale(FetchHeadAt, time.Hour)`, and
`state.IsStale` is `time.Since(zero) > 1h` - always true. It is a repo-wide
value: every unit gets the same answer regardless of its own state.
""")
    return ""


# ---- step 2 ---------------------------------------------------------------

STALE2_MODEL_TEST = r'''import (
    "testing"
    "time"

    "github.com/montjeffrey/landfirst/internal/git"
)

func TestHiddenStaleAfterOption(t *testing.T) {
    f := newFixture(t)
    f.run("checkout", "-q", "-b", "feat/s")
    f.commit("s.txt", "work", "local work")
    f.run("checkout", "-q", "main")
    f.run("fetch", "-q", "origin")

    repo, err := git.Open(f.dir)
    if err != nil {
        t.Fatal(err)
    }
    check := func(name string, opt Options, want bool) {
        t.Helper()
        m := Build(repo, opt)
        if len(m.Units) < 2 {
            t.Fatalf("%s: expected at least 2 units, got %d", name, len(m.Units))
        }
        for _, u := range m.Units {
            if u.Stale != want {
                t.Fatalf("%s: unit %s Stale = %v, want %v", name, u.Branch, u.Stale, want)
            }
        }
    }
    check("StaleAfter 24h", Options{StaleAfter: 24 * time.Hour}, false)
    check("StaleAfter 0 (default)", Options{}, false)
    time.Sleep(20 * time.Millisecond)
    check("StaleAfter 1ns", Options{StaleAfter: time.Nanosecond}, true)
}
'''

STALE2_ENGINE_TEST = r'''import (
    "context"
    "testing"
    "time"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

func TestHiddenSnapshotStaleAfterWired(t *testing.T) {
    f := newEfixture(t)
    f.run("checkout", "-q", "-b", "feat/s")
    f.commit("s.txt", "work", "local work")
    f.run("checkout", "-q", "main")
    f.run("fetch", "-q", "origin")

    eng, err := Open(f.dir)
    if err != nil {
        t.Fatal(err)
    }
    defer eng.Close()
    ctx := context.Background()
    check := func(after time.Duration, want bool) {
        t.Helper()
        s, err := eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast, StaleAfter: after})
        if err != nil {
            t.Fatal(err)
        }
        for _, u := range s.Units {
            if u.Stale != want {
                t.Fatalf("StaleAfter %v: unit %s Stale = %v, want %v", after, u.ID, u.Stale, want)
            }
        }
    }
    check(24*time.Hour, false)
    time.Sleep(20 * time.Millisecond)
    check(time.Nanosecond, true)
}
'''


def stale2_check(clone: Path, result: str) -> tuple[bool, str]:
    r1 = hidden_go_test(clone, "internal/model", "model", STALE2_MODEL_TEST)
    if not r1[0]:
        return r1
    r2 = hidden_go_test(clone, "internal/engine", "engine", STALE2_ENGINE_TEST)
    if not r2[0]:
        return r2
    return _all(r1, r2, build_and_vet(clone))


def stale2_ref(clone: Path) -> str:
    build = clone / "internal/model/build.go"
    replace_once(build, "\tMaxBranches int // branches given their own lane (0 = default)\n",
                 "\tMaxBranches int // branches given their own lane (0 = default)\n"
                 "\tStaleAfter  time.Duration // FETCH_HEAD age past which units are Stale (0 = one hour)\n")
    replace_once(build, "\tconst staleThreshold = time.Hour\n",
                 "\tstaleThreshold := opt.StaleAfter\n\tif staleThreshold <= 0 {\n"
                 "\t\tstaleThreshold = time.Hour\n\t}\n")
    eng = clone / "internal/engine/engine.go"
    replace_once(eng, "type SnapshotOptions struct {\n\tStage    protocol.Stage\n\tMaxPairs int\n}",
                 "type SnapshotOptions struct {\n\tStage    protocol.Stage\n\tMaxPairs int\n"
                 "\t// StaleAfter is the FETCH_HEAD age past which units are Stale; 0 means one hour.\n"
                 "\tStaleAfter time.Duration\n}")
    replace_once(eng, "\t\tMaxBranches: 10,\n\t}\n\tif opt.MaxPairs == 0 {",
                 "\t\tMaxBranches: 10,\n\t\tStaleAfter:  o.StaleAfter,\n\t}\n\tif opt.MaxPairs == 0 {")
    replace_once(eng, '\t"sync/atomic"\n', '\t"sync/atomic"\n\t"time"\n')
    return ""


# ---- step 3 ---------------------------------------------------------------

STALE3_TEST = r'''import (
    "context"
    "encoding/json"
    "strings"
    "testing"
    "time"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

func TestHiddenFreshnessStale(t *testing.T) {
    f := newEfixture(t)
    f.run("checkout", "-q", "-b", "feat/s")
    f.commit("s.txt", "work", "local work")
    f.run("checkout", "-q", "main")

    eng, err := Open(f.dir)
    if err != nil {
        t.Fatal(err)
    }
    defer eng.Close()
    ctx := context.Background()

    s, err := eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast})
    if err != nil {
        t.Fatal(err)
    }
    if !s.Freshness.Stale {
        t.Fatal("never fetched: Freshness.Stale = false, want true")
    }
    b, _ := json.Marshal(s.Freshness)
    if !strings.Contains(string(b), `"stale":true`) {
        t.Fatalf("Freshness JSON = %s, want a \"stale\" key", b)
    }

    f.run("fetch", "-q", "origin")
    s, err = eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast, StaleAfter: 1000 * time.Hour})
    if err != nil {
        t.Fatal(err)
    }
    if s.Freshness.Stale {
        t.Fatal("just fetched, StaleAfter 1000h: Freshness.Stale = true, want false")
    }
    time.Sleep(20 * time.Millisecond)
    s, err = eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast, StaleAfter: time.Nanosecond})
    if err != nil {
        t.Fatal(err)
    }
    if !s.Freshness.Stale {
        t.Fatal("StaleAfter 1ns: Freshness.Stale = false, want true")
    }
}
'''


def stale3_check(clone: Path, result: str) -> tuple[bool, str]:
    ok, d = hidden_go_test(clone, "internal/engine", "engine", STALE3_TEST)
    if not ok:
        return False, d
    return _all((True, d), build_and_vet(clone))


def stale3_ref(clone: Path) -> str:
    replace_once(clone / "internal/protocol/protocol.go",
                 '\tFetchHeadAt time.Time `json:"fetchHeadAt"`\n}',
                 '\tFetchHeadAt time.Time `json:"fetchHeadAt"`\n'
                 '\tStale       bool      `json:"stale"` // FetchHeadAt is older than the snapshot\'s StaleAfter\n}')
    replace_once(clone / "internal/model/types.go",
                 '\tFetchHeadAt time.Time `json:"fetchHeadAt"` // mtime of .git/FETCH_HEAD\n}',
                 '\tFetchHeadAt time.Time `json:"fetchHeadAt"` // mtime of .git/FETCH_HEAD\n'
                 '\tStale       bool      `json:"stale"`       // FetchHeadAt is past Options.StaleAfter\n}')
    replace_once(clone / "internal/model/build.go",
                 "\tfor i := range m.Units {\n\t\tm.Units[i].Stale = state.IsStale(m.Freshness.FetchHeadAt, staleThreshold)\n\t}\n",
                 "\tm.Freshness.Stale = state.IsStale(m.Freshness.FetchHeadAt, staleThreshold)\n"
                 "\tfor i := range m.Units {\n\t\tm.Units[i].Stale = m.Freshness.Stale\n\t}\n")
    replace_once(clone / "internal/engine/engine.go",
                 "protocol.Freshness{FetchHeadAt: m.Freshness.FetchHeadAt}",
                 "protocol.Freshness{FetchHeadAt: m.Freshness.FetchHeadAt, Stale: m.Freshness.Stale}")
    return ""


# ---- step 4 ---------------------------------------------------------------

STALE4_TEST = r'''import (
    "context"
    "testing"
    "time"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

func TestHiddenStalenessDoesNotMoveRevision(t *testing.T) {
    f := newEfixture(t)
    f.run("checkout", "-q", "-b", "feat/s")
    f.commit("s.txt", "work", "local work")
    f.run("checkout", "-q", "main")
    f.run("fetch", "-q", "origin")
    time.Sleep(20 * time.Millisecond)

    eng, err := Open(f.dir)
    if err != nil {
        t.Fatal(err)
    }
    defer eng.Close()
    ctx := context.Background()
    snap := func(after time.Duration) *protocol.Snapshot {
        t.Helper()
        s, err := eng.Snapshot(ctx, SnapshotOptions{Stage: protocol.StageFast, StaleAfter: after})
        if err != nil {
            t.Fatal(err)
        }
        return s
    }
    stale, fresh := snap(time.Nanosecond), snap(1000*time.Hour)
    if !stale.Freshness.Stale || fresh.Freshness.Stale {
        t.Fatalf("Freshness.Stale: 1ns=%v 1000h=%v, want true/false", stale.Freshness.Stale, fresh.Freshness.Stale)
    }
    if stale.Revision != fresh.Revision {
        t.Fatalf("crossing the stale threshold changed the revision: %s vs %s", stale.Revision, fresh.Revision)
    }

    f.run("checkout", "-q", "feat/s")
    f.commit("s2.txt", "more", "more local work")
    f.run("checkout", "-q", "main")
    if snap(1000*time.Hour).Revision == fresh.Revision {
        t.Fatal("a new commit must still change the revision")
    }
}
'''


def _revision_doc(clone: Path) -> str:
    lines = read(clone, "internal/engine/engine.go").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("func revisionOf("):
            doc = []
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith("//"):
                doc.append(lines[j])
                j -= 1
            return "\n".join(reversed(doc))
    return ""


def stale4_check(clone: Path, result: str) -> tuple[bool, str]:
    ok, d = hidden_go_test(clone, "internal/engine", "engine", STALE4_TEST)
    if not ok:
        return False, d
    if "stale" not in _revision_doc(clone).lower():
        return False, "revisionOf's doc comment does not explain the staleness exclusion"
    return _all((True, d), build_and_vet(clone))


def stale4_ref(clone: Path) -> str:
    eng = clone / "internal/engine/engine.go"
    replace_once(eng,
                 "// something the client would see differently changed. BuildMillis is the one\n"
                 "// field deliberately excluded - it is real, but it is timing noise, not state.\n",
                 "// something the client would see differently changed. Two kinds of field are\n"
                 "// deliberately excluded: BuildMillis (real, but timing noise, not state), and\n"
                 "// the stale flags (Freshness.Stale and every Unit.Stale). Those are derived\n"
                 "// from the clock and the StaleAfter threshold, not from the repository, so\n"
                 "// time passing would otherwise move the revision with nothing changed.\n"
                 "// FetchHeadAt itself stays in: a fetch is a real change.\n")
    replace_once(eng, "\tstable := struct {\n",
                 "\tfresh := s.Freshness\n\tfresh.Stale = false\n"
                 "\tunits := make([]protocol.Unit, len(s.Units))\n\tcopy(units, s.Units)\n"
                 "\tfor i := range units {\n\t\tunits[i].Stale = false\n\t}\n"
                 "\tstable := struct {\n")
    replace_once(eng, "}{s.Repo, s.Freshness, s.Units, s.Worktrees, s.Forks, s.Contentions, s.Plan}",
                 "}{s.Repo, fresh, units, s.Worktrees, s.Forks, s.Contentions, s.Plan}")
    return ""


# ---- step 5 ---------------------------------------------------------------

STALE5_TEST = r'''import (
    "strings"
    "testing"

    "github.com/montjeffrey/landfirst/internal/protocol"
)

func hiddenStaleSnap(stale bool) *protocol.Snapshot {
    return &protocol.Snapshot{
        Revision:  "rev1",
        Repo:      protocol.RepoInfo{Name: "myrepo", Base: "main", GitVersion: "2.43.0", ConflictMode: "full"},
        Freshness: protocol.Freshness{Stale: stale},
        Units:     []protocol.Unit{{ID: "branch:feat/a", Branch: "feat/a", Verdict: "never-pushed", Detail: "1 change never pushed"}},
        Forks:     []protocol.ForkPoint{{SHA: "abc", Short: "abc", Branches: []string{"feat/a"}}},
        Summary:   protocol.Summary{StrandedCommits: 1, AtRisk: 1},
    }
}

func TestHiddenTUIHeaderWarnsWhenStale(t *testing.T) {
    for _, stale := range []bool{true, false} {
        a := New(&fakeEngine{})
        a.w, a.h = 120, 40
        a.snap = hiddenStaleSnap(stale)
        a.status = ""
        got := strings.Contains(strings.ToLower(a.View()), "stale")
        if got != stale {
            t.Fatalf("Freshness.Stale=%v but the panel shows a stale warning: %v\n%s", stale, got, a.View())
        }
    }
}

func TestHiddenPlainWarnsWhenStale(t *testing.T) {
    const line = "  remote view is stale - run git fetch\n"
    out := PlainReport(hiddenStaleSnap(true))
    i := strings.Index(out, line)
    banner := strings.Index(out, "exist only on this machine")
    group := strings.Index(out, "NEVER PUSHED")
    if i < 0 || banner < 0 || group < 0 || !(banner < i && i < group) {
        t.Fatalf("want %q after the banner and before the first group:\n%s", line, out)
    }
    if strings.Contains(PlainReport(hiddenStaleSnap(false)), "remote view is stale") {
        t.Fatal("a fresh view must not print the stale warning")
    }
}
'''


def stale5_check(clone: Path, result: str) -> tuple[bool, str]:
    return hidden_go_test(clone, "internal/tui", "tui", STALE5_TEST)


def stale5_ref(clone: Path) -> str:
    plain = clone / "internal/tui/plain.go"
    replace_once(plain,
                 'exist only on this machine\\n\\n", s.Summary.StrandedCommits, s.Summary.AtRisk)\n'
                 '\t} else {\n\t\tb.WriteString("  everything with local work has reached the remote\\n\\n")\n\t}\n',
                 'exist only on this machine\\n", s.Summary.StrandedCommits, s.Summary.AtRisk)\n'
                 '\t} else {\n\t\tb.WriteString("  everything with local work has reached the remote\\n")\n\t}\n'
                 '\tif s.Freshness.Stale {\n\t\tb.WriteString("  remote view is stale - run git fetch\\n")\n\t}\n'
                 '\tb.WriteString("\\n")\n')
    replace_once(clone / "internal/tui/view.go",
                 '\t\tbanner = styBanner.Foreground(cOK).Render("  everything with local work has reached the remote")\n\t}\n',
                 '\t\tbanner = styBanner.Foreground(cOK).Render("  everything with local work has reached the remote")\n\t}\n'
                 '\tif snap.Freshness.Stale {\n'
                 '\t\tbanner += styBanner.Foreground(cWarn).Render("  ·  remote view is stale - run git fetch")\n\t}\n')
    return ""


# ---- step 6 ---------------------------------------------------------------

_FLAG_RE = re.compile(r'flag\.Duration(Var)?\(\s*(&[\w.]+\s*,\s*)?"stale-after"')


def stale6_check(clone: Path, result: str) -> tuple[bool, str]:
    if not _FLAG_RE.search(read(clone, "cmd/landfirst/main.go")):
        return False, 'cmd/landfirst/main.go has no flag.Duration("stale-after", ...)'
    ok, d = build_and_vet(clone)
    if not ok:
        return False, d
    tmp = Path(tempfile.mkdtemp(prefix="hv-h-stale6-"))
    try:
        repo = _fixture_repo(tmp, branch="feat/s", fetch=True)
        for args, want in ((("-stale-after", "1000h"), False), ((), False),
                           (("-stale-after", "1ns"), True)):
            snap, err = _json_run(clone, tmp, repo, *args)
            if snap is None:
                return False, err
            got = (snap.get("freshness") or {}).get("stale")
            if got is not want:
                return False, f"-json {' '.join(args) or '(default)'}: freshness.stale = {got!r}, want {want}"
        code, out = _run_binary(clone, tmp, repo, "-h", merge_stderr=True)
        usage = re.search(r"-stale-after[^\n]*\n(?:[ \t]{4,}[^\n]*\n)*", out)
        if not usage or "default 1h0m0s" not in usage.group(0):
            return False, f"-stale-after must default to 1h; usage: {out[-600:]}"
        code, out = _run_binary(clone, tmp, repo, "-plain", "-no-probe", "-stale-after", "1ns")
        if code != 0 or "remote view is stale" not in out:
            return False, f"-plain -stale-after 1ns: exit {code}, no stale warning: {out[-400:]}"
        return True, "flag ok"
    finally:
        _rmtree(tmp)


def stale6_ref(clone: Path) -> str:
    main = clone / "cmd/landfirst/main.go"
    replace_once(main, '\t"os"\n', '\t"os"\n\t"time"\n')
    replace_once(main,
                 '\t\tall      = flag.Bool("all", false, "draw the entire topology: every branch, every commit")\n',
                 '\t\tall      = flag.Bool("all", false, "draw the entire topology: every branch, every commit")\n'
                 '\t\tstaleAfter = flag.Duration("stale-after", time.Hour, "how old the last fetch may be before the remote view is stale")\n')
    replace_once(main, "snapOpt := engine.SnapshotOptions{Stage: stage, MaxPairs: *maxPairs}",
                 "snapOpt := engine.SnapshotOptions{Stage: stage, MaxPairs: *maxPairs, StaleAfter: *staleAfter}")
    return ""


# ================================================================= TASKS ===


def _chain(prompts, checks, refs) -> dict:
    return dict(family="H", repo="landfirst", setup=None, measure_from=0,
                steps=[dict(prompt=p, check=c, reference=r)
                       for p, c, r in zip(prompts, checks, refs, strict=True)])


TASKS = {
    "H-sugg": _chain(
        SUGG_PROMPTS,
        [sugg1_check, sugg2_check, sugg3_check, sugg4_check, sugg5_check, sugg6_check],
        [sugg1_ref, sugg2_ref, sugg3_ref, sugg4_ref, sugg5_ref, sugg6_ref]),
    "H-stale": _chain(
        STALE_PROMPTS,
        [stale1_check, stale2_check, stale3_check, stale4_check, stale5_check, stale6_check],
        [stale1_ref, stale2_ref, stale3_ref, stale4_ref, stale5_ref, stale6_ref]),
}
