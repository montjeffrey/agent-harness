"""Tests for the automatic surfaces: the SessionStart hook and MCP tools.

These are the two places memory reaches an agent without anyone typing a
command, so the properties that matter are behavioural rather than
cosmetic: the hook must never block work and never pay for chromadb, and
the MCP surface must not let an agent promote its own guesses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import mcp_server, memory_context, memory_index, memory_store
from hooks.claude import memory_context_hook as hook

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)

FILLED_SCOPE = """# Project scope

## Purpose

Portable memory for AI-assisted development.

## Active objective

Ship the automatic wiring.
"""


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv(hook.STRATEGY_ENV, raising=False)
    monkeypatch.delenv(hook.MAX_CHARS_ENV, raising=False)
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    (root / ".agent-harness" / "scope.md").write_text(FILLED_SCOPE, encoding="utf-8")
    return root


def _add(project: Path, summary: str, **overrides) -> None:
    entry = {
        "type": "decision",
        "scope": "project",
        "status": "accepted",
        "confidence": "confirmed",
        "source": {"manual": True},
        "summary": summary,
        **overrides,
    }
    memory_store.append_entries(
        [entry], directory=memory_store.project_memory_dir(project), now=NOW
    )


def _spy_selector(calls: list[str]):
    """Stand-in for resolve_selector that records the strategy asked for."""

    def spy(project_id: str, strategy: str = "auto", **kwargs):
        calls.append(strategy)
        return memory_context.deterministic_selector, "deterministic", []

    return spy


# --- SessionStart hook ----------------------------------------------------


def test_the_hook_emits_the_packet_for_an_enrolled_project(project: Path) -> None:
    _add(project, "Telemetry is local-only JSONL.")
    text = hook.emit(project)
    assert "# Project context:" in text
    assert "local-only JSONL" in text


def test_the_hook_is_silent_in_an_unenrolled_project(tmp_path: Path) -> None:
    """Otherwise it would announce itself in every repo on the machine."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert hook.emit(bare) == ""


