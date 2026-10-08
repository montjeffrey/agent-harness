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
    assert 'python -m pip install -e ".[mcp]"' in result.detail


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

    monkeypatch.setattr(
        graph_index, "chromadb_status", lambda: (False, "ImportError: unavailable")
    )
    result = doctor.check_semantic()
    assert result.level == "WARN"
    assert result.name == "semantic dependencies"
    assert 'python -m pip install -e ".[semantic]"' in result.detail
    assert "agent-harness-kit[semantic]" not in result.detail


def test_check_semantic_passes_when_available(monkeypatch) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_status", lambda: (True, None))
    result = doctor.check_semantic()
    assert result.level == "PASS"


def test_check_semantic_reports_a_chromadb_import_failure_reason(monkeypatch) -> None:
    from harness import graph_index

    monkeypatch.setattr(
        graph_index,
        "chromadb_status",
        lambda: (False, "ImportError: incompatible sqlite runtime"),
        raising=False,
    )
    result = doctor.check_semantic()

    assert result.level == "WARN"
    assert "incompatible sqlite runtime" in result.detail


def test_check_semantic_info_when_no_index_yet(monkeypatch, tmp_path: Path) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("{}", encoding="utf-8")
    result = doctor.check_graph_semantic(tmp_path)
    assert result.level == "INFO"
    assert "no graph semantic index yet" in result.detail


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
    result = doctor.check_graph_semantic(tmp_path)
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
    monkeypatch.setattr(graph_index, "probe_index", lambda _idx: None, raising=False)
    result = doctor.check_graph_semantic(tmp_path)
    assert result.level == "PASS"


def test_graph_semantic_readiness_warns_when_an_existing_index_cannot_open(
    monkeypatch, tmp_path: Path
) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("{}", encoding="utf-8")
    graph_index.index_path_for(graph_path).mkdir()
    monkeypatch.setattr(graph_index, "index_is_stale", lambda _idx, _g: False)
    monkeypatch.setattr(
        graph_index,
        "probe_index",
        lambda _idx: "no collection named 'agent_harness_graph' in this semantic index",
        raising=False,
    )

    result = doctor.check_graph_semantic(tmp_path)

    assert result.level == "WARN"
    assert "not query-ready" in result.detail
    assert "no collection named" in result.detail


def test_check_semantic_never_fails(monkeypatch, tmp_path: Path) -> None:
    from harness import graph_index

    for available in (True, False):
        monkeypatch.setattr(graph_index, "chromadb_available", lambda a=available: a)
        for project_dir in (None, tmp_path):
            result = doctor.check_graph_semantic(project_dir)
            assert result.level != "FAIL"


def test_run_checks_includes_semantic_search_check(tmp_path: Path) -> None:
    results = doctor.run_checks(tmp_path)
    names = {r.name for r in results}
    assert {"semantic dependencies", "graph semantic index", "memory semantic index"} <= names


def test_graph_semantic_readiness_reports_missing_index_separately(monkeypatch, tmp_path: Path) -> None:
    from harness import graph_index

    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    graph_path = tmp_path / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("{}", encoding="utf-8")
    result = doctor.check_graph_semantic(tmp_path)

    assert result == doctor.CheckResult(
        "INFO", "graph semantic index",
        "no graph semantic index yet -- run harness/graph_refresh.py to build one",
    )


def test_graph_semantic_readiness_never_passes_without_graph_json(
    monkeypatch, tmp_path: Path
) -> None:
    from harness import graph_index

    index_dir = tmp_path / "graphify-out" / "semantic-index"
    index_dir.mkdir(parents=True)
    (index_dir / "chroma.sqlite3").write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(graph_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(graph_index, "probe_index", lambda _idx: None)

    result = doctor.check_graph_semantic(tmp_path)

    assert result.level == "INFO"
    assert "graph.json is missing" in result.detail


def test_memory_semantic_readiness_reports_missing_index_separately(monkeypatch, tmp_path: Path) -> None:
    from harness import memory_index

    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "index_dir_for", lambda _project_id: tmp_path / "project-index")
    monkeypatch.setattr(memory_index, "machine_index_dir", lambda: tmp_path / "machine-index")

    result = doctor.check_memory_semantic(tmp_path)

    assert result == doctor.CheckResult(
        "INFO", "memory semantic index",
        "no memory semantic index yet -- run agent-harness-memory index to build one",
    )


