"""Tests for automatic hand-off: the capture hook and the SessionStart injection.

What matters: a session's hand-off is built from evidence (working-tree diff,
transcript tool calls, final message), lands as proposed entries the way
`memory_remember` writes them, never carries the user's prompt or a secret,
is written once per session state, and the next session sees it without
asking -- inside its budget, and never at the cost of a session ending.
Every test runs under conftest's isolated home, so nothing here can reach
the real ~/.agent-harness.
"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import handoff, memory_store, work
from hooks.claude import handoff_capture_hook as capture_hook
from hooks.claude import memory_context_hook as start_hook

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc)
SESSION = "aaaa1111-0000-0000-0000-000000000000"
NEXT_SESSION = "bbbb2222-0000-0000-0000-000000000000"
PROMPT = "PLEASE-DO-NOT-RECORD-THIS-PROMPT"

FINAL = """## Done
- Added `helper()` to main.go instead of a new package, because the build has one binary.
- The engine still calls the old path.

### Open questions
- Should `helper` be exported?

ANSWER: judge, Unlanded"""


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (handoff.MAX_CHARS_ENV, start_hook.STRATEGY_ENV, start_hook.MAX_CHARS_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(root, "init")
    _run(root, "config", "user.name", "Test User")
    _run(root, "config", "user.email", "test@example.test")
    _run(root, "config", "core.autocrlf", "false")
    (root / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    _run(root, "add", ".")
    _run(root, "commit", "-m", "initial")
    memory_store.init_project(root, now=NOW)
    return root


def _transcript(tmp_path: Path, repo: Path, final: str = FINAL, tools: list[dict] | None = None) -> Path:
    if tools is None:
        tools = [
            {"name": "Read", "input": {"file_path": str(repo / "main.go")}},
            {"name": "Edit", "input": {"file_path": str(repo / "main.go")}},
            {"name": "Bash", "input": {"command": "go build ./... && go vet ./..."}},
            {"name": "Write", "input": {"file_path": str(repo / "ANSWER.md")}},
        ]
    lines = [{"type": "user", "message": {"role": "user", "content": PROMPT}}]
    for n, tool in enumerate(tools):
        lines.append({"type": "assistant", "message": {"id": f"m{n}", "role": "assistant",
                      "content": [{"type": "tool_use", "id": f"t{n}", **tool}]}})
        lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{n}", "content": "ok"}]}})
    # One API message split over two lines, as Claude Code writes it.
    for text in final.split("\n\n", 1):
        lines.append({"type": "assistant", "message": {"id": "final", "role": "assistant",
                      "content": [{"type": "text", "text": text}]}})
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


def _work(repo: Path) -> None:
    (repo / "main.go").write_text("package main\n\nfunc helper() {}\n\nfunc main() { helper() }\n", encoding="utf-8")
    (repo / "ANSWER.md").write_text("# Findings\n\n`judge()` in landing.go gates cherry on Upstream.\n",
                                    encoding="utf-8")


def _entries(repo: Path) -> list[dict]:
    return list(memory_store.iter_entries(memory_store.project_memory_dir(repo)))


def _part(entries: list[dict], part: str) -> dict:
    [entry] = [e for e in entries if part in e["tags"]]
    return entry


# --- capture ---------------------------------------------------------------


def test_a_session_becomes_one_proposed_group_built_from_evidence(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)

    result = handoff.capture(repo, SESSION, _transcript(tmp_path, repo), now=LATER)

    assert result.skipped is None
    entries = _entries(repo)
    assert [e["summary"] for e in entries] == [e["summary"] for e in result.proposed]
    for e in entries:
        assert e["status"] == "proposed" and e["confidence"] == "inferred" and e["scope"] == "project"
        assert e["tags"][:2] == ["handoff", "session:aaaa1111"]
        assert e["source"]["session"] == SESSION
        assert len(e["summary"]) <= memory_store.MAX_SUMMARY_CHARS
        assert PROMPT not in json.dumps(e)

    w = _part(entries, "work")
    assert "changed 2 file(s)" in w["summary"]
    assert "ANSWER.md (new, +3)" in w["summary"] and "main.go (+3/-1)" in w["summary"]
    assert "Ran: go build, go vet." in w["summary"]
    assert w["source"]["files"] == ["ANSWER.md", "main.go"]


def test_findings_decisions_open_items_and_outcome_are_split_out(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)
    entries = handoff.capture(repo, SESSION, _transcript(tmp_path, repo), now=LATER).proposed

    findings = _part(entries, "findings")["summary"]
    assert "ANSWER: judge, Unlanded" in findings
    assert "ANSWER.md: Findings `judge()` in landing.go gates cherry on Upstream." in findings
    decision = _part(entries, "decisions")
    assert decision["type"] == "decision"
    assert "instead of a new package, because the build has one binary" in decision["summary"]
    opened = _part(entries, "open")["summary"]
    assert "Should `helper` be exported?" in opened
    assert "ANSWER" not in opened
    outcome = _part(entries, "outcome")["summary"]
    assert "The engine still calls the old path." in outcome
    assert "because the build has one binary" not in outcome  # already in decisions
    assert "Should `helper`" not in outcome  # already in open


def test_capture_leaves_the_index_and_the_working_tree_alone(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)
    before = _run(repo, "status", "--porcelain", "--untracked-files=all")

    handoff.capture(repo, SESSION, _transcript(tmp_path, repo), now=LATER)

    assert _run(repo, "diff", "--cached", "--name-only") == ""
    assert _run(repo, "status", "--porcelain", "--untracked-files=all") == before


def test_an_unchanged_session_writes_nothing_the_second_time(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)
    transcript = _transcript(tmp_path, repo)
    first = handoff.capture(repo, SESSION, transcript, now=NOW)

    second = handoff.capture(repo, SESSION, transcript, now=LATER)

    assert second.proposed == [] and second.skipped == "unchanged since the last capture"
    assert len(_entries(repo)) == len(first.proposed)


def test_a_later_capture_replaces_the_sessions_unreviewed_group(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)
    first = handoff.capture(repo, SESSION, _transcript(tmp_path, repo), now=LATER)
    (repo / "extra.go").write_text("package main\n", encoding="utf-8")

    second = handoff.capture(repo, SESSION, _transcript(tmp_path, repo, final="All done now."), now=LATER)

    assert second.replaced == len(first.proposed)
    entries = _entries(repo)
    assert len(entries) == len(second.proposed)
    assert "changed 3 file(s)" in _part(entries, "work")["summary"]


def test_a_reviewed_entry_survives_a_refresh(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)
    handoff.capture(repo, SESSION, _transcript(tmp_path, repo), now=LATER)
    memory_dir = memory_store.project_memory_dir(repo)
    rejected = [{**e, "status": "rejected"} if "decisions" in e["tags"] else e for e in _entries(repo)]
    memory_store.rewrite_entries(rejected, memory_dir)
    (repo / "extra.go").write_text("package main\n", encoding="utf-8")

    handoff.capture(repo, SESSION, _transcript(tmp_path, repo), now=LATER)

    statuses = [e["status"] for e in _entries(repo) if "decisions" in e["tags"]]
    assert sorted(statuses) == ["proposed", "rejected"]


def test_a_trivial_session_writes_nothing(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    transcript = _transcript(tmp_path, repo, final="Hello!", tools=[{"name": "Read", "input": {}}])

    result = handoff.capture(repo, SESSION, transcript, now=LATER)

    assert result.proposed == [] and result.skipped == "nothing worth handing off"
    assert _entries(repo) == []


def test_without_a_start_snapshot_the_transcripts_edits_stand_in(repo: Path, tmp_path: Path) -> None:
    _work(repo)

    entries = handoff.capture(repo, SESSION, _transcript(tmp_path, repo), now=LATER).proposed

    assert "changed 2 file(s): main.go, ANSWER.md." in _part(entries, "work")["summary"]


def test_a_secret_in_the_final_message_is_dropped_not_fatal(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)
    final = "Set api_key = sk-abcdefghijklmnopqrstuvwxyz0123 in the config.\nThe build is green."

    entries = handoff.capture(repo, SESSION, _transcript(tmp_path, repo, final=final), now=LATER).proposed

    assert entries
    assert "sk-abc" not in json.dumps(_entries(repo))
    assert "The build is green." in _part(entries, "outcome")["summary"]


def test_the_stop_events_last_message_wins_over_the_transcript(repo: Path, tmp_path: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)

    entries = handoff.capture(repo, SESSION, _transcript(tmp_path, repo), last_message="Fresh reply.",
                              now=LATER).proposed

    assert "Fresh reply." in _part(entries, "outcome")["summary"]


def test_an_unenrolled_project_is_skipped(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert handoff.capture(bare, SESSION, None).skipped == "project is not enrolled"
    assert handoff.record_start(bare, SESSION) is None
    assert not (Path.home() / ".agent-harness" / "work").exists()


def test_capture_never_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(work, "repository_for", explode)
    assert handoff.capture(repo, SESSION, None).skipped == "RuntimeError: simulated"


def test_session_state_lives_under_the_isolated_home(repo: Path, isolated_home: Path) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    [state] = list((isolated_home / ".agent-harness" / "work").rglob("*.handoff.json"))
    assert json.loads(state.read_text(encoding="utf-8"))["start_tree"]


# --- the capture hook ------------------------------------------------------


def test_the_capture_hook_exits_0_and_is_silent_for_every_input(repo: Path, monkeypatch, capsys) -> None:
    for raw in ("not json", "[1]", json.dumps({"session_id": 5}), json.dumps({"cwd": str(repo)})):
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))
        assert capture_hook.main() == 0
    assert _entries(repo) == []
    assert capsys.readouterr().out == ""


def test_the_capture_hook_writes_from_the_events_payload(repo: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    handoff.record_start(repo, SESSION, now=NOW)
    _work(repo)
    event = {"session_id": SESSION, "cwd": str(repo), "hook_event_name": "Stop",
             "transcript_path": str(_transcript(tmp_path, repo))}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    assert capture_hook.main() == 0

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert any("work" in e["tags"] for e in _entries(repo))


# --- injection -------------------------------------------------------------


def _capture_session(repo: Path, tmp_path: Path, session: str, now: datetime, name: str) -> None:
    handoff.record_start(repo, session, now=now)
    (repo / name).write_text("x\n", encoding="utf-8")
    handoff.capture(repo, session, _transcript(tmp_path, repo, final=f"Wrote {name}."), now=now)


def test_the_block_shows_the_newest_session_first(repo: Path, tmp_path: Path) -> None:
    _capture_session(repo, tmp_path, SESSION, NOW, "first.txt")
    _capture_session(repo, tmp_path, NEXT_SESSION, LATER, "second.txt")

    block = handoff.render_block(repo)

    assert block.startswith("## Hand-off from earlier sessions")
    assert block.index("Wrote second.txt.") < block.index("Wrote first.txt.")


def test_the_block_respects_its_budget(repo: Path, tmp_path: Path, monkeypatch) -> None:
    for n in range(5):
        _capture_session(repo, tmp_path, f"{n}" * 8 + "-session", NOW, f"file{n}.txt")

    assert len(handoff.render_block(repo)) <= handoff.HANDOFF_MAX_CHARS
    assert len(handoff.render_block(repo, max_chars=400)) <= 400
    monkeypatch.setenv(handoff.MAX_CHARS_ENV, "300")
    assert 0 < len(handoff.render_block(repo)) <= 300
    monkeypatch.setenv(handoff.MAX_CHARS_ENV, "0")
    assert handoff.render_block(repo) == ""


def test_the_block_skips_the_sessions_own_and_closed_entries(repo: Path, tmp_path: Path) -> None:
    _capture_session(repo, tmp_path, SESSION, NOW, "first.txt")
    assert handoff.render_block(repo, session_id=SESSION) == ""

    memory_dir = memory_store.project_memory_dir(repo)
    memory_store.rewrite_entries([{**e, "status": "rejected"} for e in _entries(repo)], memory_dir)
    assert handoff.render_block(repo) == ""


def test_session_start_snapshots_and_the_next_session_sees_the_hand_off(
    repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    start = json.dumps({"session_id": SESSION, "cwd": str(repo), "hook_event_name": "SessionStart"})
    monkeypatch.setattr("sys.stdin", io.StringIO(start))
    assert start_hook.main([]) == 0
    assert "Hand-off" not in capsys.readouterr().out
    assert handoff.load_state(work.repository_for(repo), SESSION)["start_tree"]

    _work(repo)
    stop = {"session_id": SESSION, "cwd": str(repo), "transcript_path": str(_transcript(tmp_path, repo))}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stop)))
    assert capture_hook.main() == 0

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": NEXT_SESSION, "cwd": str(repo)})))
    assert start_hook.main([]) == 0
    out = capsys.readouterr().out

    assert "## Hand-off from earlier sessions" in out
    assert "ANSWER.md (new, +3)" in out and "ANSWER: judge, Unlanded" in out
    # Printed in full in the block, so the table of contents does not list them again.
    toc = out.split("## Hand-off from earlier sessions")[0]
    for entry_id in handoff.handoff_ids(memory_store.project_memory_dir(repo)):
        assert entry_id not in toc


def test_a_broken_hand_off_store_does_not_cost_the_table_of_contents(repo: Path, monkeypatch, capsys) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr(handoff, "render_block", explode)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SESSION, "cwd": str(repo)})))

    assert start_hook.main([]) == 0
    assert "memory is enrolled but empty" in capsys.readouterr().out
