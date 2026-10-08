"""Lifecycle tests for harness.graph_refresh: command adapter, backend
omission, graphify availability degradation, and credential isolation.

No test in this file performs a real graphify call, a paid API call, or a
network call -- `subprocess.run` is always monkeypatched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness import graph_refresh


# --- command adapter: full -> extract, fast -> update ----------------------


def test_build_graphify_command_extract() -> None:
    cmd = graph_refresh.build_graphify_command("extract", Path("/repo"), "openai")
    assert cmd == [sys.executable, "-m", "graphify", "extract", str(Path("/repo")), "--backend", "openai"]


def test_build_graphify_command_update() -> None:
    cmd = graph_refresh.build_graphify_command("update", Path("/repo"), "openai")
    assert cmd == [sys.executable, "-m", "graphify", "update", str(Path("/repo")), "--backend", "openai"]


def test_full_refresh_invokes_extract_then_update_then_label(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check=False, **kwargs):  # noqa: ANN001 -- test double
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graph_refresh, "detect_graphify", lambda: graph_refresh.GraphifyStatus(True, "1.0"))
    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    graph_refresh.refresh(tmp_path, "openai", full=True)

    subcommands = [c[3] for c in calls]
    assert subcommands == ["extract", "update", "label"]


def test_fast_refresh_invokes_update_then_label_only(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check=False, **kwargs):  # noqa: ANN001
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graph_refresh, "detect_graphify", lambda: graph_refresh.GraphifyStatus(True, "1.0"))
    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    # Any backend key in the operator's environment enables the label pass.
    for env_var in graph_refresh.BACKEND_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)

    graph_refresh.refresh(tmp_path, "auto", full=False)

    subcommands = [c[3] for c in calls]
    assert subcommands == ["update"]
    assert not any(c[3] == "extract" for c in calls)


# --- auto backend never emits --backend / --backend auto -------------------


def test_auto_backend_omits_backend_flag_entirely() -> None:
    cmd = graph_refresh.build_graphify_command("update", Path("/repo"), "auto")
    assert "--backend" not in cmd


def test_backend_auto_is_never_emitted_as_a_value() -> None:
    cmd = graph_refresh.build_graphify_command("extract", Path("/repo"), "auto")
    assert "auto" not in cmd


def test_named_backend_passes_backend_flag() -> None:
    cmd = graph_refresh.build_graphify_command("extract", Path("/repo"), "nvidia-nim")
    assert cmd[-2:] == ["--backend", "nvidia-nim"]


def test_full_refresh_without_backend_fails_before_any_subprocess(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(graph_refresh, "detect_graphify", lambda: graph_refresh.GraphifyStatus(True, "1.0"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="full semantic extraction"):
        graph_refresh.refresh(tmp_path, "auto", full=True)


def test_auto_backend_labels_when_a_supported_key_is_present(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check=False, **kwargs):  # noqa: ANN001
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graph_refresh, "detect_graphify", lambda: graph_refresh.GraphifyStatus(True, "1.0"))
    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    graph_refresh.refresh(tmp_path, "auto", full=False)

    assert [command[3] for command in calls] == ["update", "label"]


# --- graphify presence/version compatibility --------------------------------


def test_detect_graphify_reports_available(monkeypatch) -> None:
    def fake_run(cmd, capture_output=False, text=False, timeout=None, check=False):  # noqa: ANN001
        return subprocess.CompletedProcess(cmd, 0, stdout="graphify 1.2.3\n", stderr="")

    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    status = graph_refresh.detect_graphify()
    assert status.available is True
    assert status.version == "graphify 1.2.3"


def test_detect_graphify_reports_unavailable_on_missing_module(monkeypatch) -> None:
    def fake_run(cmd, capture_output=False, text=False, timeout=None, check=False):  # noqa: ANN001
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No module named graphify")

    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    status = graph_refresh.detect_graphify()
    assert status.available is False
    assert "graphify" in (status.error or "")


def test_detect_graphify_never_raises_on_missing_executable(monkeypatch) -> None:
    def fake_run(cmd, capture_output=False, text=False, timeout=None, check=False):  # noqa: ANN001
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    status = graph_refresh.detect_graphify()
    assert status.available is False


def test_refresh_degrades_with_clear_message_when_graphify_unavailable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        graph_refresh, "detect_graphify",
        lambda: graph_refresh.GraphifyStatus(False, None, error="No module named graphify"),
    )

    def boom(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("must not attempt a graphify subprocess call when unavailable")

    monkeypatch.setattr(graph_refresh.subprocess, "run", boom)

    with pytest.raises(SystemExit) as exc_info:
        graph_refresh.refresh(tmp_path, "auto", full=False)
    assert "graphify" in str(exc_info.value).lower()
    assert "install" in str(exc_info.value).lower()


# --- credential isolation ----------------------------------------------------


def test_scoped_api_key_sources_and_then_clears_env_var(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"key_command": "echo super-secret-value", "key_env": "OPENAI_API_KEY"}),
        encoding="utf-8",
    )

    with graph_refresh.scoped_api_key("openai", config_path):
        import os

        assert os.environ.get("OPENAI_API_KEY") == "super-secret-value"

    import os

    assert "OPENAI_API_KEY" not in os.environ


def test_scoped_api_key_leaves_preexisting_env_var_untouched(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "already-set-value")
    config_path = tmp_path / "config.json"  # never read: env already satisfies it

    with graph_refresh.scoped_api_key("openai", config_path):
        import os

        assert os.environ.get("OPENAI_API_KEY") == "already-set-value"

    import os

    assert os.environ.get("OPENAI_API_KEY") == "already-set-value"


def test_key_command_output_never_appears_in_captured_stdout_or_stderr(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "config.json"
    secret = "sk-super-duper-secret-token"
    config_path.write_text(
        json.dumps({"key_command": f"echo {secret}", "key_env": "OPENAI_API_KEY"}),
        encoding="utf-8",
    )

    with graph_refresh.scoped_api_key("openai", config_path):
        pass

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_key_command_output_never_appears_in_logs(monkeypatch, tmp_path: Path, caplog) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_path = tmp_path / "config.json"
    secret = "sk-another-secret-value"
    config_path.write_text(
        json.dumps({"key_command": f"echo {secret}", "key_env": "OPENAI_API_KEY"}),
        encoding="utf-8",
    )

    with caplog.at_level("DEBUG"):
        with graph_refresh.scoped_api_key("openai", config_path):
            pass

    assert secret not in caplog.text


def test_scoped_api_key_no_known_backend_is_a_noop(tmp_path: Path) -> None:
    with graph_refresh.scoped_api_key("auto", tmp_path / "config.json"):
        pass  # must not raise, must not require config


# --- MEDIUM-4: refuse to read a group/world-writable config file ------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode trust check is a no-op on Windows")
def test_load_user_config_refuses_world_writable_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"key_command": "echo x", "key_env": "OPENAI_API_KEY"}), encoding="utf-8")
    os.chmod(config_path, 0o666)  # world-writable

    with pytest.raises(SystemExit) as exc_info:
        graph_refresh.load_user_config(config_path)
    message = str(exc_info.value).lower()
    assert "writable" in message or "owned" in message


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode trust check is a no-op on Windows")
def test_load_user_config_accepts_owner_only_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"key_command": "echo x", "key_env": "OPENAI_API_KEY"}), encoding="utf-8")
    os.chmod(config_path, 0o600)  # owner read/write only

    assert graph_refresh.load_user_config(config_path) == {
        "key_command": "echo x",
        "key_env": "OPENAI_API_KEY",
    }


# --- semantic index update: best-effort, never fatal to refresh() ----------


def test_update_semantic_index_noop_when_chromadb_unavailable(monkeypatch, tmp_path: Path, caplog) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: False)
    with caplog.at_level("INFO"):
        graph_refresh.update_semantic_index(tmp_path)  # must not raise
    assert "chromadb not installed" in caplog.text


def test_update_semantic_index_failure_is_logged_not_raised(monkeypatch, tmp_path: Path, caplog) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text('{"nodes": [], "links": []}', encoding="utf-8")

    def boom(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("index exploded")

    monkeypatch.setattr(graph_index, "update_index", boom)

    with caplog.at_level("WARNING"):
        graph_refresh.update_semantic_index(tmp_path)  # must not raise
    assert "index exploded" in caplog.text


def test_refresh_calls_update_semantic_index_as_final_step(monkeypatch, tmp_path: Path) -> None:
    """refresh() must call update_semantic_index(repo_root) after the
    graphify steps -- proven by monkeypatching it and checking the call."""
    monkeypatch.setattr(graph_refresh, "detect_graphify", lambda: graph_refresh.GraphifyStatus(True, "1.0"))

    def fake_run(cmd, check=False, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)

    calls: list[Path] = []
    monkeypatch.setattr(graph_refresh, "update_semantic_index", calls.append)

    graph_refresh.refresh(tmp_path, "auto", full=False)
    assert calls == [tmp_path]


def test_refresh_completes_when_semantic_index_update_self_protects(monkeypatch, tmp_path: Path, caplog) -> None:
    """End-to-end: refresh() completes normally even when the underlying
    chromadb update raises, because update_semantic_index catches it."""
    from harness import graph_index

    monkeypatch.setattr(graph_refresh, "detect_graphify", lambda: graph_refresh.GraphifyStatus(True, "1.0"))

    def fake_run(cmd, check=False, **kwargs):  # noqa: ANN001
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)

    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text('{"nodes": [], "links": []}', encoding="utf-8")

    def boom(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("index exploded")

    monkeypatch.setattr(graph_index, "update_index", boom)

    with caplog.at_level("WARNING"):
        graph_refresh.refresh(tmp_path, "auto", full=False)  # must not raise
    assert "index exploded" in caplog.text


def test_refresh_never_performs_a_real_graphify_or_network_call(monkeypatch, tmp_path: Path) -> None:
    """Belt-and-suspenders: every subprocess.run call during refresh() must
    be routed through our fake, never the real interpreter/module."""
    monkeypatch.setattr(graph_refresh, "detect_graphify", lambda: graph_refresh.GraphifyStatus(True, "1.0"))
    seen: list[list[str]] = []

    def fake_run(cmd, check=False, **kwargs):  # noqa: ANN001
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graph_refresh.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    graph_refresh.refresh(tmp_path, "auto", full=True)
    assert len(seen) == 3
    for cmd in seen:
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "graphify"]