def test_memory_semantic_readiness_warns_when_an_existing_index_cannot_open(
    monkeypatch, tmp_path: Path
) -> None:
    from harness import memory_index

    project_index = tmp_path / "project-index"
    machine_index = tmp_path / "machine-index"
    project_index.mkdir()
    machine_index.mkdir()
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "index_dir_for", lambda _project_id: project_index)
    monkeypatch.setattr(memory_index, "machine_index_dir", lambda: machine_index)
    monkeypatch.setattr(
        memory_index,
        "probe_index",
        lambda index_dir: "no collection" if index_dir == project_index else None,
        raising=False,
    )

    result = doctor.check_memory_semantic(tmp_path)

    assert result.level == "WARN"
    assert "project index is not query-ready" in result.detail
    assert "no collection" in result.detail


def test_memory_semantic_readiness_probes_a_partial_index_before_reporting_missing_scope(
    monkeypatch, tmp_path: Path
) -> None:
    from harness import memory_index

    project_index = tmp_path / "project-index"
    project_index.mkdir()
    machine_index = tmp_path / "machine-index"
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "index_dir_for", lambda _project_id: project_index)
    monkeypatch.setattr(memory_index, "machine_index_dir", lambda: machine_index)
    monkeypatch.setattr(memory_index, "probe_index", lambda _idx: "missing chroma.sqlite3")

    result = doctor.check_memory_semantic(tmp_path)

    assert result.level == "WARN"
    assert "project index is not query-ready" in result.detail
    assert "machine-scope" in result.detail


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


# --- memory store presence -------------------------------------------------
#
# The pair below is the point: an empty store and an unenrolled project must
# land on different levels, so `doctor` can say which one you are looking at.


def test_memory_store_warns_when_enrolled_but_empty(tmp_path: Path) -> None:
    from harness import memory_store

    memory_store.init_project(tmp_path)

    result = doctor.check_memory_store(tmp_path)

    assert result.level == "WARN"
    assert "enrolled but empty" in result.detail


def test_memory_store_is_info_when_not_enrolled(tmp_path: Path) -> None:
    result = doctor.check_memory_store(tmp_path)

    assert result.level == "INFO"
    assert "not enrolled" in result.detail


def test_memory_store_passes_once_something_is_recorded(tmp_path: Path) -> None:
    from harness import memory_store

    memory_store.init_project(tmp_path)
    memory_store.append_entries(
        [{
            "type": "decision",
            "scope": "project",
            "status": "proposed",
            "confidence": "inferred",
            "source": {"manual": True},
            "summary": "chose the boring option",
        }],
        directory=memory_store.project_memory_dir(tmp_path),
    )

    result = doctor.check_memory_store(tmp_path)

    assert result.level == "PASS"
    assert "1 project entries" in result.detail


def test_memory_store_is_info_without_a_project() -> None:
    assert doctor.check_memory_store(None).level == "INFO"


def test_memory_store_check_is_wired_into_run_checks(tmp_path: Path) -> None:
    from harness import memory_store

    memory_store.init_project(tmp_path)

    names = [r.name for r in doctor.run_checks(tmp_path)]

    assert "memory store" in names


# --- installed-vs-source drift --------------------------------------------
#
# The failure this group guards against is not a crash: it is an install that
# works perfectly while missing modules the checkout has. Every test here
# builds both sides in tmp_path and pins `installed_harness_dirs`, so results
# never depend on what happens to be installed on the machine running the
# suite.


