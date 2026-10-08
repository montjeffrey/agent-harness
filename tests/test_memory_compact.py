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


def test_duplicate_proposals_with_equal_detail_collapse_to_the_earliest_id() -> None:
    """Detail is the tiebreak, not the rule. When two proposals carry the same
    evidence, the earliest id still wins, so ordering stays deterministic and
    the common case behaves exactly as it did before detail was considered."""
    entries = [
        _entry("mem-2026-08-12-001", status="proposed", summary="Switched to Postgres."),
        _entry("mem-2026-08-12-002", status="proposed", summary="switched to postgres"),
        _entry("mem-2026-08-12-003", status="proposed", summary="Switched to MySQL."),
    ]
    dropped = memory_compact.find_duplicates(entries)
    assert dropped == {"mem-2026-08-12-002"}


def test_duplicate_proposals_break_an_exact_tie_on_the_earliest_id() -> None:
    """A genuine tie -- byte-identical summaries, no source, no tags, no extra
    fields -- so every rung of `detail_rank` is level and only the id is left
    to decide. Distinct from the test above, where the wordings differ in raw
    length and the last rung settles it before the tiebreak is ever consulted.
    """
    entries = [
        _entry("mem-2026-08-12-002", status="proposed", summary="Switched to Postgres."),
        _entry("mem-2026-08-12-001", status="proposed", summary="Switched to Postgres."),
    ]
    assert memory_compact.detail_rank(entries[0]) == memory_compact.detail_rank(entries[1])
    assert memory_compact.find_duplicates(entries) == {"mem-2026-08-12-002"}


def test_duplicate_proposals_keep_the_one_carrying_evidence() -> None:
    """The crack this closes. Grouping is *by* the normalized summary, so the
    surviving detail was never the wording -- it is the source, the tags, and
    any field this schema version does not know about. An agent that records a
    fact bare and then re-records it with the commit that proves it must not
    have the proof collapsed into the bare copy: `accepted` requires evidence,
    so keeping the evidence-free twin strands the entry short of review."""
    bare = _entry("mem-2026-08-12-001", status="proposed", summary="Switched to Postgres.")
    proven = _entry(
        "mem-2026-08-12-002",
        status="proposed",
        summary="switched to postgres",
        source={"commit": "abc1234", "file": "harness/memory_store.py"},
    )
    assert memory_compact.find_duplicates([bare, proven]) == {"mem-2026-08-12-001"}


def test_duplicate_proposals_prefer_richer_source_then_tags_then_wording() -> None:
    """The ladder below evidence, each rung tested where the rung above ties.

    Attribution-only source (`project`) is not evidence -- `_has_source` says
    so -- but it is still more than nothing, so it outranks an empty source.
    """
    attributed = _entry(
        "mem-2026-08-12-001", status="proposed", summary="Bare.", source={"project": "p"}
    )
    unattributed = _entry("mem-2026-08-12-002", status="proposed", summary="bare")
    assert memory_compact.find_duplicates([attributed, unattributed]) == {
        "mem-2026-08-12-002"
    }

    tagged = _entry("mem-2026-08-13-001", status="proposed", summary="Tagged.")
    tagged["tags"] = ["storage", "migration"]
    untagged = _entry("mem-2026-08-13-002", status="proposed", summary="tagged")
    assert memory_compact.find_duplicates([tagged, untagged]) == {"mem-2026-08-13-002"}

    # Same evidence and tags: the fuller wording carries the punctuation and
    # capitalisation that normalization threw away.
    fuller = _entry("mem-2026-08-14-002", status="proposed", summary="Switched to Postgres.")
    terser = _entry("mem-2026-08-14-001", status="proposed", summary="switched to postgres")
    assert memory_compact.find_duplicates([fuller, terser]) == {"mem-2026-08-14-001"}


