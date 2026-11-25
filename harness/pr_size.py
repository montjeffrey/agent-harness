"""Measure a branch the way its pull request will look to a reviewer.

``agent-harness-pr size`` counts the committed changes between a branch and
the point where it left its base branch (the merge-base), using the same
definition as the GitHub PR header: additions plus deletions, with renames
detected so a moved file counts only its edited lines. Commits that landed on
the base after the branch was cut are not counted, nor are edits that are not
committed yet -- neither is in what a push sends.

A branch cut from another unmerged branch is measured against the base, so it
carries both diffs; that is what its reviewer would read, and the reason the
review policy forbids stacking.

This module only measures. Nothing here blocks a push or a pull request.
Every git call passes ``stdin=DEVNULL`` (see `harness.work._git`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_MAX_LINES = 500
_GIT_TIMEOUT_S = 30
_BASE_CANDIDATES = ("origin/main", "origin/master", "main", "master")
_LARGEST_SHOWN = 5


class SizeError(Exception):
    """The branch could not be measured (no base branch, no merge-base, git failed)."""


@dataclass(frozen=True)
class FileCount:
    path: str
    added: int | None  # None for a binary file
    removed: int | None

    @property
    def changed(self) -> int:
        return (self.added or 0) + (self.removed or 0)


@dataclass(frozen=True)
class SizeReport:
    base: str
    merge_base: str
    head: str
    files: tuple[FileCount, ...]

    @property
    def added(self) -> int:
        return sum(f.added or 0 for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed or 0 for f in self.files)

    @property
    def changed(self) -> int:
        return self.added + self.removed

    @property
    def binary(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files if f.added is None)


def _git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SizeError(f"git {args[0]} failed: {exc}") from exc
    if r.returncode != 0:
        raise SizeError(f"git {' '.join(args)}: {r.stderr.strip() or f'exit {r.returncode}'}")
    return r.stdout


def _commit_of(root: Path, ref: str) -> str | None:
    try:
        return _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except SizeError:
        return None


def resolve_base(root: Path, explicit: str | None = None) -> str:
    """The branch a pull request from here would target.

    An explicit ref wins; otherwise the remote's default branch
    (``origin/HEAD``), then the first of origin/main, origin/master, main,
    master that exists.
    """
    if explicit:
        if _commit_of(root, explicit) is None:
            raise SizeError(f"base {explicit!r} is not a commit in {root}")
        return explicit
    try:
        head = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    except SizeError:
        head = ""
    if head.startswith("refs/remotes/") and _commit_of(root, head):
        return head.removeprefix("refs/remotes/")
    for candidate in _BASE_CANDIDATES:
        if _commit_of(root, candidate):
            return candidate
    raise SizeError("no base branch found; pass --base (looked for origin/HEAD, " + ", ".join(_BASE_CANDIDATES) + ")")


def _numstat(root: Path, old: str, new: str) -> tuple[FileCount, ...]:
    # -z keeps paths unquoted; a rename's record is "A\tD\t" followed by the
    # old and new paths as their own NUL-separated fields.
    tokens = _git(root, "diff", "--numstat", "-z", "-M", old, new).split("\0")
    out: list[FileCount] = []
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if not record:
            continue
        added, removed, path = record.split("\t", 2)
        if not path and i + 1 < len(tokens):
            path = f"{tokens[i]} => {tokens[i + 1]}"
            i += 2
        out.append(
            FileCount(
                path=path,
                added=int(added) if added.isdigit() else None,
                removed=int(removed) if removed.isdigit() else None,
            )
        )
    return tuple(out)


def measure(root: Path, base: str | None = None, head: str = "HEAD") -> SizeReport:
    """Committed changes on ``head`` since it left ``base``."""
    base_ref = resolve_base(root, base)
    head_sha = _commit_of(root, head)
    if head_sha is None:
        raise SizeError(f"{head!r} is not a commit in {root}")
    try:
        merge_base = _git(root, "merge-base", base_ref, head_sha).strip()
    except SizeError as exc:
        raise SizeError(f"{head} shares no history with {base_ref}") from exc
    return SizeReport(base=base_ref, merge_base=merge_base, head=head, files=_numstat(root, merge_base, head_sha))


def format_report(report: SizeReport, max_lines: int) -> str:
    noun = "file" if len(report.files) == 1 else "files"
    lines = [
        f"{report.changed} changed lines (+{report.added} -{report.removed}) across "
        f"{len(report.files)} {noun}, {report.head} vs {report.base} (merge-base {report.merge_base[:7]})"
    ]
    over = report.changed - max_lines
    if over > 0:
        lines.append(f"Limit {max_lines}: over by {over}. Split it into single-purpose PRs before review.")
    else:
        lines.append(f"Limit {max_lines}: within, {-over} to spare.")
    largest = sorted((f for f in report.files if f.added is not None), key=lambda f: -f.changed)
    if largest:
        lines.append("Largest files:")
        lines.extend(f"  {f.changed:>6}  {f.path}" for f in largest[:_LARGEST_SHOWN])
    if report.binary:
        lines.append("Binary, not counted: " + ", ".join(report.binary))
    return "\n".join(lines)


def _as_json(report: SizeReport, max_lines: int) -> str:
    return json.dumps(
        {
            "base": report.base,
            "merge_base": report.merge_base,
            "head": report.head,
            "added": report.added,
            "removed": report.removed,
            "changed": report.changed,
            "max_lines": max_lines,
            "over_by": max(0, report.changed - max_lines),
            "files": [asdict(f) for f in report.files],
            "binary": list(report.binary),
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-harness-pr", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    size = sub.add_parser("size", help="count this branch's changed lines against its base branch")
    size.add_argument("--repo", type=Path, default=Path.cwd(), help="repository to measure (default: cwd)")
    size.add_argument("--base", help="base branch (default: origin/HEAD, then origin/main, main, ...)")
    size.add_argument("--head", default="HEAD", help="commit to measure (default: HEAD)")
    size.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES, help="review-size limit to report against")
    size.add_argument("--json", action="store_true", help="print the counts as JSON")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        report = measure(args.repo, args.base, args.head)
    except SizeError as exc:
        print(f"cannot measure: {exc}", file=sys.stderr)
        return 2
    print(_as_json(report, args.max_lines) if args.json else format_report(report, args.max_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
