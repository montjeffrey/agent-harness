"""Weekly compaction: archive terminal work, promote durable knowledge, and
keep the inbox bounded without ever discarding something a human has not
reviewed.

The invariant these tests defend most heavily is *no silent loss*. Compaction
is the only operation that removes entries from the store of record, so every
removal must be recoverable from the archive, unreviewed entries must survive
untouched, and a crash partway through must leave the inbox readable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import memory_compact, memory_store


NOW = datetime(2026, 8, 12, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_machine_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test off the real ~/.agent-harness registry."""
    machine = tmp_path / "machine"
    monkeypatch.setattr(memory_store, "machine_memory_dir", lambda: machine)
    monkeypatch.setattr(memory_store, "_registry_path", lambda: machine / "registry.json")
    return machine


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _entry(
    entry_id: str,
    *,
    type: str = "decision",
    status: str = "accepted",
    summary: str = "A thing was decided.",
    ts: datetime | None = None,
    confidence: str = "confirmed",
    source: dict | None = None,
) -> dict:
    record = {
        "id": entry_id,
        "type": type,
        "scope": "project",
        "status": status,
        "confidence": confidence,
        "summary": summary,
        "ts": memory_store.format_ts(ts or NOW),
    }
    if status == "accepted":
        record["source"] = source or {"commit": "abc1234"}
    elif source is not None:
        record["source"] = source
    return record


def _seed(root: Path, entries: list[dict]) -> Path:
    directory = memory_store.project_memory_dir(root)
    memory_store.append_entries(entries, directory, now=NOW)
    return directory


def _inbox_ids(root: Path) -> list[str]:
    return [e["id"] for e in memory_store.iter_entries(memory_store.project_memory_dir(root))]


# --------------------------------------------------------------------------
# week stamps and summary normalization
# --------------------------------------------------------------------------


def test_week_stamp_uses_iso_week_numbering() -> None:
    assert memory_compact.week_stamp(datetime(2026, 8, 12, tzinfo=timezone.utc)) == "2026-W33"


def test_week_stamp_follows_iso_year_across_a_january_boundary() -> None:
    """2027-01-01 is a Friday in ISO week 53 of 2026 -- the ISO *year* differs
    from the calendar year, and using the calendar year would file the archive
    under a week that does not contain the date."""
    assert memory_compact.week_stamp(datetime(2027, 1, 1, tzinfo=timezone.utc)) == "2026-W53"


@pytest.mark.parametrize(
    "left,right",
    [
        ("Switched to Postgres.", "switched to postgres"),
        ("Switched  to   Postgres", "Switched to Postgres."),
        ("Switched to Postgres!", "SWITCHED TO POSTGRES"),
    ],
)
def test_normalize_summary_treats_case_spacing_and_trailing_punctuation_as_equivalent(
    left: str, right: str
) -> None:
    assert memory_compact.normalize_summary(left) == memory_compact.normalize_summary(right)


def test_normalize_summary_keeps_genuinely_different_text_distinct() -> None:
    a = memory_compact.normalize_summary("Switched to Postgres.")
    b = memory_compact.normalize_summary("Switched to MySQL.")
    assert a != b


# --------------------------------------------------------------------------
# deduplication
# --------------------------------------------------------------------------


def test_duplicate_proposals_collapse_to_the_earliest_id() -> None:
    entries = [
        _entry("mem-2026-08-12-001", status="proposed", summary="Switched to Postgres."),
        _entry("mem-2026-08-12-002", status="proposed", summary="switched to postgres"),
        _entry("mem-2026-08-12-003", status="proposed", summary="Switched to MySQL."),
    ]
    dropped = memory_compact.find_duplicates(entries)
    assert dropped == {"mem-2026-08-12-002"}


def test_accepted_entries_are_never_deduplicated() -> None:
    """An accepted entry carries evidence and review history. Two accepted
    entries that read alike may cite different commits, so collapsing them
    would destroy the audit trail the trust model exists to keep."""
    entries = [
        _entry("mem-2026-08-12-001", summary="Switched to Postgres.", source={"commit": "aaa1111"}),
        _entry("mem-2026-08-12-002", summary="switched to postgres", source={"commit": "bbb2222"}),
    ]
    assert memory_compact.find_duplicates(entries) == set()


def test_duplicate_detection_is_scoped_by_type() -> None:
    """Identical wording as a task and as a decision are different claims."""
    entries = [
        _entry("mem-2026-08-12-001", type="task", status="proposed", summary="Migrate the store."),
        _entry("mem-2026-08-12-002", type="decision", status="proposed", summary="Migrate the store."),
    ]
    assert memory_compact.find_duplicates(entries) == set()


