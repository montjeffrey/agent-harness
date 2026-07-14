"""Tests for install.py: idempotency, marker-block handling, vault file copy."""

from __future__ import annotations

import json
from pathlib import Path

import install


def test_first_install_writes_expected_files(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    summary = install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)

    assert (project / "docs" / "vault" / "blueprints" / ".gitkeep").exists()
    assert (project / "docs" / "vault" / "templates" / "feature-blueprint.md").exists()
    assert (project / "docs" / "vault" / "README.md").exists()
    assert (project / ".gitignore").read_text(encoding="utf-8").strip() == "graphify-out/"

    agents_md = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert install.MARKER_START in agents_md
    assert install.MARKER_END in agents_md
    assert "graph_query.py" in agents_md

    claude_md = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert install.CLAUDE_POINTER in claude_md

    gemini_md = (project / "GEMINI.md").read_text(encoding="utf-8")
    assert install.GEMINI_POINTER in gemini_md

    assert any("created" in line for line in summary)


def test_second_install_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)

    files_before = {
        p: p.read_text(encoding="utf-8")
        for p in project.rglob("*")
        if p.is_file()
    }

    summary_second = install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)

    files_after = {
        p: p.read_text(encoding="utf-8")
        for p in project.rglob("*")
        if p.is_file()
    }

    assert files_before == files_after
    assert all("unchanged" in line for line in summary_second)


def test_upsert_marker_block_preserves_surrounding_content(tmp_path: Path) -> None:
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# My Project\n\nSome existing human-written content.\n", encoding="utf-8")

    block = f"{install.MARKER_START}\nharness content v1\n{install.MARKER_END}"
    install.upsert_marker_block(agents_md, block)
    first = agents_md.read_text(encoding="utf-8")
    assert "Some existing human-written content." in first
    assert "harness content v1" in first

    updated_block = f"{install.MARKER_START}\nharness content v2\n{install.MARKER_END}"
    action = install.upsert_marker_block(agents_md, updated_block)
    second = agents_md.read_text(encoding="utf-8")

    assert action == "updated"
    assert "Some existing human-written content." in second
    assert "harness content v1" not in second
    assert "harness content v2" in second


def test_gitignore_append_once(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    gitignore = project / ".gitignore"
    gitignore.write_text("node_modules/\n.env\n", encoding="utf-8")

    first_action = install.ensure_gitignore_entry(project)
    assert first_action == "updated"
    content_after_first = gitignore.read_text(encoding="utf-8")
    assert content_after_first.count("graphify-out/") == 1

    second_action = install.ensure_gitignore_entry(project)
    assert second_action == "unchanged"
    content_after_second = gitignore.read_text(encoding="utf-8")
    assert content_after_second == content_after_first


def test_hooks_merge_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()

    first_action = install.install_hooks(project, install.HARNESS_ROOT)
    assert first_action in ("created", "updated")

    settings_path = project / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    pre_tool_use = settings["hooks"]["PreToolUse"]
    assert len(pre_tool_use) == 1

    second_action = install.install_hooks(project, install.HARNESS_ROOT)
    assert second_action == "unchanged"

    settings_after = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings_after["hooks"]["PreToolUse"] == pre_tool_use
