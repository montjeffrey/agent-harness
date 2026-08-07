"""Portable hierarchical memory CLI: init, remember, and status.

Capture is explicit. Nothing in this module runs automatically, summarizes
with an AI model, or opens a network socket -- it only reads and appends
local Markdown/JSONL under the project and machine memory roots described in
`harness.memory_store`.

Exit codes are part of the contract so automation can branch on them:

    0  success
    1  nothing to do (e.g. status on an uninitialized project)
    2  input rejected (schema, trust rule, or probable secret)

Usage:
    python harness/memory.py init [PATH]
    python harness/memory.py remember --type TYPE --summary TEXT [options]
    python harness/memory.py status [--project PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import memory_store
except ImportError:  # running as `python harness/memory.py`, not `-m harness.memory`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import memory_store


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="create project memory storage and register the project")
    init_p.add_argument(
        "path", nargs="?", default=".", help="project root (default: current directory)"
    )

    rem_p = sub.add_parser("remember", help="append one explicit entry to the proposal inbox")
    rem_p.add_argument("--type", choices=memory_store.ENTRY_TYPES, required=True)
    rem_p.add_argument("--summary", required=True, help="one concise, durable statement")
    rem_p.add_argument("--scope", choices=memory_store.SCOPES, default="project")
    rem_p.add_argument("--project", default=".", help="project root (default: current directory)")
    rem_p.add_argument("--tags", default=None, help="comma-separated tags")
    rem_p.add_argument("--commit", default=None, help="source commit SHA")
    rem_p.add_argument("--file", default=None, help="source path, repository-relative")
    rem_p.add_argument(
        "--confirmed", action="store_true",
        help="mark as confirmed rather than inferred (required before --accept)",
    )
    rem_p.add_argument(
        "--accept", action="store_true",
        help="record as accepted instead of proposed; needs --confirmed and a source",
    )
    rem_p.add_argument(
        "--manual", action="store_true",
        help="attest that you confirmed this by hand (counts as a source)",
    )

    status_p = sub.add_parser("status", help="report memory freshness, counts, and scope warnings")
    status_p.add_argument("--project", default=".", help="project root (default: current directory)")

    return parser.parse_args(argv)


def _split_tags(value: str | None) -> list[str] | None:
    if not value:
        return None
    tags = [t.strip() for t in value.split(",")]
    return [t for t in tags if t] or None


def _build_source(args: argparse.Namespace) -> dict | None:
    source: dict = {}
    if args.commit:
        source["commit"] = args.commit
    if args.file:
        source["file"] = args.file
    if args.manual:
        source["manual"] = True
    return source or None


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser()
    result = memory_store.init_project(root)
    print(f"agent-harness memory initialized in {root}")
    print(f"  project id: {result.project_id}")
    if result.created:
        for path in result.created:
            print(f"  created: {path}")
    else:
        print("  (already initialized -- nothing changed)")
    print("\nNext: describe the project in .agent-harness/scope.md, then record")
    print("work with `agent-harness-memory remember --type decision --summary ...`.")
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    directory = (
        memory_store.machine_memory_dir()
        if args.scope == "machine"
        else memory_store.project_memory_dir(root)
    )

    entry: dict = {
        "type": args.type,
        "scope": args.scope,
        "summary": args.summary,
        "confidence": "confirmed" if args.confirmed else "inferred",
        "status": "accepted" if args.accept else "proposed",
    }
    tags = _split_tags(args.tags)
    if tags:
        entry["tags"] = tags
    source = _build_source(args)
    if args.scope == "project":
        source = {**(source or {}), "project": memory_store.project_id_for(root)}
    if source:
        entry["source"] = source

    try:
        memory_store.append_entries([entry], directory=directory)
    except ValueError as exc:
        print(f"entry rejected: {exc}", file=sys.stderr)
        return 2

    written = list(memory_store.iter_entries(directory))[-1]
    print(f"remembered {written['id']}  [{written['status']}/{written['confidence']}] {written['type']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    base = root / ".agent-harness"
    directory = memory_store.project_memory_dir(root)

    if not base.is_dir():
        print(f"no memory storage in {root}")
        print("run `agent-harness-memory init` to create it")
        return 1

    entries = list(memory_store.iter_entries(directory))
    print(f"agent-harness memory: {root}")
    print(f"  project id: {memory_store.project_id_for(root)}")
    print(f"  entries: {len(entries)}")

    if entries:
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for rec in entries:
            by_status[rec.get("status", "?")] = by_status.get(rec.get("status", "?"), 0) + 1
            by_type[rec.get("type", "?")] = by_type.get(rec.get("type", "?"), 0) + 1
        print("  by status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
        print("  by type:   " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
        print(f"  newest:    {entries[-1].get('ts', '?')}")

        open_work = [r for r in entries if r.get("type") in ("task", "blocker")
                     and r.get("status") in ("proposed", "accepted")]
        if open_work:
            print(f"\n  open work ({len(open_work)}):")
            for rec in open_work[-5:]:
                print(f"    [{rec.get('type')}] {rec.get('summary')}")
    else:
        print("  (nothing recorded yet)")

    scope_path = base / "scope.md"
    if scope_path.is_file():
        text = scope_path.read_text(encoding="utf-8")
        if "_What this project is for._" in text:
            print("\n  WARN: scope.md is still the untouched template -- scope-drift")
            print("        detection cannot work until you fill it in.")
    return 0


_COMMANDS = {"init": cmd_init, "remember": cmd_remember, "status": cmd_status}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
