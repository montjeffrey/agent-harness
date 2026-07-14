"""Install the agent harness into a target project.

Usage:
    python install.py <project-dir> [--agents claude,codex,gemini] [--hooks]
                      [--check] [--skip-checks]

Preflight checks (harness/doctor.py) run automatically before installing and
abort on any FAIL. `--check` runs the checks only and installs nothing;
`--skip-checks` installs without them.

Idempotent: running twice against the same project produces no further
changes. Never edits content outside the `<!-- agent-harness:start -->` /
`<!-- agent-harness:end -->` markers it owns.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

HARNESS_ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = HARNESS_ROOT / "templates"
HOOK_FRAGMENT_PATH = HARNESS_ROOT / "hooks" / "claude" / "settings-fragment.json"

MARKER_START = "<!-- agent-harness:start -->"
MARKER_END = "<!-- agent-harness:end -->"

GITIGNORE_ENTRY = "graphify-out/"

VAULT_README_TEMPLATE = """# Agent <-> Human Vault Pipeline

This folder is the shared surface between AI agents and a human reviewer.
If the project root is an Obsidian vault, everything here renders natively --
wikilinks, checkboxes, and graph view included.

## The loop

```
[ Codebase ] --(graph refresh)--> [ this vault ] --(human review)--> [ Approved ]
     ^                                                                    |
     +---------------------(agent executes approved blueprint)-----------+
```

1. **Agent plans**: before feature-scale code changes, the agent maps impact
   using `graphify-out/graph.json` (via `graph_query.py`) and any available
   symbol-reference tooling, then writes a blueprint note to `blueprints/`
   from the template in `templates/feature-blueprint.md`.
2. **Human reviews**: open the blueprint, inspect linked files/dependencies,
   edit steps directly in Markdown.
3. **Human approves**: change `Status:` to `Approved` (and tick the box).
4. **Agent executes**: on its next run, the agent reads the blueprint, sees
   `Approved`, and implements -- checking off steps as it lands them.

## Folders

| Folder | Purpose |
|---|---|
| `blueprints/` | One note per proposed feature/change awaiting or holding approval |
| `templates/` | Blueprint template (`feature-blueprint.md`) |

## Querying the code graph (agents: use this, never Read graph.json)

`graph.json` can be several MB -- reading it whole wastes a full context
window. Query it instead:

```
python {harness_path}/harness/graph_query.py find <term>       # locate nodes by symbol/file name
python {harness_path}/harness/graph_query.py node <id>         # details + direct edges
python {harness_path}/harness/graph_query.py callers <id> [n]  # who depends on it (depth n)
python {harness_path}/harness/graph_query.py callees <id> [n]  # what it depends on
python {harness_path}/harness/graph_query.py impact <id>       # full transitive blast radius
python {harness_path}/harness/graph_query.py file <path-frag>  # nodes defined in a file
python {harness_path}/harness/graph_query.py community <frag>  # nodes in a module/community
```

Typical blueprint workflow: `find` the touched symbols -> `impact` each ->
paste the affected files/communities into the blueprint's Impacted Files
and Blast Radius sections.

### Keeping the graph fresh

```
python {harness_path}/harness/graph_refresh.py <repo-root>          # fast: AST update + community re-naming
python {harness_path}/harness/graph_refresh.py <repo-root> --full   # full LLM re-doc
```

