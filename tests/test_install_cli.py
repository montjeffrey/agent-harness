"""Tests for harness/install_cli.py: the module install.py now shims.

Covers what test_install.py (which exercises everything through the
`install` shim) does not: that the shim genuinely re-exports the identical
objects `harness.install_cli` defines, that `harness.install_cli.run_install`
itself (not just via the shim) writes the expected file set, that packaged
resources resolve byte-for-byte via `importlib.resources`, and idempotency
of a second run called directly against the new module.
"""

from __future__ import annotations

import importlib.resources as resources
import shutil
import sys
from pathlib import Path

import install
from harness import install_cli


def test_run_install_writes_expected_files_via_install_cli_directly(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    summary = install_cli.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)

    assert (project / "docs" / "vault" / "blueprints" / ".gitkeep").exists()
    assert (project / "docs" / "vault" / "templates" / "feature-blueprint.md").exists()
    assert (project / "docs" / "vault" / "README.md").exists()
    assert not (project / ".gitignore").exists()

    agents_md = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert install_cli.MARKER_START in agents_md
    assert install_cli.MARKER_END in agents_md
    # Graph-query instructions are present -- checked via the invariant part
    # of the command (its arguments), since the command *prefix* itself
    # legitimately differs by environment: `agent-harness-query` when the
    # installed console scripts are resolvable, `<python> -m
    # harness.graph_query` as the source-checkout fallback otherwise (see
    # `harness.install_cli._query_command`).
    assert "find <term>" in agents_md

    claude_md = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert install_cli.CLAUDE_POINTER in claude_md

    gemini_md = (project / "GEMINI.md").read_text(encoding="utf-8")
    assert install_cli.GEMINI_POINTER in gemini_md

    assert any("created" in line for line in summary)


def test_second_run_install_via_install_cli_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    install_cli.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)

    files_before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    summary_second = install_cli.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)
    files_after = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}

    assert files_before == files_after
    assert all("unchanged" in line for line in summary_second)


# Every public name test_install.py imports off `install` -- proven identical
# to the object `harness.install_cli` defines, i.e. the shim is a pure
# re-export and not a copy/reimplementation that could drift.
_SHIM_PUBLIC_NAMES = (
    "CLAUDE_POINTER",
    "GEMINI_POINTER",
    "HARNESS_ROOT",
    "HOOK_FRAGMENT_PATH",
    "MARKER_END",
    "MARKER_START",
    "VAULT_README_TEMPLATE",
    "InstallError",
    "InstallTransaction",
    "_json_string_escape",
    "_shell_quote",
    "deep_merge_hooks",
    "install_hooks",
    "install_vault_files",
    "main",
    "parse_args",
    "render_agents_block",
    "render_vault_readme",
    "run_install",
    "upsert_marker_block",
)


def test_shim_reexports_are_identical_objects_to_install_cli() -> None:
    for name in _SHIM_PUBLIC_NAMES:
        shim_obj = getattr(install, name)
        real_obj = getattr(install_cli, name)
        assert shim_obj is real_obj, f"install.{name} is not the same object as harness.install_cli.{name}"


def test_blueprint_resource_resolves_byte_for_byte_via_importlib_resources(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    install_cli.install_vault_files(project, install_cli.HARNESS_ROOT)

    # Compare as text (matching the blueprint's read_text()-based contract):
    # the source template may be checked out with platform-native line
    # endings (e.g. CRLF via git autocrlf on Windows), but install_vault_files
    # reads it in text mode and writes LF-normalized content -- a raw-bytes
    # comparison would spuriously fail on newline style alone.
    written = (project / "docs" / "vault" / "templates" / "feature-blueprint.md").read_text(encoding="utf-8")
    expected = (resources.files("templates") / "feature-blueprint.md").read_text(encoding="utf-8")

    assert written == expected


def test_agents_harness_template_resolves_via_importlib_resources() -> None:
    rendered = install_cli.render_agents_block(install_cli.HARNESS_ROOT)
    raw_template = (resources.files("templates") / "AGENTS.harness.md").read_text(encoding="utf-8")

    # The rendered block is the raw template with the {HARNESS_PATH},
    # {QUERY_CMD}, and {REFRESH_CMD} placeholders substituted -- proving
    # render_agents_block actually reads the packaged resource rather than
    # some other copy.
    expected = (
        raw_template.replace("{HARNESS_PATH}", str(install_cli.HARNESS_ROOT))
        .replace("{QUERY_CMD}", install_cli._query_command())
        .replace("{REFRESH_CMD}", install_cli._refresh_command())
    )
    assert expected == rendered


# --- Regression coverage for the render_agents_block / vault-README command
# generation (was: unconditionally emitting `python <HARNESS_PATH>/harness/
# graph_query.py ...`, which is unusable via the primary pipx install path --
# bare `python` on PATH resolves outside the pipx venv, and even with the
# right interpreter, running the .py file directly doesn't put the repo root
# on sys.path so `from harness...` imports fail). ---


def test_query_command_uses_console_script_when_resolvable_in_same_environment(monkeypatch) -> None:
    interpreter_dir = Path(sys.executable).resolve().parent
    script_name = "agent-harness-query.exe" if sys.platform == "win32" else "agent-harness-query"
    monkeypatch.setattr(shutil, "which", lambda name: str(interpreter_dir / script_name))

    assert install_cli._query_command() == "agent-harness-query"
    assert install_cli._refresh_command() == "agent-harness-refresh"


def test_query_command_falls_back_to_module_invocation_when_console_script_unresolvable(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert install_cli._query_command() == f"{sys.executable} -m harness.graph_query"
    assert install_cli._refresh_command() == f"{sys.executable} -m harness.graph_refresh"


def test_query_command_ignores_same_named_script_from_a_different_environment(monkeypatch) -> None:
    """A same-named script resolvable on PATH but living outside this
    interpreter's own Scripts/bin directory (e.g. a stale or unrelated
    pipx-managed install) must not be trusted -- fall back to module
    invocation instead of assuming it's usable."""
    other_dir = Path(sys.executable).resolve().parent.parent / "some-other-env" / "Scripts"
    script_name = "agent-harness-query.exe" if sys.platform == "win32" else "agent-harness-query"
    monkeypatch.setattr(shutil, "which", lambda name: str(other_dir / script_name))

    assert install_cli._query_command() == f"{sys.executable} -m harness.graph_query"


def test_render_agents_block_and_vault_readme_agree_and_avoid_broken_raw_script_form(monkeypatch) -> None:
    """Both generated surfaces (AGENTS.md and the vault README) must agree on
    which form of the graph-query/refresh commands they tell the agent to
    run, and neither may fall back to the broken `python <path>/harness/
    graph_query.py` form this regression covers (note: the vault README's
    prose still mentions `graph_query.py` by name in passing, so this
    specifically checks for the old broken *command* form, not any mention
    of the filename)."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    agents_block = install_cli.render_agents_block(install_cli.HARNESS_ROOT)
    vault_readme = install_cli.render_vault_readme(install_cli.HARNESS_ROOT)

    expected_query = f"{sys.executable} -m harness.graph_query"
    expected_refresh = f"{sys.executable} -m harness.graph_refresh"
    old_broken_query = f"python {install_cli.HARNESS_ROOT}/harness/graph_query.py"
    old_broken_refresh = f"python {install_cli.HARNESS_ROOT}/harness/graph_refresh.py"
    for text in (agents_block, vault_readme):
        assert expected_query in text
        assert expected_refresh in text
        assert old_broken_query not in text
        assert old_broken_refresh not in text
