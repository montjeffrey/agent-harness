"""A repository must not be able to replace the harness by shadowing its modules.

`python -m pkg` puts the current directory at `sys.path[0]`, ahead of
site-packages and `PYTHONPATH`. Stdio MCP servers start in the project being
opened, so a project holding its own `harness/` (or `graphify/`, `mcp/`)
package would run its code in place of the real one. Every launch site passes
`-P` to stop that. These tests start the real commands from a directory that
holds a hostile package and check it is never imported.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from harness import bootstrap_register, graph_refresh, install_cli

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKER = "HIJACKED"


def _hostile_package(directory: Path, package: str, module: str) -> None:
    pkg = directory / package
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / f"{module}.py").write_text(f"print({MARKER!r})\n", encoding="utf-8")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def test_registered_mcp_entry_ignores_a_harness_package_in_the_cwd(tmp_path: Path) -> None:
    _hostile_package(tmp_path, "harness", "mcp_server")
    entry = bootstrap_register.render_mcp_entry(REPO_ROOT, sys.executable)

    result = subprocess.run(
        [sys.executable, *entry["args"]],
        cwd=tmp_path, env=_env(), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120, check=False,
    )

    assert MARKER not in result.stdout + result.stderr


def test_every_registered_python_launch_passes_safe_path(tmp_path: Path) -> None:
    assert bootstrap_register.render_mcp_entry(REPO_ROOT, "/py")["args"][0] == "-P"

    install_cli.install_mcp_registration(tmp_path, REPO_ROOT)
    import json

    project_entry = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert project_entry["mcpServers"]["agent-harness"]["args"][0] == "-P"


def test_graphify_probe_ignores_a_graphify_package_in_the_cwd(tmp_path: Path, monkeypatch) -> None:
    _hostile_package(tmp_path, "graphify", "__main__")
    monkeypatch.chdir(tmp_path)

    status = graph_refresh.detect_graphify()

    assert MARKER not in (status.version or "")


def test_graphify_commands_pass_safe_path() -> None:
    cmd = graph_refresh.build_graphify_command("update", Path("/repo"), "auto")
    assert cmd[:4] == [sys.executable, "-P", "-m", "graphify"]
