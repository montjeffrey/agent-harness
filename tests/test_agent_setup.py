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


# --- one registration, every client ---------------------------------------
#
# The bug these guard against is not a crash either: each client's config
# looks correct on its own, and only the comparison between them shows that
# Claude Code was pointed at site-packages while Codex was pointed at a
# checkout. Whichever one an agent happened to be talking to decided which
# tools existed.


def test_every_client_registers_the_identical_server_entry(tmp_path: Path) -> None:
    from harness import bootstrap_register

    root = tmp_path / "checkout"
    (root / "hooks" / "antigravity").mkdir(parents=True)
    entry = agent_setup.mcp_registration(root, sys.executable)

    claude = bootstrap_register.render_mcp_entry(root.resolve(), sys.executable)
    antigravity = json.loads(
        agent_setup.render_antigravity_plugin(root, sys.executable)["mcp_config.json"]
    )["mcpServers"]["agent-harness"]
    codex = agent_setup._codex_command(root, sys.executable)

    assert claude == entry
    assert antigravity == entry
    # Codex takes the same thing through its CLI's flag surface.
    assert codex[:4] == ["codex", "mcp", "add", "agent-harness"]
    assert codex[-(len(entry["args"]) + 1):] == [entry["command"], *entry["args"]]
    for key, value in entry["env"].items():
        assert f"{key}={value}" in codex


def test_codex_and_claude_agree_on_the_pythonpath_string(tmp_path: Path) -> None:
    """Same root, same spelling -- not one checkout and one site-packages."""
    root = tmp_path / "a checkout with spaces"
    root.mkdir()
    entry = agent_setup.mcp_registration(root, sys.executable)
    codex = agent_setup._codex_command(root, sys.executable)

    env_flags = [codex[i + 1] for i, part in enumerate(codex) if part == "--env"]

    assert env_flags == [f"PYTHONPATH={entry['env']['PYTHONPATH']}"]
    assert str(root.resolve()).replace("\\", "/") == entry["env"]["PYTHONPATH"]


def test_a_relative_harness_root_resolves_once_for_all_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--harness-root .` must not mean different things to different clients."""
    monkeypatch.chdir(tmp_path)
    seen: dict[str, Path] = {}

    def _record(name):
        def _installer(harness_root, python_exe, *, dry_run, **kwargs):
            seen[name] = Path(harness_root)
            return [f"{name}: ok"]

        return _installer

    for name in ("claude", "codex", "antigravity"):
        monkeypatch.setattr(agent_setup, f"install_{name}", _record(name))

    assert agent_setup.main(["--agents", "claude,codex,antigravity", "--harness-root", ".", "--dry-run"]) == 0
    assert set(seen) == {"claude", "codex", "antigravity"}
    assert len(set(seen.values())) == 1
    assert next(iter(seen.values())) == tmp_path.resolve()


class _FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def test_codex_dry_run_shows_the_registration_without_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def _fake_run(command, **kwargs):
        calls.append(list(command))
        return _FakeCompleted(1)  # `codex mcp get` -> not registered

    monkeypatch.setattr(agent_setup.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run)

    actions = agent_setup.install_codex(tmp_path, sys.executable, dry_run=True)

    assert [c[:3] for c in calls] == [["codex", "mcp", "get"]]  # nothing was added
    assert len(actions) == 1 and "would run" in actions[0]
    entry = agent_setup.mcp_registration(tmp_path, sys.executable)
    assert entry["env"]["PYTHONPATH"] in actions[0]


def test_codex_leaves_an_existing_registration_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup will not replace configuration it cannot prove it owns."""
    calls: list[list[str]] = []

    def _fake_run(command, **kwargs):
        calls.append(list(command))
        return _FakeCompleted(0)  # already registered

    monkeypatch.setattr(agent_setup.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run)

    actions = agent_setup.install_codex(tmp_path, sys.executable, dry_run=False)

    assert [c[:3] for c in calls] == [["codex", "mcp", "get"]]
    assert actions == ["codex/mcp/agent-harness: already registered (left unchanged)"]


def test_codex_registers_the_shared_entry_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def _fake_run(command, **kwargs):
        calls.append(list(command))
        return _FakeCompleted(1 if command[:3] == ["codex", "mcp", "get"] else 0)

    monkeypatch.setattr(agent_setup.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run)

    actions = agent_setup.install_codex(tmp_path, sys.executable, dry_run=False)

    assert calls[-1] == agent_setup._codex_command(tmp_path, sys.executable)
    assert actions == ["codex/mcp/agent-harness: registered"]


def test_main_announces_the_root_every_client_is_pointed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The root is the one fact that decides which harness copy answers; say it."""
    for name in ("claude", "codex", "antigravity"):
        monkeypatch.setattr(agent_setup, f"install_{name}", lambda *a, **k: [])

    assert agent_setup.main(["--harness-root", str(tmp_path), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert f"registering harness root {tmp_path.resolve()}" in out
    assert "claude, codex, antigravity" in out


def _fake_codex(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Put a Codex CLI on PATH that has nothing registered; record its calls."""
    calls: list[list[str]] = []

    def _fake_run(command, **kwargs):
        calls.append(list(command))
        return _FakeCompleted(1 if command[:3] == ["codex", "mcp", "get"] else 0)

    monkeypatch.setattr(agent_setup.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run)
    return calls


def test_main_dry_run_writes_nothing_for_any_client(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    calls = _fake_codex(monkeypatch)

    assert agent_setup.main(["--harness-root", str(checkout), "--dry-run"]) == 0

    assert list(isolated_home.rglob("*")) == []
    assert all(c[:3] == ["codex", "mcp", "get"] for c in calls)


def test_one_setup_run_lands_the_same_root_in_every_client_config(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read back what each client was actually given, not what was rendered.

    This is the end state the whole adapter layer exists for: after one run,
    `~/.claude.json`, the Antigravity plugin and the Codex CLI all name the
    same interpreter and the same `PYTHONPATH`. The machine this was written
    on had Claude Code pointed at site-packages and Codex at a checkout.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    calls = _fake_codex(monkeypatch)

    assert agent_setup.main(["--harness-root", str(checkout), "--python", sys.executable]) == 0

    expected = agent_setup.mcp_registration(checkout, sys.executable)
    claude = json.loads((isolated_home / ".claude.json").read_text(encoding="utf-8"))
    antigravity = json.loads(
        (agent_setup.antigravity_plugin_dir(isolated_home) / "mcp_config.json").read_text(
            encoding="utf-8"
        )
    )
    codex_add = next(c for c in calls if c[:3] == ["codex", "mcp", "add"])

    assert claude["mcpServers"]["agent-harness"] == expected
    assert antigravity["mcpServers"]["agent-harness"] == expected
    assert codex_add == agent_setup._codex_command(checkout, sys.executable)
    assert f"PYTHONPATH={expected['env']['PYTHONPATH']}" in codex_add
    assert expected["env"]["PYTHONPATH"] == str(checkout.resolve()).replace("\\", "/")
