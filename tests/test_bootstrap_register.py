"""Tests for user-scope hook and MCP registration.

The bug these guard against: the registration step used to be inline
PowerShell that only ran on PowerShell 6+, so on stock Windows it aborted
the bootstrap after the install had already succeeded and left a machine
with working commands and no hooks. Nothing tested it because nothing
could -- it was shell. Now it is importable.

`isolated_home` (autouse, in conftest) points `Path.home()` at a tmp dir,
so every write here lands in the sandbox and not in the real `~/.claude`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import bootstrap_register as br

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def harness_root() -> Path:
    """The real checkout -- the hook scripts have to exist to be registered."""
    return REPO_ROOT


def _commands(settings: dict, event: str) -> list[str]:
    return [
        hook["command"]
        for entry in settings["hooks"].get(event, [])
        for hook in entry.get("hooks", [])
    ]


class TestMergeHooks:
    def test_registers_both_hooks_into_empty_settings(self, harness_root: Path) -> None:
        merged, actions = br.merge_hooks({}, harness_root, "/py")

        assert _commands(merged, "SessionStart") == [
            f"/py {harness_root.as_posix()}/hooks/claude/memory_context_hook.py"
        ]
        assert _commands(merged, "PreToolUse") == [
            f"/py {harness_root.as_posix()}/hooks/claude/graph_read_guard.py"
        ]
        assert all(action.endswith("registered") for action in actions)

    def test_pretooluse_carries_the_read_matcher(self, harness_root: Path) -> None:
        merged, _ = br.merge_hooks({}, harness_root, "/py")

        assert merged["hooks"]["PreToolUse"][0]["matcher"] == "Read"
        # SessionStart fires unconditionally, so it must not carry one.
        assert "matcher" not in merged["hooks"]["SessionStart"][0]

    def test_rerun_is_idempotent(self, harness_root: Path) -> None:
        once, _ = br.merge_hooks({}, harness_root, "/py")
        twice, actions = br.merge_hooks(once, harness_root, "/py")

        assert twice == once
        assert all(action.endswith("already current") for action in actions)

    def test_reinstall_rewrites_a_stale_interpreter_in_place(self, harness_root: Path) -> None:
        """A reinstall moves the venv; the old path must not survive or duplicate."""
        old, _ = br.merge_hooks({}, harness_root, "/old/py")
        new, actions = br.merge_hooks(old, harness_root, "/new/py")

        assert len(new["hooks"]["SessionStart"]) == 1
        assert _commands(new, "SessionStart")[0].startswith("/new/py ")
        assert any(action.endswith("updated") for action in actions)

    def test_preserves_unrelated_settings_and_foreign_hooks(self, harness_root: Path) -> None:
        existing = {
            "theme": "dark",
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other.py"}]}]},
        }
        merged, _ = br.merge_hooks(existing, harness_root, "/py")

        assert merged["theme"] == "dark"
        assert "other.py" in _commands(merged, "SessionStart")
        assert len(merged["hooks"]["SessionStart"]) == 2

    def test_does_not_mutate_the_input(self, harness_root: Path) -> None:
        existing: dict = {"hooks": {}}
        br.merge_hooks(existing, harness_root, "/py")

        assert existing == {"hooks": {}}

    def test_skips_a_spec_whose_script_is_missing(self, tmp_path: Path) -> None:
        merged, actions = br.merge_hooks({}, tmp_path, "/py")

        assert merged["hooks"] == {}
        assert all("skipped" in action for action in actions)

    def test_refuses_a_non_object_hooks_key(self, harness_root: Path) -> None:
        with pytest.raises(br.RegistrationError, match="expected an object"):
            br.merge_hooks({"hooks": []}, harness_root, "/py")

    def test_refuses_a_non_array_hook_event(self, harness_root: Path) -> None:
        with pytest.raises(br.RegistrationError, match="expected an array"):
            br.merge_hooks({"hooks": {"SessionStart": "not an array"}}, harness_root, "/py")

    def test_quotes_a_path_containing_spaces(self, tmp_path: Path) -> None:
        spaced = tmp_path / "Mi Carpeta"
        (spaced / "hooks" / "claude").mkdir(parents=True)
        for name in ("memory_context_hook.py", "graph_read_guard.py"):
            (spaced / "hooks" / "claude" / name).touch()

        merged, _ = br.merge_hooks({}, spaced, "/py")

        command = _commands(merged, "SessionStart")[0]
        assert '"' in command, f"unquoted spaced path would split into extra argv: {command}"


class TestMergeMcp:
    def test_registers_the_server(self, harness_root: Path) -> None:
        merged, actions = br.merge_mcp({}, harness_root, "/py")

        entry = merged["mcpServers"][br.MCP_SERVER_NAME]
        assert entry["command"] == "/py"
        assert entry["args"] == ["-m", "harness.mcp_server"]
        assert entry["env"]["PYTHONPATH"] == harness_root.as_posix()
        assert actions == [f"mcp/{br.MCP_SERVER_NAME}: registered"]

    def test_rerun_is_idempotent(self, harness_root: Path) -> None:
        once, _ = br.merge_mcp({}, harness_root, "/py")
        twice, actions = br.merge_mcp(once, harness_root, "/py")

        assert twice == once
        assert actions == [f"mcp/{br.MCP_SERVER_NAME}: already current"]

    def test_updates_our_own_stale_entry(self, harness_root: Path) -> None:
        old, _ = br.merge_mcp({}, harness_root, "/old/py")
        new, actions = br.merge_mcp(old, harness_root, "/new/py")

        assert new["mcpServers"][br.MCP_SERVER_NAME]["command"] == "/new/py"
        assert actions == [f"mcp/{br.MCP_SERVER_NAME}: updated"]

    def test_refuses_to_overwrite_a_foreign_server_of_the_same_name(
        self, harness_root: Path
    ) -> None:
        existing = {"mcpServers": {br.MCP_SERVER_NAME: {"command": "somebody-else"}}}

        with pytest.raises(br.RegistrationError, match="refusing to overwrite"):
            br.merge_mcp(existing, harness_root, "/py")

    def test_preserves_live_session_state_alongside_the_server(self, harness_root: Path) -> None:
        """`~/.claude.json` is Claude Code's own state file, not ours alone."""
        existing = {"numStartups": 42, "mcpServers": {"other": {"command": "x"}}}
        merged, _ = br.merge_mcp(existing, harness_root, "/py")

        assert merged["numStartups"] == 42
        assert merged["mcpServers"]["other"] == {"command": "x"}

    def test_refuses_a_non_object_mcpservers_key(self, harness_root: Path) -> None:
        with pytest.raises(br.RegistrationError, match="expected an object"):
            br.merge_mcp({"mcpServers": []}, harness_root, "/py")


