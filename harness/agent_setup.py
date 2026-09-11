"""Install user-scope adapters for Claude Code, Codex, and Antigravity.

The memory store and MCP server remain agent-neutral.  This command owns only
each client's thin adapter:

* Claude Code: its existing SessionStart / graph-guard hooks and MCP entry,
  plus the user-scope memory protocol in ``~/.claude/CLAUDE.md``.
* Codex: a user-scope stdio MCP registration through ``codex mcp add``.
* Antigravity: a global plugin with the same MCP server and a first-invocation
  memory-context hook.

Usage:
    agent-harness-setup --agents claude,codex,antigravity
    agent-harness-setup --agents codex --dry-run
    agent-harness-setup --workspace ~/code       # enable auto-enrollment

The command deliberately does not auto-record or promote memory.  It only
makes bounded retrieval and proposed capture available to each agent: the
rules it installs tell an agent when recording is worthwhile, and everything
an agent records still lands as ``proposed`` for a human to accept.

``--workspace`` is what turns enrollment automatic.  Without it a project
never gets a memory store, so the SessionStart hook stays silent and every
packet comes back empty -- the single most common reason this harness looks
like it is doing nothing.  It is a path rather than a boolean because the
blast radius has to be named: the hook runs in every directory the client is
opened in, and scaffolding committed files into a repository you merely
cloned to read is not a good surprise.
"""

from __future__ import annotations

import argparse
import importlib.resources as resources
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from harness import bootstrap_register, pathsafe

HARNESS_ROOT = Path(__file__).resolve().parent.parent
SUPPORTED_AGENTS = ("claude", "codex", "antigravity")
MCP_SERVER_NAME = bootstrap_register.MCP_SERVER_NAME


class SetupError(RuntimeError):
    """A client adapter could not be installed safely."""


def antigravity_plugin_dir(home: Path | None = None) -> Path:
    """Return Antigravity CLI's documented global plugin location."""
    return (home or Path.home()) / ".gemini" / "config" / "plugins" / MCP_SERVER_NAME


def mcp_registration(harness_root: Path, python_exe: str) -> dict:
    """The single MCP server entry every client adapter registers.

    One function, one shape, one harness root, for all three clients. This
    existed in three places before and they disagreed: Claude Code was
    registered with a `PYTHONPATH` pointing at site-packages while Codex got
    one pointing at a checkout, so the same tool name served different code
    depending on which client asked. A divergence like that is invisible from
    inside any one client -- each looks correctly configured -- and it is only
    noticed when an agent calls a tool that exists in one install and not the
    other. Every adapter below therefore renders its command, arguments and
    environment from here rather than assembling its own.
    """
    return bootstrap_register.render_mcp_entry(Path(harness_root).resolve(), python_exe)


def render_antigravity_plugin(harness_root: Path, python_exe: str) -> dict[str, str]:
    """Return the complete managed Antigravity plugin, without writing it."""
    root = Path(harness_root).resolve()
    hook_command = bootstrap_register.render_hook_command(
        python_exe, root / "hooks" / "antigravity" / "memory_context_hook.py"
    )
    server = mcp_registration(root, python_exe)
    return {
        "plugin.json": json.dumps(
            {
                "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                "name": MCP_SERVER_NAME,
                "description": "Bounded local project memory and code-graph tools.",
            },
            indent=2,
        )
        + "\n",
        "mcp_config.json": json.dumps({"mcpServers": {MCP_SERVER_NAME: server}}, indent=2)
        + "\n",
        "hooks.json": json.dumps(
            {
                "agent-harness-memory": {
                    "PreInvocation": [
                        {"type": "command", "command": hook_command, "timeout": 10}
                    ]
                }
            },
            indent=2,
        )
        + "\n",
    }


def _atomic_write(path: Path, text: str) -> None:
    from harness.install_cli import _atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, text)