def _make_checkout(
    root: Path, modules: dict[str, str], *, distribution: str = "agent-harness-kit"
) -> Path:
    package = root / "harness"
    package.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{distribution}"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    for name, text in modules.items():
        (package / name).write_text(text, encoding="utf-8")
    return package


def _make_install(root: Path, modules: dict[str, str]) -> Path:
    package = root / "site-packages" / "harness"
    package.mkdir(parents=True, exist_ok=True)
    for name, text in modules.items():
        (package / name).write_text(text, encoding="utf-8")
    return package


def test_source_checkout_root_requires_this_distribution(tmp_path: Path, monkeypatch) -> None:
    """Any project may contain a `harness/` directory; only ours counts."""
    monkeypatch.setattr(doctor, "HARNESS_ROOT", tmp_path / "nowhere")

    _make_checkout(tmp_path, {"__init__.py": ""}, distribution="somebody-elses-kit")
    assert doctor.source_checkout_root(tmp_path) is None

    _make_checkout(tmp_path, {"__init__.py": ""})
    assert doctor.source_checkout_root(tmp_path) == tmp_path.resolve()


def test_install_drift_is_info_when_no_checkout_can_be_identified(
    tmp_path: Path, monkeypatch
) -> None:
    """A machine with only the wheel installed is a perfectly good state."""
    monkeypatch.setattr(doctor, "HARNESS_ROOT", tmp_path / "nowhere")

    result = doctor.check_install_drift(tmp_path)

    assert result.level == "INFO"
    assert "no agent-harness source checkout" in result.detail


def test_install_drift_fails_when_the_install_is_missing_modules(
    tmp_path: Path, monkeypatch
) -> None:
    """The exact shape of the multi-hour misdiagnosis: tools that aren't there."""
    _make_checkout(tmp_path, {"__init__.py": "", "work.py": "WORK\n", "memory.py": "M\n"})
    installed = _make_install(tmp_path, {"__init__.py": "", "memory.py": "M\n"})
    monkeypatch.setattr(doctor, "installed_harness_dirs", lambda: [installed])

    result = doctor.check_install_drift(tmp_path)

    assert result.level == "FAIL"
    assert "work.py" in result.detail
    assert str(installed) in result.detail
    assert "pipx install --force" in result.detail
    assert str(tmp_path.resolve()) in result.detail


def test_install_drift_warns_when_only_contents_differ(tmp_path: Path, monkeypatch) -> None:
    _make_checkout(tmp_path, {"__init__.py": "", "memory.py": "new behaviour\n"})
    installed = _make_install(tmp_path, {"__init__.py": "", "memory.py": "old behaviour\n"})
    monkeypatch.setattr(doctor, "installed_harness_dirs", lambda: [installed])

    result = doctor.check_install_drift(tmp_path)

    assert result.level == "WARN"
    assert "memory.py" in result.detail
    assert "pipx install --force" in result.detail


def test_install_drift_passes_and_ignores_line_endings(tmp_path: Path, monkeypatch) -> None:
    """A checkout cloned with autocrlf must not report every module as drifted."""
    source = _make_checkout(tmp_path, {"__init__.py": "", "memory.py": ""})
    installed = _make_install(tmp_path, {"__init__.py": "", "memory.py": ""})
    # Written as bytes: text mode would translate the newlines back again.
    (source / "memory.py").write_bytes(b"line one\r\nline two\r\n")
    (installed / "memory.py").write_bytes(b"line one\nline two\n")
    monkeypatch.setattr(doctor, "installed_harness_dirs", lambda: [installed])

    assert doctor.check_install_drift(tmp_path).level == "PASS"


def test_install_drift_is_info_for_an_editable_install(tmp_path: Path, monkeypatch) -> None:
    """Checkout and install are the same directory; there is nothing to drift."""
    package = _make_checkout(tmp_path, {"__init__.py": ""})
    monkeypatch.setattr(doctor, "installed_harness_dirs", lambda: [package.resolve()])

    result = doctor.check_install_drift(tmp_path)

    assert result.level == "INFO"
    assert "checkout itself" in result.detail


