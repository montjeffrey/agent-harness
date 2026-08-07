"""Tests for install.py: idempotency, marker-block handling, vault file copy."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

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


def _snapshot(project: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_install_refuses_on_invalid_existing_settings_json_and_leaves_project_untouched(
    tmp_path: Path,
) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")

    before = _snapshot(project)
    with pytest.raises(install.InstallError):
        install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=True)
    after = _snapshot(project)

    assert before == after
    assert not (project / "AGENTS.md").exists()
    assert not (project / "docs").exists()


def test_simulated_failure_mid_install_rolls_back_fully(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    (project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("# Pre-existing human content\n", encoding="utf-8")

    before = _snapshot(project)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure mid-install")

    monkeypatch.setattr(install, "install_hooks", _boom)

    with pytest.raises(RuntimeError, match="simulated failure mid-install"):
        install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=True)

    after = _snapshot(project)
    assert before == after
    # Nothing partially applied from the steps that ran before the failure.
    assert not (project / "docs" / "vault" / "README.md").exists()


def test_install_into_path_with_spaces_and_unicode(tmp_path: Path) -> None:
    project = tmp_path / "my project (dir) - ünïcødé 中文"
    summary = install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)

    assert (project / "AGENTS.md").exists()
    assert (project / "docs" / "vault" / "README.md").exists()
    assert any("created" in line for line in summary)

    # Re-run is still idempotent against the unicode/space path.
    summary_second = install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False)
    assert all("unchanged" in line for line in summary_second)


def test_install_hooks_records_current_interpreter(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    install.install_hooks(project, install.HARNESS_ROOT)

    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre_tool_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    session_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    expected = str(Path(sys.executable)).replace("\\", "/")
    assert expected in pre_tool_cmd
    assert expected in session_cmd
    assert not pre_tool_cmd.startswith("python ")
    assert not session_cmd.startswith("python ")


def test_shell_quote_wraps_paths_with_spaces_and_leaves_simple_paths_bare() -> None:
    simple = install._shell_quote("/usr/bin/python3")
    assert simple == "/usr/bin/python3" or simple.strip("\"'") == "/usr/bin/python3"

    spaced = install._shell_quote("C:/Program Files/Python 3.14/python.exe")
    assert "C:/Program Files/Python 3.14/python.exe" in spaced
    assert spaced != "C:/Program Files/Python 3.14/python.exe"


def test_marker_block_upsert_preserves_surrounding_bytes_exactly(tmp_path: Path) -> None:
    agents_md = tmp_path / "AGENTS.md"
    original_bytes = "# Title\r\n\r\nHuman written body.\r\n".encode("utf-8")
    agents_md.write_bytes(original_bytes)

    block = f"{install.MARKER_START}\nharness content\n{install.MARKER_END}"
    install.upsert_marker_block(agents_md, block)

    updated = agents_md.read_bytes()
    assert b"# Title\r\n\r\nHuman written body.\r\n" in updated
    assert b"harness content" in updated


def test_install_hooks_harness_path_survives_spaces_and_quotes_as_valid_json(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    weird_harness = tmp_path / 'harness dir with spaces and "quotes" and ünïcøde'

    install.install_hooks(project, weird_harness)

    text = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
    settings = json.loads(text)  # must not raise -- proves the substitution stayed JSON-safe
    session_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "graph_check.py" in session_cmd


def test_install_hooks_wraps_invalid_fragment_json_as_install_error(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    bad_fragment = tmp_path / "bad-fragment.json"
    bad_fragment.write_text("{ this is not valid json <python> <harness>", encoding="utf-8")
    monkeypatch.setattr(install, "HOOK_FRAGMENT_PATH", bad_fragment)

    with pytest.raises(install.InstallError):
        install.install_hooks(project, install.HARNESS_ROOT)


def test_upsert_marker_block_appends_matching_crlf_style_to_fresh_crlf_file(tmp_path: Path) -> None:
    agents_md = tmp_path / "AGENTS.md"
    original = b"# Title\r\n\r\nExisting body.\r\n"
    agents_md.write_bytes(original)

    block = f"{install.MARKER_START}\nnew content\n{install.MARKER_END}"
    install.upsert_marker_block(agents_md, block)

    data = agents_md.read_bytes()
    assert data.startswith(original)
    appended = data[len(original):]
    assert install.MARKER_START.encode("utf-8") in appended
    # No lone LF anywhere in the newly appended chunk -- CRLF throughout.
    assert b"\n" not in appended.replace(b"\r\n", b"")


def test_mcp_flag_creates_mcp_json(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()

    action = install.install_mcp_registration(project, install.HARNESS_ROOT)
    assert action == "created"

    mcp_json_path = project / ".mcp.json"
    config = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    server = config["mcpServers"]["agent-harness"]
    assert server["args"] == ["-m", "harness.mcp_server"]
    assert "enableAllProjectMcpServers" not in config


def test_mcp_merge_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()

    first_action = install.install_mcp_registration(project, install.HARNESS_ROOT)
    assert first_action == "created"

    mcp_json_path = project / ".mcp.json"
    before = mcp_json_path.read_bytes()

    second_action = install.install_mcp_registration(project, install.HARNESS_ROOT)
    assert second_action == "unchanged"

    after = mcp_json_path.read_bytes()
    assert before == after


def test_mcp_preserves_existing_servers(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    mcp_json_path = project / ".mcp.json"
    mcp_json_path.write_text(
        json.dumps({"mcpServers": {"some-other-server": {"command": "node", "args": ["server.js"]}}}),
        encoding="utf-8",
    )

    action = install.install_mcp_registration(project, install.HARNESS_ROOT)
    assert action == "updated"

    config = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    assert "some-other-server" in config["mcpServers"]
    assert "agent-harness" in config["mcpServers"]


def test_mcp_refuses_invalid_mcp_json(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    mcp_json_path = project / ".mcp.json"
    mcp_json_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(install.InstallError):
        install.install_mcp_registration(project, install.HARNESS_ROOT)

    # refusal must not have touched the file
    assert mcp_json_path.read_text(encoding="utf-8") == "{not json"


def test_mcp_refuses_non_object_mcpservers_value(tmp_path: Path) -> None:
    """A syntactically valid .mcp.json whose 'mcpServers' key is present but
    isn't a JSON object (e.g. null) must be refused cleanly -- previously
    `dict(existing.get("mcpServers", {}))` raised an uncaught TypeError on
    `dict(None)` instead of the documented InstallError refusal."""
    project = tmp_path / "myproject"
    project.mkdir()
    mcp_json_path = project / ".mcp.json"
    mcp_json_path.write_text(json.dumps({"mcpServers": None}), encoding="utf-8")
    before = mcp_json_path.read_text(encoding="utf-8")

    with pytest.raises(install.InstallError):
        install.install_mcp_registration(project, install.HARNESS_ROOT)

    assert mcp_json_path.read_text(encoding="utf-8") == before


def test_mcp_refuses_different_existing_agent_harness_entry(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    mcp_json_path = project / ".mcp.json"
    mcp_json_path.write_text(
        json.dumps({"mcpServers": {"agent-harness": {"command": "some-other-command", "args": []}}}),
        encoding="utf-8",
    )
    before = mcp_json_path.read_text(encoding="utf-8")

    with pytest.raises(install.InstallError):
        install.install_mcp_registration(project, install.HARNESS_ROOT)

    assert mcp_json_path.read_text(encoding="utf-8") == before


def test_install_without_mcp_flag_writes_no_mcp_json(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False, install_mcp_flag=False)
    assert not (project / ".mcp.json").exists()


def test_install_with_mcp_flag_writes_mcp_json(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    summary = install.run_install(
        project, ["claude", "codex", "gemini"], install_hooks_flag=False, install_mcp_flag=True
    )
    assert (project / ".mcp.json").exists()
    assert any(".mcp.json" in line for line in summary)


def test_mcp_write_rolls_back_on_failure(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Pre-existing human content\n", encoding="utf-8")

    before = _snapshot(project)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure during mcp registration")

    monkeypatch.setattr(install, "install_mcp_registration", _boom)

    with pytest.raises(RuntimeError, match="simulated failure during mcp registration"):
        install.run_install(project, ["claude", "codex", "gemini"], install_hooks_flag=False, install_mcp_flag=True)

    after = _snapshot(project)
    assert before == after
    assert not (project / ".mcp.json").exists()


def test_install_into_path_with_spaces_and_unicode_with_hooks_flag(tmp_path: Path) -> None:
    project = tmp_path / "my project (dir) - ünïcødé 中文"
    install.run_install(project, ["claude"], install_hooks_flag=True)

    settings_path = project / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    pre_tool_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "graph_read_guard.py" in pre_tool_cmd

    if os.name != "nt":
        parts = shlex.split(pre_tool_cmd)
        assert parts[0] == sys.executable
