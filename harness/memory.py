"""Portable hierarchical memory CLI: init, remember, status, review, context,
and index.

Capture is explicit. Nothing in this module runs automatically or summarizes
with an AI model. `init`, `remember`, `status`, `review`, and `context` are
stdlib-only and never open a network socket; `index` is the one command that
may touch the optional `chromadb` extra, and it is deliberately separate from
`context` so the agent hot path can never trigger a model download.

Exit codes are part of the contract so automation can branch on them:

    0  success
    1  nothing to do, or a requested capability is unavailable
    2  input rejected (schema, trust rule, probable secret, bad transition)

Usage:
    python harness/memory.py init [PATH]
    python harness/memory.py remember --type TYPE --summary TEXT [options]
    python harness/memory.py status [--project PATH]
    python harness/memory.py review list | ACTION ENTRY_ID [options]
    python harness/memory.py context [--query TEXT] [--json] [--strategy S]
    python harness/memory.py index [--project PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import memory_context, memory_index, memory_review, memory_store
except ImportError:  # running as `python harness/memory.py`, not `-m harness.memory`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import memory_context, memory_index, memory_review, memory_store


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

    rev_p = sub.add_parser("review", help="list proposals or act on one")
    rev_p.add_argument(
        "action", choices=("list", *memory_review.REVIEW_ACTIONS),
        help="`list` shows what is awaiting review; the rest transition one entry",
    )
    rev_p.add_argument("entry_id", nargs="?", help="entry id, required for every action but `list`")
    rev_p.add_argument("--project", default=".", help="project root (default: current directory)")
    rev_p.add_argument("--scope", choices=memory_store.SCOPES, default="project")
    rev_p.add_argument(
        "--summary", default=None,
        help="replacement statement (required by supersede; amends wording on accept)",
    )
    rev_p.add_argument("--note", default=None, help="reviewer note; counts as resolve evidence")
    rev_p.add_argument("--commit", default=None, help="evidence commit SHA")
    rev_p.add_argument("--file", default=None, help="evidence path, repository-relative")

    ctx_p = sub.add_parser("context", help="emit a token-budgeted context packet")
    ctx_p.add_argument("--project", default=".", help="project root (default: current directory)")
    ctx_p.add_argument(
        "--query", default=None,
        help="what to select for (default: the active objective from scope.md)",
    )
    ctx_p.add_argument(
        "--strategy", choices=memory_index.STRATEGIES, default="auto",
        help="selection strategy; `auto` prefers semantic and falls back to "
             "deterministic with a note (default: auto)",
    )
    ctx_p.add_argument(
        "--max-chars", type=int, default=memory_context.DEFAULT_MAX_CHARS,
        help=f"hard packet ceiling (default: {memory_context.DEFAULT_MAX_CHARS})",
    )
    ctx_p.add_argument(
        "--id", action="append", default=[], dest="ids",
        help="pin an entry id to the front of its section (repeatable)",
    )
    ctx_p.add_argument(
        "--graph", type=Path, default=None,
        help="explicit path to graph.json (default: discover it in this repo)",
    )
    ctx_p.add_argument(
        "--no-graph", action="store_true",
        help="skip code-graph blast-radius annotation entirely",
    )
    ctx_p.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")

    idx_p = sub.add_parser(
        "index", help="build or refresh the ChromaDB semantic index (needs the `semantic` extra)"
    )
    idx_p.add_argument("--project", default=".", help="project root (default: current directory)")

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


def _scope_dir(scope: str, root: Path) -> Path:
    return (
        memory_store.machine_memory_dir()
        if scope == "machine"
        else memory_store.project_memory_dir(root)
    )


def cmd_review(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    directory = _scope_dir(args.scope, root)

    if args.action == "list":
        pending = memory_review.pending_entries(directory)
        if not pending:
            print(f"nothing awaiting review in {args.scope} memory")
            return 1
        print(f"{len(pending)} awaiting review in {args.scope} memory:\n")
        for rec in pending:
            print(f"  {rec['id']}  [{rec.get('type')}] {rec.get('summary')}")
        print("\nAccept with `agent-harness-memory review accept <id>`.")
        return 0

    if not args.entry_id:
        print(f"review {args.action} needs an entry id", file=sys.stderr)
        return 2

    try:
        result = memory_review.apply_review(
            directory,
            args.entry_id,
            args.action,
            summary=args.summary,
            note=args.note,
            commit=args.commit,
            file=args.file,
        )
    except LookupError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"review rejected: {exc}", file=sys.stderr)
        return 2

    print(f"{result.entry['id']} -> {result.entry['status']}")
    if result.created:
        print(f"  replacement: {result.created['id']} {result.created['summary']}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    project_id = memory_store.project_id_for(root)

    try:
        selector, strategy, notes = memory_index.resolve_selector(project_id, args.strategy)
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    # The code-graph join is on by default and silent when there is no graph:
    # a repo that never ran `graph_refresh` gets exactly the packet it always
    # got, with no error and no note. `--no-graph` is for when the graph
    # exists but you want the packet without it.
    annotator = None
    if not args.no_graph:
        from harness import memory_graph

        annotator, graph_notes = memory_graph.build_annotator(root, graph_path=args.graph)
        notes = list(notes) + graph_notes

    packet = memory_context.build_packet(
        root,
        query=args.query,
        max_chars=args.max_chars,
        include_ids=args.ids,
        selector=selector,
        strategy=strategy,
        notes=notes,
        annotator=annotator,
    )
    sys.stdout.write(
        memory_context.render_json(packet) if args.json
        else memory_context.render_markdown(packet)
    )
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    project_id = memory_store.project_id_for(root)

    if not memory_index.chromadb_available():
        print(
            'chromadb is not installed -- from this checkout run '
            '`python -m pip install -e ".[semantic]"`',
            file=sys.stderr,
        )
        return 1

    # Two indexes, not one: project entries are this repo's, machine entries
    # are shared by every project. Building them separately is what keeps a
    # machine-scope lesson from being re-embedded once per enrolled project.
    project_entries = list(
        memory_store.iter_entries(memory_store.project_memory_dir(root))
    )
    machine_entries = list(
        memory_store.iter_entries(memory_store.machine_memory_dir())
    )
    if not project_entries and not machine_entries:
        print(f"no memory entries to index in {root}")
        return 1

    for label, entries, index_dir in (
        ("project", project_entries, memory_index.index_dir_for(project_id)),
        ("machine", machine_entries, memory_index.machine_index_dir()),
    ):
        if not entries:
            continue
        stats = memory_index.build_index(entries, index_dir)
        print(f"{label}: indexed {len(entries)} entries into {index_dir}")
        print(
            f"  added={stats.added} updated={stats.updated} "
            f"deleted={stats.deleted} unchanged={stats.unchanged}"
        )
    return 0


_COMMANDS = {
    "init": cmd_init,
    "remember": cmd_remember,
    "status": cmd_status,
    "review": cmd_review,
    "context": cmd_context,
    "index": cmd_index,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