def test_install_drift_ignores_pycache(tmp_path: Path, monkeypatch) -> None:
    _make_checkout(tmp_path, {"__init__.py": ""})
    cache = tmp_path / "harness" / "__pycache__"
    cache.mkdir()
    (cache / "stale.py").write_text("junk\n", encoding="utf-8")
    installed = _make_install(tmp_path, {"__init__.py": ""})
    monkeypatch.setattr(doctor, "installed_harness_dirs", lambda: [installed])

    assert doctor.check_install_drift(tmp_path).level == "PASS"


def test_installed_harness_dirs_finds_the_running_package() -> None:
    """Whatever else it finds, the copy this process imported must be in it."""
    assert Path(doctor.__file__).resolve().parent in doctor.installed_harness_dirs()


def test_install_drift_is_wired_into_run_checks(tmp_path: Path) -> None:
    assert "install drift" in {r.name for r in doctor.run_checks(tmp_path)}


# --- instructions vs the served tool surface ------------------------------
#
# The user-scope protocol block is loaded by every session on the machine; the
# server answering those sessions is whichever copy of harness is registered.
# Nothing else in the harness compares the two.


def test_served_tool_names_come_from_the_registration_tables() -> None:
    """Read the server's own tables, never a list copied into the doctor."""
    from harness import mcp_server

    expected = (
        set(mcp_server.TOOL_TO_COMMAND)
        | set(mcp_server.MEMORY_TOOL_DESCRIPTIONS)
        | set(mcp_server.WORK_TOOL_DESCRIPTIONS)
    )

    assert doctor.served_tool_names() == expected
    assert "work_start" in expected


def test_tool_names_from_source_reads_a_foreign_install_without_running_it(
    tmp_path: Path,
) -> None:
    """The other install cannot be imported into this process, and must not be."""
    module = tmp_path / "mcp_server.py"
    module.write_text(
        "import a_module_that_does_not_exist\n"
        "raise RuntimeError('this file must never be executed')\n"
        'TOOL_TO_COMMAND: dict[str, str] = {"graph_find": "find"}\n'
        'WORK_TOOL_DESCRIPTIONS = {"work_start": "...", "work_finish": "..."}\n',
        encoding="utf-8",
    )

    assert doctor._tool_names_from_source(module) == {
        "graph_find",
        "work_start",
        "work_finish",
    }


def test_tool_names_from_source_returns_none_for_an_absent_file(tmp_path: Path) -> None:
    assert doctor._tool_names_from_source(tmp_path / "missing.py") is None


def test_referenced_tool_names_reads_code_spans_not_prose() -> None:
    block = (
        "Call `work_start` before editing, and `memory_context` once.\n"
        "Do the work and start it properly; entries stay `proposed`.\n"
        "The `agent-harness` server is registered already.\n"
    )

    assert doctor.referenced_tool_names(block) == {"work_start", "memory_context"}


def test_protocol_block_extracts_only_the_marked_region() -> None:
    text = (
        "# My own rules\nAlways use tabs.\n"
        f"{doctor.PROTOCOL_START_MARKER}\ncall `work_start`\n{doctor.PROTOCOL_END_MARKER}\n"
        "More of my own rules.\n"
    )

    block = doctor.protocol_block(text)

    assert block is not None
    assert "work_start" in block
    assert "Always use tabs" not in block
    assert doctor.protocol_block("no markers here") is None


def test_protocol_sources_include_the_installed_user_scope_block(tmp_path: Path) -> None:
    """The block in ~/.claude/CLAUDE.md is what every session actually loads."""
    rules = tmp_path / ".claude" / "CLAUDE.md"
    rules.parent.mkdir(parents=True)
    rules.write_text(
        f"{doctor.PROTOCOL_START_MARKER}\ncall `work_start`\n{doctor.PROTOCOL_END_MARKER}\n",
        encoding="utf-8",
    )

    sources = dict(doctor._protocol_sources(tmp_path, None))

    assert "work_start" in sources[str(rules)]


