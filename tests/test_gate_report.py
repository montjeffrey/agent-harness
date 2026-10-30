"""Tests for `harness.gate_report`, the per-server gate usage report.

Inputs are plain dicts in the gate-*.jsonl record format, so these tests do not
depend on `harness.gate_accounting` (only `run_report` imports it, and its test
injects a stand-in module).
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness import gate_report
from harness.telemetry_store import format_ts

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class FakeSpec:
    """Attribute-shaped config value, like `mcp_gate.ServerSpec`."""

    command: str
    args: tuple[str, ...] = ()
    idle_timeout_seconds: float = 600.0


def _rec(event: str, server: str, at: datetime, session: str = "s1", **fields) -> dict:
    return {
        "schema": 1,
        "ts": format_ts(at),
        "event": event,
        "server": server,
        "session": session,
        "client": "claude-code",
        **fields,
    }


def wake(server, at, *, startup_ms=1000, tree_mb=None, session="s1"):
    return _rec(
        "gate_wake", server, at, session, reason="call",
        startup_ms=startup_ms, root_pid=123, tree_mb=tree_mb,
    )


def call(server, at, *, ok=True, session="s1", error_kind=None):
    return _rec(
        "gate_call", server, at, session, tool="t", duration_ms=5,
        ok=ok, error_kind=error_kind,
    )


def sleep(server, at, *, reason="idle", awake_s=60.0, tree_mb=None, leaked=(), session="s1"):
    return _rec(
        "gate_sleep", server, at, session, reason=reason, awake_s=awake_s, calls=1,
        tree_mb=tree_mb, pressure="normal", available_mb=None,
        reaped_pids=[], leaked_pids=list(leaked),
    )


def _one(summaries, server):
    matches = [s for s in summaries if s.server == server]
    assert len(matches) == 1, [s.server for s in summaries]
    return matches[0]


def _recs(summary) -> str:
    return " | ".join(summary.recommendations)


# --- counting and arithmetic -------------------------------------------------


def test_counts_sessions_and_mb_hours():
    t0 = NOW - timedelta(hours=5)
    events = [
        wake("alpha", t0, startup_ms=1000, tree_mb=100.0, session="s1"),
        call("alpha", t0 + timedelta(seconds=1), session="s1"),
        call("alpha", t0 + timedelta(seconds=2), ok=False, error_kind="timeout", session="s2"),
        sleep("alpha", t0 + timedelta(hours=1), awake_s=3600.0, tree_mb=200.0, session="s1"),
        wake("alpha", t0 + timedelta(hours=2), startup_ms=3000, tree_mb=None, session="s3"),
        sleep("alpha", t0 + timedelta(hours=2, minutes=30), awake_s=1800.0, tree_mb=50.0,
              reason="exit", session="s3"),
        _rec("gate_wake_failed", "alpha", t0 + timedelta(hours=3), "s3",
             reason="call", startup_ms=60000, error="boom"),
    ]
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "alpha")
    assert summary.wakes == 2
    assert summary.wake_failures == 1
    assert summary.calls == 2
    assert summary.failed_calls == 1
    assert summary.sessions == 3
    assert summary.awake_s == pytest.approx(5400.0)
    # 200 MB for 1 h plus 50 MB for 0.5 h.
    assert summary.mb_hours == pytest.approx(225.0)
    assert summary.median_startup_ms == pytest.approx(2000.0)
    assert summary.median_tree_mb == pytest.approx(100.0)
    assert summary.sleep_reasons == {"idle": 1, "exit": 1}
    assert summary.leaked == 0


def test_sleep_without_tree_sample_adds_no_mb_hours():
    events = [sleep("alpha", NOW - timedelta(hours=1), awake_s=7200.0, tree_mb=None)]
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "alpha")
    assert summary.mb_hours == 0.0
    assert summary.median_tree_mb is None
    assert summary.median_startup_ms is None


def test_sorted_by_mb_hours_then_name():
    t = NOW - timedelta(hours=2)
    events = [
        sleep("small", t, awake_s=3600.0, tree_mb=10.0),
        sleep("big", t, awake_s=3600.0, tree_mb=500.0),
    ]
    configured = {"zeta": FakeSpec("node"), "beta": FakeSpec("node")}
    names = [s.server for s in gate_report.summarize(events, configured=configured, days=1, now=NOW)]
    assert names == ["big", "small", "beta", "zeta"]


def test_events_outside_the_window_are_ignored():
    events = [
        call("alpha", NOW - timedelta(days=10)),
        call("alpha", NOW - timedelta(days=1)),
    ]
    summary = _one(gate_report.summarize(events, days=7, now=NOW), "alpha")
    assert summary.calls == 1


def test_sweep_records_are_not_a_server():
    events = [_rec("gate_sweep", "*", NOW - timedelta(minutes=1), pids=[1, 2])]
    assert gate_report.summarize(events, days=1, now=NOW) == []


def test_days_must_be_positive():
    with pytest.raises(ValueError):
        gate_report.summarize([], days=0, now=NOW)


# --- configured-but-unused ---------------------------------------------------


def test_configured_server_without_events_appears_with_zeros_and_remove_advice():
    configured = {"idle-one": {"command": "node", "args": ["server.js"]}}
    summary = _one(gate_report.summarize([], configured=configured, days=7, now=NOW), "idle-one")
    assert (summary.wakes, summary.calls, summary.sessions) == (0, 0, 0)
    assert summary.awake_s == 0.0 and summary.mb_hours == 0.0
    assert summary.sleep_reasons == {}
    assert "remove it from gate.json" in _recs(summary)


def test_configured_server_with_calls_gets_no_remove_advice():
    configured = {"busy": FakeSpec("node")}
    events = [wake("busy", NOW - timedelta(hours=1)), call("busy", NOW - timedelta(minutes=59))]
    summary = _one(gate_report.summarize(events, configured=configured, days=7, now=NOW), "busy")
    assert "remove" not in _recs(summary)


def test_unconfigured_server_with_no_calls_gets_no_remove_advice():
    events = [wake("stray", NOW - timedelta(hours=1))]
    summary = _one(gate_report.summarize(events, days=7, now=NOW), "stray")
    assert "remove" not in _recs(summary)


# --- raise idle --------------------------------------------------------------


def _cycles(server, count, gap_s, *, reason="idle", start=None):
    """`count` wake/sleep cycles; each wake comes `gap_s` after the previous sleep."""
    t = start or NOW - timedelta(hours=20)
    out = []
    for _ in range(count):
        out.append(wake(server, t))
        t += timedelta(seconds=60)
        out.append(sleep(server, t, reason=reason))
        t += timedelta(seconds=gap_s)
    return out


def test_frequent_quick_rewakes_recommend_raising_idle():
    configured = {"web": FakeSpec("node", idle_timeout_seconds=300.0)}
    events = _cycles("web", 5, gap_s=400)  # within 2 x 300 s of each idle stop
    summary = _one(gate_report.summarize(events, configured=configured, days=1, now=NOW), "web")
    assert "raise idle_timeout_seconds" in _recs(summary)


def test_rewakes_long_after_sleep_do_not_recommend_raising_idle():
    configured = {"web": FakeSpec("node", idle_timeout_seconds=300.0)}
    events = _cycles("web", 5, gap_s=3600)
    summary = _one(gate_report.summarize(events, configured=configured, days=1, now=NOW), "web")
    assert "idle_timeout_seconds" not in _recs(summary)


def test_few_wakes_per_day_do_not_recommend_raising_idle():
    configured = {"web": FakeSpec("node", idle_timeout_seconds=300.0)}
    events = _cycles("web", 3, gap_s=10)
    summary = _one(gate_report.summarize(events, configured=configured, days=1, now=NOW), "web")
    assert "idle_timeout_seconds" not in _recs(summary)


def test_rewakes_after_exit_sleeps_do_not_recommend_raising_idle():
    # A session ending is not something a longer idle timeout would avoid.
    configured = {"web": FakeSpec("node", idle_timeout_seconds=300.0)}
    events = _cycles("web", 5, gap_s=10, reason="exit")
    summary = _one(gate_report.summarize(events, configured=configured, days=1, now=NOW), "web")
    assert "idle_timeout_seconds" not in _recs(summary)


def test_rewake_pairing_uses_timestamp_order():
    configured = {"web": {"command": "node", "idle_timeout_seconds": 300}}
    events = _cycles("web", 5, gap_s=400)
    events.reverse()  # input order must not matter
    summary = _one(gate_report.summarize(events, configured=configured, days=1, now=NOW), "web")
    assert "raise idle_timeout_seconds" in _recs(summary)


def test_wakes_in_other_sessions_are_not_rewakes():
    # Each session is its own gate process: a longer idle timeout in one cannot
    # avoid the next session's first wake, however soon after it comes.
    configured = {"web": {"command": "node", "idle_timeout_seconds": 300}}
    events = []
    for i in range(5):
        wake_ev, sleep_ev = _cycles(
            "web", 1, gap_s=0, start=NOW - timedelta(hours=10) + timedelta(seconds=400 * i)
        )
        events += [{**wake_ev, "session": f"s{i}"}, {**sleep_ev, "session": f"s{i}"}]
    summary = _one(gate_report.summarize(events, configured=configured, days=1, now=NOW), "web")
    assert "idle_timeout_seconds" not in _recs(summary)


def test_concurrent_sessions_pair_only_their_own_stops():
    # Session a re-wakes quickly after its own idle stop while session b's
    # unrelated stops and wakes interleave; a's re-wakes still count.
    configured = {"web": {"command": "node", "idle_timeout_seconds": 300}}
    a = [{**ev, "session": "a"} for ev in _cycles("web", 5, gap_s=400)]
    b = [
        {**ev, "session": "b"}
        for ev in _cycles("web", 2, gap_s=4000, start=NOW - timedelta(hours=20, seconds=-30))
    ]
    summary = _one(gate_report.summarize(a + b, configured=configured, days=1, now=NOW), "web")
    assert "raise idle_timeout_seconds" in _recs(summary)


def test_unconfigured_server_uses_default_idle_for_rewake_rule():
    events = _cycles("web", 5, gap_s=1000)  # within 2 x the 600 s default
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "web")
    assert "raise idle_timeout_seconds" in _recs(summary)


# --- pin npx packages --------------------------------------------------------


def _slow_wakes(server):
    return [wake(server, NOW - timedelta(hours=h), startup_ms=15000) for h in (1, 2, 3)]


@pytest.mark.parametrize(
    "spec",
    [
        FakeSpec("npx", ("-y", "@playwright/mcp@latest")),
        FakeSpec("npx.cmd", ("-y", "@playwright/mcp")),
        FakeSpec(r"C:\Program Files\nodejs\npx.exe", ("--yes", "some-server")),
        FakeSpec("cmd", ("/c", "npx", "-y", "@scope/pkg@latest")),
        {"command": "npx", "args": ["--package", "srv@latest", "srv"]},
    ],
)
def test_slow_unpinned_npx_recommends_pinning(spec):
    configured = {"slow": spec}
    summary = _one(
        gate_report.summarize(_slow_wakes("slow"), configured=configured, days=1, now=NOW), "slow"
    )
    assert "pin the package version" in _recs(summary)


@pytest.mark.parametrize(
    "spec",
    [
        FakeSpec("npx", ("-y", "@playwright/mcp@1.2.3")),
        FakeSpec("npx", ("-y", "some-server@0.4.0")),
        FakeSpec("node", ("server.js",)),
        FakeSpec("uvx", ("mcp-server-fetch",)),
    ],
)
def test_pinned_or_non_npx_command_gets_no_pin_advice(spec):
    configured = {"slow": spec}
    summary = _one(
        gate_report.summarize(_slow_wakes("slow"), configured=configured, days=1, now=NOW), "slow"
    )
    assert "pin" not in _recs(summary)


def test_fast_unpinned_npx_gets_no_pin_advice():
    configured = {"fast": FakeSpec("npx", ("-y", "@playwright/mcp@latest"))}
    events = [wake("fast", NOW - timedelta(hours=1), startup_ms=4000)]
    summary = _one(gate_report.summarize(events, configured=configured, days=1, now=NOW), "fast")
    assert "pin" not in _recs(summary)


# --- leaks and pressure ------------------------------------------------------


def test_leaked_pids_are_counted_and_flagged():
    events = [
        sleep("leaky", NOW - timedelta(hours=2), leaked=[11, 12]),
        sleep("leaky", NOW - timedelta(hours=1), leaked=[13]),
    ]
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "leaky")
    assert summary.leaked == 3
    assert "leaves processes behind" in _recs(summary)


def test_clean_stops_are_not_flagged_as_leaking():
    events = [sleep("clean", NOW - timedelta(hours=1), leaked=[])]
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "clean")
    assert summary.leaked == 0
    assert "leaves processes behind" not in _recs(summary)


def test_mostly_pressure_stops_flag_short_ram():
    events = [
        sleep("hog", NOW - timedelta(hours=3), reason="pressure"),
        sleep("hog", NOW - timedelta(hours=2), reason="pressure"),
        sleep("hog", NOW - timedelta(hours=1), reason="idle"),
    ]
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "hog")
    assert "short on RAM" in _recs(summary)


def test_minority_pressure_stops_are_not_flagged():
    events = [
        sleep("hog", NOW - timedelta(hours=3), reason="pressure"),
        sleep("hog", NOW - timedelta(hours=2), reason="idle"),
        sleep("hog", NOW - timedelta(hours=1), reason="idle"),
    ]
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "hog")
    assert "short on RAM" not in _recs(summary)


# --- malformed input ---------------------------------------------------------


def test_malformed_records_are_skipped_not_raised(capsys):
    t = NOW - timedelta(hours=1)
    good = call("alpha", t)
    events = [
        "not a dict",
        None,
        {**good, "server": 7},
        {k: v for k, v in good.items() if k != "server"},
        {**good, "ts": "yesterday"},
        {**good, "ts": None},
        {**good, "schema": 2},
        {**good, "session": 5},
        {**good, "ok": "yes"},
        {k: v for k, v in good.items() if k != "ok"},
        {**wake("alpha", t), "startup_ms": "fast"},
        {**wake("alpha", t), "startup_ms": True},
        {**wake("alpha", t), "tree_mb": "big"},
        {**sleep("alpha", t), "awake_s": None},
        {**sleep("alpha", t), "awake_s": float("nan")},
        {**sleep("alpha", t), "leaked_pids": "1,2"},
        {**sleep("alpha", t), "leaked_pids": [1, "2"]},
        {**sleep("alpha", t), "reason": None},
        {**sleep("alpha", t), "tree_mb": float("inf")},
        good,
        _rec("gate_something_new", "alpha", t),
    ]
    summary = _one(gate_report.summarize(events, days=1, now=NOW), "alpha")
    assert (summary.calls, summary.wakes, summary.sleep_reasons) == (1, 0, {})
    err = capsys.readouterr().err
    assert "malformed" in err and err.count("\n") == 1


def test_sleep_with_missing_optional_fields_is_accepted():
    t = NOW - timedelta(hours=1)
    rec = {k: v for k, v in sleep("alpha", t).items() if k not in ("tree_mb", "leaked_pids")}
    summary = _one(gate_report.summarize([rec], days=1, now=NOW), "alpha")
    assert summary.sleep_reasons == {"idle": 1}
    assert summary.leaked == 0


def test_naive_now_is_treated_as_utc():
    events = [call("alpha", NOW - timedelta(hours=1))]
    naive = NOW.replace(tzinfo=None)
    summary = _one(gate_report.summarize(events, days=1, now=naive), "alpha")
    assert summary.calls == 1


# --- rendering ---------------------------------------------------------------


def _render_fixture():
    t0 = NOW - timedelta(hours=5)
    events = [
        wake("alpha", t0, startup_ms=1500, tree_mb=100.0, session="s1"),
        call("alpha", t0 + timedelta(seconds=1), session="s1"),
        call("alpha", t0 + timedelta(seconds=2), ok=False, error_kind="timeout", session="s2"),
        sleep("alpha", t0 + timedelta(hours=1), awake_s=3600.0, tree_mb=200.0),
    ]
    configured = {"alpha": FakeSpec("node"), "beta": FakeSpec("node")}
    return gate_report.summarize(events, configured=configured, days=7, now=NOW)


def test_render_text_is_stable():
    expected = (
        "Gate usage over the last 7 days (by MB-hours held)\n"
        "server  wakes  fail  calls   err  sess  warm_min  start_ms  tree_mb     mb_h\n"
        "alpha       1     0      2     1     2      60.0      1500    150.0    200.0\n"
        "beta        0     0      0     0     0       0.0         -        -      0.0\n"
        "\n"
        "Recommendations:\n"
        "  - beta: no calls in the last 7 days: remove it from gate.json"
        " (or turn it off in the client)\n"
    )
    assert gate_report.render_text(_render_fixture(), days=7) == expected


def test_render_text_with_no_servers():
    text = gate_report.render_text([], days=3)
    assert text == "No gate activity recorded in the last 3 days.\n"


def test_render_json_is_stable():
    expected = {
        "servers": [
            {
                "server": "alpha",
                "wakes": 1,
                "wake_failures": 0,
                "calls": 2,
                "failed_calls": 1,
                "sessions": 2,
                "awake_s": 3600.0,
                "median_startup_ms": 1500.0,
                "median_tree_mb": 150.0,
                "mb_hours": 200.0,
                "sleep_reasons": {"idle": 1},
                "leaked": 0,
                "recommendations": [],
            },
            {
                "server": "beta",
                "wakes": 0,
                "wake_failures": 0,
                "calls": 0,
                "failed_calls": 0,
                "sessions": 0,
                "awake_s": 0.0,
                "median_startup_ms": None,
                "median_tree_mb": None,
                "mb_hours": 0.0,
                "sleep_reasons": {},
                "leaked": 0,
                "recommendations": [
                    (
                        "no calls in the last 7 days: remove it from gate.json"
                        " (or turn it off in the client)"
                    )
                ],
            },
        ]
    }
    assert gate_report.render_json(_render_fixture()) == json.dumps(
        expected, indent=2, sort_keys=True
    )


def test_summary_is_frozen():
    summary = _render_fixture()[0]
    with pytest.raises(AttributeError):
        summary.calls = 99


# --- run_report --------------------------------------------------------------


def test_run_report_reads_gate_events_from_accounting(monkeypatch, tmp_path):
    seen: dict = {}

    def fake_iter(since=None, directory=None):
        seen["since"] = since
        seen["directory"] = directory
        yield call("alpha", datetime.now(UTC) - timedelta(minutes=5))

    fake = types.ModuleType("harness.gate_accounting")
    fake.iter_gate_events = fake_iter
    monkeypatch.setitem(sys.modules, "harness.gate_accounting", fake)

    out = gate_report.run_report(
        days=2, as_json=True, configured={"beta": FakeSpec("node")}, directory=tmp_path
    )
    payload = json.loads(out)
    assert [s["server"] for s in payload["servers"]] == ["alpha", "beta"]
    assert seen["directory"] == Path(tmp_path)
    assert datetime.now(UTC) - seen["since"] == pytest.approx(timedelta(days=2), abs=timedelta(minutes=1))

    text = gate_report.run_report(days=2, as_json=False, configured=None, directory=tmp_path)
    assert text.startswith("Gate usage over the last 2 days")
