"""Portable hierarchical memory CLI: init, remember, status, review, triage,
curate, context, ingest, claim, index, compact, maintain, prune, relate, and
eval.

Capture is explicit. Nothing in this module runs automatically or summarizes
with an AI model. `init`, `remember`, `status`, `review`, `triage`,
`maintain`, and `context` are stdlib-only and never open a network socket;
`index` is the one command that may touch the optional `chromadb` extra, and
it is deliberately separate from `context` so the agent hot path can never
trigger a model download. `eval` reads the semantic index when one exists
but never builds it, so it inherits the same guarantee. `maintain` and
`status` report index drift from the build manifest `index` leaves behind,
never by opening the index.

Exit codes are part of the contract so automation can branch on them:

    0  success
    1  nothing to do, or a requested capability is unavailable
    2  input rejected (schema, trust rule, probable secret, bad transition)

Usage:
    python harness/memory.py init [PATH]
    python harness/memory.py remember --type TYPE --summary TEXT [options]
    python harness/memory.py status [--project PATH]
    python harness/memory.py review list | ACTION ENTRY_ID [options]
    python harness/memory.py triage [list|template|apply] [--by topic|type] [--group KEY]
    python harness/memory.py triage apply --decisions FILE | --accept SEL ... [--dry-run]
    python harness/memory.py curate [--project PATH]
    python harness/memory.py context [--query TEXT] [--json] [--strategy S]
    python harness/memory.py ingest [--project PATH] [--dry-run]
    python harness/memory.py claim list|claim|release [ENTRY_ID] --owner NAME [options]
    python harness/memory.py index [--project PATH] [--if-stale]
    python harness/memory.py compact [--project PATH]
    python harness/memory.py maintain [--project PATH] [--check] [--force]
    python harness/memory.py prune [--id PROJECT_ID] [--dry-run]
    python harness/memory.py relate [--dry-run] [--min-score N] [--project ID]...
    python harness/memory.py eval [--cases PATH] [--limit N] [--strategy S] [--save]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import (
        memory_claims,
        memory_compact,
        memory_curation,
        memory_context,
        memory_eval,
        memory_index,
        memory_ingest,
        memory_maintain,
        memory_relate,
        memory_review,
        memory_store,
        memory_tasks,
        work_store,
    )
except ImportError:  # running as `python harness/memory.py`, not `-m harness.memory`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import (
        memory_claims,
        memory_compact,
        memory_curation,
        memory_context,
        memory_eval,
        memory_index,
        memory_ingest,
        memory_maintain,
        memory_relate,
        memory_review,
        memory_store,
        memory_tasks,
        work_store,
    )


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

    ing_p = sub.add_parser(
        "ingest",
        help="propose pointer entries for the project's existing documentation",
    )
    ing_p.add_argument("--project", default=".", help="project root (default: current directory)")
    ing_p.add_argument(
        "--dry-run", action="store_true",
        help="report what would be proposed without writing anything",
    )
    ing_p.add_argument(
        "--no-scope", action="store_true",
        help="do not seed scope.md's Purpose from the README",
    )

    clm_p = sub.add_parser(
        "claim",
        help="claim, release, or list ownership of open tasks/blockers, so parallel "
             "agent sessions don't pick up the same work",
    )
    clm_p.add_argument(
        "action", choices=("list", "claim", "release"),
        help="`list` shows every open item and who (if anyone) holds it; "
             "`claim`/`release` need entry_id and --owner",
    )
    clm_p.add_argument("entry_id", nargs="?", help="entry id, required for claim/release")
    clm_p.add_argument(
        "--owner", choices=memory_claims.KNOWN_AGENTS, default=None,
        help="the agent program claiming/releasing/filtering (required for claim/release)",
    )
    clm_p.add_argument("--project", default=".", help="project root (default: current directory)")
    clm_p.add_argument(
        "--force", action="store_true",
        help="release: drop another owner's claim rather than only your own",
    )

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

    tri_p = sub.add_parser(
        "triage",
        help="review the proposed backlog by topic and decide many entries in one pass",
    )
    tri_p.add_argument(
        "action", nargs="?", choices=("list", "template", "apply"), default="list",
        help="`list` groups the backlog (default); `template` writes a decisions "
             "file to edit; `apply` carries out decisions non-interactively",
    )
    tri_p.add_argument("--project", default=".", help="project root (default: current directory)")
    tri_p.add_argument("--scope", choices=memory_store.SCOPES, default="project")
    tri_p.add_argument(
        "--by", choices=("topic", "type"), default="topic",
        help="group by dominant tag (topic, default) or by entry type",
    )
    tri_p.add_argument(
        "--group", default=None, metavar="KEY",
        help="list only this group, e.g. tag:memory or type:task",
    )
    tri_p.add_argument(
        "--out", default=None, metavar="FILE",
        help="template: write the decisions file here instead of stdout",
    )
    tri_p.add_argument(
        "--decisions", default=None, metavar="FILE",
        help="apply: decisions file, one `ACTION ENTRY_ID [options]` per line",
    )
    for verb in ("accept", "reject", "resolve"):
        tri_p.add_argument(
            f"--{verb}", action="append", default=[], metavar="SEL", dest=f"{verb}_selectors",
            help=f"apply: {verb} an entry id, tag:X, type:X, or all (repeatable)",
        )
    tri_p.add_argument(
        "--supersede", default=None, metavar="ENTRY_ID",
        help="apply: supersede one entry with --summary",
    )
    tri_p.add_argument("--summary", default=None, help="apply: replacement statement for --supersede")
    tri_p.add_argument("--note", default=None, help="apply: reviewer note for every flag decision")
    tri_p.add_argument("--commit", default=None, help="apply: evidence commit SHA for every flag decision")
    tri_p.add_argument("--file", default=None, help="apply: evidence path for every flag decision")
    tri_p.add_argument(
        "--dry-run", action="store_true",
        help="apply: validate and print the plan without writing",
    )

    cur_p = sub.add_parser(
        "curate",
        help="automatically accept verifiable document pointers and reject exact proposal duplicates",
    )
    cur_p.add_argument("--project", default=".", help="project root (default: current directory)")

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
    idx_p.add_argument(
        "--if-stale", action="store_true",
        help="only build when the store has drifted past "
             f"${memory_maintain.DRIFT_THRESHOLD_ENV} changed entries since the last build",
    )

    cmp_p = sub.add_parser(
        "compact",
        help="archive closed work, promote durable knowledge, and bound active memory",
    )
    cmp_p.add_argument("--project", default=".", help="project root (default: current directory)")

    mnt_p = sub.add_parser(
        "maintain",
        help="compact if due, then report index drift and the review backlog; "
             "safe to run unattended and idempotent within the interval",
    )
    mnt_p.add_argument("--project", default=".", help="project root (default: current directory)")
    mnt_p.add_argument(
        "--check", action="store_true",
        help="report only; never compact (exit 0 when due, 1 when not)",
    )
    mnt_p.add_argument(
        "--force", action="store_true",
        help="compact even when not due",
    )

    prn_p = sub.add_parser(
        "prune",
        help="drop machine-registry entries whose project directory is gone",
    )
    prn_p.add_argument(
        "--id", action="append", default=[], dest="ids",
        help="also drop this project id even if its path still exists (repeatable)",
    )
    prn_p.add_argument(
        "--dry-run", action="store_true",
        help="report what would be dropped without writing the registry",
    )

    rel_p = sub.add_parser(
        "relate",
        help="propose cross-project relationships from shared tags, "
             "cross-references, and shared symbols (machine scope, always proposed)",
    )
    rel_p.add_argument(
        "--dry-run", action="store_true",
        help="print candidates with scores and evidence without writing anything",
    )
    rel_p.add_argument(
        "--min-score", type=float, default=memory_relate.DEFAULT_MIN_SCORE,
        help=f"minimum evidence score to propose (default: {memory_relate.DEFAULT_MIN_SCORE})",
    )
    # dest is deliberately not `project`: `_touch_registry` reads that
    # attribute as a path, and here it is a list of registry ids.
    rel_p.add_argument(
        "--project", action="append", default=[], dest="project_ids", metavar="ID",
        help="only pairs involving this registered project id (repeatable)",
    )

    eval_p = sub.add_parser(
        "eval",
        help="score retrieval against a labelled case set in the working store",
    )
    eval_p.add_argument("--project", default=".", help="project root (default: current directory)")
    eval_p.add_argument(
        "--cases",
        default=None,
        help="JSONL case file (default: .agent-harness/work/eval/default.jsonl)",
    )
    eval_p.add_argument(
        "--limit",
        type=int,
        default=memory_eval.DEFAULT_LIMIT,
        help=f"entries requested per selection (default: {memory_eval.DEFAULT_LIMIT})",
    )
    eval_p.add_argument(
        "--strategy",
        action="append",
        choices=memory_eval.STRATEGIES,
        default=None,
        help="strategy to score; repeatable (default: every strategy)",
    )
    eval_p.add_argument(
        "--save",
        action="store_true",
        help="also write the report under work/eval/results/",
    )
    eval_p.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")

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

    # Maintenance: what is owed. Read from files and the index manifest
    # only, so `status` stays as cheap as it always was.
    print("\n  maintenance:")
    for line in memory_maintain.status_lines(root):
        print("  " + line)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Seed memory from documentation. Exit 1 when there was nothing to do.

    Nothing-to-do is the honest outcome for a re-run on an unchanged tree, and
    it is exit 1 rather than 0 because the contract in this module's docstring
    reserves 1 for exactly that -- automation branching on "did this change
    anything" should not have to diff the store to find out.
    """
    root = Path(args.project).expanduser()
    if not (root / ".agent-harness").is_dir():
        print(f"no memory storage in {root}", file=sys.stderr)
        print("run `agent-harness-memory init` to create it", file=sys.stderr)
        return 1

    result = memory_ingest.ingest(
        root, dry_run=args.dry_run, seed_scope=not args.no_scope
    )
    prefix = "would propose" if args.dry_run else "proposed"
    print(f"{prefix}: {result.describe()}")

    for entry in result.proposed:
        print(f"  [{entry['type']}] {entry['summary'][:110]}")
    for name, reason in result.rejected:
        print(f"  skipped {name}: {reason}", file=sys.stderr)

    if result.scope_seeded:
        verb = "would seed" if args.dry_run else "seeded"
        print(f"  {verb} scope.md purpose: {result.scope_reason[:110]}")
    elif result.scope_reason:
        print(f"  scope.md untouched: {result.scope_reason}")

    if not result.wrote_anything:
        print("nothing to do -- documentation already mapped")
        return 1
    return 0


