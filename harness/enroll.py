"""One command to make the harness active in a project.

`agent-harness-enroll` is the per-project half of setup (`bootstrap.ps1` is
the once-per-machine half). Three steps, in the order that degrades best:

    1. create project memory storage and register the project
    2. build the code graph, if graphify and a backend are available
    3. build the semantic index, if the `semantic` extra is present

**This command writes no instruction files.** It does not touch AGENTS.md,
CLAUDE.md, GEMINI.md, or a project's `.claude/settings.json`. Those are
machine-level concerns: the SessionStart hook and the MCP server are
registered once by `bootstrap.ps1` in your *user* settings, where they apply
to every project you open. Duplicating them per-project would mean N copies
of the same configuration drifting apart. `agent-harness-install` still
exists if you specifically want a project-local instruction block.

What is left is exactly what belongs to the project: its memory, its graph,
and the index over both.

Memory comes first deliberately -- it has no prerequisites and cannot fail
on a project that lacks a graph, so a project always ends up with working
memory even when steps 2 and 3 are skipped.

Every step is idempotent, so this is also the right command to re-run later.

Exit codes:

    0  everything applied
    1  memory is set up but at least one step degraded (details on stderr)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import graph_location, graph_refresh, memory_index, memory_store
except ImportError:  # running as `python harness/enroll.py`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import graph_location, graph_refresh, memory_index, memory_store


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "path", nargs="?", default=".", help="project root (default: current directory)"
    )
    parser.add_argument(
        "--no-graph", action="store_true",
        help="do not build the code graph",
    )
    parser.add_argument(
        "--refresh-graph", action="store_true",
        help="rebuild the code graph even if one already exists",
    )
    parser.add_argument(
        "--no-index", action="store_true",
        help="do not build the semantic index (it can be built later with `index`)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1

    degraded = False
    print(f"Enrolling {root}\n")

    # 1. Memory -- no preflight, cannot fail on a graph-less project.
    result = memory_store.init_project(root)
    print(f"[1/3] memory     project id {result.project_id}")
    for created in result.created:
        print(f"                 created {created}")
    if not result.created:
        print("                 already initialized -- left untouched")

    # 2. Code graph -- the thing memory selection gets to reason over.
    if args.no_graph:
        print("[2/3] graph      skipped (--no-graph)")
    else:
        existing = graph_location.find_graph_in_repo(root)
        if existing and not args.refresh_graph:
            print(f"[2/3] graph      already present at {existing}")
        else:
            exc_text = ""
            try:
                built = graph_refresh.main([str(root), "--full"])
            except SystemExit as exc:
                # graph_refresh is also a CLI entry point and exits rather
                # than returning on failure. SystemExit derives from
                # BaseException, so a bare `except Exception` lets it through
                # and kills enrollment before the index step ever runs.
                built = exc.code if isinstance(exc.code, int) else 1
            except Exception as exc:  # noqa: BLE001 -- never block enrollment
                built, exc_text = 1, str(exc)
                print(f"[2/3] graph      unavailable ({exc_text})", file=sys.stderr)
            if built == 0:
                print("[2/3] graph      built")
            else:
                degraded = True
                if not exc_text:
                    print(
                        "[2/3] graph      not built -- graphify or a backend key is "
                        "missing; memory works without it",
                        file=sys.stderr,
                    )

    # 3. Semantic index.
    if args.no_index:
        print("[3/3] index      skipped (--no-index)")
    elif not memory_index.chromadb_available():
        degraded = True
        print(
            "[3/3] index      skipped -- chromadb is not installed, so context "
            "selection will be deterministic",
            file=sys.stderr,
        )
    else:
        entries = list(
            memory_store.iter_entries(memory_store.project_memory_dir(root))
        )
        machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
        built = 0
        for records, index_dir in (
            (entries, memory_index.index_dir_for(result.project_id)),
            (machine, memory_index.machine_index_dir()),
        ):
            if records:
                memory_index.build_index(records, index_dir)
                built += len(records)
        if built:
            print(f"[3/3] index      embedded {built} entries")
        else:
            print("[3/3] index      nothing to index yet")

    print("\nNext:")
    print(f"  1. Describe the project in {root / '.agent-harness' / 'scope.md'}")
    print("  2. Record:  agent-harness-memory remember --type decision --summary ...")
    print("  3. Review:  agent-harness-memory review list")
    if degraded:
        print("\nSome steps degraded -- see the messages above.", file=sys.stderr)
    return 1 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
