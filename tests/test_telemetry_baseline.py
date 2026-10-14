"""Tests for harness.telemetry_baseline (rollups and K1-K6), the `baseline`
and `machine` CLI commands, the collect lock and the daily background
collect. All under a tmp home; nothing reads the real transcripts.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import telemetry, telemetry_baseline, telemetry_store

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


def _usage(sid: str, ts: str, *, uncached=0, cw=0, cr=0, out=0, sub=False, harness=None,
           machine="m-a", project="C:/w/proj", agent="claude-code", model="claude-opus-5-5", **extra) -> dict:
    rec = {
        "schema": 2, "agent": agent, "event": "usage", "ts": ts, "session_id": sid, "machine": machine,
        "project": project, "model": model,
        "input_tokens": uncached + cw + cr, "cache_write_tokens": cw, "cache_read_tokens": cr,
        "output_tokens": out, "tool_calls": sum((harness or {}).values()),
        "harness_tool_calls": sum((harness or {}).values()),
    }
    if harness:
        rec["harness_tools"] = harness
    if sub:
        rec["subagent"] = True
    rec.update(extra)
    return rec


def _session(sid: str, turns: int, ctx: int, *, start="2026-10-10T00:00:", sub_turns=0, **kw) -> list[dict]:
    """`turns` main turns (and `sub_turns` subagent turns), each reading `ctx`
    tokens from cache and writing 10 output tokens; the first main turn also
    writes `ctx` to cache instead of reading it."""
    recs = []
    for i in range(turns):
        cw, cr = (ctx, 0) if i == 0 else (0, ctx)
        recs.append(_usage(sid, f"{start}{i:02d}Z", cw=cw, cr=cr, out=10, **kw))
    for i in range(sub_turns):
        recs.append(_usage(sid, f"{start}{30 + i:02d}Z", cr=ctx, out=10, sub=True, **kw))
    return recs


# --- rollup and KPIs ----------------------------------------------------------


def test_percentile_interpolates_like_the_baseline_scan() -> None:
    assert telemetry_baseline.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert telemetry_baseline.percentile([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 0.9) == pytest.approx(91)
    assert telemetry_baseline.percentile([], 0.5) is None


def test_rollup_counts_main_and_subagent_turns_and_first_context() -> None:
    recs = _session("s", 3, 1000, sub_turns=2)
    recs.insert(0, _usage("s", "2026-10-10T00:00:59Z", cr=5, out=1))  # out of file order, later ts
    [s] = telemetry_baseline.rollup(recs)
    assert (s.turns, s.sub_turns) == (4, 2)
    assert s.first_ctx == 1000  # earliest main turn, not first record
    assert s.start == "2026-10-10T00:00:00Z"
    # cache write 1000, cache read 2 x 1000 main + 2 x 1000 subagent + 5,
    # output 10 on each of the five _session turns + 1.
    assert s.total == 1000 + 4 * 1000 + 5 + 5 * 10 + 1
    assert s.context_per_turn == pytest.approx(s.total / 6)


def test_kpis_on_a_known_mix() -> None:
    heavy = _session("heavy", 10, 300_000, harness={"memory_remember": 2})
    light = _session("light", 4, 20_000, harness={"memory_context": 1, "graph_find": 1, "memory_status": 1})
    one_turn = _session("bot", 1, 50_000)  # automated run, below --min-turns
    report = telemetry_baseline.baseline(heavy + light + one_turn, min_turns=3, now=NOW)

    k = report["overall"]
    assert report["sessions_in_window"] == 3
    assert k["sessions"] == 2
    assert k["turns_main"] == 14
    tok = k["tokens"]
    assert tok["cache_write"] == 300_000 + 20_000
    assert tok["cache_read"] == 9 * 300_000 + 3 * 20_000
    assert tok["output"] == 140
    assert tok["input"] == 0
    # The light session's harness calls ride on all four of its turns.
    assert k["harness"]["calls"] == {"memory_context": 4, "graph_find": 4, "memory_status": 4, "memory_remember": 20}

    kpi = k["kpi"]
    heavy_ctx = (10 * 300_000 + 100) / 10
    light_ctx = (4 * 20_000 + 40) / 4
    assert kpi["K1"]["p50"] == pytest.approx((heavy_ctx + light_ctx) / 2)
    assert kpi["K1"]["status"] == "not met"
    assert kpi["K2"]["value"] == pytest.approx(1.0)  # two sessions are all of the top 20
    assert k["concentration"]["top1"] == pytest.approx((3_000_100) / (3_000_100 + 80_040))
    assert kpi["K3"]["value"] == pytest.approx(8 / 2)  # memory_context + graph_*; memory_status is not retrieval
    assert (kpi["K4"]["retrieval"], kpi["K4"]["remember"]) == (8, 20)
    assert kpi["K4"]["status"] == "not met"
    assert kpi["K5"]["p50"] == pytest.approx((300_000 + 20_000) / 2)
    assert kpi["K6"]["value"] == pytest.approx(tok["cache_read"] / (tok["cache_read"] + tok["cache_write"]))
    assert k["heaviest"][0]["turns_main"] == 10


def test_subagent_turns_do_not_count_towards_min_turns() -> None:
    recs = _session("s", 2, 1000, sub_turns=10)
    assert telemetry_baseline.baseline(recs, min_turns=3, now=NOW)["overall"]["sessions"] == 0
    assert telemetry_baseline.baseline(recs, min_turns=2, now=NOW)["overall"]["sessions"] == 1


def test_days_window_and_synthetic_and_non_usage_are_excluded() -> None:
    old = _session("old", 5, 1000, start="2026-08-01T00:00:")
    new = _session("new", 5, 1000)
    noise = [
        _usage("new", "2026-10-10T00:00:50Z", model="<synthetic>"),
        {"schema": 2, "agent": "claude-code", "event": "session_start", "ts": "2026-10-10T00:00:00Z",
         "session_id": "new"},
        _usage("", "2026-10-10T00:00:51Z", cr=10**9),  # no session id: unattributable
    ]
    report = telemetry_baseline.baseline(old + new + noise, days=30, min_turns=3, now=NOW)
    assert report["overall"]["sessions"] == 1
    assert report["overall"]["turns_main"] == 5
    assert telemetry_baseline.baseline(old + new, min_turns=3, now=NOW)["overall"]["sessions"] == 2


def test_schema_1_records_count_as_uncached_and_are_flagged() -> None:
    legacy = [{"schema": 1, "agent": "claude-code", "event": "usage", "ts": f"2026-10-10T00:00:0{i}Z",
               "session_id": "old", "input_tokens": 100, "output_tokens": 1} for i in range(3)]
    k = telemetry_baseline.baseline(legacy, now=NOW)["overall"]
    assert k["tokens"]["input"] == 300
    assert k["unsplit_records"] == 3
    assert k["kpi"]["K6"]["value"] == 0


def test_codex_cache_split_reads_the_same_way() -> None:
    recs = [{"schema": 2, "agent": "codex", "event": "usage", "ts": f"2026-10-10T00:00:0{i}Z",
             "session_id": "c", "input_tokens": 100, "cache_read_tokens": 80, "output_tokens": 5}
            for i in range(3)]
    k = telemetry_baseline.baseline(recs, now=NOW)["overall"]
    assert k["tokens"] == {"input": 60, "cache_write": 0, "cache_read": 240, "output": 15, "total": 315}
    assert k["kpi"]["K6"]["value"] == pytest.approx(0.8)


def test_grouping_by_machine_project_and_month() -> None:
    recs = (
        _session("a1", 3, 1000, machine="m-a", project="git:aaa")
        + _session("b1", 3, 9000, machine="m-b", project="git:aaa", start="2026-09-20T00:00:")
        + _session("b2", 3, 5000, machine="m-b", project="dir:bbb")
    )
    by_machine = telemetry_baseline.baseline(recs, by="machine", now=NOW)["groups"]
    assert list(by_machine) == ["m-b", "m-a"]  # heaviest first
    assert by_machine["m-b"]["sessions"] == 2
    by_project = telemetry_baseline.baseline(recs, by="project", now=NOW, labels={"git:aaa": "proj"})
    assert set(by_project["groups"]) == {"git:aaa", "dir:bbb"}
    assert by_project["groups"]["git:aaa"]["sessions"] == 2
    assert by_project["project_labels"] == {"git:aaa": "proj"}
    by_month = telemetry_baseline.baseline(recs, by="month", now=NOW)["groups"]
    assert {k: v["sessions"] for k, v in by_month.items()} == {"2026-10": 2, "2026-09": 1}
    # Same session id on two machines stays two sessions.
    twin = _session("x", 3, 10, machine="m-a") + _session("x", 3, 10, machine="m-b")
    assert telemetry_baseline.baseline(twin, now=NOW)["overall"]["sessions"] == 2


def test_render_mentions_every_kpi() -> None:
    text = telemetry_baseline.render(
        telemetry_baseline.baseline(_session("s", 4, 2000), by="month", now=NOW)
    )
    for kpi in ("K1", "K2", "K3", "K4", "K5", "K6"):
        assert kpi in text
    assert "month 2026-10" in text


def test_human_formats_like_the_baseline_doc() -> None:
    assert telemetry_baseline.human(4_940_000_000) == "4.94 B"
    assert telemetry_baseline.human(110_000_000) == "110 M"
    assert telemetry_baseline.human(30_200) == "30.2 k"
    assert telemetry_baseline.human(None) == "-"


# --- end to end: transcripts -> collect -> export -> baseline -------------------


def _write_transcript(home: Path, sid: str, turns: int, start: datetime, cwd: str, tool: str | None = None) -> None:
    lines = []
    for i in range(turns):
        ts = (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        usage = {"input_tokens": 1, "cache_creation_input_tokens": 100 if i == 0 else 0,
                 "cache_read_input_tokens": 0 if i == 0 else 1000, "output_tokens": 10}
        base = {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": ts}
        message = {"id": f"msg_{sid}_{i}", "model": "claude-opus-5-5", "usage": usage}
        lines.append({**base, "message": {**message, "content": [{"type": "text", "text": "t"}]}})
        if tool:
            lines.append({**base, "message": {**message, "content": [{"type": "tool_use", "name": tool}]}})
    path = home / ".claude" / "projects" / "C--w-proj" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_collect_export_baseline_end_to_end(isolated_home: Path, tmp_path: Path,
                                            capsys: pytest.CaptureFixture) -> None:
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    _write_transcript(isolated_home, "s-big", 6, recent, "C:/w/proj", tool="mcp__agent-harness__memory_search")
    _write_transcript(isolated_home, "s-small", 3, recent, "C:/w/proj")
    _write_transcript(isolated_home, "s-bot", 1, recent, "C:/w/proj")

    assert telemetry.main(["collect", "--agent", "claude-code"]) == 0
    assert telemetry.main(["collect", "--agent", "claude-code"]) == 0  # no duplicates on a rerun
    stored = [r for r in telemetry_store.iter_events() if r["event"] == "usage"]
    assert len(stored) == 10
    machine = telemetry_store.machine_identity()["id"]
    assert {r["machine"] for r in stored} == {machine}

    export = tmp_path / "OneDrive" / "telemetry"
    assert telemetry.main(["export", "--to", str(export)]) == 0
    out_json = tmp_path / "snap.json"
    capsys.readouterr()
    assert telemetry.main(["baseline", "--from", str(export), "--days", "30", "--min-turns", "3",
                           "--by", "machine", "--json", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert "K1 context/turn" in text

    report = json.loads(out_json.read_text(encoding="utf-8"))
    k = report["overall"]
    assert report["sessions_in_window"] == 3
    assert k["sessions"] == 2
    assert k["turns_main"] == 9
    assert k["tokens"] == {"input": 9, "cache_write": 200, "cache_read": 7000, "output": 90, "total": 7299}
    assert k["kpi"]["K4"]["retrieval"] == 6
    assert k["kpi"]["K3"]["value"] == pytest.approx(3.0)
    assert k["kpi"]["K5"]["p50"] == pytest.approx(101)
    assert list(report["groups"]) == [machine]

    # The local store gives the same answer as the export.
    capsys.readouterr()
    local_json = tmp_path / "local.json"
    assert telemetry.main(["baseline", "--days", "30", "--json", str(local_json)]) == 0
    assert json.loads(local_json.read_text(encoding="utf-8"))["overall"]["tokens"] == k["tokens"]


def test_baseline_on_an_empty_store_says_so(capsys: pytest.CaptureFixture) -> None:
    assert telemetry.main(["baseline"]) == 0
    assert "no sessions match" in capsys.readouterr().out


def test_baseline_rejects_a_missing_from_dir(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        telemetry.main(["baseline", "--from", str(tmp_path / "nope")])


def test_machine_cli_shows_and_labels(capsys: pytest.CaptureFixture) -> None:
    assert telemetry.main(["machine", "--label", "desk"]) == 0
    assert "(desk)" in capsys.readouterr().out
    assert telemetry.main(["machine"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("machine m-") and "(desk)" in out


# --- collect lock and the daily background collect ------------------------------


def test_collect_skips_while_another_holds_the_lock(capsys: pytest.CaptureFixture) -> None:
    lock = telemetry_store.telemetry_dir() / ".collect.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("123", encoding="utf-8")
    assert telemetry.main(["collect"]) == 0
    assert "another collect is running" in capsys.readouterr().out
    assert lock.exists()


def test_collect_clears_a_stale_lock(capsys: pytest.CaptureFixture) -> None:
    lock = telemetry_store.telemetry_dir() / ".collect.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("123", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(lock, (old, old))
    assert telemetry.main(["collect"]) == 0
    assert "total written" in capsys.readouterr().out
    assert not lock.exists()


def test_collect_quiet_prints_nothing(capsys: pytest.CaptureFixture) -> None:
    assert telemetry.main(["collect", "--quiet"]) == 0
    assert capsys.readouterr().out == ""


def test_daily_collect_runs_once_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(telemetry.AUTO_ENV, raising=False)
    started: list[list[str]] = []
    assert telemetry.maybe_start_daily_collect(now=NOW, spawn=started.append) is True
    assert started[0][1:] == [str(Path(telemetry.__file__).resolve()), "collect", "--quiet"]
    assert telemetry.maybe_start_daily_collect(now=NOW + timedelta(hours=19), spawn=started.append) is False
    assert telemetry.maybe_start_daily_collect(now=NOW + timedelta(hours=21), spawn=started.append) is True
    assert len(started) == 2


def test_daily_collect_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(telemetry.AUTO_ENV, "0")
    started: list[list[str]] = []
    assert telemetry.maybe_start_daily_collect(now=NOW, spawn=started.append) is False
    assert started == []


def test_daily_collect_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(telemetry.AUTO_ENV, raising=False)

    def boom(argv):
        raise OSError("cannot spawn")

    assert telemetry.maybe_start_daily_collect(now=NOW, spawn=boom) is False


def test_session_start_hook_honours_the_off_switch() -> None:
    from hooks.claude import memory_context_hook

    assert os.environ[telemetry.AUTO_ENV] == "0"  # set by conftest for every test
    assert memory_context_hook.start_daily_telemetry() is False
    assert not (telemetry_store.telemetry_dir() / ".auto_collect.json").exists()