# --------------------------------------------------------------------------
# what leaves the inbox, and what must not
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(memory_store.CLOSED_STATUSES))
def test_closed_entries_are_terminal(status: str) -> None:
    assert memory_compact.is_terminal(_entry("mem-2026-08-12-001", status=status)) is True


@pytest.mark.parametrize("status", ["proposed", "accepted"])
def test_live_entries_are_not_terminal(status: str) -> None:
    assert memory_compact.is_terminal(_entry("mem-2026-08-12-001", status=status)) is False


def test_compact_removes_terminal_entries_from_the_inbox(project: Path) -> None:
    _seed(project, [
        _entry("mem-2026-08-10-001", status="resolved", type="task"),
        _entry("mem-2026-08-10-002", status="accepted"),
    ])
    memory_compact.compact(project, now=NOW)
    assert _inbox_ids(project) == ["mem-2026-08-10-002"]


def test_compact_never_discards_an_unreviewed_proposal(project: Path) -> None:
    """A proposed entry has not been seen by a human. Compaction is automated
    maintenance and must not be able to decide its fate."""
    _seed(project, [_entry("mem-2026-08-10-001", status="proposed")])
    memory_compact.compact(project, now=NOW)
    assert _inbox_ids(project) == ["mem-2026-08-10-001"]


def test_compact_keeps_accepted_entries_in_the_inbox(project: Path) -> None:
    """Promotion into durable.md is a *copy*, not a move.

    `memory_context` reads durable knowledge out of the inbox -- it treats only
    scope.md and active.md as prose. Moving an accepted decision into
    durable.md would therefore delete it from retrieval and from the semantic
    index, which is the exact opposite of what compaction is for.
    """
    _seed(project, [_entry("mem-2026-08-10-001", type="decision")])
    memory_compact.compact(project, now=NOW)
    assert _inbox_ids(project) == ["mem-2026-08-10-001"]


def test_compact_reports_nothing_to_do_on_an_empty_inbox(project: Path) -> None:
    result = memory_compact.compact(project, now=NOW)
    assert result.archived == 0
    assert result.promoted == 0
    assert result.index_stale is False


# --------------------------------------------------------------------------
# the archive
# --------------------------------------------------------------------------


def test_archive_is_written_under_the_iso_week_filename(project: Path) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])
    result = memory_compact.compact(project, now=NOW)
    expected = memory_store.project_memory_dir(project) / "archive" / "2026-W33.md"
    assert result.archive_path == expected
    assert expected.is_file()


def test_every_removed_entry_is_recoverable_from_the_archive(project: Path) -> None:
    _seed(project, [
        _entry("mem-2026-08-10-001", status="resolved", type="task", summary="Ship the thing."),
        _entry("mem-2026-08-10-002", status="superseded", summary="Use MySQL."),
    ])
    result = memory_compact.compact(project, now=NOW)
    text = result.archive_path.read_text(encoding="utf-8")
    for token in ("mem-2026-08-10-001", "Ship the thing.", "mem-2026-08-10-002", "Use MySQL."):
        assert token in text, token


def test_rerunning_in_the_same_week_appends_rather_than_overwriting(project: Path) -> None:
    """`archive/YYYY-Www.md` is immutable history. A second run in the same
    ISO week must not erase the first run's record."""
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task", summary="First run.")])
    memory_compact.compact(project, now=NOW)

    _seed(project, [_entry("mem-2026-08-11-001", status="resolved", type="task", summary="Second run.")])
    result = memory_compact.compact(project, now=NOW + timedelta(hours=1))

    text = result.archive_path.read_text(encoding="utf-8")
    assert "First run." in text
    assert "Second run." in text


def test_compacting_with_nothing_terminal_does_not_create_an_archive(project: Path) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", status="accepted")])
    result = memory_compact.compact(project, now=NOW)
    assert result.archived == 0
    assert not result.archive_path.exists()


# --------------------------------------------------------------------------
# promotion into durable.md
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entry_type", ["decision", "discovery", "lesson"])
def test_accepted_durable_types_are_promoted(project: Path, entry_type: str) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", type=entry_type, summary="Durable knowledge.")])
    memory_compact.compact(project, now=NOW)
    durable = (memory_store.project_memory_dir(project) / "durable.md").read_text(encoding="utf-8")
    assert "mem-2026-08-10-001" in durable
    assert "Durable knowledge." in durable