def test_duplicate_proposals_keep_unknown_fields_over_a_bare_twin() -> None:
    """A field this schema version cannot read is still detail. The store
    preserves unknown keys for forward compatibility, and dedup must not be
    the one place that quietly prefers the copy without them."""
    bare = _entry("mem-2026-08-12-001", status="proposed", summary="Switched to Postgres.")
    extended = _entry("mem-2026-08-12-002", status="proposed", summary="switched to postgres")
    extended["rationale"] = "measured on the benchmark harness"
    assert memory_compact.find_duplicates([bare, extended]) == {"mem-2026-08-12-001"}


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
# merging a dropped twin's support into the survivor
# --------------------------------------------------------------------------


def test_merge_unions_tags_and_source_keys_with_the_keeper_winning() -> None:
    keeper = _entry(
        "mem-2026-08-12-001", status="proposed", summary="Switched.",
        source={"commit": "aaa1111"},
    )
    keeper["tags"] = ["storage"]
    twin = _entry(
        "mem-2026-08-12-002", status="proposed", summary="switched",
        source={"commit": "bbb2222", "file": "harness/memory_store.py"},
    )
    twin["tags"] = ["migration", "storage"]

    merged = memory_compact.merge_duplicate_detail(keeper, [twin])

    assert merged["tags"] == ["storage", "migration"]
    assert merged["source"]["file"] == "harness/memory_store.py"
    # The keeper's own commit is not overwritten by the twin's different one:
    # merging fills gaps, it never rewrites an assertion the keeper made.
    assert merged["source"]["commit"] == "aaa1111"
    # Pure: the inputs are untouched.
    assert keeper["tags"] == ["storage"]
    assert keeper["source"] == {"commit": "aaa1111"}


def test_merge_never_escalates_confidence_or_rewrites_identity() -> None:
    """The trust rule the review model exists to enforce. A `confirmed` twin
    must not lend its confidence to an `inferred` survivor -- that would
    promote an entry no human confirmed, which is exactly what review is for.
    """
    keeper = _entry(
        "mem-2026-08-12-001", status="proposed", summary="Switched.",
        confidence="inferred",
    )
    twin = _entry(
        "mem-2026-08-12-002", status="proposed", summary="switched",
        confidence="confirmed",
    )
    twin["ts"] = memory_store.format_ts(NOW + timedelta(days=1))

    merged = memory_compact.merge_duplicate_detail(keeper, [twin])

    for field in ("id", "ts", "type", "scope", "status", "summary", "confidence", "schema"):
        assert merged.get(field) == keeper.get(field), field


def test_merge_adopts_an_unknown_field_the_keeper_lacks() -> None:
    keeper = _entry("mem-2026-08-12-001", status="proposed", summary="Switched.")
    twin = _entry("mem-2026-08-12-002", status="proposed", summary="switched")
    twin["rationale"] = "measured on the benchmark harness"

    merged = memory_compact.merge_duplicate_detail(keeper, [twin])
    assert merged["rationale"] == "measured on the benchmark harness"


def test_merge_leaves_a_keeper_with_nothing_to_gain_unchanged() -> None:
    keeper = _entry(
        "mem-2026-08-12-001", status="proposed", summary="Switched.",
        source={"commit": "aaa1111"},
    )
    twin = _entry("mem-2026-08-12-002", status="proposed", summary="switched")
    assert memory_compact.merge_duplicate_detail(keeper, [twin]) == keeper


def test_compact_folds_the_dropped_twins_support_into_the_survivor(project: Path) -> None:
    """End-to-end, and the case ranking alone could not get right.

    The richer entry wins on `detail_rank` -- it carries the extra source key
    -- but the poorer one held a tag the winner lacked. Ranking would have
    archived that tag; merging keeps it. The loser is still recoverable
    verbatim from the archive, so the fold is auditable rather than a silent
    edit, and the winner keeps its own id and claim.
    """
    poorer_id, richer_id = "mem-2026-08-10-001", "mem-2026-08-10-002"
    poorer = _entry(
        poorer_id, status="proposed", summary="Switched to Postgres.",
        source={"commit": "aaa1111"},
    )
    poorer["tags"] = ["storage"]
    richer = _entry(
        richer_id, status="proposed", summary="switched to postgres",
        source={"commit": "aaa1111", "file": "harness/memory_store.py"},
    )
    richer["tags"] = ["migration"]
    _seed(project, [poorer, richer])

    result = memory_compact.compact(project, now=NOW)

    survivors = list(memory_store.iter_entries(memory_store.project_memory_dir(project)))
    assert [e["id"] for e in survivors] == [richer_id]
    assert survivors[0]["source"]["file"] == "harness/memory_store.py"
    assert sorted(survivors[0]["tags"]) == ["migration", "storage"]
    assert result.merged == 1
    assert poorer_id in result.archive_path.read_text(encoding="utf-8")


