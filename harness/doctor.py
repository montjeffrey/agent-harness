"""Preflight environment checks for the agent harness.

Reports what is present, missing, or misconfigured BEFORE a full install,
so `install.py` never half-applies into a broken environment.

Usage:
    python harness/doctor.py [<project-dir>]

Exit codes: 0 = no failures (warnings allowed), 1 = at least one FAIL.

Check levels:
    FAIL - install would break or silently misbehave; install.py refuses.
    WARN - harness installs fine but a capability is degraded (e.g. graph
           refresh unavailable until graphify is installed).
    INFO - environment facts useful to the operator; never blocks.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import config_validation
except ImportError:  # running as `python harness/doctor.py`, not `-m harness.doctor`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import config_validation
DEFAULT_CONFIG_PATH = Path.home() / ".agent-harness" / "config.json"
DEFAULT_TELEMETRY_DIR = Path.home() / ".agent-harness" / "telemetry"

MIN_PYTHON = (3, 11)

REQUIRED_HARNESS_FILES = (
    "install.py",
    "harness/graph_query.py",
    "harness/graph_refresh.py",
    "templates/AGENTS.harness.md",
    "templates/feature-blueprint.md",
    "hooks/claude/settings-fragment.json",
)

AGENT_CLIS = (
    ("claude", "Claude Code"),
    ("codex", "OpenAI Codex"),
    ("gemini", "Gemini CLI"),
    ("agy", "Antigravity"),
)


@dataclass(frozen=True)
class CheckResult:
    level: str  # "PASS" | "WARN" | "FAIL" | "INFO"
    name: str
    detail: str


def check_python() -> CheckResult:
    version = sys.version_info[:3]
    version_str = ".".join(str(v) for v in version)
    if version >= MIN_PYTHON:
        return CheckResult("PASS", "python", f"{version_str} (>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} required)")
    return CheckResult(
        "FAIL", "python",
        f"{version_str} found; the harness needs Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
    )


def _check_installed_resources() -> CheckResult:
    """Installed-wheel equivalent of the sibling-file check below.

    A pip-installed `agent-harness` has no `install.py` sibling to anchor
    HARNESS_ROOT against (only the `harness`/`templates`/`hooks` packages
    ship in the wheel), so the console-script entry points (`agent-harness-
    doctor` et al.) need a packaging-aware check instead of a filesystem
    sibling-path check. Uses importlib.resources so this works regardless of
    where the wheel was installed.
    """
    import importlib.resources as res

    missing: list[str] = []
    try:
        if not (res.files("templates") / "AGENTS.harness.md").is_file():
            missing.append("templates/AGENTS.harness.md")
        if not (res.files("templates") / "feature-blueprint.md").is_file():
            missing.append("templates/feature-blueprint.md")
        if not (res.files("hooks.claude") / "settings-fragment.json").is_file():
            missing.append("hooks/claude/settings-fragment.json")
        if importlib.util.find_spec("harness.install_cli") is None:
            missing.append("harness.install_cli (broken wheel build?)")
    except ModuleNotFoundError as exc:
        return CheckResult("FAIL", "harness files", f"packaged resources not importable: {exc}")
    if missing:
        return CheckResult(
            "FAIL", "harness files",
            f"missing packaged resources: {', '.join(missing)} (broken wheel build?)",
        )
    return CheckResult("PASS", "harness files", "all packaged resources present (installed wheel)")


def check_harness_files(harness_root: Path = HARNESS_ROOT) -> CheckResult:
    # Only the real default HARNESS_ROOT is eligible for the installed-wheel
    # fallback -- an explicitly-passed harness_root (as tests do to exercise
    # the FAIL path) always uses the sibling-file check unchanged below.
    if harness_root == HARNESS_ROOT and not (harness_root / "install.py").exists():
        return _check_installed_resources()
    missing = [rel for rel in REQUIRED_HARNESS_FILES if not (harness_root / rel).exists()]
    if missing:
        return CheckResult(
            "FAIL", "harness files",
            f"missing from {harness_root}: {', '.join(missing)} (incomplete clone?)",
        )
    return CheckResult("PASS", "harness files", f"all {len(REQUIRED_HARNESS_FILES)} core files present")


def check_git() -> CheckResult:
    if shutil.which("git"):
        return CheckResult("PASS", "git", "on PATH")
    return CheckResult(
        "WARN", "git",
        "not on PATH -- graph staleness checks (built_at_commit vs HEAD) won't work",
    )


def check_graphify() -> CheckResult:
    try:
        spec = importlib.util.find_spec("graphify")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        return CheckResult("PASS", "graphify", "python module importable")
    return CheckResult(
        "WARN", "graphify",
        "module not importable -- graph_query works on an existing graph, but "
        "graph_refresh needs it (pip install graphifyy)",
    )


def check_mcp() -> CheckResult:
    """PASS if the optional `mcp` Python SDK is importable (enables
    `harness.mcp_server` / `agent-harness-mcp`), else WARN -- the harness
    (and `--hooks`-based installs) work fine without it; only the MCP tool
    surface is unavailable."""
    try:
        spec = importlib.util.find_spec("mcp")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        return CheckResult("PASS", "mcp sdk", "python module importable -- MCP server available")
    return CheckResult(
        "WARN", "mcp sdk",
        "module not importable -- MCP tool server unavailable (pip install agent-harness[mcp])",
    )


def check_backend_key(config_path: Path = DEFAULT_CONFIG_PATH) -> CheckResult:
    """Verify a graph-refresh LLM backend is resolvable without printing any key."""
    try:
        from harness.graph_refresh import BACKEND_ENV_VARS
    except ImportError:
        sys.path.insert(0, str(HARNESS_ROOT))
        from harness.graph_refresh import BACKEND_ENV_VARS

    backend = os.environ.get("HARNESS_GRAPH_BACKEND") or "auto"
    env_var = BACKEND_ENV_VARS.get(backend)

    if env_var is None:
        return CheckResult(
            "WARN", "graph backend",
            f"resolves to '{backend}' (no HARNESS_GRAPH_BACKEND set) -- community "
            "labeling silently degrades to file names without an LLM backend; "
            "set HARNESS_GRAPH_BACKEND (e.g. nvidia-nim)",
        )
    if os.environ.get(env_var):
        return CheckResult("PASS", "graph backend", f"'{backend}' with {env_var} set in environment")

    if not config_path.exists():
        return CheckResult(
            "WARN", "graph backend",
            f"'{backend}' selected but {env_var} unset and {config_path} absent -- "
            f'create it with {{"key_command": "...", "key_env": "{env_var}"}}',
        )
    try:
        config = config_validation.load_json_object(config_path)
    except config_validation.ConfigValidationError as exc:
        return CheckResult("FAIL", "graph backend", f"{config_path} unreadable/invalid JSON: {exc}")

    key_command = config.get("key_command")
    if not key_command or config.get("key_env") != env_var:
        return CheckResult(
            "WARN", "graph backend",
            f"{config_path} has no key_command matching key_env={env_var}",
        )

    result = subprocess.run(key_command, shell=True, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return CheckResult(
            "FAIL", "graph backend",
            f"key_command exited {result.returncode} -- graph refresh would fail",
        )
    if not result.stdout.strip():
        return CheckResult("FAIL", "graph backend", "key_command produced no output")
    return CheckResult("PASS", "graph backend", f"'{backend}' key sourced via key_command (value not shown)")


def check_semantic(project_dir: Path | None = None) -> CheckResult:
    """Optional `[semantic]` extra: the ChromaDB-backed `search` subcommand.

    NEVER returns FAIL -- this extra is opt-in, and installs (and the rest
    of the harness) must keep working perfectly well without it.
    """
    try:
        from harness import graph_index
    except ImportError:  # pragma: no cover -- harness.graph_index always ships
        sys.path.insert(0, str(HARNESS_ROOT))
        from harness import graph_index

    if not graph_index.chromadb_available():
        return CheckResult(
            "WARN", "semantic search",
            "chromadb not importable -- the `search` subcommand is unavailable; "
            'pip install "agent-harness[semantic]" to enable it',
        )
    if project_dir is None:
        return CheckResult("PASS", "semantic search", "chromadb importable")

    graph_path = project_dir.resolve() / "graphify-out" / "graph.json"
    index_dir = graph_index.index_path_for(graph_path)
    if not index_dir.exists():
        return CheckResult(
            "INFO", "semantic search",
            "chromadb importable; no semantic index yet -- run harness/graph_refresh.py to build one",
        )
    if graph_index.index_is_stale(index_dir, graph_path):
        return CheckResult(
            "WARN", "semantic search",
            "semantic index is older than graph.json -- run harness/graph_refresh.py to rebuild it",
        )
    return CheckResult("PASS", "semantic search", "chromadb importable; semantic index present and up to date")


def check_agent_clis() -> CheckResult:
    found = [label for cmd, label in AGENT_CLIS if shutil.which(cmd)]
    if found:
        return CheckResult("INFO", "agent CLIs", f"detected: {', '.join(found)}")
    return CheckResult(
        "WARN", "agent CLIs",
        "none of claude/codex/gemini/agy found on PATH -- the harness installs "
        "fine, but no agent will be reading its instruction blocks yet",
    )


def check_telemetry(telemetry_dir: Path = DEFAULT_TELEMETRY_DIR) -> CheckResult:
    """Report local telemetry store state. Absence is normal on a fresh
    install (telemetry is collected manually via `harness/telemetry.py
    collect`, never automatically), so this never WARNs or FAILs -- INFO
    only, both when the dir is missing and when it's present.
    """
    if not telemetry_dir.is_dir():
        return CheckResult("INFO", "telemetry", "no telemetry collected yet")

    try:
        from harness import telemetry_store
    except ImportError:
        sys.path.insert(0, str(HARNESS_ROOT))
        from harness import telemetry_store

    count = 0
    agents: set[str] = set()
    for rec in telemetry_store.iter_events(directory=telemetry_dir):
        count += 1
        agent = rec.get("agent")
        if isinstance(agent, str):
            agents.add(agent)

    if count == 0:
        return CheckResult("INFO", "telemetry", "telemetry dir present but no valid records found")
    agents_str = ", ".join(sorted(agents)) or "none"
    return CheckResult("INFO", "telemetry", f"{count} record(s) from agent(s): {agents_str}")


def check_project(project_dir: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    project_dir = project_dir.resolve()

    if not project_dir.exists():
        results.append(CheckResult(
            "INFO", "project dir",
            f"{project_dir} does not exist yet -- install.py will create it",
        ))
        return results
    if not project_dir.is_dir():
        results.append(CheckResult("FAIL", "project dir", f"{project_dir} exists but is not a directory"))
        return results
    if not os.access(project_dir, os.W_OK):
        results.append(CheckResult("FAIL", "project dir", f"{project_dir} is not writable"))
        return results
    results.append(CheckResult("PASS", "project dir", str(project_dir)))

    if (project_dir / ".git").exists():
        results.append(CheckResult("INFO", "project git", "is a git repository"))
    else:
        results.append(CheckResult(
            "INFO", "project git",
            "not a git repository -- .gitignore entry will still be written",
        ))

    settings_path = project_dir / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            config_validation.load_json_object(settings_path)
            results.append(CheckResult("PASS", "claude settings", ".claude/settings.json is valid JSON"))
        except config_validation.ConfigValidationError as exc:
            results.append(CheckResult(
                "FAIL", "claude settings",
                f".claude/settings.json is invalid ({exc}) -- fix it before --hooks merge",
            ))

    mcp_json_path = project_dir / ".mcp.json"
    if mcp_json_path.exists():
        try:
            mcp_config = config_validation.load_json_object(mcp_json_path)
            servers = mcp_config.get("mcpServers", {})
            if not isinstance(servers, dict):
                results.append(CheckResult(
                    "FAIL", "mcp registration",
                    f".mcp.json has a non-object 'mcpServers' value ({type(servers).__name__}) "
                    "-- fix it before --mcp merge",
                ))
            elif "agent-harness" in servers:
                results.append(CheckResult(
                    "INFO", "mcp registration", ".mcp.json registers the agent-harness MCP server",
                ))
            else:
                results.append(CheckResult(
                    "INFO", "mcp registration",
                    ".mcp.json exists but does not register agent-harness -- run install.py --mcp",
                ))
        except config_validation.ConfigValidationError as exc:
            results.append(CheckResult(
                "FAIL", "mcp registration", f".mcp.json is invalid ({exc}) -- fix it before --mcp merge",
            ))
    else:
        results.append(CheckResult(
            "INFO", "mcp registration", "no .mcp.json yet -- run install.py --mcp to register the MCP server",
        ))

    graph_path = project_dir / "graphify-out" / "graph.json"
    if graph_path.exists():
        size_mb = graph_path.stat().st_size / (1024 * 1024)
        results.append(CheckResult("INFO", "code graph", f"graph.json present ({size_mb:.1f} MB)"))
    else:
        results.append(CheckResult(
            "INFO", "code graph",
            "no graphify-out/graph.json yet -- run harness/graph_refresh.py <project> --full after install",
        ))

    agents_md = project_dir / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text(encoding="utf-8")
        if "<!-- agent-harness:start -->" in content:
            results.append(CheckResult("INFO", "AGENTS.md", "harness block already present (install will refresh it)"))
        else:
            results.append(CheckResult(
                "INFO", "AGENTS.md",
                "exists; harness block will be appended, existing content preserved",
            ))

    return results


def run_checks(project_dir: Path | None = None) -> list[CheckResult]:
    results = [
        check_python(),
        check_harness_files(),
        check_git(),
        check_graphify(),
        check_mcp(),
        check_backend_key(),
        check_semantic(project_dir),
        check_agent_clis(),
        check_telemetry(),
    ]
    if project_dir is not None:
        results.extend(check_project(project_dir))
    return results


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.level == "FAIL" for r in results)


def format_report(results: list[CheckResult]) -> str:
    icons = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]", "INFO": "[ -- ]"}
    lines = ["agent-harness doctor", "=" * 60]
    lines.extend(f"{icons[r.level]} {r.name}: {r.detail}" for r in results)
    lines.append("=" * 60)
    fails = sum(1 for r in results if r.level == "FAIL")
    warns = sum(1 for r in results if r.level == "WARN")
    if fails:
        lines.append(f"{fails} failure(s), {warns} warning(s) -- fix failures before installing.")
    elif warns:
        lines.append(f"0 failures, {warns} warning(s) -- safe to install; warnings limit some features.")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    project_dir = Path(args[0]) if args else None
    results = run_checks(project_dir)
    print(format_report(results))
    return 1 if has_failures(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