The graph records the commit it was built from (`built_at_commit` in
graph.json) -- check that against `git rev-parse HEAD` to detect staleness.
"""

CLAUDE_POINTER = (
    "Harness instructions: see AGENTS.md harness block; query code relations "
    "with graph_query.py, never Read graph.json."
)
GEMINI_POINTER = CLAUDE_POINTER


def render_agents_block(harness_path: Path) -> str:
    template = (TEMPLATES_DIR / "AGENTS.harness.md").read_text(encoding="utf-8")
    return template.replace("{HARNESS_PATH}", str(harness_path))


def upsert_marker_block(file_path: Path, block: str) -> str:
    """Insert or replace the agent-harness marker block in file_path.

    Returns "created", "updated", or "unchanged".
    """
    block = block.strip("\n")
    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(block + "\n", encoding="utf-8")
        return "created"

    original = file_path.read_text(encoding="utf-8")
    start_idx = original.find(MARKER_START)
    end_idx = original.find(MARKER_END)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        end_idx += len(MARKER_END)
        new_content = original[:start_idx] + block + original[end_idx:]
    else:
        separator = "" if original.endswith("\n\n") else ("\n\n" if original.endswith("\n") else "\n\n")
        new_content = original + separator + block + "\n"

    if new_content == original:
        return "unchanged"
    file_path.write_text(new_content, encoding="utf-8")
    return "updated"


def ensure_gitignore_entry(project_dir: Path) -> str:
    gitignore_path = project_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(GITIGNORE_ENTRY + "\n", encoding="utf-8")
        return "created"

    content = gitignore_path.read_text(encoding="utf-8")
    existing_lines = {line.strip() for line in content.splitlines()}
    if GITIGNORE_ENTRY.strip("/") in {line.strip("/") for line in existing_lines}:
        return "unchanged"

    separator = "" if content.endswith("\n") or not content else "\n"
    gitignore_path.write_text(content + separator + GITIGNORE_ENTRY + "\n", encoding="utf-8")
    return "updated"


def install_vault_files(project_dir: Path, harness_path: Path) -> list[str]:
    actions: list[str] = []

    blueprints_dir = project_dir / "docs" / "vault" / "blueprints"
    blueprints_dir.mkdir(parents=True, exist_ok=True)
    keep_file = blueprints_dir / ".gitkeep"
    if not keep_file.exists():
        keep_file.write_text("", encoding="utf-8")
        actions.append("docs/vault/blueprints/.gitkeep: created")
    else:
        actions.append("docs/vault/blueprints/.gitkeep: unchanged")

    templates_dir = project_dir / "docs" / "vault" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    blueprint_dest = templates_dir / "feature-blueprint.md"
    blueprint_src = TEMPLATES_DIR / "feature-blueprint.md"
    blueprint_content = blueprint_src.read_text(encoding="utf-8")
    if not blueprint_dest.exists() or blueprint_dest.read_text(encoding="utf-8") != blueprint_content:
        blueprint_dest.write_text(blueprint_content, encoding="utf-8")
        actions.append("docs/vault/templates/feature-blueprint.md: written")
    else:
        actions.append("docs/vault/templates/feature-blueprint.md: unchanged")

    readme_dest = project_dir / "docs" / "vault" / "README.md"
    readme_content = VAULT_README_TEMPLATE.format(harness_path=harness_path)
    if not readme_dest.exists() or readme_dest.read_text(encoding="utf-8") != readme_content:
        readme_dest.write_text(readme_content, encoding="utf-8")
        actions.append("docs/vault/README.md: written")
    else:
        actions.append("docs/vault/README.md: unchanged")

    return actions


def deep_merge_hooks(existing: dict, fragment: dict) -> dict:
    """Merge fragment's hooks into existing settings without duplicating entries."""
    merged = dict(existing)
    merged_hooks = dict(merged.get("hooks", {}))

    for event, entries in fragment.get("hooks", {}).items():
        current_entries = list(merged_hooks.get(event, []))
        for entry in entries:
            if entry not in current_entries:
                current_entries.append(entry)
        merged_hooks[event] = current_entries

    merged["hooks"] = merged_hooks
    return merged


def install_hooks(project_dir: Path, harness_path: Path) -> str:
    fragment_text = HOOK_FRAGMENT_PATH.read_text(encoding="utf-8")
    # Use forward slashes so the substituted path stays valid inside the JSON
    # string literal (raw Windows backslashes would break JSON parsing).
    harness_path_display = str(harness_path).replace("\\", "/")
    fragment_text = fragment_text.replace("<harness>", harness_path_display)
    fragment = json.loads(fragment_text)

    settings_path = project_dir / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        existing = {}

    merged = deep_merge_hooks(existing, fragment)
    if merged == existing:
        return "unchanged"

    settings_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return "updated" if existing else "created"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_dir", type=Path, help="target project directory")
    parser.add_argument("--agents", default="claude,codex,gemini", help="comma-separated: claude,codex,gemini")
    parser.add_argument("--hooks", action="store_true", help="install the Claude Code PreToolUse hook fragment")
    parser.add_argument("--check", action="store_true", help="run preflight checks only; install nothing")
    parser.add_argument("--skip-checks", action="store_true", help="install without running preflight checks")
    return parser.parse_args(argv)


def run_install(project_dir: Path, agents: list[str], install_hooks_flag: bool) -> list[str]:
    project_dir = project_dir.resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    summary: list[str] = []

    vault_actions = install_vault_files(project_dir, HARNESS_ROOT)
    summary.extend(vault_actions)

    gitignore_action = ensure_gitignore_entry(project_dir)
    summary.append(f".gitignore: {gitignore_action}")

    agents_block = render_agents_block(HARNESS_ROOT)
    agents_action = upsert_marker_block(project_dir / "AGENTS.md", agents_block)
    summary.append(f"AGENTS.md: {agents_action}")

    if "claude" in agents:
        claude_action = upsert_marker_block(
            project_dir / "CLAUDE.md", f"{MARKER_START}\n{CLAUDE_POINTER}\n{MARKER_END}"
        )
        summary.append(f"CLAUDE.md: {claude_action}")

    if "gemini" in agents:
        gemini_action = upsert_marker_block(
            project_dir / "GEMINI.md", f"{MARKER_START}\n{GEMINI_POINTER}\n{MARKER_END}"
        )
        summary.append(f"GEMINI.md: {gemini_action}")

    if install_hooks_flag:
        hooks_action = install_hooks(project_dir, HARNESS_ROOT)
        summary.append(f".claude/settings.json: {hooks_action}")

    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]

    if not args.skip_checks:
        from harness.doctor import format_report, has_failures, run_checks

        results = run_checks(args.project_dir)
        print(format_report(results))
        if has_failures(results):
            print("\nPreflight failed -- nothing was installed. Fix the failures above")
            print("(or rerun with --skip-checks to override) and try again.")
            return 1
        if args.check:
            return 0
        print()
    elif args.check:
        print("--check and --skip-checks are mutually exclusive")
        return 2

    summary = run_install(args.project_dir, agents, args.hooks)
    print(f"agent-harness installed into {args.project_dir.resolve()}:")
    for line in summary:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