def test_compact_leaves_unparseable_lines_alone_while_merging(project: Path) -> None:
    """The splice stays line-level. A line this schema version cannot read may
    be a future schema, so gaining a rewrite path for merged survivors must
    not cost the guarantee that everything else is carried across untouched."""
    keeper = _entry("mem-2026-08-10-001", status="proposed", summary="Switched to Postgres.")
    twin = _entry(
        "mem-2026-08-10-002", status="proposed", summary="switched to postgres",
        source={"file": "harness/memory_store.py"},
    )
    directory = _seed(project, [keeper, twin])
    lines = memory_store.read_lines(directory)
    memory_store.write_lines([*lines, '{"schema": 99, "id": "from-the-future"}'], directory)

    memory_compact.compact(project, now=NOW)

    raw = (directory / "inbox.jsonl").read_text(encoding="utf-8")
    assert '"from-the-future"' in raw


# --------------------------------------------------------------------------
# near-duplicates: reported, never removed
# --------------------------------------------------------------------------


def _near(entries: list[dict]) -> set[str]:
    return memory_compact.find_near_duplicates(entries)


def test_near_duplicates_flag_a_summary_contained_in_a_longer_one() -> None:
    """The claim compaction must *not* act on. One summary's words are a
    subset of the other's, so they are probably the same work recorded twice
    -- but "probably" is exactly why a human decides and compaction reports."""
    short = _entry(
        "mem-2026-08-12-001", status="proposed", summary="Migrated the users table."
    )
    long = _entry(
        "mem-2026-08-12-002",
        status="proposed",
        summary="Migrated the users table to Postgres with zero downtime.",
    )
    assert _near([short, long]) == {"mem-2026-08-12-001", "mem-2026-08-12-002"}


def test_exact_duplicates_are_not_also_reported_as_near_duplicates() -> None:
    """`find_duplicates` already removes these. Counting them twice would
    advertise a review backlog that running `compact` silently clears."""
    entries = [
        _entry("mem-2026-08-12-001", status="proposed", summary="Migrated the users table."),
        _entry("mem-2026-08-12-002", status="proposed", summary="migrated the users table"),
    ]
    assert _near(entries) == set()


def test_near_duplicates_ignore_short_summaries() -> None:
    """Containment over two or three words is coincidence, not repetition."""
    entries = [
        _entry("mem-2026-08-12-001", status="proposed", summary="Fix it."),
        _entry("mem-2026-08-12-002", status="proposed", summary="Fix it later today."),
    ]
    assert _near(entries) == set()


def test_near_duplicates_are_scoped_by_type_and_never_touch_accepted() -> None:
    typed = [
        _entry(
            "mem-2026-08-12-001", type="task", status="proposed",
            summary="Migrated the users table.",
        ),
        _entry(
            "mem-2026-08-12-002", type="decision", status="proposed",
            summary="Migrated the users table to Postgres with zero downtime.",
        ),
    ]
    assert _near(typed) == set()

    accepted = [
        _entry("mem-2026-08-12-003", summary="Migrated the users table."),
        _entry(
            "mem-2026-08-12-004",
            summary="Migrated the users table to Postgres with zero downtime.",
        ),
    ]
    assert _near(accepted) == set()


