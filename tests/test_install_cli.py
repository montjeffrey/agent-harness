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
from pathlib import Path

import install
from harness import install_cli


def test_run_install_writes_expected_files_via_install_cli_directly(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    summary = install_cli.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)

    assert (project / "docs" / "vault" / "blueprints" / ".gitkeep").exists()
    assert (project / "docs" / "vault" / "templates" / "feature-blueprint.md").exists()
    assert (project / "docs" / "vault" / "README.md").exists()
    assert (project / ".gitignore").read_text(encoding="utf-8").strip() == "graphify-out/"

    agents_md = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert install_cli.MARKER_START in agents_md
    assert install_cli.MARKER_END in agents_md
    assert "graph_query.py" in agents_md

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
    "GITIGNORE_ENTRY",
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
    "ensure_gitignore_entry",
    "install_hooks",
    "install_vault_files",
    "main",
    "parse_args",
    "render_agents_block",
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

    # The rendered block is the raw template with the {HARNESS_PATH}
    # placeholder substituted -- proving render_agents_block actually reads
    # the packaged resource rather than some other copy.
    assert raw_template.replace("{HARNESS_PATH}", str(install_cli.HARNESS_ROOT)) == rendered
