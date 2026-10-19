"""The one-shot refresh notice: threshold, transcript reading, hook output.

Runs under conftest's isolated home, so refresh state never reaches the
operator's real ~/.agent-harness.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import context_refresh as cr
from hooks.claude import handoff_capture_hook as capture_hook

SESSION = "sess-1"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (cr.THRESHOLD_ENV, cr.REREAD_ENV):
        monkeypatch.delenv(name, raising=False)


def _call(msg_id: str, read: int, session: str = SESSION, sidechain: bool = False,
          write: int = 0, model: str = "claude-opus-5-5") -> dict:
    return {"type": "assistant", "sessionId": session, "isSidechain": sidechain,
            "message": {"id": msg_id, "role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 1, "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": read, "output_tokens": 10}}}


def _transcript(tmp_path: Path, calls: list[dict], name: str = SESSION) -> Path:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("".join(json.dumps(c) + "\n" for c in calls), encoding="utf-8")
    return path


def _record_starts(contexts: list[int], when: datetime = NOW) -> None:
    for i, ctx in enumerate(contexts):
        cr._write_json(cr.state_dir() / "starts" / f"s{i}.json",
                       {"context": ctx, "ts": when.isoformat(timespec="seconds")})


# --- threshold ---------------------------------------------------------------


def test_without_history_the_default_start_gives_a_lean_threshold() -> None:
    value, basis = cr.threshold(NOW)
    # 2.0 x (40k default start + 25k re-read) = 130k
    assert value == 130_000
    assert "default fresh start 40k" in basis


def test_the_threshold_follows_this_machines_recorded_fresh_starts() -> None:
    _record_starts([86_000, 87_000, 88_000])
    value, basis = cr.threshold(NOW)
    # 2.0 x (87k median + 25k) = 224k, rounded to 220k
    assert value == 220_000
    assert "median fresh start 87k over 3 sessions" in basis


def test_starts_older_than_the_window_and_resumed_starts_are_ignored() -> None:
    _record_starts([300_000, 300_000, 300_000], when=NOW - timedelta(days=cr.MAX_AGE_DAYS + 1))
    cr._write_json(cr.state_dir() / "starts" / "resumed.json",
                   {"context": 0, "ts": NOW.isoformat(timespec="seconds")})
    assert cr.fresh_starts(NOW) == []
    assert cr.threshold(NOW)[0] == 130_000


def test_the_threshold_is_clamped() -> None:
    _record_starts([300_000] * 3)
    assert cr.threshold(NOW)[0] == cr.MAX_THRESHOLD


def test_environment_fixes_or_disables_the_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cr.THRESHOLD_ENV, "180k")
    assert cr.threshold(NOW) == (180_000, f"set by {cr.THRESHOLD_ENV}")
    monkeypatch.setenv(cr.THRESHOLD_ENV, "off")
    assert cr.threshold(NOW)[0] == 0


def test_the_reread_estimate_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cr.REREAD_ENV, "75000")
    assert cr.threshold(NOW)[0] == 230_000  # 2.0 x (40k + 75k)


# --- transcript --------------------------------------------------------------


def test_context_is_the_latest_main_loop_call(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [
        _call("m1", 100_000),
        _call("m2", 205_000, write=5_000),
        _call("s1", 900_000, sidechain=True),           # a subagent's own context
        _call("m3", 0, model="<synthetic>"),
    ])
    assert cr.context_tokens(path, SESSION) == 210_001


def test_inherited_history_is_not_this_sessions_context(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [_call("p1", 50_000, session="parent"),
                                  _call("m1", 300_000)])
    assert cr.context_tokens(path, SESSION) == 300_001
    ctx, fresh = cr.first_call(path, SESSION)
    assert ctx == 300_001 and fresh is False


def test_a_fresh_session_reports_its_first_call(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [_call("m1", 87_000), _call("m2", 120_000)])
    assert cr.first_call(path, SESSION) == (87_001, True)


def test_unreadable_transcripts_give_nothing(tmp_path: Path) -> None:
    assert cr.context_tokens(tmp_path / "missing.jsonl", SESSION) is None
    assert cr.first_call(None, SESSION) == (None, False)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{\n", encoding="utf-8")
    assert cr.context_tokens(bad, SESSION) is None


# --- check ---------------------------------------------------------------------


def test_the_notice_fires_once_per_session(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 250_000)])

    first = cr.check(SESSION, path, handoff_saved=True, now=NOW)
    assert first is not None
    assert first.context == 250_001 and first.threshold == 130_000
    assert "250k" in first.message and "130k" in first.message
    assert "hand-off note is saved" in first.message

    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 250_000), _call("m3", 400_000)])
    assert cr.check(SESSION, path, handoff_saved=True, now=NOW) is None


def test_no_notice_below_the_threshold_and_it_can_still_fire_later(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 100_000)])
    assert cr.check(SESSION, path, handoff_saved=True, now=NOW) is None
    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 100_000), _call("m3", 140_000)])
    assert cr.check(SESSION, path, handoff_saved=True, now=NOW) is not None


def test_the_first_check_records_the_fresh_start_once(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [_call("m1", 87_000)])
    cr.check(SESSION, path, handoff_saved=False, now=NOW)
    assert cr.fresh_starts(NOW) == [87_001]
    path = _transcript(tmp_path, [_call("m1", 99_000)])
    cr.check(SESSION, path, handoff_saved=False, now=NOW)
    assert cr.fresh_starts(NOW) == [87_001]


def test_the_notice_says_when_no_handoff_was_saved(tmp_path: Path) -> None:
    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 300_000)])
    notice = cr.check(SESSION, path, handoff_saved=False, now=NOW)
    assert notice is not None and "No hand-off note was saved" in notice.message


def test_a_disabled_threshold_never_fires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cr.THRESHOLD_ENV, "0")
    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 900_000)])
    assert cr.check(SESSION, path, handoff_saved=True, now=NOW) is None


# --- the hook --------------------------------------------------------------------


def _run_hook(monkeypatch, capsys, payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert capture_hook.main() == 0
    return capsys.readouterr().out


def test_the_hook_prints_one_user_only_notice_on_stop(tmp_path: Path, monkeypatch, capsys) -> None:
    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 260_000)])
    payload = {"session_id": SESSION, "cwd": str(tmp_path), "hook_event_name": "Stop",
               "transcript_path": str(path)}

    out = _run_hook(monkeypatch, capsys, payload)
    data = json.loads(out)
    # Only the user-facing field: nothing that would reach the model.
    assert set(data) == {"systemMessage"}
    assert "260k" in data["systemMessage"]

    assert _run_hook(monkeypatch, capsys, payload) == ""


def test_the_hook_says_nothing_on_session_end(tmp_path: Path, monkeypatch, capsys) -> None:
    path = _transcript(tmp_path, [_call("m1", 40_000), _call("m2", 260_000)])
    out = _run_hook(monkeypatch, capsys, {"session_id": SESSION, "cwd": str(tmp_path),
                                          "hook_event_name": "SessionEnd",
                                          "transcript_path": str(path)})
    assert out == ""