def test_unrelated_proposals_are_not_near_duplicates() -> None:
    entries = [
        _entry("mem-2026-08-12-001", status="proposed", summary="Migrated the users table."),
        _entry("mem-2026-08-12-002", status="proposed", summary="Rotated the signing keys."),
    ]
    assert _near(entries) == set()


def test_compact_never_removes_a_near_duplicate(project: Path) -> None:
    """The load-bearing guarantee. The governing design permits *reporting*
    stale memory and forbids removal without a human, and a containment match
    is not equivalence: the longer entry states a strictly more specific claim
    that collapsing would destroy."""
    _seed(project, [
        _entry("mem-2026-08-10-001", status="proposed", summary="Migrated the users table."),
        _entry(
            "mem-2026-08-10-002",
            status="proposed",
            summary="Migrated the users table to Postgres with zero downtime.",
        ),
    ])
    result = memory_compact.compact(project, now=NOW)
    assert result.duplicates == 0
    assert _inbox_ids(project) == ["mem-2026-08-10-001", "mem-2026-08-10-002"]


def test_near_duplicate_pressure_points_at_review_not_compact() -> None:
    """Compaction cannot clear these, so the note must not send the operator
    to `compact` -- the exact failure the pressure design warns about."""
    entries = [
        _entry("mem-2026-08-12-001", status="proposed", summary="Migrated the users table."),
        _entry(
            "mem-2026-08-12-002",
            status="proposed",
            summary="Migrated the users table to Postgres with zero downtime.",
        ),
    ]
    measured = memory_compact.pressure(entries, NOW)
    assert measured.near_duplicate_proposals == 2
    assert measured.duplicate_proposals == 0

    note = next(n for n in memory_compact.pressure_notes(measured) if "near-duplicate" in n)
    assert "review" in note and "compact" not in note


def test_near_duplicate_note_is_silent_at_zero() -> None:
    measured = memory_compact.pressure(
        [_entry("mem-2026-08-12-001", status="proposed")], NOW
    )
    assert measured.near_duplicate_proposals == 0
    assert not any("near-duplicate" in n for n in memory_compact.pressure_notes(measured))


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


def test_compact_keeps_the_evidenced_twin_and_archives_the_bare_one(project: Path) -> None:
    """End-to-end proof that the survivor rule reaches the store of record.

    `find_duplicates` feeds `compact` and `pressure` alike, so ranking inside
    it is the whole wiring -- but "the helper returns the right set" is not the
    claim that matters. This asserts the claim that does: the entry left in the
    inbox is the one carrying the commit, and the bare twin is recoverable from
    the archive rather than gone.
    """
    bare_id, proven_id = "mem-2026-08-10-001", "mem-2026-08-10-002"
    _seed(project, [
        _entry(bare_id, status="proposed", summary="Switched to Postgres."),
        _entry(
            proven_id,
            status="proposed",
            summary="switched to postgres",
            source={"commit": "abc1234", "file": "harness/memory_store.py"},
        ),
    ])

    result = memory_compact.compact(project, now=NOW)

    # The bare twin was written *first* and has the lower id, so the old rule
    # would have kept it and archived the commit.
    assert _inbox_ids(project) == [proven_id]
    assert result.duplicates == 1
    archive = result.archive_path.read_text(encoding="utf-8")
    assert bare_id in archive and proven_id not in archive
    survivor = next(
        e for e in memory_store.iter_entries(memory_store.project_memory_dir(project))
    )
    assert survivor["source"]["commit"] == "abc1234"


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


# --------------------------------------------------------------------------
# compaction pressure -- the signal that says compaction is worth running
# --------------------------------------------------------------------------


def _proposal(entry_id: str, summary: str, *, ts: datetime | None = None) -> dict:
    return _entry(entry_id, type="discovery", status="proposed", summary=summary, ts=ts)


