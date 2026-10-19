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


def _assistant_line(msg_id: str | None, ts: str, block: str, output_tokens: int = 50) -> dict:
    message: dict = {
        "model": "claude-sonnet-5-5",
        "content": [{"type": block}],
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 1000,
            "output_tokens": output_tokens,
        },
    }
    if msg_id is not None:
        message["id"] = msg_id
    return {"type": "assistant", "sessionId": "sess-1", "cwd": "C:\\fake\\project",
            "timestamp": ts, "message": message}


def _write_session(fake_home: Path, lines: list[dict]) -> None:
    project_dir = fake_home / ".claude" / "projects" / "C--fake-project"
    project_dir.mkdir(parents=True)
    (project_dir / "session-1.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def _usage(records: list[dict]) -> list[dict]:
    return [r for r in records if r["event"] == "usage"]


def test_claude_code_counts_a_message_split_across_lines_once(fake_home: Path) -> None:
    # Claude Code writes one API response as one JSONL line per content block
    # (thinking, then tool_use), and every line repeats the message's full usage.
    from harness.collectors import claude_code

    _write_session(fake_home, [
        _assistant_line("msg_1", "2026-10-12T13:00:00.000Z", "thinking"),
        _assistant_line("msg_1", "2026-10-12T13:00:00.100Z", "tool_use"),
        _assistant_line("msg_2", "2026-10-12T13:00:05.000Z", "text", output_tokens=7),
    ])

    usage = _usage(claude_code.collect(since=None))
    assert len(usage) == 2
    assert sum(r["input_tokens"] for r in usage) == 2 * 1102
    assert sum(r["output_tokens"] for r in usage) == 50 + 7


def test_claude_code_since_does_not_recount_a_message_seen_before_it(fake_home: Path) -> None:
    # A collect that ran between a message's two lines already counted it; the
    # later line must not be counted again on the next, watermarked collect.
    from harness.collectors import claude_code

    _write_session(fake_home, [
        _assistant_line("msg_1", "2026-10-12T13:00:00.000Z", "thinking"),
        _assistant_line("msg_1", "2026-10-12T13:00:02.000Z", "tool_use"),
    ])

    since = telemetry_store.parse_ts("2026-10-12T13:00:01Z")
    assert _usage(claude_code.collect(since=since)) == []


def test_claude_code_lines_without_message_id_are_each_counted(fake_home: Path) -> None:
    # Nothing to deduplicate on: keep the pre-existing one-record-per-line behaviour.
    from harness.collectors import claude_code

    _write_session(fake_home, [
        _assistant_line(None, "2026-10-12T13:00:00.000Z", "text"),
        _assistant_line(None, "2026-10-12T13:00:01.000Z", "text"),
    ])

    assert len(_usage(claude_code.collect(since=None))) == 2


def _write_subagent(fake_home: Path, rel: str, lines: list[dict]) -> None:
    path = fake_home / ".claude" / "projects" / "C--fake-project" / "session-1" / "subagents" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def _sub_line(msg_id: str, ts: str, agent_id: str) -> dict:
    line = _assistant_line(msg_id, ts, "text", output_tokens=11)
    line["agentId"] = agent_id
    line["isSidechain"] = True
    return line


def test_claude_code_counts_subagent_transcripts(fake_home: Path) -> None:
    # Subagent transcripts live beside the session, under
    # <session-id>/subagents/ (and subagents/workflows/<run>/), and carry the
    # parent's sessionId. Their usage is real spend for that session.
    from harness.collectors import claude_code

    _write_session(fake_home, [_assistant_line("msg_1", "2026-10-12T13:00:00.000Z", "text")])
    _write_subagent(fake_home, "agent-a1.jsonl", [_sub_line("msg_s1", "2026-10-12T13:00:01.000Z", "a1")])
    _write_subagent(fake_home, "workflows/wf_1/agent-b2.jsonl",
                    [_sub_line("msg_s2", "2026-10-12T13:00:02.000Z", "b2")])
    _write_subagent(fake_home, "agent-a1.meta.json", [{"not": "a transcript"}])
    _write_subagent(fake_home, "workflows/wf_1/journal.jsonl",
                    [_sub_line("msg_journal", "2026-10-12T13:00:03.000Z", "j")])

    records = claude_code.collect(since=None)
    usage = _usage(records)
    assert sorted(r.get("subagent_id", "") for r in usage) == ["", "a1", "b2"]
    assert sum(r["output_tokens"] for r in usage) == 50 + 11 + 11
    assert all(r["session_id"] == "sess-1" for r in usage)
    # A subagent is part of its parent's session, not a session of its own.
    assert [r["event"] for r in records].count("session_start") == 1


def test_claude_code_message_in_parent_and_subagent_counted_once(fake_home: Path) -> None:
    from harness.collectors import claude_code

    _write_session(fake_home, [_assistant_line("msg_shared", "2026-10-12T13:00:00.000Z", "text")])
    _write_subagent(fake_home, "agent-a1.jsonl", [_sub_line("msg_shared", "2026-10-12T13:00:00.000Z", "a1")])

    assert len(_usage(claude_code.collect(since=None))) == 1


def test_claude_code_subagents_without_parent_file_are_still_counted(fake_home: Path) -> None:
    from harness.collectors import claude_code

    _write_subagent(fake_home, "agent-a1.jsonl", [_sub_line("msg_s1", "2026-10-12T13:00:01.000Z", "a1")])

    records = claude_code.collect(since=None)
    assert [r.get("subagent_id") for r in _usage(records)] == ["a1"]
    assert [r["event"] for r in records].count("session_start") == 0


def test_claude_code_line_with_unicode_line_separator_is_not_split(fake_home: Path) -> None:
    # JSON.stringify leaves U+2028 unescaped inside strings; JSONL records are
    # separated by "\n" only, so the line must survive intact.
    from harness.collectors import claude_code

    line = _assistant_line("msg_1", "2026-10-12T13:00:00.000Z", "text")
    line["message"]["content"] = [{"type": "text", "text": "before after"}]
    project_dir = fake_home / ".claude" / "projects" / "C--fake-project"
    project_dir.mkdir(parents=True)
    (project_dir / "session-1.jsonl").write_text(
        json.dumps(line, ensure_ascii=False) + "\r\n", encoding="utf-8")

    assert len(_usage(claude_code.collect(since=None))) == 1


def test_claude_code_long_windows_paths_get_the_extended_prefix() -> None:
    # Deep cwd names push subagent transcripts past MAX_PATH (260); without
    # long-path support enabled, a plain open raises and the file was skipped.
    from harness.collectors import claude_code

    long_path = Path("C:\\") / ("x" * 250) / "agent-a1.jsonl"
    short_path = Path("C:\\short\\agent-a1.jsonl")
    if claude_code.os.name == "nt":
        assert claude_code._io_path(long_path).startswith("\\\\?\\")
    else:
        assert claude_code._io_path(long_path) == str(long_path)
    assert claude_code._io_path(short_path) == str(short_path)


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


# --- claude_code collector: schema 2 ------------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _assistant(mid: str, ts: str, *, content=None, usage=None, model="claude-opus-5-5", session="s-1"):
    return {
        "type": "assistant",
        "sessionId": session,
        "cwd": "C:/fake/proj",
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": model,
            "content": content or [],
            "usage": usage if usage is not None else {
                "input_tokens": 3,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 20,
            },
        },
    }


