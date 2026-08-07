"""Tests for confirmed plans, tracked action tasks, and bottleneck detection.

The properties that matter: an approval always carries the human's own words
as evidence, a task cannot be closed without proof, and bottleneck scoring
is deterministic so the same store always ranks the same way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import mcp_server, memory_context, memory_store, memory_tasks

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)
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


def _work(project: Path, summary: str, tags: list[str], age_days: int = 0,
          type: str = "task") -> str:
    memory_store.append_entries(
        [{"type": type, "scope": "project", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": summary, "tags": tags}],
        directory=_dir(project), now=NOW - timedelta(days=age_days),
    )
    return _entries(project)[-1]["id"]


# --- confirm --------------------------------------------------------------


def test_confirm_records_the_plan_and_its_steps(project: Path) -> None:
    result = memory_tasks.confirm(
        _dir(project), "Split the machine index.", SAID,
        tasks=["Add machine_index_dir", "Route selector by scope"], now=NOW,
    )
    assert result.decision["type"] == "decision"
    assert len(result.tasks) == 2
    assert len(_entries(project)) == 3


def test_a_confirmed_plan_is_accepted_and_confirmed(project: Path) -> None:
    result = memory_tasks.confirm(_dir(project), "Do the thing.", SAID, now=NOW)
    assert result.decision["status"] == "accepted"
    assert result.decision["confidence"] == "confirmed"


def test_the_humans_own_words_are_stored_as_the_evidence(project: Path) -> None:
    """This text is the entire audit trail for an in-session approval."""
    result = memory_tasks.confirm(
        _dir(project), "Do the thing.", SAID, tasks=["step one"], now=NOW
    )
    assert result.decision["source"]["confirmation"] == SAID
    assert result.tasks[0]["source"]["confirmation"] == SAID


def test_an_approval_with_no_recorded_confirmation_is_refused(project: Path) -> None:
    with pytest.raises(ValueError, match="confirmation text is required"):
        memory_tasks.confirm(_dir(project), "Do the thing.", "   ", now=NOW)


def test_confirm_requires_a_summary(project: Path) -> None:
    with pytest.raises(ValueError, match="summary is required"):
        memory_tasks.confirm(_dir(project), "  ", SAID, now=NOW)


def test_tasks_link_back_to_their_plan(project: Path) -> None:
    result = memory_tasks.confirm(
        _dir(project), "A plan.", SAID, tasks=["a", "b"], now=NOW
    )
    assert all(t["plan"] == result.decision["id"] for t in result.tasks)


def test_confirm_allocates_unique_consecutive_ids(project: Path) -> None:
    result = memory_tasks.confirm(
        _dir(project), "A plan.", SAID, tasks=["a", "b", "c"], now=NOW
    )
    ids = [result.decision["id"], *(t["id"] for t in result.tasks)]
    assert len(set(ids)) == 4
    assert [r["id"] for r in _entries(project)] == ids


def test_confirm_does_not_collide_with_existing_entries(project: Path) -> None:
    _work(project, "Pre-existing work.", ["setup"])
    result = memory_tasks.confirm(_dir(project), "A plan.", SAID, tasks=["a"], now=NOW)
    ids = [r["id"] for r in _entries(project)]
    assert len(set(ids)) == len(ids) == 3
    assert result.decision["id"] in ids


def test_a_secret_in_the_confirmation_is_rejected_and_nothing_is_written(
    project: Path,
) -> None:
    with pytest.raises(ValueError, match="secret"):
        memory_tasks.confirm(
            _dir(project), "A plan.",
            "approved, token ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", now=NOW,
        )
    assert _entries(project) == []


def test_confirm_carries_tags_onto_every_entry(project: Path) -> None:
    result = memory_tasks.confirm(
        _dir(project), "A plan.", SAID, tasks=["a"], tags=["memory", "index"], now=NOW
    )
    assert result.decision["tags"] == ["memory", "index"]
    assert result.tasks[0]["tags"] == ["memory", "index"]


# --- complete -------------------------------------------------------------


def test_complete_resolves_a_task_with_evidence(project: Path) -> None:
    result = memory_tasks.confirm(_dir(project), "A plan.", SAID, tasks=["a"], now=NOW)
    entry = memory_tasks.complete(
        _dir(project), result.tasks[0]["id"], commit="a5f6f70", now=NOW
    )
    assert entry["status"] == "resolved"
    assert entry["resolution"]["commit"] == "a5f6f70"


def test_a_task_cannot_be_closed_without_evidence(project: Path) -> None:
    result = memory_tasks.confirm(_dir(project), "A plan.", SAID, tasks=["a"], now=NOW)
    with pytest.raises(ValueError, match="evidence"):
        memory_tasks.complete(_dir(project), result.tasks[0]["id"], now=NOW)


def test_a_completed_task_leaves_the_open_list(project: Path) -> None:
    result = memory_tasks.confirm(
        _dir(project), "A plan.", SAID, tasks=["a", "b"], now=NOW
    )
    memory_tasks.complete(_dir(project), result.tasks[0]["id"], note="done", now=NOW)
    assert len(memory_tasks.open_work(_entries(project))) == 1


# --- bottlenecks ----------------------------------------------------------


def test_a_single_open_item_is_not_a_bottleneck(project: Path) -> None:
    """One open task is work in progress; calling it a bottleneck is noise."""
    _work(project, "Just started.", ["auth"])
    assert memory_tasks.bottlenecks(_entries(project), now=NOW) == []


def test_a_tag_with_repeated_open_work_scores_as_a_bottleneck(project: Path) -> None:
    _work(project, "First auth thing.", ["auth"])
    _work(project, "Second auth thing.", ["auth"])
    [found] = memory_tasks.bottlenecks(_entries(project), now=NOW)
    assert found.tag == "auth"
    assert found.open_count == 2


def test_blockers_weigh_more_than_tasks(project: Path) -> None:
    _work(project, "Blocked A.", ["auth"], type="blocker")
    _work(project, "Blocked B.", ["auth"], type="blocker")
    _work(project, "Task A.", ["docs"])
    _work(project, "Task B.", ["docs"])
    ranked = memory_tasks.bottlenecks(_entries(project), now=NOW)
    assert ranked[0].tag == "auth"
    assert ranked[0].score > ranked[1].score


def test_age_beyond_the_stale_threshold_raises_the_score(project: Path) -> None:
    _work(project, "Old A.", ["old"], age_days=60)
    _work(project, "Old B.", ["old"], age_days=60)
    _work(project, "New A.", ["new"])
    _work(project, "New B.", ["new"])
    ranked = memory_tasks.bottlenecks(_entries(project), now=NOW)
    assert ranked[0].tag == "old"
    assert ranked[0].oldest_days == 60


def test_resolved_work_stops_counting_toward_a_bottleneck(project: Path) -> None:
    first = _work(project, "One.", ["auth"])
    _work(project, "Two.", ["auth"])
    assert memory_tasks.bottlenecks(_entries(project), now=NOW)
    memory_tasks.complete(_dir(project), first, note="shipped", now=NOW)
    assert memory_tasks.bottlenecks(_entries(project), now=NOW) == []


def test_bottleneck_ordering_is_stable_across_runs(project: Path) -> None:
    for tag in ("alpha", "beta", "gamma"):
        _work(project, f"{tag} one", [tag])
        _work(project, f"{tag} two", [tag])
    entries = _entries(project)
    first = [b.tag for b in memory_tasks.bottlenecks(entries, now=NOW)]
    second = [b.tag for b in memory_tasks.bottlenecks(entries, now=NOW)]
    assert first == second


# --- prioritization -------------------------------------------------------


def test_work_on_a_stalling_topic_outranks_work_on_a_healthy_one(
    project: Path,
) -> None:
    _work(project, "Healthy solo task.", ["docs"], age_days=1)
    _work(project, "Stuck blocker A.", ["auth"], age_days=30, type="blocker")
    _work(project, "Stuck blocker B.", ["auth"], age_days=30, type="blocker")
    ranked = memory_tasks.prioritized(_entries(project), now=NOW)
    assert "auth" in (ranked[0].get("tags") or [])


def test_prioritized_returns_only_open_work(project: Path) -> None:
    done = _work(project, "Finished.", ["x"])
    _work(project, "Open.", ["x"])
    memory_tasks.complete(_dir(project), done, note="done", now=NOW)
    ranked = memory_tasks.prioritized(_entries(project), now=NOW)
    assert [r["summary"] for r in ranked] == ["Open."]


# --- context packet integration -------------------------------------------


def test_bottlenecks_surface_in_the_context_packet(project: Path) -> None:
    _work(project, "Stuck A.", ["auth"], age_days=30, type="blocker")
    _work(project, "Stuck B.", ["auth"], age_days=30, type="blocker")
    packet = memory_context.build_packet(project, now=NOW)
    section = next(s for s in packet.sections if s.key == "priorities")
    assert any("Bottleneck:" in u and "auth" in u for u in section.units)


def test_the_priorities_section_is_absent_when_nothing_is_stuck(
    project: Path,
) -> None:
    _work(project, "One lonely task.", ["auth"])
    packet = memory_context.build_packet(project, now=NOW)
    section = next(s for s in packet.sections if s.key == "priorities")
    assert section.units == ()


def test_priorities_respect_the_packet_budget(project: Path) -> None:
    for n in range(60):
        _work(project, f"Stuck item {n}: " + "detail " * 20, ["auth"], age_days=30)
    packet = memory_context.build_packet(project, max_chars=800, now=NOW)
    assert packet.total_chars <= 800


# --- MCP surface ----------------------------------------------------------


def test_memory_confirm_tool_round_trips(project: Path) -> None:
    out = mcp_server.dispatch_memory(
        "memory_confirm",
        {"project": str(project), "summary": "Split the index.",
         "confirmation": SAID, "tasks": "step one\nstep two", "tags": "memory"},
    )
    assert "confirmed" in out
    assert out.count("task mem-") == 2
    assert len(_entries(project)) == 3


def test_memory_confirm_refuses_without_the_humans_words(project: Path) -> None:
    out = mcp_server.dispatch_memory(
        "memory_confirm",
        {"project": str(project), "summary": "Do it.", "confirmation": ""},
    )
    assert "confirmation text is required" in out
    assert _entries(project) == []


def test_memory_task_done_tool_closes_a_task(project: Path) -> None:
    result = memory_tasks.confirm(_dir(project), "A plan.", SAID, tasks=["a"], now=NOW)
    out = mcp_server.dispatch_memory(
        "memory_task_done",
        {"project": str(project), "entry_id": result.tasks[0]["id"],
         "note": "verified in the test suite"},
    )
    assert "resolved" in out


def test_memory_task_done_without_evidence_is_refused(project: Path) -> None:
    result = memory_tasks.confirm(_dir(project), "A plan.", SAID, tasks=["a"], now=NOW)
    out = mcp_server.dispatch_memory(
        "memory_task_done",
        {"project": str(project), "entry_id": result.tasks[0]["id"]},
    )
    assert "evidence" in out


def test_memory_tasks_tool_lists_open_work_and_bottlenecks(project: Path) -> None:
    _work(project, "Stuck A.", ["auth"], age_days=30, type="blocker")
    _work(project, "Stuck B.", ["auth"], age_days=30, type="blocker")
    out = mcp_server.dispatch_memory("memory_tasks", {"project": str(project)})
    assert "2 open" in out
    assert "bottlenecks:" in out
    assert "auth" in out


def test_memory_tasks_tool_on_an_empty_project(project: Path) -> None:
    out = mcp_server.dispatch_memory("memory_tasks", {"project": str(project)})
    assert "no open action tasks" in out


def test_the_confirm_tools_are_registered() -> None:
    names = set(mcp_server.MEMORY_TOOL_DESCRIPTIONS)
    assert {"memory_confirm", "memory_task_done", "memory_tasks"} <= names