def cmd_curate(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    if not (root / ".agent-harness").is_dir():
        print(f"no memory storage in {root}", file=sys.stderr)
        return 1
    try:
        result = memory_curation.curate(root)
    except (ValueError, LookupError, OSError) as exc:
        print(f"curation failed: {exc}", file=sys.stderr)
        return 2
    print(memory_curation.render(result))
    return 0 if result.changed else 1


def cmd_claim(args: argparse.Namespace) -> int:
    """Claim, release, or list ownership of open tasks/blockers.

    Exit 1 for "nothing to show"/"could not act" (no open work, entry not
    claimed, etc.); exit 2 for a rejected transition (unknown owner, already
    claimed by someone else and not stale, wrong entry type).
    """
    root = Path(args.project).expanduser()
    directory = memory_store.project_memory_dir(root)

    if args.action == "list":
        entries = list(memory_store.iter_entries(directory))
        open_items = memory_tasks.open_work(entries)
        if not open_items:
            print("no open tasks or blockers")
            return 1
        for entry in open_items:
            c = entry.get("claim")
            if c and not memory_claims.is_stale(c):
                tag = f"claimed by {c.get('owner')} since {c.get('ts')}"
            elif c:
                tag = f"stale claim by {c.get('owner')} (free to take)"
            else:
                tag = "unclaimed"
            print(f"  {entry['id']}  [{entry.get('type')}] {entry.get('summary')}  -- {tag}")
        return 0

    if not args.entry_id or not args.owner:
        print(f"claim {args.action} needs an entry id and --owner", file=sys.stderr)
        return 2

    try:
        if args.action == "claim":
            result = memory_claims.claim(directory, args.entry_id, args.owner)
            print(f"{result.entry['id']} -> claimed by {args.owner}")
            if result.reclaimed_from:
                print(f"  (took over a stale claim from {result.reclaimed_from})")
        else:
            entry = memory_claims.release(directory, args.entry_id, args.owner, force=args.force)
            print(f"{entry['id']} -> released")
    except LookupError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"claim rejected: {exc}", file=sys.stderr)
        return 2
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