def _tool(name: str) -> dict:
    return {"type": "tool_use", "id": "toolu_x", "name": name, "input": {}}


def test_claude_code_splits_cache_and_keeps_input_sum(fake_home: Path) -> None:
    from harness.collectors import claude_code

    _write_jsonl(fake_home / ".claude" / "projects" / "C--p" / "s-1.jsonl", [
        _assistant("msg_1", "2026-10-01T00:00:00Z"),
    ])
    [usage] = [r for r in claude_code.collect() if r["event"] == "usage"]
    assert usage["input_tokens"] == 1103  # schema-1 meaning unchanged
    assert usage["cache_write_tokens"] == 100
    assert usage["cache_read_tokens"] == 1000
    assert usage["output_tokens"] == 20
    telemetry_store.validate_record(usage)


def test_claude_code_dedupes_usage_per_message_id_and_counts_tools_on_every_line(fake_home: Path) -> None:
    """One response written as three lines: thinking, text, then the tool call.

    The usage block repeats on each line and must count once; the tool_use
    sits on the third line and must still be credited to that message.
    """
    from harness.collectors import claude_code

    _write_jsonl(fake_home / ".claude" / "projects" / "C--p" / "s-1.jsonl", [
        _assistant("msg_1", "2026-10-01T00:00:00Z", content=[{"type": "thinking", "thinking": ""}]),
        _assistant("msg_1", "2026-10-01T00:00:01Z", content=[{"type": "text", "text": "x"}]),
        _assistant("msg_1", "2026-10-01T00:00:02Z", content=[
            _tool("mcp__agent-harness__memory_remember"),
            _tool("mcp__agent-harness__memory_context"),
            _tool("Read"),
        ]),
        _assistant("msg_2", "2026-10-01T00:01:00Z", content=[_tool("Bash")]),
    ])
    usages = [r for r in claude_code.collect() if r["event"] == "usage"]
    assert len(usages) == 2
    first, second = usages
    assert first["ts"] == "2026-10-01T00:00:00Z"
    assert first["input_tokens"] == 1103
    assert first["tool_calls"] == 3
    assert first["harness_tool_calls"] == 2
    assert first["harness_tools"] == {"memory_remember": 1, "memory_context": 1}
    assert second["tool_calls"] == 1
    assert second["harness_tool_calls"] == 0
    assert "harness_tools" not in second