def test_instruction_contract_fails_when_a_server_lacks_a_named_tool(monkeypatch) -> None:
    """The check that would have caught the work_start gap."""
    monkeypatch.setattr(
        doctor,
        "served_tool_surfaces",
        lambda project_dir=None: [("stale-install", {"memory_context"})],
    )
    monkeypatch.setattr(
        doctor,
        "_protocol_sources",
        lambda home, project_dir: [
            ("~/.claude/CLAUDE.md", "call `work_start` then `memory_context`")
        ],
    )

    result = doctor.check_instruction_contract()

    assert result.level == "FAIL"
    assert "work_start" in result.detail
    assert "stale-install" in result.detail
    assert "pipx install --force" in result.detail


def test_instruction_contract_checks_every_server_not_just_the_imported_one(
    monkeypatch,
) -> None:
    """A current checkout plus a stale install is still a broken machine."""
    monkeypatch.setattr(
        doctor,
        "served_tool_surfaces",
        lambda project_dir=None: [
            ("checkout", {"work_start", "memory_context"}),
            ("stale-install", {"memory_context"}),
        ],
    )
    monkeypatch.setattr(
        doctor, "_protocol_sources", lambda home, project_dir: [("template", "`work_start`")]
    )

    result = doctor.check_instruction_contract()

    assert result.level == "FAIL"
    assert "stale-install" in result.detail


def test_instruction_contract_passes_when_every_named_tool_is_served(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor,
        "served_tool_surfaces",
        lambda project_dir=None: [
            ("a", {"work_start", "memory_context"}),
            ("b", {"work_start", "memory_context"}),
        ],
    )
    monkeypatch.setattr(
        doctor,
        "_protocol_sources",
        lambda home, project_dir: [("template", "`work_start` `memory_context`")],
    )

    assert doctor.check_instruction_contract().level == "PASS"