class TestMain:
    def test_writes_both_user_config_files(self, harness_root: Path) -> None:
        assert br.main(["--harness-root", str(harness_root), "--python", "/py"]) == 0

        settings = json.loads(br.user_settings_path().read_text(encoding="utf-8"))
        mcp = json.loads(br.user_mcp_config_path().read_text(encoding="utf-8"))
        assert "SessionStart" in settings["hooks"]
        assert br.MCP_SERVER_NAME in mcp["mcpServers"]

    def test_skip_mcp_leaves_the_mcp_config_alone(self, harness_root: Path) -> None:
        assert (
            br.main(["--harness-root", str(harness_root), "--python", "/py", "--skip-mcp"]) == 0
        )

        assert br.user_settings_path().exists()
        assert not br.user_mcp_config_path().exists()

    def test_dry_run_writes_nothing(self, harness_root: Path) -> None:
        assert (
            br.main(["--harness-root", str(harness_root), "--python", "/py", "--dry-run"]) == 0
        )

        assert not br.user_settings_path().exists()

    def test_invalid_existing_settings_fails_without_writing(self, harness_root: Path) -> None:
        settings_path = br.user_settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("{ not json", encoding="utf-8")

        assert br.main(["--harness-root", str(harness_root), "--python", "/py"]) == 1
        assert settings_path.read_text(encoding="utf-8") == "{ not json"

    def test_a_bad_mcp_config_does_not_half_apply_the_hooks(self, harness_root: Path) -> None:
        """Both merges are computed before either is written."""
        mcp_path = br.user_mcp_config_path()
        mcp_path.write_text(json.dumps({"mcpServers": {br.MCP_SERVER_NAME: {"command": "x"}}}))

        assert br.main(["--harness-root", str(harness_root), "--python", "/py"]) == 1
        assert not br.user_settings_path().exists()