def test_the_hook_is_silent_when_there_is_nothing_recorded_yet(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    assert hook.emit(root) == ""


def test_the_hook_defaults_to_deterministic_not_auto(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto` would import chromadb on every session start in every project."""
    calls: list[str] = []
    monkeypatch.setattr(memory_index, "resolve_selector", _spy_selector(calls))
    _add(project, "Something recorded.")
    hook.emit(project)
    assert calls == ["deterministic"]


def test_the_hook_strategy_is_opt_in_via_env(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(memory_index, "resolve_selector", _spy_selector(calls))
    monkeypatch.setenv(hook.STRATEGY_ENV, "auto")
    _add(project, "Something recorded.")
    hook.emit(project)
    assert calls == ["auto"]


def test_the_hook_uses_a_smaller_budget_than_the_cli(project: Path) -> None:
    """Hook output is charged to every session whether it is used or not."""
    assert hook.HOOK_MAX_CHARS < memory_context.DEFAULT_MAX_CHARS
    for n in range(100):
        _add(project, f"Fact {n}: " + "detail " * 30)
    # The packet body respects the budget; the rendered header and warnings
    # sit outside it, hence the allowance.
    assert len(hook.emit(project)) <= hook.HOOK_MAX_CHARS + 600


def test_the_budget_is_overridable_by_env(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(50):
        _add(project, f"Fact {n}: " + "detail " * 30)
    monkeypatch.setenv(hook.MAX_CHARS_ENV, "300")
    small = hook.emit(project)
    monkeypatch.setenv(hook.MAX_CHARS_ENV, "6000")
    assert len(hook.emit(project)) > len(small)


def test_a_garbage_env_budget_falls_back_to_the_default(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(hook.MAX_CHARS_ENV, "not-a-number")
    _add(project, "Something recorded.")
    assert hook.emit(project)  # did not raise


def test_the_hook_exits_zero_even_when_the_packet_blows_up(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open: a memory bug must never stop someone from working."""

    def explode(*args, **kwargs):
        raise RuntimeError("simulated harness bug")

    monkeypatch.setattr(hook, "emit", explode)
    assert hook.main([str(project)]) == 0


def test_the_hook_exits_zero_on_a_nonexistent_path() -> None:
    assert hook.main(["/nonexistent/path/that/does/not/exist"]) == 0


def test_the_hook_writes_nothing_to_disk(project: Path) -> None:
    _add(project, "Something recorded.")
    directory = memory_store.project_memory_dir(project)
    before = sorted(p.name for p in directory.iterdir())
    hook.emit(project)
    assert sorted(p.name for p in directory.iterdir()) == before


# --- auto-enrollment ------------------------------------------------------


def test_auto_enroll_is_off_by_default(tmp_path: Path) -> None:
    """The hook runs in every directory Claude Code opens. Off is the only
    safe default."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert hook.should_auto_enroll(bare) is False
    assert hook.emit(bare) == ""
    assert not (bare / ".agent-harness").exists()


def test_the_flag_alone_does_not_enable_auto_enroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a workspace, opening any cloned repo would scaffold into it."""
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv(hook.AUTO_ENROLL_ENV, "1")
    monkeypatch.delenv(hook.WORKSPACE_ENV, raising=False)
    assert hook.should_auto_enroll(bare) is False


def test_a_workspace_alone_does_not_enable_auto_enroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.delenv(hook.AUTO_ENROLL_ENV, raising=False)
    monkeypatch.setenv(hook.WORKSPACE_ENV, str(tmp_path))
    assert hook.should_auto_enroll(bare) is False


def test_auto_enroll_fires_inside_the_configured_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inside = tmp_path / "workspace" / "myproject"
    inside.mkdir(parents=True)
    monkeypatch.setenv(hook.AUTO_ENROLL_ENV, "1")
    monkeypatch.setenv(hook.WORKSPACE_ENV, str(tmp_path / "workspace"))
    assert hook.should_auto_enroll(inside) is True

    hook.emit(inside)
    assert (inside / ".agent-harness" / "scope.md").is_file()


def test_auto_enroll_refuses_outside_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    monkeypatch.setenv(hook.AUTO_ENROLL_ENV, "1")
    monkeypatch.setenv(hook.WORKSPACE_ENV, str(tmp_path / "workspace"))
    assert hook.should_auto_enroll(outside) is False
    hook.emit(outside)
    assert not (outside / ".agent-harness").exists()


def test_several_workspace_roots_are_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    a, b = tmp_path / "wsA" / "p", tmp_path / "wsB" / "p"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    monkeypatch.setenv(hook.AUTO_ENROLL_ENV, "true")
    monkeypatch.setenv(
        hook.WORKSPACE_ENV,
        os.pathsep.join([str(tmp_path / "wsA"), str(tmp_path / "wsB")]),
    )
    assert hook.should_auto_enroll(a) is True
    assert hook.should_auto_enroll(b) is True


def test_auto_enroll_does_not_build_an_index_or_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-second job at every session start is the trap this avoids."""
    inside = tmp_path / "workspace" / "myproject"
    inside.mkdir(parents=True)
    monkeypatch.setenv(hook.AUTO_ENROLL_ENV, "1")
    monkeypatch.setenv(hook.WORKSPACE_ENV, str(tmp_path / "workspace"))

    def boom(*args, **kwargs):
        raise AssertionError("the hook must never build an index")

    monkeypatch.setattr(memory_index, "build_index", boom)
    hook.emit(inside)
    assert not (inside / "graphify-out").exists()


# --- MCP tools ------------------------------------------------------------


def test_the_mcp_module_imports_without_the_sdk_or_chromadb() -> None:
    assert callable(mcp_server.dispatch_memory)


def test_memory_context_tool_returns_a_packet(project: Path) -> None:
    _add(project, "Telemetry is local-only JSONL.")
    out = mcp_server.dispatch_memory("memory_context", {"project": str(project)})
    assert "# Project context:" in out
    assert "local-only JSONL" in out


def test_memory_context_tool_honours_max_chars(project: Path) -> None:
    for n in range(100):
        _add(project, f"Fact {n}: " + "detail " * 30)
    small = mcp_server.dispatch_memory(
        "memory_context", {"project": str(project), "max_chars": 500}
    )
    big = mcp_server.dispatch_memory(
        "memory_context", {"project": str(project), "max_chars": 5000}
    )
    assert len(small) < len(big)


def test_memory_status_tool_summarizes_counts(project: Path) -> None:
    _add(project, "One accepted fact.")
    out = mcp_server.dispatch_memory("memory_status", {"project": str(project)})
    assert "1 entries" in out
    assert "accepted=1" in out


def test_memory_status_on_an_empty_project_says_so(project: Path) -> None:
    out = mcp_server.dispatch_memory("memory_status", {"project": str(project)})
    assert "no memory recorded yet" in out


def test_memory_remember_records_a_proposed_inferred_entry(project: Path) -> None:
    """The whole trust model rests on an agent's own entries being proposals."""
    out = mcp_server.dispatch_memory(
        "memory_remember",
        {"project": str(project), "type": "discovery", "summary": "Found a leak."},
    )
    assert "proposed" in out
    [rec] = list(memory_store.iter_entries(memory_store.project_memory_dir(project)))
    assert rec["status"] == "proposed"
    assert rec["confidence"] == "inferred"


def test_memory_remember_cannot_be_talked_into_accepting_itself(
    project: Path,
) -> None:
    """Status/confidence are hardcoded, not parameters -- extra keys are inert."""
    mcp_server.dispatch_memory(
        "memory_remember",
        {"project": str(project), "type": "decision", "summary": "Self-promoted.",
         "status": "accepted", "confidence": "confirmed"},
    )
    [rec] = list(memory_store.iter_entries(memory_store.project_memory_dir(project)))
    assert rec["status"] == "proposed"
    assert rec["confidence"] == "inferred"


def test_no_review_or_accept_tool_is_exposed() -> None:
    """Promotion stays on the CLI, in front of a person."""
    names = set(mcp_server.MEMORY_TOOL_DESCRIPTIONS)
    assert not any("review" in n or "accept" in n for n in names)
    assert names == {
        "memory_context", "memory_status", "memory_remember",
        "memory_confirm", "memory_task_done", "memory_tasks",
    }


def test_memory_remember_rejects_an_unknown_type(project: Path) -> None:
    out = mcp_server.dispatch_memory(
        "memory_remember",
        {"project": str(project), "type": "vibe", "summary": "nope"},
    )
    assert "unknown type" in out
    assert not list(
        memory_store.iter_entries(memory_store.project_memory_dir(project))
    )


def test_memory_remember_requires_a_summary(project: Path) -> None:
    out = mcp_server.dispatch_memory(
        "memory_remember", {"project": str(project), "type": "task", "summary": "  "}
    )
    assert "summary is required" in out


def test_memory_remember_rejects_a_probable_secret_as_text_not_a_crash(
    project: Path,
) -> None:
    out = mcp_server.dispatch_memory(
        "memory_remember",
        {"project": str(project), "type": "discovery",
         "summary": "token ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
    )
    assert "secret" in out
    assert not list(
        memory_store.iter_entries(memory_store.project_memory_dir(project))
    )


def test_memory_remember_splits_tags(project: Path) -> None:
    mcp_server.dispatch_memory(
        "memory_remember",
        {"project": str(project), "type": "lesson", "summary": "Quote paths.",
         "tags": "windows, onedrive"},
    )
    [rec] = list(memory_store.iter_entries(memory_store.project_memory_dir(project)))
    assert rec["tags"] == ["windows", "onedrive"]


def test_an_unknown_memory_tool_returns_text_not_an_exception(project: Path) -> None:
    out = mcp_server.dispatch_memory("memory_telepathy", {"project": str(project)})
    assert "unknown tool" in out


def test_the_graph_dispatch_is_untouched_by_the_memory_addition() -> None:
    assert len(mcp_server.TOOL_TO_COMMAND) == 7
    assert "unknown tool" in mcp_server.dispatch("memory_context", {"term": "x"})


# --- hook fragment --------------------------------------------------------


def _fragment() -> dict:
    import importlib.resources as resources

    return json.loads(
        (resources.files("hooks.claude") / "settings-fragment.json").read_text(
            encoding="utf-8"
        )
    )


def test_the_fragment_registers_the_memory_hook() -> None:
    commands = [
        h["command"]
        for entry in _fragment()["hooks"]["SessionStart"]
        for h in entry["hooks"]
    ]
    assert any("memory_context_hook.py" in c for c in commands)
    assert any("graph_check.py" in c for c in commands)


def test_reinstalling_does_not_duplicate_session_start_hooks() -> None:
    """deep_merge_hooks must stay idempotent now that SessionStart has two."""
    from harness import install_cli

    fragment = _fragment()
    once = install_cli.deep_merge_hooks({}, fragment)
    twice = install_cli.deep_merge_hooks(once, fragment)
    assert once == twice
    assert len(twice["hooks"]["SessionStart"]) == 2


# --- enroll ---------------------------------------------------------------


def test_enroll_writes_no_instruction_files(tmp_path: Path) -> None:
    """Enroll is memory + graph + index. Instruction files and project-local
    settings are machine-level concerns, registered once by bootstrap."""
    from harness import enroll

    root = tmp_path / "fresh"
    root.mkdir()
    enroll.main([str(root), "--no-graph", "--no-index"])

    assert (root / ".agent-harness" / "scope.md").is_file()
    for unwanted in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".mcp.json"):
        assert not (root / unwanted).exists(), f"enroll wrote {unwanted}"
    assert not (root / ".claude").exists()


def test_enroll_survives_graph_refresh_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph_refresh is a CLI entry point and raises SystemExit on failure --
    which `except Exception` does not catch, killing the index step."""
    from harness import enroll, graph_refresh

    def exits(*args, **kwargs):
        raise SystemExit(1)

    monkeypatch.setattr(graph_refresh, "main", exits)
    root = tmp_path / "fresh"
    root.mkdir()

    code = enroll.main([str(root), "--no-index"])
    assert code == 1  # degraded, not crashed
    assert (root / ".agent-harness" / "scope.md").is_file()


def test_enroll_is_idempotent(tmp_path: Path) -> None:
    from harness import enroll

    root = tmp_path / "fresh"
    root.mkdir()
    enroll.main([str(root), "--no-graph", "--no-index"])
    before = (root / ".agent-harness" / "scope.md").read_text(encoding="utf-8")
    enroll.main([str(root), "--no-graph", "--no-index"])
    assert (root / ".agent-harness" / "scope.md").read_text(encoding="utf-8") == before