def test_instruction_contract_is_info_without_any_instruction_block(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_protocol_sources", lambda home, project_dir: [])

    result = doctor.check_instruction_contract()

    assert result.level == "INFO"
    assert "agent-harness-setup" in result.detail


def test_the_shipped_protocol_only_names_tools_the_shipped_server_registers() -> None:
    """The contract this repository is itself responsible for keeping true."""
    import importlib.resources as resources

    template = (resources.files("templates") / "MEMORY.protocol.md").read_text(
        encoding="utf-8"
    )
    block = doctor.protocol_block(template) or template

    assert doctor.referenced_tool_names(block) <= (doctor.served_tool_names() or set())


def test_instruction_contract_is_wired_into_run_checks(tmp_path: Path) -> None:
    assert "instruction contract" in {r.name for r in doctor.run_checks(tmp_path)}


def test_served_tool_surfaces_reports_the_imported_server_and_each_foreign_install(
    tmp_path: Path, monkeypatch
) -> None:
    """One surface per harness copy, the imported one first and never twice."""
    from harness import mcp_server

    imported_dir = Path(mcp_server.__file__).resolve().parent
    stale = _make_install(tmp_path, {"__init__.py": ""})
    (stale / "mcp_server.py").write_text(
        'TOOL_TO_COMMAND = {"graph_find": "find"}\n'
        'MEMORY_TOOL_DESCRIPTIONS = {"memory_context": "..."}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "installed_harness_dirs", lambda: [imported_dir, stale])

    surfaces = doctor.served_tool_surfaces()

    assert [label for label, _ in surfaces] == [str(imported_dir), str(stale)]
    assert surfaces[0][1] == doctor.served_tool_names()
    assert surfaces[1][1] == {"graph_find", "memory_context"}


def test_protocol_sources_include_the_checkout_template(tmp_path: Path, monkeypatch) -> None:
    """The template is the block setup will install next; check it too."""
    monkeypatch.setattr(doctor, "HARNESS_ROOT", tmp_path / "nowhere")
    checkout = tmp_path / "checkout"
    _make_checkout(checkout, {"__init__.py": ""})
    template = checkout / "templates" / "MEMORY.protocol.md"
    template.parent.mkdir()
    template.write_text(
        f"{doctor.PROTOCOL_START_MARKER}\ncall `work_start`\n{doctor.PROTOCOL_END_MARKER}\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    sources = dict(doctor._protocol_sources(home, checkout))

    assert set(sources) == {str(template)}
    assert "work_start" in sources[str(template)]


def test_instruction_contract_end_to_end_against_a_stale_install_on_disk(
    tmp_path: Path, monkeypatch
) -> None:
    """No pinned surfaces or sources: real files on both sides of the compare.

    The installed user-scope block only names a tool the stale server has,
    so it must not be blamed; the checkout template names `work_start`, which
    the stale install lacks, and that is the failure reported -- attributed
    to the install's path, with the reinstall command for this checkout.
    """
    monkeypatch.setattr(doctor, "HARNESS_ROOT", tmp_path / "nowhere")
    checkout = tmp_path / "checkout"
    _make_checkout(checkout, {"__init__.py": ""})
    template = checkout / "templates" / "MEMORY.protocol.md"
    template.parent.mkdir()
    template.write_text("call `memory_context` then `work_start`\n", encoding="utf-8")

    stale = _make_install(tmp_path, {"__init__.py": ""})
    (stale / "mcp_server.py").write_text(
        'MEMORY_TOOL_DESCRIPTIONS = {"memory_context": "..."}\n', encoding="utf-8"
    )
    monkeypatch.setattr(doctor, "installed_harness_dirs", lambda: [stale])

    home = tmp_path / "home"
    rules = home / ".claude" / "CLAUDE.md"
    rules.parent.mkdir(parents=True)
    rules.write_text(
        f"{doctor.PROTOCOL_START_MARKER}\ncall `memory_context`\n{doctor.PROTOCOL_END_MARKER}\n",
        encoding="utf-8",
    )

    result = doctor.check_instruction_contract(checkout, home)

    assert result.level == "FAIL"
    assert "work_start" in result.detail
    assert str(stale) in result.detail
    assert str(template) in result.detail
    assert str(rules) not in result.detail
    assert doctor._reinstall_command(checkout.resolve()) in result.detail


class _FakeDistribution:
    """Just enough of importlib.metadata.Distribution for `installed_harness_dirs`."""

    def __init__(self, name: str, root: Path, files: list[str]) -> None:
        self.metadata = {"Name": name}
        self._root = root
        self.files = files

    def locate_file(self, entry: str) -> Path:
        return self._root / entry


def test_installed_harness_dirs_walks_distribution_metadata(tmp_path: Path, monkeypatch) -> None:
    """The site-packages copy is found through RECORD, not through `harness.__file__`.

    A checkout on `sys.path` shadows the install, so the imported package says
    nothing about the copy every other project loads. Only our distribution
    counts; a stranger's package that also ships `harness/__init__.py` does not.
    """
    import importlib.metadata

    ours = tmp_path / "venv-a"
    theirs = tmp_path / "venv-b"
    for root in (ours, theirs):
        (root / "harness").mkdir(parents=True)
        (root / "harness" / "__init__.py").write_text("", encoding="utf-8")
    fakes = [
        _FakeDistribution("agent_harness_kit", ours, ["harness/__init__.py", "harness/doctor.py"]),
        _FakeDistribution("somebody-elses-kit", theirs, ["harness/__init__.py"]),
    ]
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: fakes)

    found = doctor.installed_harness_dirs()

    assert (ours / "harness").resolve() in found
    assert (theirs / "harness").resolve() not in found
    # The package this process imported is still reported alongside.
    assert Path(doctor.__file__).resolve().parent in found
