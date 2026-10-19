"""Tests for harness.telemetry_store: schema 1 and 2 validation, JSONL append,
month-sharding, and tolerant reading.

Every test monkeypatches HOME/USERPROFILE to `tmp_path` so nothing here
ever touches the real `~/.agent-harness/telemetry/` directory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import telemetry_store


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_telemetry_dir_is_under_home(fake_home: Path) -> None:
    assert telemetry_store.telemetry_dir() == fake_home / ".agent-harness" / "telemetry"


def test_append_events_creates_dir_and_month_shard(fake_home: Path) -> None:
    now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    written = telemetry_store.append_events(
        [{"agent": "claude-code", "event": "session_start"}], now=now
    )
    assert written == 1
    shard = fake_home / ".agent-harness" / "telemetry" / "events-202608.jsonl"
    assert shard.exists()
    lines = shard.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_append_events_auto_fills_schema_and_ts(fake_home: Path) -> None:
    now = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    telemetry_store.append_events([{"agent": "codex", "event": "usage"}], now=now)
    [rec] = list(telemetry_store.iter_events())
    assert rec["schema"] == telemetry_store.SCHEMA_VERSION == 2
    assert rec["ts"] == "2026-08-06T12:00:00Z"


def test_append_events_rejects_unknown_agent(fake_home: Path) -> None:
    with pytest.raises(ValueError, match="agent"):
        telemetry_store.append_events([{"agent": "chatgpt", "event": "usage"}])


def test_append_events_rejects_unknown_event(fake_home: Path) -> None:
    with pytest.raises(ValueError, match="event"):
        telemetry_store.append_events([{"agent": "codex", "event": "world_domination"}])


def test_append_events_rejects_unsupported_schema_version(fake_home: Path) -> None:
    with pytest.raises(ValueError, match="schema"):
        telemetry_store.append_events(
            [{"schema": 99, "agent": "codex", "event": "usage"}]
        )


def test_append_events_rejects_limit_remaining_without_kind(fake_home: Path) -> None:
    with pytest.raises(ValueError, match="limit_kind"):
        telemetry_store.append_events(
            [{"agent": "codex", "event": "rate_limit", "limit_remaining": 50}]
        )


def test_append_events_accepts_limit_remaining_with_kind(fake_home: Path) -> None:
    written = telemetry_store.append_events(
        [{"agent": "codex", "event": "rate_limit", "limit_remaining": 50, "limit_kind": "percent"}]
    )
    assert written == 1


def test_append_events_all_or_nothing_on_invalid_batch(fake_home: Path) -> None:
    with pytest.raises(ValueError):
        telemetry_store.append_events([
            {"agent": "codex", "event": "usage"},
            {"agent": "not-an-agent", "event": "usage"},
        ])
    assert list(telemetry_store.iter_events()) == []


def test_append_events_preserves_unknown_extra_fields(fake_home: Path) -> None:
    telemetry_store.append_events(
        [{"agent": "codex", "event": "usage", "future_field": {"nested": True}}]
    )
    [rec] = list(telemetry_store.iter_events())
    assert rec["future_field"] == {"nested": True}


def test_iter_events_skips_malformed_lines(fake_home: Path) -> None:
    telemetry_store.append_events([{"agent": "codex", "event": "usage"}])
    shard = telemetry_store.telemetry_dir() / f"events-{datetime.now(timezone.utc):%Y%m}.jsonl"
    with shard.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write("[1, 2, 3]\n")  # valid JSON, not an object
        fh.write("\n")  # blank line
        fh.write('{"agent": "codex", "event": "usage", "schema": 99}\n')  # unknown schema
    records = list(telemetry_store.iter_events())
    assert len(records) == 1


def test_iter_events_missing_dir_returns_empty(fake_home: Path) -> None:
    assert list(telemetry_store.iter_events()) == []


def test_iter_events_filters_by_agent_and_project(fake_home: Path) -> None:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    telemetry_store.append_events([
        {"agent": "codex", "event": "usage", "project": "proj-a"},
        {"agent": "gemini", "event": "usage", "project": "proj-a"},
        {"agent": "codex", "event": "usage", "project": "proj-b"},
    ], now=now)

    codex_only = list(telemetry_store.iter_events(agent="codex"))
    assert len(codex_only) == 2

    proj_a_only = list(telemetry_store.iter_events(project="proj-a"))
    assert len(proj_a_only) == 2

    both = list(telemetry_store.iter_events(agent="codex", project="proj-b"))
    assert len(both) == 1


def test_iter_events_filters_by_since(fake_home: Path) -> None:
    telemetry_store.append_events(
        [{"agent": "codex", "event": "usage", "ts": "2026-01-01T00:00:00Z"}]
    )
    telemetry_store.append_events(
        [{"agent": "codex", "event": "usage", "ts": "2026-06-01T00:00:00Z"}]
    )
    since = datetime(2026, 3, 1, tzinfo=timezone.utc)
    recent = list(telemetry_store.iter_events(since=since))
    assert len(recent) == 1
    assert recent[0]["ts"] == "2026-06-01T00:00:00Z"


def test_format_and_parse_ts_roundtrip() -> None:
    dt = datetime(2026, 8, 6, 4, 44, 12, tzinfo=timezone.utc)
    s = telemetry_store.format_ts(dt)
    assert s.endswith("Z")
    assert telemetry_store.parse_ts(s) == dt


def test_validate_record_does_not_mutate_input(fake_home: Path) -> None:
    original = {"agent": "codex", "event": "usage"}
    telemetry_store.validate_record(original)
    assert original == {"agent": "codex", "event": "usage"}


# --- schema 2 -------------------------------------------------------------------


def test_schema_1_records_are_still_read_beside_schema_2(fake_home: Path) -> None:
    shard = telemetry_store.telemetry_dir() / "events-202608.jsonl"
    shard.parent.mkdir(parents=True)
    shard.write_text(
        '{"schema": 1, "ts": "2026-08-06T00:00:00Z", "agent": "codex", "event": "usage", "input_tokens": 5}\n'
        '{"schema": 2, "ts": "2026-08-07T00:00:00Z", "agent": "codex", "event": "usage", "input_tokens": 9,'
        ' "cache_read_tokens": 4, "machine": "m-abc"}\n',
        encoding="utf-8",
    )
    records = list(telemetry_store.iter_events())
    assert [r["schema"] for r in records] == [1, 2]
    assert records[1]["cache_read_tokens"] == 4


def test_validate_record_accepts_a_schema_1_record(fake_home: Path) -> None:
    rec = telemetry_store.validate_record({"schema": 1, "agent": "codex", "event": "usage"})
    assert rec["schema"] == 1


@pytest.mark.parametrize("field", ["cache_write_tokens", "cache_read_tokens", "tool_calls", "harness_tool_calls"])
@pytest.mark.parametrize("bad", [-1, 1.5, "3", True])
def test_validate_record_rejects_bad_counts(fake_home: Path, field: str, bad: object) -> None:
    with pytest.raises(ValueError, match=field):
        telemetry_store.validate_record({"agent": "codex", "event": "usage", field: bad})


def test_validate_record_rejects_non_string_machine(fake_home: Path) -> None:
    with pytest.raises(ValueError, match="machine"):
        telemetry_store.validate_record({"agent": "codex", "event": "usage", "machine": 7})


def test_machine_identity_is_created_once_and_random(fake_home: Path) -> None:
    import platform

    assert telemetry_store.machine_identity(create=False) is None
    first = telemetry_store.machine_identity()
    again = telemetry_store.machine_identity()
    assert first["id"] == again["id"]
    assert first["id"].startswith("m-")
    assert platform.node().lower() not in first["id"].lower()
    assert telemetry_store.machine_path() == fake_home / ".agent-harness" / "machine.json"

    # A second machine (another home) gets its own id.
    (fake_home / ".agent-harness" / "machine.json").unlink()
    assert telemetry_store.machine_identity()["id"] != first["id"]


def test_machine_label_set_and_cleared(fake_home: Path) -> None:
    ident = telemetry_store.set_machine_label("  laptop ")
    assert ident["label"] == "laptop"
    assert telemetry_store.machine_identity()["label"] == "laptop"
    assert telemetry_store.machine_identity()["id"] == ident["id"]
    assert "label" not in telemetry_store.set_machine_label("")


def test_machine_identity_recreated_when_file_is_corrupt(fake_home: Path) -> None:
    path = telemetry_store.machine_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert telemetry_store.machine_identity(create=False) is None
    assert telemetry_store.machine_identity()["id"].startswith("m-")
