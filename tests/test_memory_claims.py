"""Tests for job claiming: which agent program owns an open task right now.

The properties that matter: an unknown owner or a wrong-type/closed entry is
refused, a live claim by someone else blocks a second claim, a stale claim
can be taken over (and says who it took it from), release respects
ownership unless forced, and `claimed_by`/`unclaimed` partition open work
correctly for "what's mine" / "what's free" queries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import mcp_server, memory, memory_claims, memory_store, memory_tasks

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
SAID = "yes, go ahead with that plan"


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _dir(project: Path) -> Path:
    return memory_store.project_memory_dir(project)


def _entries(project: Path) -> list[dict]:
    return list(memory_store.iter_entries(_dir(project)))


def _task(project: Path, summary: str = "Do the thing.", type: str = "task") -> str:
    result = memory_tasks.confirm(_dir(project), "A plan.", SAID, tasks=[summary], now=NOW)
    return result.tasks[0]["id"]


def _decision(project: Path) -> str:
    result = memory_tasks.confirm(_dir(project), "A plan.", SAID, now=NOW)
    return result.decision["id"]


# --- claim ------------------------------------------------------------------


def test_claiming_an_open_task_records_owner_and_timestamp(project: Path) -> None:
    task_id = _task(project)
    result = memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    assert result.entry["claim"] == {"owner": "claude", "ts": memory_store.format_ts(NOW)}
    assert result.reclaimed_from is None


def test_the_claim_persists_in_the_store(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "codex", now=NOW)
    entry = next(e for e in _entries(project) if e["id"] == task_id)
    assert entry["claim"]["owner"] == "codex"


def test_an_unknown_owner_is_refused(project: Path) -> None:
    task_id = _task(project)
    with pytest.raises(ValueError, match="unknown owner"):
        memory_claims.claim(_dir(project), task_id, "gpt5-scratch", now=NOW)


def test_only_task_and_blocker_entries_can_be_claimed(project: Path) -> None:
    decision_id = _decision(project)
    with pytest.raises(ValueError, match="only"):
        memory_claims.claim(_dir(project), decision_id, "claude", now=NOW)


def test_a_resolved_task_cannot_be_claimed(project: Path) -> None:
    task_id = _task(project)
    memory_tasks.complete(_dir(project), task_id, note="already done", now=NOW)
    with pytest.raises(ValueError, match="only open work"):
        memory_claims.claim(_dir(project), task_id, "claude", now=NOW)


def test_a_live_claim_by_someone_else_blocks_a_second_claim(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    with pytest.raises(ValueError, match="already claimed by 'claude'"):
        memory_claims.claim(_dir(project), task_id, "codex", now=NOW)


def test_the_same_owner_can_reclaim_to_refresh_the_timestamp(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    later = NOW + timedelta(hours=1)
    result = memory_claims.claim(_dir(project), task_id, "claude", now=later)
    assert result.entry["claim"]["ts"] == memory_store.format_ts(later)


def test_a_stale_claim_is_taken_over_and_names_who_it_preempted(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    stale_later = NOW + timedelta(hours=memory_claims.CLAIM_STALE_HOURS + 1)
    result = memory_claims.claim(_dir(project), task_id, "codex", now=stale_later)
    assert result.entry["claim"]["owner"] == "codex"
    assert result.reclaimed_from == "claude"


def test_claiming_a_nonexistent_entry_raises_lookup_error(project: Path) -> None:
    with pytest.raises(LookupError):
        memory_claims.claim(_dir(project), "mem-2026-08-24-999", "claude", now=NOW)


# --- release ------------------------------------------------------------------


def test_releasing_a_claim_removes_it(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    entry = memory_claims.release(_dir(project), task_id, "claude", now=NOW)
    assert "claim" not in entry


def test_releasing_an_unclaimed_entry_is_refused(project: Path) -> None:
    task_id = _task(project)
    with pytest.raises(ValueError, match="not currently claimed"):
        memory_claims.release(_dir(project), task_id, "claude", now=NOW)


def test_releasing_someone_elses_claim_is_refused_without_force(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    with pytest.raises(ValueError, match="claimed by 'claude'"):
        memory_claims.release(_dir(project), task_id, "codex", now=NOW)


def test_releasing_someone_elses_claim_succeeds_with_force(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    entry = memory_claims.release(_dir(project), task_id, "codex", now=NOW, force=True)
    assert "claim" not in entry


# --- is_stale / claimed_by / unclaimed --------------------------------------


def test_is_stale_is_false_just_under_the_threshold() -> None:
    c = {"owner": "claude", "ts": memory_store.format_ts(NOW)}
    just_under = NOW + timedelta(hours=memory_claims.CLAIM_STALE_HOURS) - timedelta(minutes=1)
    assert memory_claims.is_stale(c, now=just_under) is False


def test_is_stale_is_true_just_over_the_threshold() -> None:
    c = {"owner": "claude", "ts": memory_store.format_ts(NOW)}
    just_over = NOW + timedelta(hours=memory_claims.CLAIM_STALE_HOURS) + timedelta(minutes=1)
    assert memory_claims.is_stale(c, now=just_over) is True


def test_a_claim_with_an_unparsable_timestamp_counts_as_stale() -> None:
    assert memory_claims.is_stale({"owner": "claude", "ts": "not-a-date"}, now=NOW) is True


def test_claimed_by_returns_only_that_owners_live_claims(project: Path) -> None:
    mine = _task(project, "Mine.")
    theirs = _task(project, "Theirs.")
    memory_claims.claim(_dir(project), mine, "claude", now=NOW)
    memory_claims.claim(_dir(project), theirs, "codex", now=NOW)
    got = memory_claims.claimed_by(_entries(project), "claude", now=NOW)
    assert [e["id"] for e in got] == [mine]


def test_claimed_by_excludes_stale_claims(project: Path) -> None:
    task_id = _task(project)
    memory_claims.claim(_dir(project), task_id, "claude", now=NOW)
    stale_later = NOW + timedelta(hours=memory_claims.CLAIM_STALE_HOURS + 1)
    assert memory_claims.claimed_by(_entries(project), "claude", now=stale_later) == []


def test_unclaimed_excludes_live_claims_but_includes_stale_ones(project: Path) -> None:
    free = _task(project, "Free.")
    live = _task(project, "Live.")
    stale = _task(project, "Stale.")
    later = NOW + timedelta(hours=memory_claims.CLAIM_STALE_HOURS + 1)
    memory_claims.claim(_dir(project), stale, "claude", now=NOW)  # will be stale by `later`
    memory_claims.claim(_dir(project), live, "claude", now=later)  # still fresh at `later`
    got = {e["id"] for e in memory_claims.unclaimed(_entries(project), now=later)}
    assert got == {free, stale}


# --- MCP surface --------------------------------------------------------------


def test_memory_claim_tool_round_trips(project: Path) -> None:
    task_id = _task(project)
    out = mcp_server.dispatch_memory(
        "memory_claim",
        {"project": str(project), "entry_id": task_id, "owner": "claude"},
    )
    assert "claimed by claude" in out


def test_memory_claim_tool_reports_a_conflicting_live_claim(project: Path) -> None:
    task_id = _task(project)
    mcp_server.dispatch_memory(
        "memory_claim", {"project": str(project), "entry_id": task_id, "owner": "claude"}
    )
    out = mcp_server.dispatch_memory(
        "memory_claim", {"project": str(project), "entry_id": task_id, "owner": "codex"}
    )
    assert "already claimed by 'claude'" in out


def test_memory_release_tool_round_trips(project: Path) -> None:
    task_id = _task(project)
    mcp_server.dispatch_memory(
        "memory_claim", {"project": str(project), "entry_id": task_id, "owner": "claude"}
    )
    out = mcp_server.dispatch_memory(
        "memory_release", {"project": str(project), "entry_id": task_id, "owner": "claude"}
    )
    assert "released" in out


def test_memory_tasks_tool_shows_claim_status(project: Path) -> None:
    task_id = _task(project)
    mcp_server.dispatch_memory(
        "memory_claim", {"project": str(project), "entry_id": task_id, "owner": "agy"}
    )
    out = mcp_server.dispatch_memory("memory_tasks", {"project": str(project)})
    assert "claimed by agy" in out


def test_the_claim_tools_are_registered() -> None:
    names = set(mcp_server.MEMORY_TOOL_DESCRIPTIONS)
    assert {"memory_claim", "memory_release"} <= names


# --- CLI surface --------------------------------------------------------------


def test_cli_claim_then_list_shows_the_owner(project: Path, capsys) -> None:
    task_id = _task(project)
    code = memory.main(["claim", "claim", task_id, "--owner", "claude", "--project", str(project)])
    assert code == 0
    capsys.readouterr()

    code = memory.main(["claim", "list", "--project", str(project)])
    out = capsys.readouterr().out
    assert code == 0
    assert "claimed by claude" in out


def test_cli_claim_rejects_an_unknown_owner(project: Path) -> None:
    task_id = _task(project)
    with pytest.raises(SystemExit):
        memory.main(["claim", "claim", task_id, "--owner", "not-a-real-agent", "--project", str(project)])


def test_cli_claim_without_entry_id_is_rejected(project: Path, capsys) -> None:
    code = memory.main(["claim", "claim", "--owner", "claude", "--project", str(project)])
    assert code == 2


def test_cli_release_round_trips(project: Path, capsys) -> None:
    task_id = _task(project)
    memory.main(["claim", "claim", task_id, "--owner", "claude", "--project", str(project)])
    capsys.readouterr()
    code = memory.main(["claim", "release", task_id, "--owner", "claude", "--project", str(project)])
    out = capsys.readouterr().out
    assert code == 0
    assert "released" in out


def test_cli_release_conflict_exits_2(project: Path, capsys) -> None:
    task_id = _task(project)
    memory.main(["claim", "claim", task_id, "--owner", "claude", "--project", str(project)])
    capsys.readouterr()
    code = memory.main(["claim", "release", task_id, "--owner", "codex", "--project", str(project)])
    assert code == 2


def test_cli_claim_list_on_a_project_with_no_open_work(project: Path, capsys) -> None:
    code = memory.main(["claim", "list", "--project", str(project)])
    out = capsys.readouterr().out
    assert code == 1
    assert "no open tasks or blockers" in out
