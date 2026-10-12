"""Install the agent harness into a target project.

Usage:
    python -m harness.install_cli <project-dir> [--agents claude,codex,gemini]
                      [--hooks] [--check] [--skip-checks]

Also available as the installed console script `agent-harness-install`, and
(for source checkouts) via the `install.py` shim at the repo root.

Preflight checks (harness/doctor.py) run automatically before installing and
abort on any FAIL. `--check` runs the checks only and installs nothing;
`--skip-checks` installs without them.

Idempotent: running twice against the same project produces no further
changes. Never edits content outside the `<!-- agent-harness:start -->` /
`<!-- agent-harness:end -->` markers it owns.

Resource loading: template and hook-fragment content is read via
`importlib.resources` rather than a filesystem path relative to this file,
so this module works identically whether it's running from a source
checkout or from an installed wheel in site-packages (mirrors the
installed-resources check in `harness/doctor.py`).
"""

from __future__ import annotations

import argparse
import importlib.resources as resources
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from harness import atomicwrite, config_validation, memory_store, pathsafe

log = logging.getLogger(__name__)

# Parent-of-`harness/` -- the repo root in a source checkout, and the
# site-packages root when installed as a wheel (since harness/, templates/,
# and hooks/ install as sibling top-level packages in both layouts). Used
# only as the default `<harness>` substitution token / vault-README
# `harness_path`, never for reading packaged resources -- those go through
# `importlib.resources` below so they resolve correctly even if this layout
# assumption ever stops holding (e.g. a zipped/zipapp install).
HARNESS_ROOT = Path(__file__).resolve().parent.parent

HOOK_FRAGMENT_PATH = resources.files("hooks.claude") / "settings-fragment.json"

MARKER_START = "<!-- agent-harness:start -->"
MARKER_END = "<!-- agent-harness:end -->"

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
{query_cmd} find <term>       # locate nodes by symbol/file name
{query_cmd} node <id>         # details + direct edges
{query_cmd} callers <id> [n]  # who depends on it (depth n)
{query_cmd} callees <id> [n]  # what it depends on
{query_cmd} impact <id>       # full transitive blast radius
{query_cmd} file <path-frag>  # nodes defined in a file
{query_cmd} community <frag>  # nodes in a module/community
```

Typical blueprint workflow: `find` the touched symbols -> `impact` each ->
paste the affected files/communities into the blueprint's Impacted Files
and Blast Radius sections.

### Keeping the graph fresh

```
{refresh_cmd} <repo-root>          # fast: AST update + community re-naming
{refresh_cmd} <repo-root> --full   # full LLM re-doc
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
        atomicwrite.replace_with_retry(tmp_path, path)
    except BaseException:
        # Cleanup must not mask the failure that brought us here: the same
        # contention that can deny the replace can deny this unlink, and an
        # exception raised here would replace the real one.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
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


def _console_scripts_available() -> bool:
    """True when the `agent-harness-*` console scripts installed by this
    package are actually resolvable from the current interpreter's
    environment, i.e. this process is running from a pip/pipx install where
    setuptools generated entry-point scripts alongside the interpreter.

    A bare source checkout (running `install.py` or
    `python -m harness.install_cli` straight out of a git clone, with no
    `pip install` ever having happened) has no such scripts on PATH, so
    instructions that invoke them by bare name would fail with "command not
    found" -- detect this instead of assuming it, so the generated
    instructions are correct either way.

    `shutil.which` alone isn't enough: PATH could contain an unrelated,
    stale, or wrong-environment script that happens to share the name (e.g.
    a different pipx-managed `agent-harness` install). Require the resolved
    script to live in the same `Scripts`/`bin` directory as `sys.executable`
    -- the directory setuptools/pip actually write entry-point scripts into
    -- before trusting it.
    """
    script_name = "agent-harness-query.exe" if sys.platform == "win32" else "agent-harness-query"
    found = shutil.which(script_name)
    if found is None:
        return False
    interpreter_dir = Path(sys.executable).resolve().parent
    return Path(found).resolve().parent == interpreter_dir


def _query_command() -> str:
    """Command prefix for `graph_query.py` subcommands in generated docs.

    Prefers the installed `agent-harness-query` console script (always
    correct on PATH after a pip/pipx install); falls back to
    `<this interpreter> -m harness.graph_query`, which -- unlike invoking
    the `.py` file directly -- puts the repo root on `sys.path` correctly
    when run from the repo root, for a bare source checkout. The interpreter
    path is shell-quoted (matching `install_hooks`' handling of the same
    value) since it's spliced into copy-pasteable command text that may
    contain spaces (e.g. a Windows `Program Files` install).
    """
    if _console_scripts_available():
        return "agent-harness-query"
    return f"{pathsafe.shell_quote(sys.executable)} -m harness.graph_query"


def _refresh_command() -> str:
    """Command prefix for `graph_refresh.py` in generated docs; see `_query_command`."""
    if _console_scripts_available():
        return "agent-harness-refresh"
    return f"{pathsafe.shell_quote(sys.executable)} -m harness.graph_refresh"


def render_agents_block(harness_path: Path) -> str:
    template = (resources.files("templates") / "AGENTS.harness.md").read_text(encoding="utf-8")
    return (
        template.replace("{HARNESS_PATH}", str(harness_path))
        .replace("{QUERY_CMD}", _query_command())
        .replace("{REFRESH_CMD}", _refresh_command())
    )


def render_vault_readme(harness_path: Path) -> str:
    return VAULT_README_TEMPLATE.format(
        harness_path=harness_path,
        query_cmd=_query_command(),
        refresh_cmd=_refresh_command(),
    )


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