def test_pressure_counts_exactly_what_compaction_removes(project: Path) -> None:
    """The premise of the whole nudge: what the note advertises is what running
    `compact` actually clears. If the predicates and the removal rules ever
    diverge, the operator runs the command, sees fewer entries go, and stops
    believing the line. This is the test that catches that divergence."""
    _seed(project, [
        _entry("mem-2026-08-10-001", type="task", status="resolved", summary="Shipped."),
        _entry("mem-2026-08-10-002", status="rejected", summary="Not doing that."),
        _proposal("mem-2026-08-10-003", "The same observation."),
        _proposal("mem-2026-08-10-004", "The same observation!"),
        _proposal("mem-2026-08-10-005", "the  SAME observation"),
        _proposal("mem-2026-08-10-006", "A different observation."),
        _entry("mem-2026-08-10-007", summary="Live knowledge."),
    ])

    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(project)))
    pressure = memory_compact.pressure(entries, NOW)

    before = set(_inbox_ids(project))
    memory_compact.compact(project, now=NOW)
    removed = before - set(_inbox_ids(project))

    assert pressure.closed_in_inbox == 2
    assert pressure.duplicate_proposals == 2
    assert pressure.closed_in_inbox + pressure.duplicate_proposals == len(removed)


def test_pressure_ignores_terminal_entries_that_compaction_cannot_remove() -> None:
    """`compact` discards the empty id before removing, so a terminal entry
    with no id survives the pass. Counting it would overstate the payoff."""
    pressure = memory_compact.pressure(
        [{"id": "", "type": "task", "status": "resolved", "ts": memory_store.format_ts(NOW)}],
        NOW,
    )
    assert pressure.closed_in_inbox == 0


def test_a_clean_store_says_nothing() -> None:
    """Silence at zero is what keeps the note credible when it appears."""
    entries = [_entry("mem-2026-08-10-001", summary="Live knowledge.")]
    pressure = memory_compact.pressure(entries, NOW)
    assert memory_compact.pressure_notes(pressure) == ()


def test_the_compact_note_names_both_counts_and_the_command() -> None:
    entries = [
        _entry("mem-2026-08-10-001", type="task", status="resolved"),
        _proposal("mem-2026-08-10-002", "Repeated."),
        _proposal("mem-2026-08-10-003", "Repeated."),
    ]
    notes = memory_compact.pressure_notes(memory_compact.pressure(entries, NOW))
    compact_note = next(n for n in notes if "compact" in n)
    assert "1 closed" in compact_note
    assert "1 duplicate proposal" in compact_note
    assert "`agent-harness-memory compact`" in compact_note


def test_the_compact_note_omits_a_count_that_is_zero() -> None:
    """`0 duplicate proposals` is noise in a line whose whole value is that it
    only ever states something worth acting on."""
    entries = [_entry("mem-2026-08-10-001", type="task", status="resolved")]
    notes = memory_compact.pressure_notes(memory_compact.pressure(entries, NOW))
    compact_note = next(n for n in notes if "compact" in n)
    assert "duplicate" not in compact_note


def test_age_alone_fires_the_review_note() -> None:
    """One proposal left sitting a fortnight is the observed failure; the count
    trigger would never catch it."""
    old = NOW - timedelta(days=23)
    entries = [_proposal("mem-2026-07-20-001", "Nobody has looked at this.", ts=old)]
    pressure = memory_compact.pressure(entries, NOW)

    assert pressure.unreviewed == 1
    assert pressure.oldest_unreviewed_days == 23

    notes = memory_compact.pressure_notes(pressure)
    assert len(notes) == 1
    assert "oldest 23d" in notes[0]
    assert "`agent-harness-memory review`" in notes[0]


def test_count_alone_fires_the_review_note() -> None:
    """A sudden pile-up of proposals recorded today is not stale, but it is
    still a backlog no human has been through."""
    entries = [
        _proposal(f"mem-2026-08-12-00{n}", f"Observation {n}.") for n in range(1, 6)
    ]
    notes = memory_compact.pressure_notes(memory_compact.pressure(entries, NOW))
    assert len(notes) == 1
    assert "5 proposals unreviewed" in notes[0]


