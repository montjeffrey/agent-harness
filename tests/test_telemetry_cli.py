"""Tests for harness.telemetry's `main()` CLI: collect, tail, aggregate.

All tests monkeypatch HOME/USERPROFILE to tmp_path and drive `main([...])`
directly, asserting on captured stdout -- never touching the real
`~/.agent-harness/telemetry/` directory.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
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


def test_collect_persists_watermark_and_skips_duplicates_on_second_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    all_events = [
        {"agent": "codex", "event": "usage", "ts": "2026-08-01T00:00:00Z", "input_tokens": 1},
        {"agent": "codex", "event": "usage", "ts": "2026-08-02T00:00:00Z", "input_tokens": 2},
    ]

    def fake_collect(since):
        # Mirrors the real collector contract: filters inclusively (ts >=
        # since) when a watermark is supplied, same full history otherwise.
        if since is None:
            return list(all_events)
        return [r for r in all_events if telemetry_store.parse_ts(r["ts"]) >= since]

    _install_fake_collector(monkeypatch, "codex", fake_collect)

    rc1 = telemetry.main(["collect", "--agent", "codex"])
    assert rc1 == 0
    assert "total written: 2" in capsys.readouterr().out

    # Second run with the exact same underlying collector data must not
    # re-append anything it already wrote.
    rc2 = telemetry.main(["collect", "--agent", "codex"])
    assert rc2 == 0
    assert "total written: 0" in capsys.readouterr().out

    records = list(telemetry_store.iter_events())
    assert len(records) == 2


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


# --- the watermark file survives a crash mid-save --------------------------
#
# `_save_collect_state` used a bare `write_text`, which truncates before it
# writes, and `_load_collect_state` reads a tolerant `{}` from anything it
# cannot parse. Together those turn a process killed mid-save into the silent
# loss of *every* agent's watermark, not just the one being updated -- and the
# next `collect` re-ingests all history from the beginning.


@contextmanager
def _a_write_that_dies_halfway_through_its_payload() -> Iterator[None]:
    """Make `write_text` land half its data and then fail, for this block only.

    Under the old bare `write_text` the half lands on the state file itself,
    leaving JSON that `_load_collect_state` can only read as "no watermarks
    yet". Under temp-then-replace it lands on a scratch sibling that never
    reaches the destination.

    Scoped to its own `MonkeyPatch` rather than taking the test's: `undo()` on
    the shared one also reverts the autouse `fake_home`, and the rest of the
    test would then run against the developer's real telemetry directory.
    """
    real_write_text = Path.write_text

    def dies_halfway(self: Path, data: str, **kwargs: object) -> int:
        real_write_text(self, data[: len(data) // 2], **kwargs)  # type: ignore[arg-type]
        raise OSError(28, "No space left on device")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "write_text", dies_halfway)
        yield


def test_a_crash_mid_save_costs_the_update_not_the_watermarks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    all_events = [
        {"agent": "codex", "event": "usage", "ts": "2026-08-01T00:00:00Z", "input_tokens": 1},
        {"agent": "codex", "event": "usage", "ts": "2026-08-02T00:00:00Z", "input_tokens": 2},
    ]

    def fake_collect(since):
        if since is None:
            return list(all_events)
        return [r for r in all_events if telemetry_store.parse_ts(r["ts"]) >= since]

    _install_fake_collector(monkeypatch, "codex", fake_collect)

    assert telemetry.main(["collect", "--agent", "codex"]) == 0
    capsys.readouterr()
    saved = telemetry._load_collect_state()
    assert saved, "precondition: the first run must have persisted a watermark"

    with _a_write_that_dies_halfway_through_its_payload():
        with pytest.raises(OSError):
            telemetry._save_collect_state({"codex": "2026-09-09T00:00:00Z"})

    assert telemetry._load_collect_state() == saved, "the failed update ate the file"

    # The consequence that matters: no re-ingestion of history already stored.
    assert telemetry.main(["collect", "--agent", "codex"]) == 0
    assert "total written: 0" in capsys.readouterr().out
    assert len(list(telemetry_store.iter_events())) == 2


def test_a_crash_mid_save_strands_no_temp_file() -> None:
    """Unique temp names cannot self-heal by being overwritten next run.

    Forward protection rather than proof of the fix: against the bare
    `write_text` this passed too, because that wrote no temp at all. It fails
    the day someone reaches for a scratch file without a cleanup path.
    """
    telemetry._save_collect_state({"codex": "2026-08-01T00:00:00Z"})
    directory = telemetry._collect_state_path().parent

    with _a_write_that_dies_halfway_through_its_payload():
        with pytest.raises(OSError):
            telemetry._save_collect_state({"codex": "2026-09-09T00:00:00Z"})

    assert not [p for p in directory.iterdir() if p.name.endswith(".tmp")]


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