def test_claude_code_reads_subagent_transcripts_as_part_of_the_session(fake_home: Path) -> None:
    from harness.collectors import claude_code

    project = fake_home / ".claude" / "projects" / "C--p"
    _write_jsonl(project / "s-1.jsonl", [_assistant("msg_main", "2026-10-01T00:00:00Z")])
    _write_jsonl(project / "s-1" / "subagents" / "workflows" / "wf_1" / "agent-a1.jsonl", [
        _assistant("msg_sub", "2026-10-01T00:00:05Z", session="s-1"),
        _assistant("msg_main", "2026-10-01T00:00:06Z", session="s-1"),  # same id as main: counted once
    ])
    usages = [r for r in claude_code.collect() if r["event"] == "usage"]
    assert len(usages) == 2
    main, sub = usages
    assert "subagent" not in main
    assert sub["subagent"] is True
    assert {r["session_id"] for r in usages} == {"s-1"}


def test_claude_code_skips_synthetic_messages(fake_home: Path) -> None:
    from harness.collectors import claude_code

    _write_jsonl(fake_home / ".claude" / "projects" / "C--p" / "s-1.jsonl", [
        _assistant("msg_1", "2026-10-01T00:00:00Z", model="<synthetic>",
                   usage={"input_tokens": 0, "output_tokens": 0}),
    ])
    assert [r for r in claude_code.collect() if r["event"] == "usage"] == []


def test_claude_code_since_never_reemits_an_id_first_seen_earlier(fake_home: Path) -> None:
    from datetime import datetime, timezone

    from harness.collectors import claude_code

    _write_jsonl(fake_home / ".claude" / "projects" / "C--p" / "s-1.jsonl", [
        _assistant("msg_1", "2026-10-01T00:00:00Z"),
        _assistant("msg_1", "2026-10-01T00:00:09Z", content=[_tool("Read")]),  # straddles the watermark
        _assistant("msg_2", "2026-10-01T00:00:10Z"),
    ])
    since = datetime(2026, 10, 1, 0, 0, 5, tzinfo=timezone.utc)
    usages = [r for r in claude_code.collect(since) if r["event"] == "usage"]
    assert [r["ts"] for r in usages] == ["2026-10-01T00:00:10Z"]


def test_claude_code_skips_files_untouched_since_the_watermark(fake_home: Path) -> None:
    import os
    from datetime import datetime, timezone

    from harness.collectors import claude_code

    path = _write_jsonl(fake_home / ".claude" / "projects" / "C--p" / "s-1.jsonl", [
        _assistant("msg_1", "2030-01-01T00:00:00Z"),  # timestamp after `since`, file mtime before it
    ])
    old = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(path, (old, old))
    assert claude_code.collect(datetime(2026, 6, 1, tzinfo=timezone.utc)) == []
    assert len(claude_code.collect(None)) == 2  # session_start + usage


def test_codex_maps_cached_input_onto_the_schema_2_cache_fields(fake_home: Path) -> None:
    from harness.collectors import codex

    _write_jsonl(fake_home / ".codex" / "sessions" / "2026" / "10" / "01" / "rollout-x.jsonl", [
        {"timestamp": "2026-10-01T00:00:00Z", "type": "session_meta",
         "payload": {"session_id": "c-1", "cwd": "C:/p", "timestamp": "2026-10-01T00:00:00Z"}},
        {"timestamp": "2026-10-01T00:00:01Z", "type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                          "cache_write_input_tokens": 10, "output_tokens": 5}},
        }},
    ])
    [usage] = [r for r in codex.collect() if r["event"] == "usage"]
    assert usage["input_tokens"] == 100  # cached part stays inside the total
    assert usage["cache_read_tokens"] == 60
    assert usage["cached_input_tokens"] == 60
    assert usage["cache_write_tokens"] == 10
    telemetry_store.validate_record(usage)


def test_claude_code_skips_history_inherited_by_a_resumed_session(fake_home: Path) -> None:
    """A resumed session's file starts with its parent's lines, tagged with
    the parent's id; only the parent may count them."""
    from harness.collectors import claude_code

    project = fake_home / ".claude" / "projects" / "C--p"
    _write_jsonl(project / "parent.jsonl", [
        _assistant("msg_p1", "2026-10-01T00:00:00Z", session="parent"),
        _assistant("msg_p2", "2026-10-01T00:01:00Z", session="parent"),
    ])
    _write_jsonl(project / "child.jsonl", [
        _assistant("msg_p1", "2026-10-01T00:00:00Z", session="parent"),
        _assistant("msg_p2", "2026-10-01T00:01:00Z", session="parent"),
        _assistant("msg_c1", "2026-10-02T00:00:00Z", session="child"),
    ])
    records = claude_code.collect()
    usage = [(r["session_id"], r["ts"]) for r in records if r["event"] == "usage"]
    assert sorted(usage) == [
        ("child", "2026-10-02T00:00:00Z"),
        ("parent", "2026-10-01T00:00:00Z"),
        ("parent", "2026-10-01T00:01:00Z"),
    ]
    starts = {r["session_id"]: r["ts"] for r in records if r["event"] == "session_start"}
    assert starts == {"parent": "2026-10-01T00:00:00Z", "child": "2026-10-02T00:00:00Z"}
