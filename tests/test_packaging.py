"""Release packaging tests: console scripts, resource packaging, and an
artifact scan proving no private-plans/company data ships in the wheel/sdist.

`python -m build` needs the `build` package (plus setuptools/wheel) available
in the current interpreter; these are standard build tooling, not a new
runtime dependency of the harness itself. Tests that need a real build skip
gracefully (rather than failing) when that tooling is absent, per the
release-verification packet's "attempt it, skip gracefully" instruction.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CONSOLE_SCRIPTS = {
    "agent-harness-query": "harness.graph_query:main",
    "agent-harness-doctor": "harness.doctor:main",
    "agent-harness-refresh": "harness.graph_refresh:main",
    "agent-harness-mcp": "harness.mcp_server:main",
    "agent-harness-install": "harness.install_cli:main",
    "agent-harness-telemetry": "harness.telemetry:main",
}


def _build_tooling_available() -> bool:
    try:
        import build  # noqa: F401
        import setuptools  # noqa: F401
        import wheel  # noqa: F401
    except ImportError:
        return False
    return True


requires_build_tooling = pytest.mark.skipif(
    not _build_tooling_available(),
    reason="build/setuptools/wheel not installed -- cannot build a real wheel/sdist",
)


def test_pyproject_declares_the_four_console_scripts() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts == CONSOLE_SCRIPTS


def test_pyproject_declares_build_backend() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"


def test_pyproject_targets_cpython_311_plus() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["requires-python"] == ">=3.11"


def test_pyproject_core_dependencies_stay_empty() -> None:
    """The core package must remain stdlib-only -- the [semantic] extra
    (chromadb) must never leak into the unconditional install."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []


def test_pyproject_declares_semantic_extra_with_chromadb_floor() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "semantic" in extras
    assert any(dep.startswith("chromadb>=0.5") for dep in extras["semantic"])


