"""Tests for portable per-agent adapter registration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness import agent_setup


def test_rendered_antigravity_plugin_is_complete_and_uses_stdio_mcp(tmp_path: Path) -> None:
    files = agent_setup.render_antigravity_plugin(tmp_path, sys.executable)

    manifest = json.loads(files["plugin.json"])
    mcp = json.loads(files["mcp_config.json"])
    hooks = json.loads(files["hooks.json"])

    assert manifest["name"] == "agent-harness"
    assert mcp["mcpServers"]["agent-harness"]["args"] == ["-m", "harness.mcp_server"]
    assert "PYTHONPATH" in mcp["mcpServers"]["agent-harness"]["env"]
    pre_invocation = hooks["agent-harness-memory"]["PreInvocation"]
    assert pre_invocation[0]["type"] == "command"
    assert "memory_context_hook.py" in pre_invocation[0]["command"]


def test_antigravity_install_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    harness = tmp_path / "harness"
    (harness / "hooks" / "antigravity").mkdir(parents=True)
    (harness / "hooks" / "antigravity" / "memory_context_hook.py").write_text("pass\n")

    first = agent_setup.install_antigravity(harness, sys.executable, dry_run=False, home=home)
    second = agent_setup.install_antigravity(harness, sys.executable, dry_run=False, home=home)

    assert any("written" in action for action in first)
    assert all("already current" in action for action in second)


def test_antigravity_install_refuses_a_foreign_plugin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugin = agent_setup.antigravity_plugin_dir(home)
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"name":"someone-else"}\n')

    with pytest.raises(agent_setup.SetupError, match="different Antigravity plugin"):
        agent_setup.install_antigravity(tmp_path, sys.executable, dry_run=False, home=home)


def test_antigravity_hook_injects_only_on_the_first_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks.antigravity import memory_context_hook as hook

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(hook.memory_context_hook, "emit", lambda root: f"context for {root}")

    later = hook.render({"invocationNum": 1, "workspacePaths": [str(project)]})
    first = hook.render({"invocationNum": 0, "workspacePaths": [str(project)]})

    assert later == {}
    assert first["injectSteps"][0]["ephemeralMessage"].startswith("context for")


def test_antigravity_hook_is_silent_without_a_workspace() -> None:
    from hooks.antigravity import memory_context_hook as hook

    assert hook.render({"invocationNum": 0, "workspacePaths": []}) == {}


def test_codex_install_skips_cleanly_when_the_cli_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_setup.shutil, "which", lambda _: None)

    actions = agent_setup.install_codex(tmp_path, sys.executable, dry_run=False)

    assert actions == [
        "codex/mcp: skipped (Codex CLI is not on PATH; rerun "
        "`agent-harness-setup --agents codex` after installing it)"
    ]


def test_main_rejects_unknown_agent() -> None:
    assert agent_setup.main(["--agents", "claude,unknown"]) == 2


# --- user-scope memory rules ---------------------------------------------


def test_memory_rules_install_into_claude_user_scope(tmp_path: Path) -> None:
    """Rules are what make capture happen; the hooks only ever read."""
    actions = agent_setup.install_memory_rules(home=tmp_path, dry_run=False)

    target = agent_setup.claude_rules_path(tmp_path)
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "memory_remember" in text
    assert "memory_context" in text
    assert "proposed" in text
    assert any("memory protocol" in a for a in actions)


def test_memory_rules_are_idempotent(tmp_path: Path) -> None:
    agent_setup.install_memory_rules(home=tmp_path, dry_run=False)
    first = agent_setup.claude_rules_path(tmp_path).read_text(encoding="utf-8")
    actions = agent_setup.install_memory_rules(home=tmp_path, dry_run=False)
    assert agent_setup.claude_rules_path(tmp_path).read_text(encoding="utf-8") == first
    assert any("unchanged" in a for a in actions)


def test_memory_rules_preserve_surrounding_user_content(tmp_path: Path) -> None:
    """This file is the user's own; only the marked block is ours to own."""
    target = agent_setup.claude_rules_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        """# My rules

Always use tabs.
""",
        encoding="utf-8",
    )

    agent_setup.install_memory_rules(home=tmp_path, dry_run=False)

    text = target.read_text(encoding="utf-8")
    assert "Always use tabs." in text
    assert "memory_remember" in text


def test_memory_rules_dry_run_writes_nothing(tmp_path: Path) -> None:
    actions = agent_setup.install_memory_rules(home=tmp_path, dry_run=True)
    assert not agent_setup.claude_rules_path(tmp_path).exists()
    assert any("would install" in a for a in actions)


def test_the_memory_rules_carry_no_project_specific_paths() -> None:
    """User scope applies to every repository, including ones with no graph.

    The per-project block's graph and blueprint instructions would be wrong in
    most of them, so this template must not inherit any of it.
    """
    import importlib.resources as resources

    text = (resources.files("templates") / "MEMORY.protocol.md").read_text(
        encoding="utf-8"
    )
    for token in ("graphify-out", "docs/vault", "{QUERY_CMD}", "{REFRESH_CMD}"):
        assert token not in text


# --- auto-enrollment ------------------------------------------------------


def test_workspace_sets_both_variables_the_hook_requires() -> None:
    """Setting one alone looks enabled and does nothing."""
    merged, action = agent_setup.merge_workspace_env({}, [Path("/code")])

    assert merged["env"][agent_setup.AUTO_ENROLL_ENV] == "1"
    assert str(Path("/code")) in merged["env"][agent_setup.WORKSPACE_ENV]
    assert action == "updated"


def test_workspace_preserves_unrelated_env_and_settings() -> None:
    settings = {"hooks": {"SessionStart": []}, "env": {"EDITOR": "vim"}}
    merged, _ = agent_setup.merge_workspace_env(settings, [Path("/code")])

    assert merged["env"]["EDITOR"] == "vim"
    assert merged["hooks"] == {"SessionStart": []}


def test_workspace_reports_unchanged_on_a_second_run() -> None:
    merged, _ = agent_setup.merge_workspace_env({}, [Path("/code")])
    _again, action = agent_setup.merge_workspace_env(merged, [Path("/code")])
    assert action == "unchanged"


def test_workspace_refuses_a_malformed_env_rather_than_overwriting_it() -> None:
    """settings.json also holds hooks and permissions."""
    with pytest.raises(agent_setup.SetupError):
        agent_setup.merge_workspace_env({"env": "nope"}, [Path("/code")])


def test_multiple_workspaces_are_pathsep_joined() -> None:
    import os

    merged, _ = agent_setup.merge_workspace_env({}, [Path("/a"), Path("/b")])
    assert os.pathsep in merged["env"][agent_setup.WORKSPACE_ENV]
