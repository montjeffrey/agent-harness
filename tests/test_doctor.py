"""Tests for the preflight doctor."""

from __future__ import annotations

import json
from pathlib import Path

from harness import doctor


def test_check_python_passes_on_current_interpreter() -> None:
    result = doctor.check_python()
    assert result.level == "PASS"


def test_check_harness_files_pass_on_real_root() -> None:
    result = doctor.check_harness_files()
    assert result.level == "PASS"


def test_check_harness_files_fail_on_empty_root(tmp_path: Path) -> None:
    result = doctor.check_harness_files(tmp_path)
    assert result.level == "FAIL"
    assert "install.py" in result.detail


def test_check_project_missing_dir_is_info(tmp_path: Path) -> None:
    results = doctor.check_project(tmp_path / "does-not-exist")
    assert [r.level for r in results] == ["INFO"]


def test_check_project_invalid_claude_settings_fails(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    assert levels["claude settings"] == "FAIL"


def test_check_project_claude_settings_that_is_a_list_fails(tmp_path: Path) -> None:
    """A syntactically valid JSON array is still not a usable settings object."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    assert levels["claude settings"] == "FAIL"


def test_check_project_uses_shared_config_validation(tmp_path: Path, monkeypatch) -> None:
    """doctor.check_project must route through the shared validator, not its
    own ad hoc json.loads -- proven by making the shared validator raise for
    input doctor's old inline check would have accepted.
    """
    from harness import config_validation

    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    def _boom(_path):
        raise config_validation.ConfigValidationError("forced failure for test")

    monkeypatch.setattr(doctor.config_validation, "load_json_object", _boom)
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    assert levels["claude settings"] == "FAIL"
    details = {r.name: r.detail for r in results}
    assert "forced failure for test" in details["claude settings"]


def test_check_project_valid_claude_settings_passes(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    assert levels["claude settings"] == "PASS"


def test_check_backend_key_warns_without_backend(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HARNESS_GRAPH_BACKEND", raising=False)
    result = doctor.check_backend_key(config_path=tmp_path / "config.json")
    assert result.level == "WARN"
    assert "auto" in result.detail


def test_check_backend_key_passes_with_env_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_GRAPH_BACKEND", "nvidia-nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "test-value")
    result = doctor.check_backend_key(config_path=tmp_path / "config.json")
    assert result.level == "PASS"
    assert "test-value" not in result.detail  # never echo key material


def test_check_backend_key_fails_on_broken_key_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_GRAPH_BACKEND", "nvidia-nim")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"key_command": "exit 1", "key_env": "NVIDIA_API_KEY"}),
        encoding="utf-8",
    )
    result = doctor.check_backend_key(config_path=config)
    assert result.level == "FAIL"


def test_check_backend_key_fails_on_malformed_config_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_GRAPH_BACKEND", "nvidia-nim")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")
    result = doctor.check_backend_key(config_path=config)
    assert result.level == "FAIL"


def test_has_failures_and_report() -> None:
    results = [
        doctor.CheckResult("PASS", "a", "ok"),
        doctor.CheckResult("WARN", "b", "hmm"),
    ]
    assert not doctor.has_failures(results)
    report = doctor.format_report(results)
    assert "1 warning(s)" in report and "safe to install" in report

    results.append(doctor.CheckResult("FAIL", "c", "bad"))
    assert doctor.has_failures(results)
    assert "fix failures" in doctor.format_report(results)


def test_check_mcp_passes_when_sdk_importable(monkeypatch) -> None:
    import types

    monkeypatch.setattr(
        doctor.importlib.util, "find_spec", lambda name: types.SimpleNamespace() if name == "mcp" else None
    )
    result = doctor.check_mcp()
    assert result.level == "PASS"


def test_check_mcp_warns_when_sdk_missing(monkeypatch) -> None:
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)
    result = doctor.check_mcp()
    assert result.level == "WARN"
    assert "agent-harness-kit[mcp]" in result.detail


def test_check_mcp_registered_in_run_checks() -> None:
    results = doctor.run_checks()
    names = {r.name for r in results}
    assert "mcp sdk" in names


def test_check_project_mcp_json_missing_is_info(tmp_path: Path) -> None:
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    assert levels["mcp registration"] == "INFO"


def test_check_project_mcp_json_registers_agent_harness_is_info(tmp_path: Path) -> None:
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps({"mcpServers": {"agent-harness": {"command": "python", "args": ["-m", "harness.mcp_server"]}}}),
        encoding="utf-8",
    )
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    detail = {r.name: r.detail for r in results}
    assert levels["mcp registration"] == "INFO"
    assert "agent-harness" in detail["mcp registration"]


def test_check_project_invalid_mcp_json_fails(tmp_path: Path) -> None:
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text("{not json", encoding="utf-8")
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    assert levels["mcp registration"] == "FAIL"


def test_check_project_mcpservers_non_object_value_fails(tmp_path: Path) -> None:
    """A syntactically valid .mcp.json whose 'mcpServers' key is present but
    isn't a JSON object (e.g. a list) must FAIL cleanly -- previously
    `"agent-harness" in servers` silently treated a list as having no
    registrations instead of reporting the malformed config."""
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": []}), encoding="utf-8")
    results = doctor.check_project(tmp_path)
    levels = {r.name: r.level for r in results}
    assert levels["mcp registration"] == "FAIL"


def test_run_checks_includes_project_section(tmp_path: Path) -> None:
    without_project = doctor.run_checks()
    with_project = doctor.run_checks(tmp_path)
    assert len(with_project) > len(without_project)


# --- check_semantic: WARN/PASS/INFO matrix, and never FAIL ------------------


def test_check_semantic_warns_when_chromadb_unavailable(monkeypatch) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: False)
    result = doctor.check_semantic()
    assert result.level == "WARN"
    assert "agent-harness-kit[semantic]" in result.detail


def test_check_semantic_passes_when_available_no_project_dir(monkeypatch) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    result = doctor.check_semantic()
    assert result.level == "PASS"


def test_check_semantic_info_when_no_index_yet(monkeypatch, tmp_path: Path) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    result = doctor.check_semantic(tmp_path)
    assert result.level == "INFO"
    assert "no semantic index yet" in result.detail


def test_check_semantic_warns_when_index_stale(monkeypatch, tmp_path: Path) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("{}", encoding="utf-8")
    index_dir = graph_index.index_path_for(graph_path)
    index_dir.mkdir()
    (index_dir / "chroma.sqlite3").write_text("x", encoding="utf-8")

    monkeypatch.setattr(graph_index, "index_is_stale", lambda _idx, _g: True)
    result = doctor.check_semantic(tmp_path)
    assert result.level == "WARN"
    assert "older than graph.json" in result.detail


def test_check_semantic_passes_when_index_fresh(monkeypatch, tmp_path: Path) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("{}", encoding="utf-8")
    index_dir = graph_index.index_path_for(graph_path)
    index_dir.mkdir()
    (index_dir / "chroma.sqlite3").write_text("x", encoding="utf-8")

    monkeypatch.setattr(graph_index, "index_is_stale", lambda _idx, _g: False)
    result = doctor.check_semantic(tmp_path)
    assert result.level == "PASS"


def test_check_semantic_never_fails(monkeypatch, tmp_path: Path) -> None:
    from harness import graph_index

    for available in (True, False):
        monkeypatch.setattr(graph_index, "chromadb_available", lambda a=available: a)
        for project_dir in (None, tmp_path):
            result = doctor.check_semantic(project_dir)
            assert result.level != "FAIL"


def test_run_checks_includes_semantic_search_check(tmp_path: Path) -> None:
    results = doctor.run_checks(tmp_path)
    names = {r.name for r in results}
    assert "semantic search" in names


def test_check_telemetry_absent_dir_is_info(tmp_path: Path) -> None:
    result = doctor.check_telemetry(telemetry_dir=tmp_path / "does-not-exist")
    assert result.level == "INFO"
    assert "no telemetry collected yet" in result.detail


def test_check_telemetry_present_dir_reports_count_and_agents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from harness import telemetry_store

    telemetry_dir = tmp_path / ".agent-harness" / "telemetry"
    telemetry_store.append_events([
        {"agent": "codex", "event": "usage"},
        {"agent": "gemini", "event": "session_start"},
    ])

    result = doctor.check_telemetry(telemetry_dir=telemetry_dir)
    assert result.level == "INFO"
    assert "2 record" in result.detail
    assert "codex" in result.detail
    assert "gemini" in result.detail


def test_check_telemetry_present_but_empty_dir_is_info(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / ".agent-harness" / "telemetry"
    telemetry_dir.mkdir(parents=True)
    result = doctor.check_telemetry(telemetry_dir=telemetry_dir)
    assert result.level == "INFO"
    assert "no valid records" in result.detail


def test_run_checks_includes_telemetry_check(tmp_path: Path) -> None:
    results = doctor.run_checks()
    names = [r.name for r in results]
    assert "telemetry" in names
