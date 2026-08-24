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
    "templates/MEMORY.protocol.md",
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
        if not (res.files("templates") / "MEMORY.protocol.md").is_file():
            missing.append("templates/MEMORY.protocol.md")
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
        "module not importable -- MCP tool server unavailable; from this checkout run "
        '`python -m pip install -e ".[mcp]"`',
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


def _local_semantic_install_command() -> str:
    """The only install instruction valid for this unpublished checkout."""
    return 'python -m pip install -e ".[semantic]"'


def check_semantic() -> CheckResult:
    """Report whether the optional ChromaDB dependency is importable.

    NEVER returns FAIL -- this extra is opt-in, and installs (and the rest
    of the harness) must keep working perfectly well without it.
    """
    try:
        from harness import graph_index
    except ImportError:  # pragma: no cover -- harness.graph_index always ships
        sys.path.insert(0, str(HARNESS_ROOT))
        from harness import graph_index

    available, reason = graph_index.chromadb_status()
    if not available:
        return CheckResult(
            "WARN", "semantic dependencies",
            "chromadb not importable -- graph search and memory selection remain "
            f"deterministic ({reason}); from this checkout run "
            f"`{_local_semantic_install_command()}`",
        )
    return CheckResult("PASS", "semantic dependencies", "chromadb importable")


def check_graph_semantic(project_dir: Path | None = None) -> CheckResult:
    """Report whether the project's graph semantic index is query-ready."""
    from harness import graph_index

    if not graph_index.chromadb_available():
        return CheckResult(
            "INFO", "graph semantic index",
            "unavailable until chromadb is installed; graph find queries still work",
        )
    if project_dir is None:
        return CheckResult("INFO", "graph semantic index", "no project supplied; cannot inspect graph semantic index")

    graph_path = project_dir.resolve() / "graphify-out" / "graph.json"
    if not graph_path.is_file():
        return CheckResult(
            "INFO", "graph semantic index",
            "graph.json is missing; graph semantic index is unavailable until graph refresh builds it",
        )
    index_dir = graph_index.index_path_for(graph_path)
    if not index_dir.exists():
        return CheckResult(
            "INFO", "graph semantic index",
            "no graph semantic index yet -- run harness/graph_refresh.py to build one",
        )
    if graph_index.index_is_stale(index_dir, graph_path):
        return CheckResult(
            "WARN", "graph semantic index",
            "graph semantic index is older than graph.json -- run harness/graph_refresh.py to rebuild it",
        )
    problem = graph_index.probe_index(index_dir)
    if problem is not None:
        return CheckResult(
            "WARN", "graph semantic index",
            "graph semantic index is not query-ready "
            f"({problem}) -- run harness/graph_refresh.py to rebuild it",
        )
    return CheckResult("PASS", "graph semantic index", "graph semantic index present and up to date")


def check_memory_semantic(project_dir: Path | None = None) -> CheckResult:
    """Report whether the supplied project's memory semantic index is ready."""
    from harness import memory_index, memory_store

    if not memory_index.chromadb_available():
        return CheckResult(
            "INFO", "memory semantic index",
            "unavailable until chromadb is installed; memory selection remains deterministic",
        )
    if project_dir is None:
        return CheckResult("INFO", "memory semantic index", "no project supplied; cannot inspect memory semantic index")

    project_index = memory_index.index_dir_for(memory_store.project_id_for(project_dir))
    machine_index = memory_index.machine_index_dir()
    present = [name for name, path in (("project", project_index), ("machine", machine_index)) if path.exists()]
    if not present:
        return CheckResult(
            "INFO", "memory semantic index",
            "no memory semantic index yet -- run agent-harness-memory index to build one",
        )
    problems = [
        (name, memory_index.probe_index(path))
        for name, path in (("project", project_index), ("machine", machine_index))
        if name in present
    ]
    unreadable = [(name, problem) for name, problem in problems if problem is not None]
    if unreadable:
        details = [
            f"{name} index is not query-ready ({problem})"
            for name, problem in unreadable
        ]
        missing = [name for name in ("project", "machine") if name not in present]
        details.extend(
            f"no {name}-scope semantic index yet; that scope uses deterministic selection"
            for name in missing
        )
        return CheckResult(
            "WARN", "memory semantic index",
            f"{'; '.join(details)} -- run agent-harness-memory index to rebuild it",
        )
    if len(present) == 1:
        return CheckResult(
            "INFO", "memory semantic index",
            f"only the {present[0]}-scope memory semantic index exists; missing scope uses deterministic selection",
        )
    return CheckResult("PASS", "memory semantic index", "project and machine memory semantic indexes present")


def check_memory_store(project_dir: Path | None = None) -> CheckResult:
    """Report whether the supplied project's memory store holds anything.

    Distinct from `check_memory_semantic`, which asks whether the *index* over
    the entries is query-ready. This asks the prior question: are there any
    entries at all? The two failure modes look identical from the outside and
    are fixed differently -- an empty store needs recording, a stale index
    needs rebuilding.

    An enrolled store with zero entries is a WARN, not an INFO, because
    enrollment is supposed to leave something behind. When it is empty, the
    session hook stays nearly silent and an unenrolled project and a wired-but-
    idle one become indistinguishable at a glance. That ambiguity is the whole
    reason this check exists.
    """
    from harness import memory_store

    if project_dir is None:
        return CheckResult("INFO", "memory store", "no project supplied; cannot inspect memory store")

    if not (Path(project_dir) / ".agent-harness").is_dir():
        return CheckResult(
            "INFO", "memory store",
            "project is not enrolled; run agent-harness-enroll to create .agent-harness/",
        )

    try:
        project_entries = sum(
            1 for _ in memory_store.iter_entries(memory_store.project_memory_dir(Path(project_dir)))
        )
    except OSError as exc:
        return CheckResult("FAIL", "memory store", f"project memory is unreadable ({exc})")

    try:
        machine_entries = sum(1 for _ in memory_store.iter_entries(memory_store.machine_memory_dir()))
    except OSError:
        machine_entries = 0

    if project_entries == 0:
        detail = (
            "enrolled but empty -- 0 project entries recorded, so the session hook "
            "has nothing to inject and a wired project looks identical to an "
            "unenrolled one; run agent-harness-memory ingest to seed pointers from "
            "existing documentation, or record by hand with "
            "agent-harness-memory remember"
        )
        if machine_entries:
            detail += f" ({machine_entries} machine-scope entries exist and are unaffected)"
        return CheckResult("WARN", "memory store", detail)

    return CheckResult(
        "PASS", "memory store",
        f"{project_entries} project entries, {machine_entries} machine entries",
    )


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
        check_semantic(),
        check_graph_semantic(project_dir),
        check_memory_semantic(project_dir),
        check_memory_store(project_dir),
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
