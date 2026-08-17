"""User-scope registration of the harness hooks and MCP server.

This is step 5 of `bootstrap.ps1`, lifted out of PowerShell. It registers,
once per machine, in the USER configuration rather than per project:

    ~/.claude/settings.json   SessionStart + PreToolUse hooks
    ~/.claude.json            the `agent-harness` MCP server

Why Python and not PowerShell: the original in-script version merged the
settings JSON with `ConvertFrom-Json -AsHashtable`, and `-AsHashtable` is
PowerShell 6+ only. On a stock Windows box -- Windows PowerShell 5.1, no
`pwsh` -- that threw, and because `bootstrap.ps1` runs under
`$ErrorActionPreference = 'Stop'` it aborted the whole bootstrap *after*
the pipx install had already succeeded. The result was a machine where
every `agent-harness-*` command worked but no hook was ever registered,
which looks like the harness silently doing nothing. Doing the merge here
means one implementation that behaves the same under 5.1, 7, and POSIX
shells, and it reuses the validation and atomic-write helpers the project
already trusts for the project-scope installer.

Idempotent. Re-running after a reinstall REWRITES an existing harness entry
rather than skipping it: a reinstall changes the interpreter path, and a
skip would leave the hook pointing at a venv that no longer exists.

Exit codes:

    0  everything applied (or already current)
    1  refused to touch a config that is invalid or not ours
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import config_validation, pathsafe
except ImportError:  # running as `python harness/bootstrap_register.py`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import config_validation, pathsafe


MCP_SERVER_NAME = "agent-harness"


class RegistrationError(RuntimeError):
    """Refusing to modify a configuration file; nothing was written."""


@dataclass(frozen=True)
class HookSpec:
    event: str
    script: str  # POSIX-style path relative to the harness root
    matcher: str | None


# Deliberately a subset of `hooks/claude/settings-fragment.json`, which is the
# project-scope install. `graph_check.py` is omitted here because it reports on
# a specific project's graph freshness; at user scope it would fire in every
# project, including ones that were never enrolled.
HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec(event="SessionStart", script="hooks/claude/memory_context_hook.py", matcher=None),
    HookSpec(event="PreToolUse", script="hooks/claude/graph_read_guard.py", matcher="Read"),
)


def user_settings_path() -> Path:
    """Claude Code's user-scope hooks/permissions file."""
    return Path.home() / ".claude" / "settings.json"


def user_mcp_config_path() -> Path:
    """Claude Code's user-scope MCP server file.

    Note this is `~/.claude.json`, NOT `~/.claude/settings.json` -- settings
    holds hooks and permissions only, and a `mcpServers` key placed there is
    ignored. Project scope uses a repo-local `.mcp.json` instead; see
    `harness.install_cli.install_mcp_registration`.
    """
    return Path.home() / ".claude.json"


def render_hook_command(python_exe: str, script_path: Path) -> str:
    """Render the `command` string for a hook entry.

    Both halves go through the same argv-safe quoting as the project-scope
    installer, so a checkout under `C:\\Users\\...\\OneDrive\\Escritorio\\...`
    (spaces, non-ASCII) tokenizes as two arguments and not five.
    """
    python_token = pathsafe.shell_quote(str(Path(python_exe)).replace("\\", "/"))
    script_token = pathsafe.shell_quote(str(script_path).replace("\\", "/"))
    return f"{python_token} {script_token}"


def render_hook_entry(spec: HookSpec, harness_root: Path, python_exe: str) -> dict:
    entry: dict = {
        "hooks": [
            {
                "type": "command",
                "command": render_hook_command(python_exe, harness_root / spec.script),
            }
        ]
    }
    if spec.matcher is not None:
        entry["matcher"] = spec.matcher
    return entry


def _is_ours(entry: object, leaf: str) -> bool:
    """Does this settings entry already invoke our hook script?

    Matched on the script's file name rather than the whole command: the
    interpreter path changes on every reinstall, so a full-string comparison
    would treat a re-run as a brand new hook and append a duplicate.
    """
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks", []):
        if isinstance(hook, dict) and leaf in str(hook.get("command", "")):
            return True
    return False