TRIAGE_SUMMARY_CHARS = 96


def _triage_line(entry: dict) -> str:
    summary = str(entry.get("summary") or "").strip().replace("\n", " ")
    if len(summary) > TRIAGE_SUMMARY_CHARS:
        summary = summary[: TRIAGE_SUMMARY_CHARS - 3] + "..."
    date = str(entry.get("ts") or "")[:10]
    return f"    {entry.get('id')}  {date}  [{entry.get('type')}] {summary}"


def _flag_decisions(args: argparse.Namespace, entries: list[dict]) -> list[memory_review.Decision]:
    """Turn `--accept/--reject/--resolve/--supersede` flags into decisions.

    Selectors expand against the *pending* entries, so `--reject tag:plan`
    can never touch an accepted entry that happens to share the tag.
    """
    # Overlapping selectors are normal (`tag:a` and `tag:b` share entries).
    # The same verb twice is one decision; two different verbs for one entry
    # is a real conflict and is named as such rather than left for the batch
    # to reject as a bare "decided more than once".
    chosen: dict[str, tuple[str, str]] = {}
    for verb in ("accept", "reject", "resolve"):
        for selector in getattr(args, f"{verb}_selectors"):
            for entry in memory_review.select_pending(entries, selector):
                entry_id = str(entry["id"])
                previous = chosen.get(entry_id)
                if previous is not None and previous[0] != verb:
                    raise ValueError(
                        f"{entry_id} matched both --{previous[0]} {previous[1]} "
                        f"and --{verb} {selector}; narrow the selectors"
                    )
                chosen.setdefault(entry_id, (verb, selector))
    decisions = [
        memory_review.Decision(
            entry_id=entry_id, action=verb,
            note=args.note, commit=args.commit, file=args.file,
        )
        for entry_id, (verb, _selector) in chosen.items()
    ]
    if args.supersede:
        if args.supersede in chosen:
            raise ValueError(
                f"{args.supersede} matched both --{chosen[args.supersede][0]} "
                f"and --supersede; narrow the selectors"
            )
        decisions.append(memory_review.Decision(
            entry_id=args.supersede, action="supersede", summary=args.summary,
            note=args.note, commit=args.commit, file=args.file,
        ))
    return decisions