def install_antigravity(
    harness_root: Path, python_exe: str, *, dry_run: bool, home: Path | None = None
) -> list[str]:
    """Install or refresh only the harness-owned Antigravity plugin files."""
    plugin_dir = antigravity_plugin_dir(home)
    files = render_antigravity_plugin(harness_root, python_exe)
    manifest = plugin_dir / "plugin.json"
    if manifest.exists():
        try:
            existing = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SetupError(f"{manifest} is unreadable; refusing to replace it ({exc})") from exc
        if not isinstance(existing, dict) or existing.get("name", MCP_SERVER_NAME) != MCP_SERVER_NAME:
            raise SetupError(f"{plugin_dir} belongs to a different Antigravity plugin; refusing to overwrite it")

    actions: list[str] = []
    for relative, content in files.items():
        path = plugin_dir / relative
        if path.exists() and path.read_text(encoding="utf-8") == content:
            actions.append(f"antigravity/{relative}: already current")
        else:
            actions.append(f"antigravity/{relative}: {'would write' if dry_run else 'written'}")
            if not dry_run:
                _atomic_write(path, content)
    return actions


def _codex_command(harness_root: Path, python_exe: str) -> list[str]:
    """Translate the shared registration into Codex's CLI surface.

    Derived from `mcp_registration` rather than spelled out again, so Codex
    receives byte-identical command, args and environment to Claude Code and
    Antigravity for the same harness root.
    """
    entry = mcp_registration(harness_root, python_exe)
    command = ["codex", "mcp", "add", MCP_SERVER_NAME]
    for key, value in entry["env"].items():
        command += ["--env", f"{key}={value}"]
    command += ["--", entry["command"], *entry["args"]]
    return command


def install_codex(harness_root: Path, python_exe: str, *, dry_run: bool) -> list[str]:
    """Register the stdio server through Codex's supported CLI surface."""
    if shutil.which("codex") is None:
        return [
            "codex/mcp: skipped (Codex CLI is not on PATH; rerun "
            "`agent-harness-setup --agents codex` after installing it)"
        ]

    existing = subprocess.run(
        ["codex", "mcp", "get", MCP_SERVER_NAME], capture_output=True, text=True, check=False
    )
    if existing.returncode == 0:
        return [f"codex/mcp/{MCP_SERVER_NAME}: already registered (left unchanged)"]

    command = _codex_command(harness_root, python_exe)
    if dry_run:
        display = " ".join(pathsafe.shell_quote(part) for part in command)
        return [f"codex/mcp/{MCP_SERVER_NAME}: would run {display}"]

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SetupError(f"Codex MCP registration failed: {detail}")
    return [f"codex/mcp/{MCP_SERVER_NAME}: registered"]


AUTO_ENROLL_ENV = "AGENT_HARNESS_AUTO_ENROLL"
WORKSPACE_ENV = "AGENT_HARNESS_WORKSPACE"


def merge_workspace_env(settings: dict, workspaces: list[Path]) -> tuple[dict, str]:
    """Return `(settings, action)` with auto-enrollment enabled for `workspaces`.

    Both variables are set together because the hook requires both, and
    setting one alone produces a configuration that looks enabled and does
    nothing -- the exact failure this option exists to end.

    Existing unrelated `env` keys are preserved, and a malformed `env` is
    refused rather than overwritten: this file also holds the user's hooks and
    permissions, and silently rewriting it would be a far worse outcome than
    declining to enable a convenience.
    """
    merged = dict(settings)
    raw_env = merged.get("env", {})
    if not isinstance(raw_env, dict):
        raise SetupError(
            f"'env' is a {type(raw_env).__name__}, expected an object; "
            "refusing to modify settings"
        )
    env = dict(raw_env)
    value = os.pathsep.join(str(Path(w).expanduser()) for w in workspaces)
    before = (env.get(AUTO_ENROLL_ENV), env.get(WORKSPACE_ENV))
    env[AUTO_ENROLL_ENV] = "1"
    env[WORKSPACE_ENV] = value
    merged["env"] = env
    action = "unchanged" if before == ("1", value) else "updated"
    return merged, action