def test_a_small_fresh_backlog_stays_quiet() -> None:
    """Four proposals recorded this morning is the system working."""
    entries = [
        _proposal(f"mem-2026-08-12-00{n}", f"Observation {n}.") for n in range(1, 5)
    ]
    assert memory_compact.pressure_notes(memory_compact.pressure(entries, NOW)) == ()


@pytest.mark.parametrize("age,fires", [(6, False), (7, True), (8, True)])
def test_the_age_trigger_fires_on_its_exact_boundary(age: int, fires: bool) -> None:
    """One proposal is well under the count threshold, so this isolates the age
    trigger at the boundary the constant names."""
    entries = [_proposal("mem-2026-08-01-001", "Waiting.", ts=NOW - timedelta(days=age))]
    notes = memory_compact.pressure_notes(memory_compact.pressure(entries, NOW))
    assert bool(notes) is fires


def test_a_future_timestamp_cannot_print_a_negative_age() -> None:
    """A clock skew or a hand-edited stamp must not produce `oldest -3d`. An
    operator reads that as a broken tool rather than as a fact about the store,
    and the line's only value is that it is believed."""
    ahead = NOW + timedelta(days=3)
    entries = [
        _proposal(f"mem-2026-08-15-00{n}", f"From the future {n}.", ts=ahead)
        for n in range(1, 6)
    ]
    pressure = memory_compact.pressure(entries, NOW)
    assert pressure.oldest_unreviewed_days == 0

    notes = memory_compact.pressure_notes(pressure)
    assert len(notes) == 1
    assert "-" not in notes[0].split("oldest")[1].split("d")[0]


def test_a_naive_now_is_read_as_utc_rather_than_raising() -> None:
    """`format_ts` treats a naive datetime as UTC, so measuring against one
    must not be the single input that takes the packet down."""
    entries = [_proposal("mem-2026-08-01-001", "Waiting.", ts=NOW - timedelta(days=9))]
    pressure = memory_compact.pressure(entries, NOW.replace(tzinfo=None))
    assert pressure.oldest_unreviewed_days == 9


def test_malformed_entries_neither_raise_nor_inflate_the_counts() -> None:
    """`pressure` is total over bad input rather than wrapped in a catch: an
    unrecognised status is neither terminal nor proposed, and an entry whose
    timestamp cannot be read still counts as unreviewed but cannot contribute
    an age."""
    entries = [
        {"id": "mem-2026-08-10-001", "type": "task", "status": "in-flight"},
        {"id": "mem-2026-08-10-002", "type": "discovery", "status": "proposed"},
        {"id": "mem-2026-08-10-003", "type": "discovery", "status": "proposed", "ts": "last Tuesday"},
        {"id": "mem-2026-08-10-004"},
    ]
    pressure = memory_compact.pressure(entries, NOW)

    assert pressure.closed_in_inbox == 0
    assert pressure.unreviewed == 2
    assert pressure.oldest_unreviewed_days is None


def test_the_review_note_drops_the_age_clause_when_no_age_is_known() -> None:
    entries = [
        {
            "id": f"mem-2026-08-10-00{n}",
            "type": "discovery",
            "scope": "project",
            "status": "proposed",
            "summary": f"Observation {n}.",
        }
        for n in range(1, 6)
    ]
    notes = memory_compact.pressure_notes(memory_compact.pressure(entries, NOW))
    assert len(notes) == 1
    assert "5 proposals unreviewed" in notes[0]
    assert "oldest" not in notes[0]


def test_machine_pressure_never_asks_for_a_compaction_that_cannot_happen() -> None:
    """`compact` operates on the project directory only. A compact note against
    machine pressure would print a command that silently clears nothing --
    exactly the failure that trains an operator to ignore the line."""
    entries = [
        _entry("mem-2026-08-10-001", type="task", status="resolved"),
        _proposal("mem-2026-08-10-002", "Repeated."),
        _proposal("mem-2026-08-10-003", "Repeated."),
    ]
    notes = memory_compact.pressure_notes(memory_compact.pressure(entries, NOW), scope="machine")
    assert not any("compact" in n for n in notes)


