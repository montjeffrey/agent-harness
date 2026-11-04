"""`agent-harness-pr size`: a branch's changed lines against its merge-base.

The number has to match what a reviewer sees on the pull request, so these
tests pin the GitHub header's definition (additions plus deletions, renames
detected) and the two things that make a local count lie: commits that landed
on the base after the branch was cut, and edits that are not committed yet.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness import pr_size


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _lines(n: int, tag: str = "line") -> str:
    return "".join(f"{tag} {i}\n" for i in range(n))


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.name", "Test User")
    _run(root, "config", "user.email", "test@example.test")
    _run(root, "config", "core.autocrlf", "false")
    _commit(root, {"app.py": _lines(10)}, "initial")
    return root


def test_counts_additions_plus_deletions_since_the_merge_base(repo: Path) -> None:
    _run(repo, "switch", "-q", "-c", "feature")
    _commit(repo, {"app.py": _lines(7) + _lines(5, "new"), "test_app.py": _lines(20)}, "feature")
    _run(repo, "switch", "-q", "main")
    _commit(repo, {"later.py": _lines(300)}, "main moves on after the branch was cut")
    _run(repo, "switch", "-q", "feature")

    report = pr_size.measure(repo, base="main")

    # app.py: 3 lines removed, 5 added; test_app.py: 20 added -- tests count too.
    assert (report.added, report.removed, report.changed) == (25, 3, 28)
    assert {f.path for f in report.files} == {"app.py", "test_app.py"}
    assert report.merge_base != _run(repo, "rev-parse", "main")


def test_a_branch_cut_from_an_unmerged_branch_carries_both_diffs(repo: Path) -> None:
    _run(repo, "switch", "-q", "-c", "parent")
    _commit(repo, {"parent.py": _lines(400)}, "parent work, not merged")
    _run(repo, "switch", "-q", "-c", "child")
    _commit(repo, {"child.py": _lines(40)}, "child work")

    assert pr_size.measure(repo, base="main").changed == 440


def test_a_pure_rename_counts_no_lines(repo: Path) -> None:
    _run(repo, "switch", "-q", "-c", "move")
    _run(repo, "mv", "app.py", "core.py")
    _run(repo, "commit", "-q", "-m", "rename only")

    report = pr_size.measure(repo, base="main")

    assert report.changed == 0
    assert len(report.files) == 1


def test_binary_files_are_listed_but_add_no_lines(repo: Path) -> None:
    _run(repo, "switch", "-q", "-c", "assets")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00" * 50)
    _commit(repo, {"notes.md": _lines(4)}, "binary plus text")

    report = pr_size.measure(repo, base="main")

    assert report.changed == 4
    assert report.binary == ("logo.png",)


def test_uncommitted_edits_are_not_counted(repo: Path) -> None:
    _run(repo, "switch", "-q", "-c", "wip")
    _commit(repo, {"done.py": _lines(6)}, "committed")
    (repo / "scratch.py").write_text(_lines(900), encoding="utf-8")

    assert pr_size.measure(repo, base="main").changed == 6


def test_the_base_defaults_to_the_remote_default_branch(repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _run(tmp_path, "init", "-q", "--bare", str(remote))
    _run(repo, "remote", "add", "origin", str(remote))
    _run(repo, "push", "-q", "origin", "main")
    _run(repo, "remote", "set-head", "origin", "main")

    assert pr_size.resolve_base(repo) == "origin/main"
    assert pr_size.resolve_base(repo, "main") == "main"


def test_the_base_falls_back_to_a_local_main_without_a_remote(repo: Path) -> None:
    assert pr_size.resolve_base(repo) == "main"


def test_no_base_branch_is_an_error_not_a_zero(repo: Path) -> None:
    _run(repo, "branch", "-q", "-m", "main", "trunk")

    with pytest.raises(pr_size.SizeError):
        pr_size.resolve_base(repo)
    with pytest.raises(pr_size.SizeError):
        pr_size.measure(repo, base="no-such-branch")


def test_the_report_names_the_overage_and_the_largest_files(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(repo, "switch", "-q", "-c", "big")
    _commit(repo, {"big.py": _lines(450), "small.py": _lines(30), "tests/test_big.py": _lines(80)}, "big")

    code = pr_size.main(["size", "--repo", str(repo), "--base", "main", "--max-lines", "500"])

    out = capsys.readouterr().out
    assert code == 0  # measures only; nothing is blocked
    assert "560 changed lines" in out
    assert "over by 60" in out
    assert out.index("big.py") < out.index("tests/test_big.py") < out.index("small.py")


def test_json_output_carries_the_counts(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(repo, "switch", "-q", "-c", "small")
    _commit(repo, {"a.py": _lines(12)}, "small")

    assert pr_size.main(["size", "--repo", str(repo), "--base", "main", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["changed"] == 12
    assert data["max_lines"] == pr_size.DEFAULT_MAX_LINES
    assert data["over_by"] == 0
    assert data["base"] == "main"


def test_an_unmeasurable_branch_exits_2(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert pr_size.main(["size", "--repo", str(repo), "--base", "no-such-branch"]) == 2
    assert "cannot measure" in capsys.readouterr().err