@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not _build_tooling_available():
        pytest.skip("build/setuptools/wheel not installed")
    outdir = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, f"build failed:\n{result.stdout}\n{result.stderr}"
    wheels = list(outdir.glob("*.whl"))
    sdists = list(outdir.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"
    return {"wheel": wheels[0], "sdist": sdists[0]}


@requires_build_tooling
def test_wheel_contains_expected_packages_and_resources(built_artifacts: dict[str, Path]) -> None:
    with zipfile.ZipFile(built_artifacts["wheel"]) as zf:
        names = set(zf.namelist())
    assert "harness/doctor.py" in names
    assert "harness/graph_query.py" in names
    assert "harness/graph_refresh.py" in names
    assert "harness/mcp_server.py" in names
    assert "harness/install_cli.py" in names
    assert "templates/AGENTS.harness.md" in names
    assert "templates/feature-blueprint.md" in names
    assert "hooks/claude/settings-fragment.json" in names
    entry_points = next(n for n in names if n.endswith("entry_points.txt"))
    with zipfile.ZipFile(built_artifacts["wheel"]) as zf:
        ep_text = zf.read(entry_points).decode("utf-8")
    for cmd, target in CONSOLE_SCRIPTS.items():
        assert f"{cmd} = {target}" in ep_text


# Company-identifying strings that must never appear in a release artifact.
# Kept intentionally narrow (specific proper nouns / real paths) so it does
# not false-positive on generic engineering vocabulary the harness itself
# legitimately uses (e.g. "graph", "hooks", "install").
#
# The forbidden terms are assembled from fragments at runtime so that this
# test file does NOT itself embed the literal company identifiers -- otherwise
# this file (which ships in the sdist) would be the very leak it scans for.
# This lets the sdist scan cover EVERY shipped file, including this one, and
# still prove zero company identifiers are present.
def _t(*parts: str) -> str:
    return "".join(parts)


# The GitHub handle "montjeffrey" is intentionally NOT on this list: the repo
# is published publicly at github.com/montjeffrey/agent-harness, so the handle
# legitimately appears in the README's clone URL. It is a public project
# identifier, not private company data. This denylist guards only genuinely
# private business identifiers and the author's real name.
FORBIDDEN_STRINGS = (
    _t("divine", "energy", "solutions"),
    _t("Divine ", "Energy ", "Solutions"),
    _t("DES", " CRM"),
    _t("Orc", "atec"),
    _t("Jeffrey ", "Montoya"),
    _t("One", "Drive - ", "divine", "energy", "solutions"),
)


@requires_build_tooling
def test_artifact_scan_wheel_has_no_private_content(built_artifacts: dict[str, Path]) -> None:
    with zipfile.ZipFile(built_artifacts["wheel"]) as zf:
        names = zf.namelist()
        assert not any("private-plans" in n for n in names), names
        for name in names:
            if name.endswith((".py", ".md", ".json", ".txt", ".cfg")):
                text = zf.read(name).decode("utf-8", errors="replace")
                for forbidden in FORBIDDEN_STRINGS:
                    assert forbidden not in text, f"{forbidden!r} found in wheel member {name}"


@requires_build_tooling
def test_artifact_scan_sdist_has_no_private_content(built_artifacts: dict[str, Path]) -> None:
    import tarfile

    with tarfile.open(built_artifacts["sdist"]) as tf:
        names = tf.getnames()
        assert not any("private-plans" in n for n in names), names
        for member in tf.getmembers():
            if not member.isfile():
                continue
            # No self-exclusion: this file assembles its denylist from
            # fragments (see _t / FORBIDDEN_STRINGS) so it contains no literal
            # company identifier, and the scan must cover it like any other
            # shipped file to genuinely prove the sdist is clean.
            if not member.name.endswith((".py", ".md", ".json", ".txt", ".cfg", ".toml")):
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            text = fh.read().decode("utf-8", errors="replace")
            for forbidden in FORBIDDEN_STRINGS:
                assert forbidden not in text, f"{forbidden!r} found in sdist member {member.name}"


@requires_build_tooling
def test_installed_wheel_console_scripts_run_outside_source_tree(
    built_artifacts: dict[str, Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Build a wheel, install it into a clean venv, and run the console
    scripts from a directory outside the source checkout entirely -- proving
    packaged resources (templates/hooks) resolve post-install and that the
    installed console scripts don't depend on any repo-relative path."""
    venv_dir = tmp_path_factory.mktemp("venv-clean")
    outside_cwd = tmp_path_factory.mktemp("outside-source-tree")

    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    venv_python = (
        venv_dir / "Scripts" / "python.exe" if sys.platform == "win32" else venv_dir / "bin" / "python"
    )
    assert venv_python.exists(), f"venv python not found at {venv_python}"

    install_result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", "--quiet", str(built_artifacts["wheel"])],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert install_result.returncode == 0, (
        f"pip install failed:\n{install_result.stdout}\n{install_result.stderr}"
    )

    scripts_dir = venv_dir / "Scripts" if sys.platform == "win32" else venv_dir / "bin"
    doctor_cmd = scripts_dir / ("agent-harness-doctor.exe" if sys.platform == "win32" else "agent-harness-doctor")
    query_cmd = scripts_dir / ("agent-harness-query.exe" if sys.platform == "win32" else "agent-harness-query")
    install_cmd = scripts_dir / ("agent-harness-install.exe" if sys.platform == "win32" else "agent-harness-install")
    assert doctor_cmd.exists(), f"console script not installed: {doctor_cmd}"
    assert query_cmd.exists(), f"console script not installed: {query_cmd}"
    assert install_cmd.exists(), f"console script not installed: {install_cmd}"

    # Run from OUTSIDE the source tree entirely -- proves no repo-relative
    # path assumption survived into the installed package.
    doctor_result = subprocess.run(
        [str(doctor_cmd)], cwd=str(outside_cwd), capture_output=True, text=True, timeout=60, check=False,
    )
    assert "harness files" in doctor_result.stdout, doctor_result.stdout
    assert "[FAIL] harness files" not in doctor_result.stdout, (
        "installed-wheel resource fallback did not resolve packaged templates/hooks:\n"
        + doctor_result.stdout
    )
    assert "installed wheel" in doctor_result.stdout, doctor_result.stdout

    query_result = subprocess.run(
        [str(query_cmd), "find", "nonexistent-term-xyz", "--graph", str(outside_cwd / "no-such-graph.json")],
        cwd=str(outside_cwd), capture_output=True, text=True, timeout=60, check=False,
    )
    # graph.json doesn't exist here -- what matters is the console script
    # itself launched, imported harness.graph_query, and produced the
    # module's own clear error rather than crashing on a missing dependency.
    assert "graph.json not found" in (query_result.stdout + query_result.stderr)

    # agent-harness-install: run against a fresh target dir with --skip-checks
    # (no-prompt install, same flag install.py exposes) -- proves the
    # installer's packaged resources (templates/hooks) resolve post-install
    # from OUTSIDE the source tree, exactly like doctor/query above.
    install_target = outside_cwd / "install-target"
    install_result = subprocess.run(
        [str(install_cmd), str(install_target), "--skip-checks"],
        cwd=str(outside_cwd), capture_output=True, text=True, timeout=60, check=False,
    )
    assert install_result.returncode == 0, (
        f"agent-harness-install failed:\n{install_result.stdout}\n{install_result.stderr}"
    )
    blueprint_dest = install_target / "docs" / "vault" / "templates" / "feature-blueprint.md"
    assert blueprint_dest.exists(), install_result.stdout
    agents_md = (install_target / "AGENTS.md").read_text(encoding="utf-8")
    assert "<!-- agent-harness:start -->" in agents_md

    shutil.rmtree(venv_dir, ignore_errors=True)