@pytest.mark.parametrize("entry_type", ["task", "blocker"])
def test_work_items_are_not_promoted(project: Path, entry_type: str) -> None:
    """Tasks and blockers are transient by nature; durable.md is for knowledge
    that outlives the work that produced it."""
    _seed(project, [_entry("mem-2026-08-10-001", type=entry_type, summary="Do the work.")])
    memory_compact.compact(project, now=NOW)
    durable = (memory_store.project_memory_dir(project) / "durable.md").read_text(encoding="utf-8")
    assert "mem-2026-08-10-001" not in durable


def test_proposed_entries_are_not_promoted(project: Path) -> None:
    """Promotion asserts the knowledge is confirmed. The trust model already
    guarantees accepted implies evidence, so status alone is the gate."""
    _seed(project, [_entry("mem-2026-08-10-001", status="proposed", summary="Maybe true.")])
    memory_compact.compact(project, now=NOW)
    durable = (memory_store.project_memory_dir(project) / "durable.md").read_text(encoding="utf-8")
    assert "mem-2026-08-10-001" not in durable


def test_promotion_is_idempotent_across_runs(project: Path) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", summary="Said once.")])
    memory_compact.compact(project, now=NOW)
    memory_compact.compact(project, now=NOW + timedelta(days=7))
    durable = (memory_store.project_memory_dir(project) / "durable.md").read_text(encoding="utf-8")
    assert durable.count("mem-2026-08-10-001") == 1


def test_promotion_preserves_hand_written_durable_content(project: Path) -> None:
    path = memory_store.project_memory_dir(project) / "durable.md"
    path.write_text("# Durable project memory\n\nHand-written architecture note.\n", encoding="utf-8")
    _seed(project, [_entry("mem-2026-08-10-001", summary="Promoted note.")])
    memory_compact.compact(project, now=NOW)
    text = path.read_text(encoding="utf-8")
    assert "Hand-written architecture note." in text
    assert "Promoted note." in text


# --------------------------------------------------------------------------
# active.md
# --------------------------------------------------------------------------


def test_active_memory_carries_open_work(project: Path) -> None:
    _seed(project, [
        _entry("mem-2026-08-10-001", type="task", status="accepted", summary="Finish compaction."),
        _entry("mem-2026-08-10-002", type="blocker", status="accepted", summary="Index is stale."),
    ])
    memory_compact.compact(project, now=NOW)
    active = (memory_store.project_memory_dir(project) / "active.md").read_text(encoding="utf-8")
    assert "Finish compaction." in active
    assert "Index is stale." in active


def test_active_memory_records_the_compaction_timestamp(project: Path) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", type="task", status="accepted")])
    memory_compact.compact(project, now=NOW)
    active = (memory_store.project_memory_dir(project) / "active.md").read_text(encoding="utf-8")
    assert memory_store.format_ts(NOW) in active
    assert "2026-W33" in active


def test_active_memory_drops_resolved_work(project: Path) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", type="task", status="resolved", summary="Already done.")])
    memory_compact.compact(project, now=NOW)
    active = (memory_store.project_memory_dir(project) / "active.md").read_text(encoding="utf-8")
    assert "Already done." not in active