def install_auto_enrollment(workspaces: list[Path], *, dry_run: bool) -> list[str]:
    """Turn on auto-enrollment for projects under `workspaces`.

    This is the step that makes the rest of the system visible. Until a
    project has a memory store the SessionStart hook emits nothing and every
    packet is empty, which reads as a broken install rather than an unenrolled
    project.
    """
    path = bootstrap_register.user_settings_path()
    settings, action = merge_workspace_env(bootstrap_register._load(path), workspaces)
    roots = ", ".join(str(Path(w).expanduser()) for w in workspaces)
    if dry_run:
        return [f"claude: would enable auto-enrollment under {roots}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_register._save(path, settings)
    return [f"claude: auto-enrollment {action} for {roots}"]


def claude_rules_path(home: Path | None = None) -> Path:
    """Claude Code's user-scope instruction file, loaded in every project."""
    return (home or Path.home()) / ".claude" / "CLAUDE.md"


def install_memory_rules(*, home: Path | None = None, dry_run: bool) -> list[str]:
    """Install the memory protocol into Claude Code's user-scope rules.

    User scope rather than per-project is the whole point. The MCP tools are
    already registered once for every project, so the instructions that say
    *when* to call them belong in the one file every session loads. The
    per-project block that `agent-harness-install` writes stays separate
    because it carries graph and blueprint conventions that only make sense
    where a graph and a vault actually exist; this template deliberately
    contains neither, so it is true in any repository.

    Writing rules is what makes capture happen at all. The hooks only ever
    read: an agent records because these instructions tell it what is worth
    recording, and everything it records is still `proposed` until a human
    accepts it.
    """
    from harness import install_cli

    template = (
        resources.files("templates") / "MEMORY.protocol.md"
    ).read_text(encoding="utf-8")
    target = claude_rules_path(home)
    if dry_run:
        return [f"claude: would install memory protocol into {target}"]
    target.parent.mkdir(parents=True, exist_ok=True)
    action = install_cli.upsert_marker_block(target, template)
    return [f"claude: memory protocol {action} in {target}"]


def install_claude(
    harness_root: Path,
    python_exe: str,
    *,
    dry_run: bool,
    home: Path | None = None,
) -> list[str]:
    """Use the established atomic Claude registration path."""
    args = ["--harness-root", str(Path(harness_root).resolve()), "--python", python_exe]
    if dry_run:
        args.append("--dry-run")
    if bootstrap_register.main(args) != 0:
        raise SetupError("Claude Code registration failed; see the diagnostic above")
    return ["claude: hooks and MCP registration complete"] + install_memory_rules(
        home=home, dry_run=dry_run
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--agents", default=",".join(SUPPORTED_AGENTS),
        help="comma-separated adapters: claude,codex,antigravity (default: all)",
    )
    parser.add_argument("--harness-root", type=Path, default=HARNESS_ROOT)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing configuration")
    parser.add_argument(
        "--workspace",
        action="append",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "enable auto-enrollment for projects under PATH (repeatable). "
            "Without this, a project never gets a memory store and every "
            "context packet comes back empty."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    requested = [item.strip().lower() for item in args.agents.split(",") if item.strip()]
    unknown = sorted(set(requested) - set(SUPPORTED_AGENTS))
    if not requested or unknown:
        choices = ", ".join(SUPPORTED_AGENTS)
        print(f"invalid --agents value; choose one or more of: {choices}", file=sys.stderr)
        return 2

    # Resolved once, here, so every adapter in this run is handed the same
    # root: a relative `--harness-root .` must not become a checkout path for
    # one client and something else for the next.
    harness_root = Path(args.harness_root).resolve()
    # Said out loud because it is the one fact that decides which copy of the
    # harness every client will talk to. Run from an installed console script
    # this defaults to site-packages, run from a checkout it is the checkout;
    # an operator who cannot see which one they got cannot notice the split.
    print(f"registering harness root {harness_root} for: {', '.join(requested)}")
    installers = {
        "claude": lambda: install_claude(harness_root, args.python, dry_run=args.dry_run),
        "codex": lambda: install_codex(harness_root, args.python, dry_run=args.dry_run),
        "antigravity": lambda: install_antigravity(harness_root, args.python, dry_run=args.dry_run),
    }
    failed = False
    if args.workspace:
        try:
            for action in install_auto_enrollment(args.workspace, dry_run=args.dry_run):
                print(action)
        except SetupError as exc:
            failed = True
            print(f"[WARN] auto-enrollment: {exc}", file=sys.stderr)
    for agent in requested:
        try:
            for action in installers[agent]():
                print(action)
        except SetupError as exc:
            failed = True
            print(f"[WARN] {agent}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