def _describe_graph_exclude(exclude: memory_store.ExcludeResult) -> str | None:
    """One summary line for the graph's git exclusion, or None for nothing
    to say (not a git repository)."""
    line = memory_store.GRAPH_EXCLUDE_LINE
    if exclude.status == "added":
        return f".git/info/exclude: added {line}"
    if exclude.status == "present":
        return ".git/info/exclude: unchanged"
    if exclude.status == "tracked":
        return f".git/info/exclude: unchanged (git tracks {line}; not excluding it)"
    if exclude.status == "failed":
        return f".git/info/exclude: not updated ({exclude.note})"
    return None


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
    blueprint_content = (resources.files("templates") / "feature-blueprint.md").read_text(encoding="utf-8")
    if not blueprint_dest.exists() or _read_preserving_newlines(blueprint_dest) != blueprint_content:
        _write(blueprint_dest, blueprint_content, txn)
        actions.append("docs/vault/templates/feature-blueprint.md: written")
    else:
        actions.append("docs/vault/templates/feature-blueprint.md: unchanged")

    readme_dest = project_dir / "docs" / "vault" / "README.md"
    readme_content = render_vault_readme(harness_path)
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


MCP_SERVER_NAME = "agent-harness"


def install_mcp_registration(
    project_dir: Path, harness_path: Path, txn: InstallTransaction | None = None
) -> str:
    """Register the agent-harness MCP server in the target project's
    top-level `.mcp.json` (Claude Code's project-scoped MCP server config --
    NOT `.claude/settings.json`, which stays hooks/permissions only).

    Idempotent, same load-validate-merge-compare-write shape as
    `install_hooks`: returns "unchanged"/"created"/"updated". Refuses
    (raises `InstallError`, no write) if `.mcp.json` exists but is invalid
    JSON, if its top-level `"mcpServers"` key is present but isn't a JSON
    object (e.g. `null` or a list), or if it already has a DIFFERENT
    `"agent-harness"` entry -- this never silently overwrites someone else's
    server definition of the same name.

    Does NOT set `enableAllProjectMcpServers` -- approval is left to the
    user via Claude Code's normal project-MCP-server prompt.
    """
    python_display = str(Path(sys.executable)).replace("\\", "/")
    harness_display = str(harness_path).replace("\\", "/")

    server_entry = {
        "command": python_display,
        "args": ["-m", "harness.mcp_server"],
        "env": {"PYTHONPATH": harness_display},
    }

    mcp_json_path = project_dir / ".mcp.json"

    if mcp_json_path.exists():
        try:
            existing = config_validation.load_json_object(mcp_json_path)
        except config_validation.ConfigValidationError as exc:
            raise InstallError(
                f"{mcp_json_path} is invalid; refusing to modify MCP registration ({exc})"
            ) from exc
    else:
        existing = {}

    raw_servers = existing.get("mcpServers", {})
    if not isinstance(raw_servers, dict):
        raise InstallError(
            f"{mcp_json_path} has a non-object 'mcpServers' value "
            f"({type(raw_servers).__name__}); refusing to modify MCP registration"
        )
    servers = dict(raw_servers)
    current = servers.get(MCP_SERVER_NAME)
    if current is not None and current != server_entry:
        raise InstallError(
            f"{mcp_json_path} already has a different '{MCP_SERVER_NAME}' MCP server entry; "
            "refusing to overwrite it. Remove or rename the existing entry and rerun --mcp."
        )

    merged = dict(existing)
    servers[MCP_SERVER_NAME] = server_entry
    merged["mcpServers"] = servers

    if merged == existing:
        return "unchanged"

    _write(mcp_json_path, json.dumps(merged, indent=2) + "\n", txn)
    return "updated" if existing else "created"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_dir", type=Path, help="target project directory")
    parser.add_argument("--agents", default="claude,codex,gemini", help="comma-separated: claude,codex,gemini")
    parser.add_argument("--hooks", action="store_true", help="install the Claude Code PreToolUse hook fragment")
    parser.add_argument(
        "--mcp", action="store_true", help="register the agent-harness MCP server in .mcp.json (Claude Code)"
    )
    parser.add_argument("--check", action="store_true", help="run preflight checks only; install nothing")
    parser.add_argument("--skip-checks", action="store_true", help="install without running preflight checks")
    return parser.parse_args(argv)


def run_install(
    project_dir: Path,
    agents: list[str],
    install_hooks_flag: bool,
    install_mcp_flag: bool = False,
) -> list[str]:
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

        if install_mcp_flag:
            mcp_action = install_mcp_registration(project_dir, HARNESS_ROOT, txn)
            summary.append(f".mcp.json: {mcp_action}")
    except Exception:
        txn.rollback()
        raise

    # Keep the graph's build output out of git through the repository's
    # private `.git/info/exclude`, never the project's `.gitignore` (ADR 0004:
    # the exclusion must not itself become a diff others see). It runs last
    # and outside the transaction, which covers only the project tree: by now
    # nothing can fail, so a failed install never leaves this line behind, and
    # `ensure_graph_git_exclude` never raises.
    exclude_line = _describe_graph_exclude(memory_store.ensure_graph_git_exclude(project_dir))
    if exclude_line:
        summary.append(exclude_line)

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
        summary = run_install(args.project_dir, agents, args.hooks, args.mcp)
    except InstallError as exc:
        print(f"Install failed -- nothing was left applied: {exc}")
        return 1

    print(f"agent-harness installed into {args.project_dir.resolve()}:")
    for line in summary:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