def cmd_triage(args: argparse.Namespace) -> int:
    """Review the proposed backlog in bulk, by topic, without a prompt loop.

    `list` and `template` are read-only. `apply` is the only writer, and it
    writes once: every decision is validated against one snapshot of the
    inbox before the splice, so exit 2 always means "nothing changed". This
    command is for the human operator; agents have no route into it.
    """
    root = Path(args.project).expanduser()
    directory = _scope_dir(args.scope, root)
    pending = memory_review.pending_entries(directory)

    if args.action in ("list", "template"):
        if not pending:
            print(f"nothing awaiting review in {args.scope} memory")
            return 1
        if args.action == "template":
            text = memory_review.render_template(pending)
            if args.out:
                out = Path(args.out).expanduser()
                out.write_text(text, encoding="utf-8")
                print(f"wrote decisions template for {len(pending)} entries to {out}")
                print(f"edit the verbs, then: agent-harness-memory triage apply --decisions {out}")
            else:
                sys.stdout.write(text)
            return 0

        groups = memory_review.group_pending(pending, by=args.by)
        if args.group:
            groups = [g for g in groups if g.key == args.group]
            if not groups:
                print(f"no group {args.group!r}; groups are: "
                      + ", ".join(g.key for g in memory_review.group_pending(pending, by=args.by)),
                      file=sys.stderr)
                return 2
        print(f"{len(pending)} proposed in {args.scope} memory, {len(groups)} group(s) by {args.by}:\n")
        for group in groups:
            kinds = ", ".join(f"{k}={v}" for k, v in sorted(group.types.items()))
            print(f"  {group.key}  ({group.size}: {kinds})")
            for entry in group.entries:
                print(_triage_line(entry))
            print()
        print("Decide in bulk:  agent-harness-memory triage apply --accept tag:X --reject <id> ...")
        print("Or by file:      agent-harness-memory triage template --out decisions.txt")
        return 0

    # apply
    decisions: list[memory_review.Decision] = []
    try:
        if args.decisions:
            path = Path(args.decisions).expanduser()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                print(f"triage rejected: cannot read {path}: {exc}", file=sys.stderr)
                return 2
            decisions.extend(memory_review.parse_decisions(lines))
        decisions.extend(_flag_decisions(args, pending))
    except ValueError as exc:
        print(f"triage rejected: {exc}", file=sys.stderr)
        return 2

    live = [d for d in decisions if not d.is_skip]
    if not live:
        if not decisions and not args.decisions:
            print("triage apply needs --decisions FILE or at least one of "
                  "--accept/--reject/--resolve/--supersede", file=sys.stderr)
            return 2
        print("nothing to apply -- every decision is `skip`")
        return 1

    try:
        results = memory_review.apply_reviews(directory, live, dry_run=args.dry_run)
    except LookupError as exc:
        print(f"triage rejected: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"triage rejected: {exc}", file=sys.stderr)
        return 2

    verb = "would apply" if args.dry_run else "applied"
    print(f"{verb} {len(results)} decision(s) to {args.scope} memory:")
    for result in results:
        print(f"  {result.entry['id']} -> {result.entry['status']}")
        if result.created:
            print(f"    replacement: {result.created['id']} {result.created['summary']}")
    if args.dry_run:
        print("\ndry run -- nothing was written.")
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

    if not memory_index.chromadb_available():
        print(
            'chromadb is not installed -- from this checkout run '
            '`python -m pip install -e ".[semantic]"`',
            file=sys.stderr,
        )
        return 1

    # Drift is decided from the manifest alone, before chromadb is imported:
    # a `--if-stale` run that finds nothing to do must cost nothing.
    if args.if_stale:
        drift = memory_maintain.project_drift(root)
        if not memory_maintain.any_stale(drift):
            for scope in ("project", "machine"):
                print(f"{drift[scope].describe()}")
            print("index is fresh -- nothing to rebuild")
            return 1

    # Two indexes, not one: project entries are this repo's, machine entries
    # are shared by every project. Building them separately is what keeps a
    # machine-scope lesson from being re-embedded once per enrolled project.
    # `build_indexes` also writes the manifest that `status`, `maintain`,
    # and `--if-stale` read drift from.
    reports = memory_maintain.build_indexes(root)
    if not reports:
        print(f"no memory entries to index in {root}")
        return 1

    for report in reports:
        stats = report.stats
        print(f"{report.scope}: indexed {report.count} entries into {report.index_dir}")
        print(
            f"  added={stats.added} updated={stats.updated} "
            f"deleted={stats.deleted} unchanged={stats.unchanged}"
        )
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    try:
        result = memory_compact.compact(root)
    except memory_compact.CompactLockError as exc:
        print(f"compaction refused: {exc}", file=sys.stderr)
        return 2

    print(f"compacted {root} ({result.week})")
    print(f"  archived: {result.archived} (of which duplicates: {result.duplicates})")
    print(f"  promoted: {result.promoted}")
    print(f"  active:   {result.carried} carried, {result.omitted} omitted")
    if result.archived:
        print(f"  archive:  {result.archive_path}")

    if not result.changed:
        print("\nnothing to compact -- no closed work and no new durable knowledge")
        return 1

    # Removals are the whole point, and they are exactly what leaves the
    # semantic index behind: `update_index` drops departed ids, but only when
    # it is re-run. Saying so here is the difference between a compaction that
    # freshens retrieval and one that only freshens the files.
    if result.index_stale:
        print("\nentries were removed -- the semantic index still serves them.")
        print("run `agent-harness-memory index` to drop them from retrieval.")
    return 0


def cmd_maintain(args: argparse.Namespace) -> int:
    """Compact if due; report what else is owed. Idempotent within the interval.

    Exit 0 when compaction ran (or, with `--check`, when it is due), 1 when
    there was nothing to do, 2 when another compaction holds the lock. The
    index is never built here -- see the module docstring -- so a stale
    index is advice in the output, not an action.
    """
    root = Path(args.project).expanduser()
    if not (root / ".agent-harness").is_dir():
        print(f"no memory storage in {root}", file=sys.stderr)
        print("run `agent-harness-memory init` to create it", file=sys.stderr)
        return 1
    try:
        result = memory_maintain.maintain(root, force=args.force, check=args.check)
    except memory_compact.CompactLockError as exc:
        print(f"maintenance refused: {exc}", file=sys.stderr)
        return 2

    print(memory_maintain.render(result, root))
    if args.check:
        return 0 if result.due.due else 1
    return 0 if result.ran else 1


def cmd_prune(args: argparse.Namespace) -> int:
    """Remove dead entries from the machine project registry.

    Exit 1 when there is nothing to prune, per this module's contract -- an
    unchanged registry is the same "nothing to do" that `ingest` and
    `compact` report, and automation should not have to diff the file to find
    out. A named id that is not registered is exit 2: succeeding quietly on a
    typo would tell the operator they retired a project they did not.
    """
    try:
        result = memory_store.prune_registry(args.ids, dry_run=args.dry_run)
    except LookupError as exc:
        print(f"prune rejected: {exc}", file=sys.stderr)
        return 2

    total = len(result.removed) + len(result.kept)
    if not result.changed:
        print(f"nothing to prune -- all {total} registered projects still exist")
        return 1

    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {len(result.removed)} of {total} registered projects:")
    for project_id, path, reason in result.removed:
        why = "path is gone" if reason == "missing" else "named by --id"
        print(f"  {project_id}  ({why}) {path or '(no path recorded)'}")
    print(f"  kept: {len(result.kept)}")
    if args.dry_run:
        print("\ndry run -- the registry was not written.")
    return 0


def cmd_relate(args: argparse.Namespace) -> int:
    """Propose cross-project relationships into machine memory.

    Exit 0 when something was proposed (or, on a dry run, would be); exit 1
    when every candidate is already covered or nothing reached the
    threshold; exit 2 for an unregistered `--project` id, a negative
    `--min-score`, or a proposal the store contract refuses. Everything
    written is `proposed`/`inferred`; review is what promotes it.
    """
    try:
        result = memory_relate.relate(
            only=args.project_ids, min_score=args.min_score, dry_run=args.dry_run
        )
    except LookupError as exc:
        print(f"relate rejected: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"relate rejected: {exc}", file=sys.stderr)
        return 2

    print(memory_relate.render(result))
    if not result.proposed:
        print("nothing to propose")
        return 1
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    root = Path(args.project).expanduser()
    cases_path = (
        Path(args.cases).expanduser()
        if args.cases
        else memory_eval.default_cases_path(root)
    )

    try:
        cases = memory_eval.load_cases(cases_path)
    except FileNotFoundError:
        print(f"no eval case file at {cases_path}", file=sys.stderr)
        print(
            "write one line of JSON per case, e.g. "
            '{"name": "auth", "query": "how did we redo login", '
            '"relevant": ["project:mem-2026-08-12-001"]}',
            file=sys.stderr,
        )
        return 1
    except memory_eval.EvalCaseError as exc:
        # Rejected, not skipped: scoring a case set with a hole in it reports a
        # number for a question that was never asked.
        print(f"{exc}", file=sys.stderr)
        return 2

    try:
        report = memory_eval.evaluate(
            root,
            cases,
            limit=args.limit,
            strategies=args.strategy or memory_eval.STRATEGIES,
        )
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    sys.stdout.write(
        memory_eval.render_json(report) if args.json
        else memory_eval.render_markdown(report)
    )

    if args.save:
        # `ensure_work_dir` runs first so the tier's self-ignoring `.gitignore`
        # exists before anything lands beside it -- a saved report is untracked
        # from the moment it exists, not from the next time someone remembers.
        # The path itself comes only from `report_path`, which stamps it from
        # the report's own clock.
        work_store.ensure_work_dir(root)
        destination = memory_eval.report_path(root, report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(memory_eval.render_json(report), encoding="utf-8")
        print(f"\nsaved {destination}", file=sys.stderr)

    # `aggregate` is empty only when no strategy scored a single case -- the
    # requested backend was unavailable, so nothing was measured. That is the
    # exit-code contract's "capability unavailable", not success: a 0 here
    # would let automation bank a measured-nothing run as a baseline, the same
    # way averaging an unmeasured zero would make an absent backend look bad.
    # The report still prints; the note in it is the diagnosis.
    if not report.aggregate():
        return 1

    return 0


_COMMANDS = {
    "init": cmd_init,
    "remember": cmd_remember,
    "status": cmd_status,
    "review": cmd_review,
    "triage": cmd_triage,
    "curate": cmd_curate,
    "context": cmd_context,
    "ingest": cmd_ingest,
    "claim": cmd_claim,
    "index": cmd_index,
    "compact": cmd_compact,
    "maintain": cmd_maintain,
    "prune": cmd_prune,
    "relate": cmd_relate,
    "eval": cmd_eval,
}


def _touch_registry(args: argparse.Namespace) -> None:
    """Refresh `last_seen` for the project this invocation is about.

    Without this every registry record carries `first_seen == last_seen`
    forever, so nothing can tell a project worked on this week from one
    abandoned a year ago -- which is exactly the question `prune` and any
    future staleness policy need answered.

    Only already-registered projects are touched (`touch_project` will not
    add one), and failure is swallowed: the registry is bookkeeping, and a
    read-only home should not turn `status` into an error.
    """
    target = getattr(args, "project", None) or getattr(args, "path", None)
    if not target:
        return
    root = Path(target).expanduser()
    try:
        if (root / ".agent-harness").is_dir():
            memory_store.touch_project(root)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    _touch_registry(args)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