def test_active_memory_omits_whole_entries_past_the_budget_and_says_how_many(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budgeting is unit-atomic, like the context packet: an entry is included
    whole or not at all, and the reader is told a count was dropped rather
    than being handed a silently truncated list."""
    monkeypatch.setattr(memory_compact, "ACTIVE_MAX_ENTRIES", 2)
    _seed(project, [
        _entry(f"mem-2026-08-10-{n:03d}", type="task", status="accepted", summary=f"Task {n}.")
        for n in range(1, 6)
    ])
    memory_compact.compact(project, now=NOW)
    active = (memory_store.project_memory_dir(project) / "active.md").read_text(encoding="utf-8")
    assert "3 further" in active


def test_active_memory_stays_within_the_character_budget(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_compact, "ACTIVE_MAX_CHARS", 400)
    _seed(project, [
        _entry(f"mem-2026-08-10-{n:03d}", type="task", status="accepted", summary="x" * 200)
        for n in range(1, 10)
    ])
    memory_compact.compact(project, now=NOW)
    active = (memory_store.project_memory_dir(project) / "active.md").read_text(encoding="utf-8")
    assert len(active) <= 400


# --------------------------------------------------------------------------
# durability: unparseable lines, ordering, locking, rollback
# --------------------------------------------------------------------------


def test_unparseable_inbox_lines_survive_compaction(project: Path) -> None:
    """A line this schema version cannot parse may be a future schema, not
    garbage. Line-level splicing carries it across untouched."""
    directory = memory_store.project_memory_dir(project)
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])
    with (directory / "inbox.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"id": "mem-9999-01-01-001", "schema": 99, "unknown": true}\n')
        fh.write("not json at all\n")

    memory_compact.compact(project, now=NOW)

    raw = memory_store.read_lines(directory)
    assert any("not json at all" in line for line in raw)
    assert any('"schema": 99' in line for line in raw)


def test_the_inbox_is_spliced_last_so_a_late_failure_cannot_lose_entries(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering is the real crash-safety guarantee: the inbox is the store of
    record and everything else is derived, so it must be the final write. A
    failure before it means the next run merely redoes derived work."""
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(memory_compact, "_write_active", boom)
    with pytest.raises(OSError):
        memory_compact.compact(project, now=NOW)

    assert _inbox_ids(project) == ["mem-2026-08-10-001"]


def test_a_failed_compaction_restores_the_derived_files(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = memory_store.project_memory_dir(project)
    before = (directory / "durable.md").read_text(encoding="utf-8")
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(memory_compact, "_write_active", boom)
    with pytest.raises(OSError):
        memory_compact.compact(project, now=NOW)

    assert (directory / "durable.md").read_text(encoding="utf-8") == before


def test_a_second_compaction_refuses_while_one_holds_the_lock(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    with memory_compact.compact_lock(directory, now=NOW):
        with pytest.raises(memory_compact.CompactLockError):
            memory_compact.compact(project, now=NOW)


def test_the_lock_is_released_after_a_successful_run(project: Path) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])
    memory_compact.compact(project, now=NOW)
    assert not (memory_store.project_memory_dir(project) / ".compact.lock").exists()


def test_the_lock_is_released_when_the_run_fails(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(memory_compact, "_write_active", boom)
    with pytest.raises(OSError):
        memory_compact.compact(project, now=NOW)
    assert not (memory_store.project_memory_dir(project) / ".compact.lock").exists()


def test_a_stale_lock_is_broken_rather_than_blocking_forever(project: Path) -> None:
    """A crashed run must not wedge compaction permanently."""
    directory = memory_store.project_memory_dir(project)
    stale = NOW - timedelta(seconds=memory_compact.LOCK_STALE_SECONDS + 60)
    (directory / ".compact.lock").write_text(
        json.dumps({"pid": 999999, "ts": memory_store.format_ts(stale)}), encoding="utf-8"
    )
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])
    result = memory_compact.compact(project, now=NOW)
    assert result.archived == 1


def test_a_corrupt_lock_file_does_not_wedge_compaction(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    (directory / ".compact.lock").write_text("not json", encoding="utf-8")
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])
    result = memory_compact.compact(project, now=NOW)
    assert result.archived == 1


# --------------------------------------------------------------------------
# retrieval freshness -- the reason compaction exists
# --------------------------------------------------------------------------


def test_removing_entries_marks_the_semantic_index_stale(project: Path) -> None:
    """`update_index` deletes ids that are gone, but only when it is re-run.
    Until then the index keeps serving archived entries, so compaction has to
    say the index needs rebuilding."""
    _seed(project, [_entry("mem-2026-08-10-001", status="resolved", type="task")])
    result = memory_compact.compact(project, now=NOW)
    assert result.index_stale is True


def test_a_no_op_compaction_does_not_claim_the_index_is_stale(project: Path) -> None:
    _seed(project, [_entry("mem-2026-08-10-001", status="accepted")])
    result = memory_compact.compact(project, now=NOW)
    assert result.index_stale is False


def test_context_still_builds_a_packet_after_compaction(project: Path) -> None:
    """active.md is consumed as prose by `memory_context`. A rewrite that broke
    that parse would take the whole packet down."""
    from harness import memory_context

    _seed(project, [
        _entry("mem-2026-08-10-001", type="task", status="accepted", summary="Open work here."),
        _entry("mem-2026-08-10-002", status="resolved", type="task", summary="Closed work."),
        _entry("mem-2026-08-10-003", type="decision", summary="A real decision."),
    ])
    memory_compact.compact(project, now=NOW)

    rendered = memory_context.render_markdown(
        memory_context.build_packet(project, strategy="deterministic")
    )
    assert "Open work here." in rendered
    assert "A real decision." in rendered
