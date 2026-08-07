"""Contract tests for harness.collectors.run_collector, plus fixture-based
tests for the three real collectors (claude_code, codex, gemini) using
synthetic logs under tmp_path -- never the real home directory.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from harness import telemetry_store
from harness.collectors import run_collector


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _install_fake_collector(monkeypatch: pytest.MonkeyPatch, module_name: str, collect_fn) -> None:
    module = types.ModuleType(f"harness.collectors.{module_name}")
    module.collect = collect_fn
    monkeypatch.setitem(sys.modules, f"harness.collectors.{module_name}", module)


# --- run_collector contract -------------------------------------------------


def test_run_collector_never_raises_on_collector_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(since):
        raise RuntimeError("simulated collector crash")

    _install_fake_collector(monkeypatch, "claude_code", boom)
    records = run_collector("claude-code")
    assert len(records) == 1
    assert records[0]["event"] == "collector_error"
    assert records[0]["agent"] == "claude-code"
    telemetry_store.validate_record(records[0])  # must itself be schema-valid


def test_run_collector_returns_valid_records_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    def fine(since):
        return [{"agent": "codex", "event": "usage", "input_tokens": 5}]

    _install_fake_collector(monkeypatch, "codex", fine)
    records = run_collector("codex")
    assert len(records) == 1
    assert records[0]["input_tokens"] == 5
    assert records[0]["event"] == "usage"


def test_run_collector_drops_store_invalid_records_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def mixed(since):
        return [
            {"agent": "gemini", "event": "usage"},
            {"agent": "not-a-real-agent", "event": "usage"},
        ]

    _install_fake_collector(monkeypatch, "gemini", mixed)
    records = run_collector("gemini")
    assert len(records) == 2
    events = sorted(r["event"] for r in records)
    assert events == ["collector_error", "usage"]
    for rec in records:
        telemetry_store.validate_record(rec)


def test_run_collector_missing_module_never_raises() -> None:
    records = run_collector("codex")  # no fake installed, no real ~/.codex/sessions in tmp_path home
    assert records == []


# --- claude_code collector: fixture-based -----------------------------------


def test_claude_code_collect_from_synthetic_log(fake_home: Path) -> None:
    from harness.collectors import claude_code

    project_dir = fake_home / ".claude" / "projects" / "C--fake-project"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "session-1.jsonl"
    lines = [
        {"type": "mode", "mode": "normal", "sessionId": "sess-1"},
        {
            "type": "assistant",
            "sessionId": "sess-1",
            "cwd": "C:\\fake\\project",
            "timestamp": "2026-08-06T04:44:12.928Z",
            "message": {
                "model": "claude-fable-5",
                "usage": {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 50,
                },
            },
        },
    ]
    session_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    records = claude_code.collect(since=None)
    events = {r["event"] for r in records}
    assert "session_start" in events
    assert "usage" in events
    usage = next(r for r in records if r["event"] == "usage")
    assert usage["input_tokens"] == 102
    assert usage["output_tokens"] == 50
    assert usage["model"] == "claude-fable-5"
    assert usage["session_id"] == "sess-1"


def test_claude_code_collect_missing_dir_returns_empty(fake_home: Path) -> None:
    from harness.collectors import claude_code

    assert claude_code.collect(since=None) == []


def test_claude_code_collect_tolerates_truncated_file(fake_home: Path) -> None:
    from harness.collectors import claude_code

    project_dir = fake_home / ".claude" / "projects" / "C--fake"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "session-1.jsonl"
    session_file.write_text('{"type": "assistant", "message": {"usage"', encoding="utf-8")

    assert claude_code.collect(since=None) == []


# --- codex collector: fixture-based -----------------------------------------


def test_codex_collect_from_synthetic_log(fake_home: Path) -> None:
    from harness.collectors import codex

    day_dir = fake_home / ".codex" / "sessions" / "2026" / "07" / "30"
    day_dir.mkdir(parents=True)
    session_file = day_dir / "rollout-test.jsonl"
    lines = [
        {
            "timestamp": "2026-07-30T07:03:13.713Z",
            "type": "session_meta",
            "payload": {"session_id": "019fb1c3", "cwd": "C:\\fake\\proj", "timestamp": "2026-07-30T07:03:13.368Z"},
        },
        {
            "timestamp": "2026-07-30T07:03:20.283Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30},
                },
                "rate_limits": {"primary": {"used_percent": 21.0}},
            },
        },
    ]
    session_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    records = codex.collect(since=None)
    events = {r["event"] for r in records}
    assert events == {"session_start", "usage", "rate_limit"}
    usage = next(r for r in records if r["event"] == "usage")
    # input_tokens already includes the cached subset upstream -- must not
    # be summed with cached_input_tokens (that would double count it).
    assert usage["input_tokens"] == 100
    assert usage["cached_input_tokens"] == 20
    assert usage["output_tokens"] == 30
    rate = next(r for r in records if r["event"] == "rate_limit")
    assert rate["limit_remaining"] == 79.0
    assert rate["limit_kind"] == "percent"


def test_codex_collect_missing_dir_returns_empty(fake_home: Path) -> None:
    from harness.collectors import codex

    assert codex.collect(since=None) == []


# --- gemini collector: fixture-based ----------------------------------------


def test_gemini_collect_from_synthetic_history(fake_home: Path) -> None:
    from harness.collectors import gemini

    hist_dir = fake_home / ".gemini" / "antigravity-cli"
    hist_dir.mkdir(parents=True)
    history = hist_dir / "history.jsonl"
    lines = [
        {"display": "some prompt text", "timestamp": 1780273535554, "workspace": "C:\\fake\\proj", "conversationId": "conv-1"},
        {"display": "second prompt", "timestamp": 1780273999999, "workspace": "C:\\fake\\proj", "conversationId": "conv-1"},
    ]
    history.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    records = gemini.collect(since=None)
    assert len(records) == 1  # dedup by conversationId
    assert records[0]["event"] == "session_start"
    assert records[0]["session_id"] == "conv-1"
    assert "display" not in records[0]
    for value in records[0].values():
        if isinstance(value, str):
            assert "prompt" not in value  # prompt text must never leak into a record


def test_gemini_collect_missing_file_returns_empty(fake_home: Path) -> None:
    from harness.collectors import gemini

    assert gemini.collect(since=None) == []