def test_the_machine_review_note_names_the_scope_the_flag_does_not_default_to() -> None:
    old = NOW - timedelta(days=23)
    entries = [
        _proposal(f"mem-2026-07-20-00{n}", f"Unreviewed on the machine {n}.", ts=old)
        for n in range(1, 7)
    ]
    notes = memory_compact.pressure_notes(memory_compact.pressure(entries, NOW), scope="machine")
    assert len(notes) == 1
    assert "6 machine proposals unreviewed, oldest 23d" in notes[0]
    assert "`agent-harness-memory review --scope machine`" in notes[0]
# --------------------------------------------------------------------------
# due-ness -- when `maintain` should run this
# --------------------------------------------------------------------------


@pytest.fixture
def no_interval_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(memory_compact.COMPACT_INTERVAL_ENV, raising=False)


def test_the_interval_defaults_and_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(memory_compact.COMPACT_INTERVAL_ENV, raising=False)
    assert memory_compact.compact_interval_days() == memory_compact.COMPACT_INTERVAL_DAYS
    monkeypatch.setenv(memory_compact.COMPACT_INTERVAL_ENV, "3")
    assert memory_compact.compact_interval_days() == 3
    monkeypatch.setenv(memory_compact.COMPACT_INTERVAL_ENV, "weekly")
    assert memory_compact.compact_interval_days() == memory_compact.COMPACT_INTERVAL_DAYS
    monkeypatch.setenv(memory_compact.COMPACT_INTERVAL_ENV, "-2")
    assert memory_compact.compact_interval_days() == 0


def test_a_fresh_project_has_never_been_compacted(project: Path) -> None:
    assert memory_compact.last_compacted(memory_store.project_memory_dir(project)) is None


def test_compaction_stamps_active_md_with_when_it_ran(project: Path) -> None:
    """The stamp `render_active` writes is the record of the last run -- there
    is deliberately no second state file that could disagree with it."""
    memory_compact.compact(project, now=NOW)
    assert memory_compact.last_compacted(memory_store.project_memory_dir(project)) == NOW


def test_an_unparseable_stamp_reads_as_never(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    (directory / "active.md").write_text(
        "# Active memory\n\nLast compacted: yesterday\n", encoding="utf-8"
    )
    assert memory_compact.last_compacted(directory) is None


def test_a_never_compacted_project_is_due(project: Path, no_interval_override: None) -> None:
    status = memory_compact.compaction_due(memory_store.project_memory_dir(project), now=NOW)
    assert status.due is True
    assert status.reason == "never compacted"
    assert status.last is None


def test_a_recently_compacted_project_is_not_due(project: Path, no_interval_override: None) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_compact.compact(project, now=NOW)
    status = memory_compact.compaction_due(directory, now=NOW + timedelta(days=2))
    assert status.due is False
    assert "next due in 5 day(s)" in status.reason
    assert status.last == NOW


def test_a_project_past_the_interval_is_due(project: Path, no_interval_override: None) -> None:
    directory = memory_store.project_memory_dir(project)
    memory_compact.compact(project, now=NOW)
    status = memory_compact.compaction_due(directory, now=NOW + timedelta(days=7))
    assert status.due is True
    assert "7 day(s) ago" in status.reason


def test_a_zero_interval_is_always_due(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(memory_compact.COMPACT_INTERVAL_ENV, "0")
    directory = memory_store.project_memory_dir(project)
    memory_compact.compact(project, now=NOW)
    assert memory_compact.compaction_due(directory, now=NOW).due is True


def test_deciding_due_ness_takes_no_lock_and_writes_nothing(project: Path) -> None:
    directory = memory_store.project_memory_dir(project)
    before = (directory / "active.md").read_bytes()
    memory_compact.compaction_due(directory, now=NOW)
    assert (directory / "active.md").read_bytes() == before
    assert not (directory / memory_compact.LOCK_NAME).exists()
