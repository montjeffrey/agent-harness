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


def test_run_checks_includes_project_section(tmp_path: Path) -> None:
    without_project = doctor.run_checks()
    with_project = doctor.run_checks(tmp_path)
    assert len(with_project) > len(without_project)