def merge_hooks(
    settings: dict, harness_root: Path, python_exe: str, specs: tuple[HookSpec, ...] = HOOK_SPECS
) -> tuple[dict, list[str]]:
    """Return `(merged_settings, actions)` without touching disk."""
    merged = dict(settings)
    raw_hooks = merged.get("hooks", {})
    if not isinstance(raw_hooks, dict):
        raise RegistrationError(
            f"'hooks' is a {type(raw_hooks).__name__}, expected an object; "
            "refusing to modify hook registration"
        )
    hooks: dict[str, list[object]] = {}
    for event, entries in raw_hooks.items():
        # Claude Code expects each event value to be an array of hook groups.
        # Do not silently coerce a malformed value (notably a string, which
        # `list()` would split into characters) and then write a config that
        # destroys the user's original setting.
        if not isinstance(entries, list):
            raise RegistrationError(
                f"'hooks.{event}' is a {type(entries).__name__}, expected an array; "
                "refusing to modify hook registration"
            )
        hooks[event] = list(entries)
    actions: list[str] = []

    for spec in specs:
        leaf = spec.script.rsplit("/", 1)[-1]
        if not (harness_root / spec.script).exists():
            actions.append(f"{spec.event}/{leaf}: skipped (script not found)")
            continue

        desired = render_hook_entry(spec, harness_root, python_exe)
        entries = hooks.setdefault(spec.event, [])
        for index, existing in enumerate(entries):
            if _is_ours(existing, leaf):
                if existing == desired:
                    actions.append(f"{spec.event}/{leaf}: already current")
                else:
                    entries[index] = desired
                    actions.append(f"{spec.event}/{leaf}: updated")
                break
        else:
            entries.append(desired)
            actions.append(f"{spec.event}/{leaf}: registered")

    merged["hooks"] = hooks
    return merged, actions


def render_mcp_entry(harness_root: Path, python_exe: str) -> dict:
    """Same server shape as the project-scope registration.

    `PYTHONPATH` points at the checkout on purpose: bootstrap installs the
    harness *from* this checkout, so edits there should be what the server
    runs, exactly as the hook commands above invoke the checkout's scripts.
    """
    return {
        "command": str(Path(python_exe)).replace("\\", "/"),
        "args": ["-m", "harness.mcp_server"],
        "env": {"PYTHONPATH": str(harness_root).replace("\\", "/")},
    }


def merge_mcp(config: dict, harness_root: Path, python_exe: str) -> tuple[dict, list[str]]:
    """Return `(merged_config, actions)` without touching disk."""
    desired = render_mcp_entry(harness_root, python_exe)

    raw_servers = config.get("mcpServers", {})
    if not isinstance(raw_servers, dict):
        raise RegistrationError(
            f"'mcpServers' is a {type(raw_servers).__name__}, expected an object; "
            "refusing to modify MCP registration"
        )
    servers = dict(raw_servers)
    current = servers.get(MCP_SERVER_NAME)

    if current == desired:
        return dict(config), [f"mcp/{MCP_SERVER_NAME}: already current"]

    # An entry under our name that does not launch our server belongs to
    # somebody else. Overwriting it would silently break their tooling, so
    # this stops and asks rather than guessing.
    if current is not None and not _launches_our_server(current):
        raise RegistrationError(
            f"a different '{MCP_SERVER_NAME}' MCP server is already registered; "
            "refusing to overwrite it -- rename or remove it and re-run"
        )

    servers[MCP_SERVER_NAME] = desired
    merged = dict(config)
    merged["mcpServers"] = servers
    return merged, [f"mcp/{MCP_SERVER_NAME}: {'updated' if current else 'registered'}"]


def _launches_our_server(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("args"), list)
        and "harness.mcp_server" in entry["args"]
    )


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return config_validation.load_json_object(path)
    except config_validation.ConfigValidationError as exc:
        raise RegistrationError(f"{path} is invalid; refusing to modify it ({exc})") from exc


def _save(path: Path, data: dict) -> None:
    # Atomic replace, so a crash mid-write cannot leave Claude Code holding a
    # truncated `~/.claude.json` -- that file also carries live session state,
    # not just our one key.
    path.parent.mkdir(parents=True, exist_ok=True)
    from harness.install_cli import _atomic_write_text

    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--harness-root",
        default=str(HARNESS_ROOT),
        help="the checkout whose hook scripts should be registered (default: this one)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter the hooks and MCP server run under (default: this one)",
    )
    parser.add_argument("--skip-mcp", action="store_true", help="register hooks only")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    harness_root = Path(args.harness_root).resolve()

    try:
        settings_path = user_settings_path()
        settings, actions = merge_hooks(_load(settings_path), harness_root, args.python)
        targets: list[tuple[Path, dict]] = [(settings_path, settings)]

        if not args.skip_mcp:
            mcp_path = user_mcp_config_path()
            mcp_config, mcp_actions = merge_mcp(_load(mcp_path), harness_root, args.python)
            targets.append((mcp_path, mcp_config))
            actions += mcp_actions
    except RegistrationError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    for action in actions:
        print(f"    {action}")

    if args.dry_run:
        print("    (dry run -- nothing written)")
        return 0

    for path, data in targets:
        if _load(path) != data:
            _save(path, data)
            print(f"    wrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
