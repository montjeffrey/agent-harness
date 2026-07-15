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
import os
import sys
import tempfile
from pathlib import Path

from harness import config_validation, pathsafe

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

This is an **advisory convention**, not a technical control: nothing in the
harness stops an agent from writing code before a blueprint's `Status:` field
says `Approved`. The loop below only works if agents choose to follow it.

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
3. **Human approves**: change the blueprint's `Status:` field to `Approved`.
4. **Agent executes**: on its next run, the agent reads the blueprint, sees
   `Status: Approved`, and implements -- checking off steps as it lands them.

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


class InstallError(RuntimeError):
    """Installation cannot proceed; any applied writes have been rolled back."""


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` via temp-file + atomic replace.

    Never leaves a torn/partial write visible at `path`: either the full
    new content lands, or the original file (if any) is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    _atomic_write_bytes(path, content.encode(encoding))


def _shell_quote(value: str) -> str:
    """Thin alias for `harness.pathsafe.shell_quote` (kept for existing
    call sites / tests within this module)."""
    return pathsafe.shell_quote(value)


def _json_string_escape(value: str) -> str:
    """Thin alias for `harness.pathsafe.json_string_escape` (kept for
    existing call sites / tests within this module)."""
    return pathsafe.json_string_escape(value)


class InstallTransaction:
    """Tracks per-file writes and per-directory creation for one install run.

    Every write goes through `write_text`, which snapshots the prior content
    (or records "did not exist") before an atomic replace. `rollback()`
    restores every tracked file to its prior state and removes any
    directories the transaction created that are now empty again -- so a
    mid-install failure leaves the target project byte-for-byte unchanged.
    """

    def __init__(self) -> None:
        self._file_snapshots: list[tuple[Path, bytes | None]] = []
        self._created_dirs: list[Path] = []

    def ensure_dir(self, path: Path) -> None:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
        if missing:
            path.mkdir(parents=True, exist_ok=True)
            self._created_dirs.extend(missing)

    def write_text(self, path: Path, content: str, *, encoding: str = "utf-8") -> None:
        self.ensure_dir(path.parent)
        original = path.read_bytes() if path.exists() else None
        _atomic_write_text(path, content, encoding=encoding)
        self._file_snapshots.append((path, original))

    def rollback(self) -> None:
        for path, original in reversed(self._file_snapshots):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, original)
        for directory in sorted(set(self._created_dirs), key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass  # not empty (holds untracked content) or already gone -- leave it
        self._file_snapshots.clear()
        self._created_dirs.clear()


def _write(path: Path, content: str, txn: InstallTransaction | None) -> None:
    if txn is not None:
        txn.write_text(path, content)
    else:
        _atomic_write_text(path, content)


def _ensure_dir(path: Path, txn: InstallTransaction | None) -> None:
    if txn is not None:
        txn.ensure_dir(path)
    else:
        path.mkdir(parents=True, exist_ok=True)


def _read_preserving_newlines(path: Path, *, encoding: str = "utf-8") -> str:
    """Read text with newline translation disabled so CRLF/LF round-trips exactly.

    `Path.read_text(newline=...)` only exists on CPython 3.13+, but the harness
    supports 3.11+. `Path.open(newline="")` has accepted the argument for far
    longer, so route through it to keep byte-for-byte newline preservation on
    every supported interpreter.
    """
    with path.open("r", encoding=encoding, newline="") as fh:
        return fh.read()


def render_agents_block(harness_path: Path) -> str:
    template = (TEMPLATES_DIR / "AGENTS.harness.md").read_text(encoding="utf-8")
    return template.replace("{HARNESS_PATH}", str(harness_path))


def upsert_marker_block(file_path: Path, block: str, txn: InstallTransaction | None = None) -> str:
    """Insert or replace the agent-harness marker block in file_path.

    Returns "created", "updated", or "unchanged". Reads/writes with
    `newline=""` so any pre-existing content outside the marker block
    (including its exact line endings) round-trips byte-for-byte.
    """
    block = block.strip("\n")
    if not file_path.exists():
        _write(file_path, block + "\n", txn)
        return "created"

    original = _read_preserving_newlines(file_path)
    start_idx = original.find(MARKER_START)
    end_idx = original.find(MARKER_END)

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        end_idx += len(MARKER_END)
        new_content = original[:start_idx] + block + original[end_idx:]
    else:
        # Match the file's own newline style so a fresh append never mixes
        # CRLF body content with an LF separator/block.
        newline = "\r\n" if "\r\n" in original else "\n"
        if not original:
            separator = ""
        elif original.endswith(newline * 2):
            separator = ""
        elif original.endswith(newline):
            separator = newline
        else:
            separator = newline * 2
        appended = separator + block + "\n"
        if newline == "\r\n":
            appended = appended.replace("\r\n", "\n").replace("\n", "\r\n")
        new_content = original + appended

    if new_content == original:
        return "unchanged"
    _write(file_path, new_content, txn)
    return "updated"


def ensure_gitignore_entry(project_dir: Path, txn: InstallTransaction | None = None) -> str:
    gitignore_path = project_dir / ".gitignore"
    if not gitignore_path.exists():
        _write(gitignore_path, GITIGNORE_ENTRY + "\n", txn)
        return "created"

    content = _read_preserving_newlines(gitignore_path)
    existing_lines = {line.strip() for line in content.splitlines()}
    if GITIGNORE_ENTRY.strip("/") in {line.strip("/") for line in existing_lines}:
        return "unchanged"

    separator = "" if content.endswith("\n") or not content else "\n"
    _write(gitignore_path, content + separator + GITIGNORE_ENTRY + "\n", txn)
    return "updated"


def install_vault_files(
    project_dir: Path, harness_path: Path, txn: InstallTransaction | None = None
) -> list[str]:
    actions: list[str] = []

    blueprints_dir = project_dir / "docs" / "vault" / "blueprints"
    _ensure_dir(blueprints_dir, txn)
    keep_file = blueprints_dir / ".gitkeep"
    if not keep_file.exists():
        _write(keep_file, "", txn)
        actions.append("docs/vault/blueprints/.gitkeep: created")
    else:
        actions.append("docs/vault/blueprints/.gitkeep: unchanged")

    templates_dir = project_dir / "docs" / "vault" / "templates"
    _ensure_dir(templates_dir, txn)
    blueprint_dest = templates_dir / "feature-blueprint.md"
    blueprint_src = TEMPLATES_DIR / "feature-blueprint.md"
    blueprint_content = blueprint_src.read_text(encoding="utf-8")
    if not blueprint_dest.exists() or _read_preserving_newlines(blueprint_dest) != blueprint_content:
        _write(blueprint_dest, blueprint_content, txn)
        actions.append("docs/vault/templates/feature-blueprint.md: written")
    else:
        actions.append("docs/vault/templates/feature-blueprint.md: unchanged")

    readme_dest = project_dir / "docs" / "vault" / "README.md"
    readme_content = VAULT_README_TEMPLATE.format(harness_path=harness_path)
    if not readme_dest.exists() or _read_preserving_newlines(readme_dest) != readme_content:
        _write(readme_dest, readme_content, txn)
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


def install_hooks(project_dir: Path, harness_path: Path, txn: InstallTransaction | None = None) -> str:
    fragment_text = HOOK_FRAGMENT_PATH.read_text(encoding="utf-8")

    # Both substitutions go through the same quote-then-JSON-escape pipeline
    # so a harness install path and the interpreter path get identical
    # treatment for spaces/Unicode/embedded-quote characters. The template
    # relies on this quoting being authoritative (it does not add its own
    # literal quotes around either placeholder) -- a quoted token followed
    # immediately by an unquoted path suffix (e.g. `<harness>/hooks/...`)
    # still tokenizes as one argument on both POSIX shells and Windows
    # CreateProcess-style argv parsing.
    harness_display = str(harness_path).replace("\\", "/")
    harness_token = _json_string_escape(_shell_quote(harness_display))
    fragment_text = fragment_text.replace("<harness>", harness_token)

    # Record the interpreter that ran the installer -- not whatever "python"
    # happens to resolve to on PATH later.
    python_display = str(Path(sys.executable)).replace("\\", "/")
    python_token = _json_string_escape(_shell_quote(python_display))
    fragment_text = fragment_text.replace("<python>", python_token)

    try:
        fragment = json.loads(fragment_text)
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"hook fragment template produced invalid JSON after substitution: {exc}"
        ) from exc

    settings_path = project_dir / ".claude" / "settings.json"

    if settings_path.exists():
        try:
            existing = config_validation.load_json_object(settings_path)
        except config_validation.ConfigValidationError as exc:
            raise InstallError(
                f"{settings_path} is invalid; refusing to modify hooks ({exc})"
            ) from exc
    else:
        existing = {}

    merged = deep_merge_hooks(existing, fragment)
    if merged == existing:
        return "unchanged"

    _write(settings_path, json.dumps(merged, indent=2) + "\n", txn)
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
    """Install the harness into `project_dir`.

    Every write in this run shares one `InstallTransaction`. If any step
    raises, already-applied writes (and any directories this run created)
    are rolled back before the exception propagates, so a mid-install
    failure leaves the target project byte-for-byte unchanged.
    """
    project_dir = project_dir.resolve()
    txn = InstallTransaction()
    summary: list[str] = []

    try:
        txn.ensure_dir(project_dir)

        vault_actions = install_vault_files(project_dir, HARNESS_ROOT, txn)
        summary.extend(vault_actions)

        gitignore_action = ensure_gitignore_entry(project_dir, txn)
        summary.append(f".gitignore: {gitignore_action}")

        agents_block = render_agents_block(HARNESS_ROOT)
        agents_action = upsert_marker_block(project_dir / "AGENTS.md", agents_block, txn)
        summary.append(f"AGENTS.md: {agents_action}")

        if "claude" in agents:
            claude_action = upsert_marker_block(
                project_dir / "CLAUDE.md", f"{MARKER_START}\n{CLAUDE_POINTER}\n{MARKER_END}", txn
            )
            summary.append(f"CLAUDE.md: {claude_action}")

        if "gemini" in agents:
            gemini_action = upsert_marker_block(
                project_dir / "GEMINI.md", f"{MARKER_START}\n{GEMINI_POINTER}\n{MARKER_END}", txn
            )
            summary.append(f"GEMINI.md: {gemini_action}")

        if install_hooks_flag:
            hooks_action = install_hooks(project_dir, HARNESS_ROOT, txn)
            summary.append(f".claude/settings.json: {hooks_action}")
    except Exception:
        txn.rollback()
        raise

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

    try:
        summary = run_install(args.project_dir, agents, args.hooks)
    except InstallError as exc:
        print(f"Install failed -- nothing was left applied: {exc}")
        return 1

    print(f"agent-harness installed into {args.project_dir.resolve()}:")
    for line in summary:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
