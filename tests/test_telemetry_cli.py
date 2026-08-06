"""Tests for harness.telemetry's `main()` CLI: collect, tail, aggregate.

All tests monkeypatch HOME/USERPROFILE to tmp_path and drive `main([...])`
directly, asserting on captured stdout -- never touching the real
`~/.agent-harness/telemetry/` directory.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import telemetry, telemetry_store


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


# --- collect -----------------------------------------------------------------


def test_collect_with_no_real_logs_exits_zero_cleanly(capsys: pytest.CaptureFixture) -> None:
    rc = telemetry.main(["collect"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total written: 0" in out


def test_collect_single_agent_writes_events(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _install_fake_collector(monkeypatch, "codex", lambda since: [{"agent": "codex", "event": "usage", "input_tokens": 3}])
    rc = telemetry.main(["collect", "--agent", "codex"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "codex: collected 1 record" in out
    assert "total written: 1" in out
    [rec] = list(telemetry_store.iter_events())
    assert rec["input_tokens"] == 3


def test_collect_all_agents_still_exits_zero_when_all_fail(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    def boom(since):
        raise RuntimeError("nope")

    for name in ("claude_code", "codex", "gemini"):
        _install_fake_collector(monkeypatch, name, boom)
    rc = telemetry.main(["collect"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("collector_error") == 0 or "error(s)" in out  # errors are counted, not raised
    # each collector produced exactly one collector_error record, all written
    records = list(telemetry_store.iter_events())
    assert len(records) == 3
    assert all(r["event"] == "collector_error" for r in records)


# --- tail ----------------------------------------------------------------


def test_tail_on_empty_store(capsys: pytest.CaptureFixture) -> None:
    rc = telemetry.main(["tail"])
    assert rc == 0
    assert "no telemetry events" in capsys.readouterr().out


def test_tail_prints_most_recent_n(capsys: pytest.CaptureFixture) -> None:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    telemetry_store.append_events([
        {"agent": "codex", "event": "usage", "ts": "2026-08-01T00:00:00Z", "input_tokens": 1},
        {"agent": "codex", "event": "usage", "ts": "2026-08-02T00:00:00Z", "input_tokens": 2},
        {"agent": "gemini", "event": "session_start", "ts": "2026-08-03T00:00:00Z"},
    ], now=now)
    rc = telemetry.main(["tail", "-n", "2"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    assert "2026-08-02" in out[0]
    assert "2026-08-03" in out[1]


def test_tail_filters_by_agent_and_project(capsys: pytest.CaptureFixture) -> None:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    telemetry_store.append_events([
        {"agent": "codex", "event": "usage", "project": "proj-a"},
        {"agent": "gemini", "event": "usage", "project": "proj-a"},
    ], now=now)
    rc = telemetry.main(["tail", "--agent", "codex"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "codex" in out
    assert "gemini" not in out


# --- aggregate -------------------------------------------------------------


def test_aggregate_on_empty_store(capsys: pytest.CaptureFixture) -> None:
    rc = telemetry.main(["aggregate"])
    assert rc == 0
    assert "no telemetry events" in capsys.readouterr().out


def test_aggregate_by_agent_with_mixed_completeness_records(capsys: pytest.CaptureFixture) -> None:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    telemetry_store.append_events([
        {"agent": "codex", "event": "usage", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
        {"agent": "codex", "event": "usage"},  # no tokens at all -- must not crash
        {"agent": "codex", "event": "rate_limit", "limit_remaining": 80, "limit_kind": "percent"},
        {"agent": "gemini", "event": "session_start"},
    ], now=now)
    rc = telemetry.main(["aggregate", "--by", "agent"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "agent=codex: events=3 input_tokens=10 output_tokens=5" in out
    assert "cost_usd=0.0100 (reported, n=1)" in out
    assert "latest_limit_remaining=80 (percent)" in out
    assert "agent=gemini: events=1" in out
    assert "cost_usd=(none reported)" in out


def test_aggregate_by_project(capsys: pytest.CaptureFixture) -> None:
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    telemetry_store.append_events([
        {"agent": "codex", "event": "usage", "project": "proj-a", "input_tokens": 1},
        {"agent": "gemini", "event": "usage", "project": "proj-b", "input_tokens": 2},
    ], now=now)
    rc = telemetry.main(["aggregate", "--by", "project"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "project=proj-a" in out
    assert "project=proj-b" in out


def test_aggregate_since_filters_older_events(capsys: pytest.CaptureFixture) -> None:
    telemetry_store.append_events([{"agent": "codex", "event": "usage", "ts": "2026-01-01T00:00:00Z"}])
    telemetry_store.append_events([{"agent": "codex", "event": "usage", "ts": "2026-06-01T00:00:00Z"}])
    rc = telemetry.main(["aggregate", "--since", "2026-03-01T00:00:00Z"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "events=1" in out


def test_aggregate_rejects_bad_since() -> None:
    with pytest.raises(SystemExit):
        telemetry.main(["aggregate", "--since", "not-a-timestamp"])
