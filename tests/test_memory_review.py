"""Tests for review: the only path from `proposed` to `accepted`.

Covers `harness.memory_review` transitions and the `review` CLI surface.
Exit codes are contract: 0 success, 1 nothing to do / not found, 2 rejected.

Every test fakes HOME so nothing touches the real `~/.agent-harness/`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory, memory_review, memory_store

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


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


def _remember(project: Path, **overrides) -> str:
    entry = {
        "type": "decision",
        "scope": "project",
        "summary": "Chose JSONL over SQLite for the inbox.",
        **overrides,
    }
    memory_store.append_entries([entry], directory=_dir(project), now=NOW)
    return list(memory_store.iter_entries(_dir(project)))[-1]["id"]


def _entry(project: Path, entry_id: str) -> dict:
    for rec in memory_store.iter_entries(_dir(project)):
        if rec["id"] == entry_id:
            return rec
    raise AssertionError(f"{entry_id} not found")


# --- accept ---------------------------------------------------------------


def test_accept_promotes_to_accepted_and_confirmed(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert result.entry["status"] == "accepted"
    assert result.entry["confidence"] == "confirmed"
    assert _entry(project, entry_id)["status"] == "accepted"


def test_accept_records_the_operator_attestation_as_evidence(project: Path) -> None:
    """Accepting with no commit/file is itself the evidence, and must say so."""
    entry_id = _remember(project)
    result = memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert result.entry["source"]["manual"] is True


def test_accept_prefers_a_supplied_commit_over_manual_attestation(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "accept", commit="a5f6f70", now=NOW
    )
    assert result.entry["source"]["commit"] == "a5f6f70"
    assert "manual" not in result.entry["source"]


def test_accept_can_amend_the_summary(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "accept",
        summary="Inbox is JSONL; durable is Markdown.", now=NOW,
    )
    assert result.entry["summary"] == "Inbox is JSONL; durable is Markdown."


def test_accept_preserves_existing_project_attribution(project: Path) -> None:
    entry_id = _remember(project, source={"project": "proj-abc12345"})
    result = memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert result.entry["source"]["project"] == "proj-abc12345"


# --- reject / resolve -----------------------------------------------------


def test_reject_closes_the_entry_without_accepting_it(project: Path) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "reject", note="superseded by the design doc", now=NOW
    )
    assert result.entry["status"] == "rejected"
    assert result.entry["review_note"] == "superseded by the design doc"


def test_resolve_requires_evidence(project: Path) -> None:
    entry_id = _remember(project, type="task", summary="Wire up the context command.")
    with pytest.raises(ValueError, match="evidence"):
        memory_review.apply_review(_dir(project), entry_id, "resolve", now=NOW)


def test_resolve_keeps_its_evidence(project: Path) -> None:
    entry_id = _remember(project, type="blocker", summary="Compaction needs a lock.")
    result = memory_review.apply_review(
        _dir(project), entry_id, "resolve", commit="9248b7a", now=NOW
    )
    assert result.entry["status"] == "resolved"
    assert result.entry["resolution"]["commit"] == "9248b7a"


def test_only_tasks_and_blockers_can_be_resolved(project: Path) -> None:
    entry_id = _remember(project, type="decision")
    with pytest.raises(ValueError, match="resolved"):
        memory_review.apply_review(
            _dir(project), entry_id, "resolve", note="done", now=NOW
        )


# --- supersede ------------------------------------------------------------


def test_supersede_preserves_the_original_and_links_the_replacement(
    project: Path,
) -> None:
    entry_id = _remember(project)
    result = memory_review.apply_review(
        _dir(project), entry_id, "supersede",
        summary="Switched the inbox from JSONL to SQLite after all.", now=NOW,
    )
    original = _entry(project, entry_id)
    assert original["status"] == "superseded"
    assert original["summary"] == "Chose JSONL over SQLite for the inbox."
    assert original["superseded_by"] == result.created["id"]
    assert result.created["supersedes"] == entry_id
    assert result.created["status"] == "accepted"


def test_supersede_needs_a_replacement_summary(project: Path) -> None:
    entry_id = _remember(project)
    with pytest.raises(ValueError, match="summary"):
        memory_review.apply_review(_dir(project), entry_id, "supersede", now=NOW)


def test_supersede_carries_the_original_tags_forward(project: Path) -> None:
    entry_id = _remember(project, tags=["storage", "schema"])
    result = memory_review.apply_review(
        _dir(project), entry_id, "supersede", summary="Now SQLite.", now=NOW
    )
    assert result.created["tags"] == ["storage", "schema"]


# --- guards ---------------------------------------------------------------


def test_a_closed_entry_is_not_reviewed_again(project: Path) -> None:
    entry_id = _remember(project)
    memory_review.apply_review(_dir(project), entry_id, "reject", now=NOW)
    with pytest.raises(ValueError, match="already rejected"):
        memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)


def test_an_unknown_entry_id_raises_lookup_error(project: Path) -> None:
    with pytest.raises(LookupError):
        memory_review.apply_review(
            _dir(project), "mem-1999-01-01-001", "accept", now=NOW
        )


def test_an_unknown_action_raises(project: Path) -> None:
    entry_id = _remember(project)
    with pytest.raises(ValueError, match="unknown review action"):
        memory_review.apply_review(_dir(project), entry_id, "vibe", now=NOW)


def test_a_failed_transition_leaves_the_inbox_byte_identical(project: Path) -> None:
    entry_id = _remember(project, type="decision")
    path = _dir(project) / "inbox.jsonl"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        memory_review.apply_review(
            _dir(project), entry_id, "resolve", note="x", now=NOW
        )
    assert path.read_bytes() == before


def test_review_carries_unparseable_lines_across_untouched(project: Path) -> None:
    """A corrupt line elsewhere must survive the review of an unrelated entry."""
    entry_id = _remember(project)
    path = _dir(project) / "inbox.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")

    memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert "{not json at all" in path.read_text(encoding="utf-8")


def test_review_does_not_renumber_surrounding_entries(project: Path) -> None:
    first = _remember(project)
    second = _remember(project, summary="A second, unrelated decision.")
    memory_review.apply_review(_dir(project), first, "accept", now=NOW)
    ids = [r["id"] for r in memory_store.iter_entries(_dir(project))]
    assert ids == [first, second]


# --- store primitives -----------------------------------------------------


def test_write_lines_replaces_atomically_and_leaves_no_temp_file(project: Path) -> None:
    directory = _dir(project)
    memory_store.write_lines(['{"schema": 1}'], directory)
    assert (directory / "inbox.jsonl").read_text(encoding="utf-8") == '{"schema": 1}\n'
    assert not (directory / "inbox.jsonl.tmp").exists()


def test_rewrite_entries_refuses_records_without_an_id(project: Path) -> None:
    with pytest.raises(ValueError, match="id"):
        memory_store.rewrite_entries(
            [{"type": "task", "scope": "project", "summary": "no id"}],
            directory=_dir(project), now=NOW,
        )


def test_rewrite_entries_validates_before_writing_anything(project: Path) -> None:
    _remember(project)
    path = _dir(project) / "inbox.jsonl"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        memory_store.rewrite_entries(
            [{"id": "mem-2026-08-07-009", "type": "vibe", "scope": "project",
              "summary": "bad type"}],
            directory=_dir(project), now=NOW,
        )
    assert path.read_bytes() == before


# --- CLI ------------------------------------------------------------------


def test_review_list_reports_pending_entries(project: Path, capsys) -> None:
    _remember(project)
    assert memory.main(["review", "list", "--project", str(project)]) == 0
    assert "awaiting review" in capsys.readouterr().out


def test_review_list_on_an_empty_inbox_exits_1(project: Path, capsys) -> None:
    assert memory.main(["review", "list", "--project", str(project)]) == 1
    assert "nothing awaiting review" in capsys.readouterr().out


def test_review_accept_through_the_cli(project: Path, capsys) -> None:
    entry_id = _remember(project)
    assert memory.main(["review", "accept", entry_id, "--project", str(project)]) == 0
    assert "accepted" in capsys.readouterr().out
    assert _entry(project, entry_id)["status"] == "accepted"


def test_review_action_without_an_id_exits_2(project: Path, capsys) -> None:
    assert memory.main(["review", "accept", "--project", str(project)]) == 2
    assert "entry id" in capsys.readouterr().err


def test_review_unknown_id_exits_1(project: Path, capsys) -> None:
    code = memory.main(
        ["review", "accept", "mem-1999-01-01-001", "--project", str(project)]
    )
    assert code == 1
    assert "no entry" in capsys.readouterr().err


def test_review_bad_transition_exits_2(project: Path, capsys) -> None:
    entry_id = _remember(project, type="decision")
    code = memory.main(
        ["review", "resolve", entry_id, "--project", str(project), "--note", "done"]
    )
    assert code == 2
    assert "review rejected" in capsys.readouterr().err


def test_review_operates_on_machine_scope_too(project: Path) -> None:
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "summary": "Quote OneDrive paths."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    entry_id = list(
        memory_store.iter_entries(memory_store.machine_memory_dir())
    )[-1]["id"]
    code = memory.main(
        ["review", "accept", entry_id, "--project", str(project), "--scope", "machine"]
    )
    assert code == 0
    machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    assert machine[-1]["status"] == "accepted"


def test_accepted_entries_survive_a_round_trip_through_json(project: Path) -> None:
    entry_id = _remember(project)
    memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    lines = (_dir(project) / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["status"] == "accepted"


# --- batches: triage's one-write contract ---------------------------------


def _pending_ids(project: Path) -> list[str]:
    return [r["id"] for r in memory_review.pending_entries(_dir(project))]


def test_a_batch_applies_every_decision_in_one_write(project: Path) -> None:
    a = _remember(project, summary="Accept me.")
    b = _remember(project, summary="Reject me.")
    c = _remember(project, type="task", summary="Resolve me.")

    results = memory_review.apply_reviews(_dir(project), [
        memory_review.Decision(a, "accept", commit="abc1234"),
        memory_review.Decision(b, "reject", note="dup"),
        memory_review.Decision(c, "resolve", note="shipped"),
    ], now=NOW)

    assert [r.entry["status"] for r in results] == ["accepted", "rejected", "resolved"]
    assert _entry(project, a)["source"]["commit"] == "abc1234"
    assert _entry(project, b)["review_note"] == "dup"
    assert _entry(project, c)["resolution"]["note"] == "shipped"
    assert _pending_ids(project) == []


def test_one_bad_decision_rejects_the_whole_batch_and_writes_nothing(project: Path) -> None:
    """Exit 2 has to mean "the inbox is exactly as it was". A batch that
    half-applied would make the code meaningless to an unattended caller."""
    a = _remember(project, summary="Fine.")
    b = _remember(project, type="decision", summary="Cannot be resolved.")
    path = _dir(project) / "inbox.jsonl"
    before = path.read_bytes()

    with pytest.raises(ValueError, match=b):
        memory_review.apply_reviews(_dir(project), [
            memory_review.Decision(a, "accept"),
            memory_review.Decision(b, "resolve", note="x", line=7),
        ], now=NOW)

    assert path.read_bytes() == before


def test_an_unknown_id_in_a_batch_names_the_line_and_writes_nothing(project: Path) -> None:
    a = _remember(project)
    before = (_dir(project) / "inbox.jsonl").read_bytes()
    with pytest.raises(LookupError, match="line 3"):
        memory_review.apply_reviews(_dir(project), [
            memory_review.Decision(a, "accept", line=1),
            memory_review.Decision("mem-1999-01-01-001", "reject", line=3),
        ], now=NOW)
    assert (_dir(project) / "inbox.jsonl").read_bytes() == before


def test_a_batch_refuses_to_decide_one_entry_twice(project: Path) -> None:
    a = _remember(project)
    with pytest.raises(ValueError, match="more than once"):
        memory_review.apply_reviews(_dir(project), [
            memory_review.Decision(a, "accept"),
            memory_review.Decision(a, "reject"),
        ], now=NOW)
    assert _entry(project, a)["status"] == "proposed"


def test_skip_decisions_transition_nothing(project: Path) -> None:
    a = _remember(project)
    before = (_dir(project) / "inbox.jsonl").read_bytes()
    results = memory_review.apply_reviews(
        _dir(project), [memory_review.Decision(a, "skip")], now=NOW
    )
    assert results == []
    assert (_dir(project) / "inbox.jsonl").read_bytes() == before


def test_two_supersedes_in_one_batch_get_distinct_replacement_ids(project: Path) -> None:
    """`next_entry_id` scans the file, which the batch has not written yet;
    without an allocator both replacements would be handed the same id."""
    a = _remember(project, summary="Old A.")
    b = _remember(project, summary="Old B.")
    results = memory_review.apply_reviews(_dir(project), [
        memory_review.Decision(a, "supersede", summary="New A."),
        memory_review.Decision(b, "supersede", summary="New B."),
    ], now=NOW)
    created = [r.created["id"] for r in results]
    assert len(set(created)) == 2
    ids = [r["id"] for r in memory_store.iter_entries(_dir(project))]
    assert len(ids) == len(set(ids)) == 4
    assert _entry(project, a)["superseded_by"] == created[0]
    assert _entry(project, b)["superseded_by"] == created[1]


def test_a_dry_run_batch_plans_but_writes_nothing(project: Path) -> None:
    a = _remember(project)
    before = (_dir(project) / "inbox.jsonl").read_bytes()
    results = memory_review.apply_reviews(
        _dir(project), [memory_review.Decision(a, "accept")], now=NOW, dry_run=True
    )
    assert results[0].entry["status"] == "accepted"
    assert (_dir(project) / "inbox.jsonl").read_bytes() == before


def test_a_batch_carries_unparseable_lines_across_untouched(project: Path) -> None:
    a = _remember(project)
    path = _dir(project) / "inbox.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    memory_review.apply_reviews(_dir(project), [memory_review.Decision(a, "accept")], now=NOW)
    assert "{not json at all" in path.read_text(encoding="utf-8")


# --- decisions file ---------------------------------------------------------


def test_decision_lines_follow_the_review_cli_grammar() -> None:
    decision = memory_review.parse_decision_line(
        'supersede mem-2026-08-07-001 --summary "Now SQLite." --commit abc --note ok', line=4
    )
    assert decision == memory_review.Decision(
        "mem-2026-08-07-001", "supersede", summary="Now SQLite.", commit="abc", note="ok", line=4
    )


@pytest.mark.parametrize("text", ["", "   ", "# a comment", "## tag:memory (2)"])
def test_blank_and_comment_lines_are_not_decisions(text: str) -> None:
    assert memory_review.parse_decision_line(text) is None


@pytest.mark.parametrize(
    "text,problem",
    [
        ("accept", "expected"),
        ("vibe mem-2026-08-07-001", "unknown action"),
        ("accept mem-2026-08-07-001 --wat x", "unexpected"),
        ("accept mem-2026-08-07-001 --note", "needs a value"),
        ('accept mem-2026-08-07-001 --note "unterminated', "quotation"),
    ],
)
def test_malformed_decision_lines_name_the_line(text: str, problem: str) -> None:
    with pytest.raises(ValueError, match="line 9") as excinfo:
        memory_review.parse_decision_line(text, line=9)
    assert problem in str(excinfo.value).lower()


def test_parse_decisions_reads_a_whole_file(project: Path) -> None:
    decisions = memory_review.parse_decisions([
        "# header",
        "skip mem-2026-08-07-001",
        "",
        "accept mem-2026-08-07-002 --file docs/x.md",
    ])
    assert [d.action for d in decisions] == ["skip", "accept"]
    assert decisions[1].file == "docs/x.md"
    assert decisions[1].line == 4


def test_the_template_lists_every_pending_entry_as_skip(project: Path) -> None:
    a = _remember(project, summary="First.", tags=["memory"])
    b = _remember(project, type="task", summary="Second.")
    text = memory_review.render_template(memory_review.pending_entries(_dir(project)))
    assert f"skip {a}" in text
    assert f"skip {b}" in text
    assert "# [decision] First." in text
    assert "tags: memory" in text
    # The template must round-trip through the parser as a no-op.
    decisions = memory_review.parse_decisions(text.splitlines())
    assert {d.entry_id for d in decisions} == {a, b}
    assert all(d.is_skip for d in decisions)


# --- selectors and grouping ------------------------------------------------


def test_selectors_match_by_id_tag_type_and_all(project: Path) -> None:
    a = _remember(project, tags=["memory", "plan"])
    b = _remember(project, type="task", summary="A task.", tags=["plan"])
    c = _remember(project, type="lesson", summary="Untagged lesson.")
    entries = list(memory_store.iter_entries(_dir(project)))

    def ids(selector: str) -> list[str]:
        return [e["id"] for e in memory_review.select_pending(entries, selector)]

    assert ids(a) == [a]
    assert ids("tag:plan") == [a, b]
    assert ids("tag:memory") == [a]
    assert ids("tag:untagged") == [c]
    assert ids("type:task") == [b]
    assert ids("all") == [a, b, c]


def test_selectors_never_reach_a_non_pending_entry(project: Path) -> None:
    a = _remember(project, tags=["plan"])
    memory_review.apply_review(_dir(project), a, "accept", now=NOW)
    entries = list(memory_store.iter_entries(_dir(project)))
    assert memory_review.select_pending(entries, "tag:plan") == []
    with pytest.raises(ValueError, match="not a pending entry id"):
        memory_review.select_pending(entries, a)


def test_a_selector_with_an_unknown_type_is_rejected(project: Path) -> None:
    with pytest.raises(ValueError, match="unknown type"):
        memory_review.select_pending([], "type:vibe")


def test_topic_grouping_clusters_on_the_most_common_tag(project: Path) -> None:
    """Each entry lands in exactly one cluster -- the tag most shared across
    the backlog -- so the listing reads as a few themes, not N lines."""
    a = _remember(project, summary="A.", tags=["plan", "memory"])
    b = _remember(project, summary="B.", tags=["memory"])
    c = _remember(project, summary="C.", tags=["plan", "memory"])
    d = _remember(project, type="task", summary="D.")
    groups = memory_review.group_pending(memory_review.pending_entries(_dir(project)))

    assert [(g.key, g.size) for g in groups] == [("tag:memory", 3), ("type:task", 1)]
    assert [e["id"] for e in groups[0].entries] == [a, b, c]
    assert groups[1].entries[0]["id"] == d
    assert groups[0].types == {"decision": 3}


def test_grouping_by_type_ignores_tags(project: Path) -> None:
    _remember(project, tags=["plan"])
    _remember(project, type="task", summary="T.", tags=["plan"])
    groups = memory_review.group_pending(memory_review.pending_entries(_dir(project)), by="type")
    assert sorted(g.key for g in groups) == ["type:decision", "type:task"]


def test_grouping_is_deterministic_across_tag_ties(project: Path) -> None:
    _remember(project, summary="X.", tags=["zeta", "alpha"])
    groups = memory_review.group_pending(memory_review.pending_entries(_dir(project)))
    assert [g.key for g in groups] == ["tag:alpha"]


def test_an_unknown_grouping_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown grouping"):
        memory_review.group_pending([], by="vibes")


# --- triage CLI -----------------------------------------------------------


def test_triage_list_groups_the_backlog(project: Path, capsys) -> None:
    a = _remember(project, tags=["memory"])
    b = _remember(project, type="task", summary="Do it.")
    assert memory.main(["triage", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "2 proposed in project memory, 2 group(s) by topic" in out
    assert "tag:memory" in out and "type:task" in out
    assert a in out and b in out
    assert "triage apply" in out


def test_triage_list_on_an_empty_backlog_exits_1(project: Path, capsys) -> None:
    assert memory.main(["triage", "--project", str(project)]) == 1
    assert "nothing awaiting review" in capsys.readouterr().out


def test_triage_list_can_show_one_group_and_rejects_an_unknown_one(project: Path, capsys) -> None:
    _remember(project, tags=["memory"])
    _remember(project, type="task", summary="Do it.")
    assert memory.main(["triage", "--project", str(project), "--group", "type:task"]) == 0
    out = capsys.readouterr().out
    assert "type:task" in out and "tag:memory" not in out
    assert memory.main(["triage", "--project", str(project), "--group", "tag:nope"]) == 2
    assert "groups are" in capsys.readouterr().err


def test_triage_template_writes_a_decisions_file(project: Path, tmp_path: Path, capsys) -> None:
    a = _remember(project)
    out = tmp_path / "decisions.txt"
    assert memory.main(["triage", "template", "--project", str(project), "--out", str(out)]) == 0
    assert f"skip {a}" in out.read_text(encoding="utf-8")
    assert "triage apply --decisions" in capsys.readouterr().out


def test_triage_template_to_stdout(project: Path, capsys) -> None:
    a = _remember(project)
    assert memory.main(["triage", "template", "--project", str(project)]) == 0
    assert f"skip {a}" in capsys.readouterr().out


def test_triage_apply_from_a_decisions_file(project: Path, tmp_path: Path, capsys) -> None:
    a = _remember(project, summary="Keep.")
    b = _remember(project, summary="Drop.")
    c = _remember(project, summary="Later.")
    decisions = tmp_path / "d.txt"
    decisions.write_text(
        f"accept {a} --commit abc1234\nreject {b} --note dup\nskip {c}\n", encoding="utf-8"
    )
    assert memory.main(
        ["triage", "apply", "--project", str(project), "--decisions", str(decisions)]
    ) == 0
    out = capsys.readouterr().out
    assert "applied 2 decision(s)" in out
    assert _entry(project, a)["status"] == "accepted"
    assert _entry(project, b)["status"] == "rejected"
    assert _entry(project, c)["status"] == "proposed"


def test_triage_apply_with_selectors(project: Path, capsys) -> None:
    a = _remember(project, tags=["plan"])
    b = _remember(project, tags=["plan"])
    c = _remember(project, type="task", summary="T.")
    code = memory.main([
        "triage", "apply", "--project", str(project),
        "--accept", "tag:plan", "--resolve", c, "--note", "verified",
    ])
    assert code == 0
    assert _entry(project, a)["status"] == _entry(project, b)["status"] == "accepted"
    assert _entry(project, c)["resolution"]["note"] == "verified"


def test_triage_apply_can_supersede(project: Path) -> None:
    a = _remember(project, summary="Old.")
    assert memory.main([
        "triage", "apply", "--project", str(project), "--supersede", a, "--summary", "New.",
    ]) == 0
    assert _entry(project, a)["status"] == "superseded"


def test_triage_apply_dedupes_overlapping_selectors_with_the_same_verb(project: Path) -> None:
    a = _remember(project, tags=["plan", "memory"])
    assert memory.main([
        "triage", "apply", "--project", str(project),
        "--accept", "tag:plan", "--accept", "tag:memory",
    ]) == 0
    assert _entry(project, a)["status"] == "accepted"


def test_triage_apply_rejects_conflicting_verbs_for_one_entry(project: Path, capsys) -> None:
    a = _remember(project, type="task", summary="T.", tags=["plan"])
    code = memory.main([
        "triage", "apply", "--project", str(project), "--accept", "tag:plan", "--reject", a,
    ])
    assert code == 2
    assert "matched both" in capsys.readouterr().err
    assert _entry(project, a)["status"] == "proposed"


def test_triage_apply_with_a_bad_decision_writes_nothing_and_exits_2(
    project: Path, tmp_path: Path, capsys
) -> None:
    a = _remember(project)
    b = _remember(project, type="decision", summary="Not resolvable.")
    path = _dir(project) / "inbox.jsonl"
    before = path.read_bytes()
    decisions = tmp_path / "d.txt"
    decisions.write_text(f"accept {a}\nresolve {b} --note x\n", encoding="utf-8")
    assert memory.main(
        ["triage", "apply", "--project", str(project), "--decisions", str(decisions)]
    ) == 2
    assert "triage rejected" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_triage_apply_with_a_malformed_file_exits_2(project: Path, tmp_path: Path, capsys) -> None:
    _remember(project)
    decisions = tmp_path / "d.txt"
    decisions.write_text("vibe mem-2026-08-07-001\n", encoding="utf-8")
    assert memory.main(
        ["triage", "apply", "--project", str(project), "--decisions", str(decisions)]
    ) == 2
    assert "line 1" in capsys.readouterr().err


def test_triage_apply_with_a_missing_file_exits_2(project: Path, tmp_path: Path, capsys) -> None:
    assert memory.main([
        "triage", "apply", "--project", str(project), "--decisions", str(tmp_path / "nope.txt"),
    ]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_triage_apply_without_decisions_exits_2(project: Path, capsys) -> None:
    _remember(project)
    assert memory.main(["triage", "apply", "--project", str(project)]) == 2
    assert "needs --decisions" in capsys.readouterr().err


def test_triage_apply_of_an_all_skip_file_exits_1(project: Path, tmp_path: Path, capsys) -> None:
    a = _remember(project)
    decisions = tmp_path / "d.txt"
    decisions.write_text(f"skip {a}\n", encoding="utf-8")
    assert memory.main(
        ["triage", "apply", "--project", str(project), "--decisions", str(decisions)]
    ) == 1
    assert "every decision is `skip`" in capsys.readouterr().out


def test_triage_apply_dry_run_writes_nothing(project: Path, capsys) -> None:
    a = _remember(project)
    before = (_dir(project) / "inbox.jsonl").read_bytes()
    assert memory.main(
        ["triage", "apply", "--project", str(project), "--accept", a, "--dry-run"]
    ) == 0
    out = capsys.readouterr().out
    assert "would apply 1" in out and "nothing was written" in out
    assert (_dir(project) / "inbox.jsonl").read_bytes() == before


def test_triage_operates_on_machine_scope_too(project: Path, capsys) -> None:
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "summary": "Quote OneDrive paths."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    entry_id = list(memory_store.iter_entries(memory_store.machine_memory_dir()))[-1]["id"]
    assert memory.main(["triage", "--project", str(project), "--scope", "machine"]) == 0
    assert entry_id in capsys.readouterr().out
    assert memory.main([
        "triage", "apply", "--project", str(project), "--scope", "machine", "--accept", "all",
    ]) == 0
    machine = list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    assert machine[-1]["status"] == "accepted"
