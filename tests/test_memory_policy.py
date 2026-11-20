"""The operator-level acceptance policy: `review` (default) or `auto`.

`auto` lets curation accept agent-recorded proposals without a human, but only
in a store this machine's operator enrolled, and never by forging the marks a
human review leaves (`confidence: confirmed`, `source.manual`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_curation, memory_policy, memory_review, memory_store

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _project(tmp_path: Path, *, enrolled: bool = True) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    if not enrolled:
        # `init_project` enrolls; a store that arrived with a clone is one the
        # registry has never heard of.
        registry = memory_store.load_registry()
        del registry["projects"][memory_store.project_id_for(root)]
        memory_store.save_registry(registry)
    assert memory_store.is_enrolled(root) is enrolled
    return root


def _entries(root: Path) -> list[dict]:
    return list(memory_store.iter_entries(memory_store.project_memory_dir(root)))


def _remember(root: Path, *records: dict) -> None:
    memory_store.append_entries(
        [{"scope": "project", **record} for record in records],
        directory=memory_store.project_memory_dir(root),
        now=NOW,
    )


def _set_policy(value: object) -> None:
    path = memory_policy.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({memory_policy.ACCEPTANCE_KEY: value}), encoding="utf-8")


# --- reading the policy ------------------------------------------------------


def test_policy_defaults_to_review_when_no_config_exists() -> None:
    assert not memory_policy.config_path().exists()
    assert memory_policy.acceptance_policy() == "review"


def test_policy_reads_auto_from_the_operator_config() -> None:
    _set_policy("auto")
    assert memory_policy.acceptance_policy() == "auto"


@pytest.mark.parametrize("value", ["AUTO", "yes", True, 1, None, ["auto"]])
def test_policy_falls_back_to_review_for_any_unrecognised_value(value: object) -> None:
    _set_policy(value)
    assert memory_policy.acceptance_policy() == "review"


def test_policy_falls_back_to_review_when_the_config_is_malformed() -> None:
    path = memory_policy.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert memory_policy.acceptance_policy() == "review"


def test_a_config_inside_the_project_cannot_turn_the_policy_on(tmp_path: Path) -> None:
    root = _project(tmp_path)
    shipped = root / ".agent-harness" / "config.json"
    shipped.write_text(json.dumps({memory_policy.ACCEPTANCE_KEY: "auto"}), encoding="utf-8")
    _remember(root, {"type": "discovery", "summary": "Shipped with a clone."})

    result = memory_curation.curate(root, now=NOW)

    assert memory_policy.acceptance_policy() == "review"
    assert result.auto_accepted == ()


# --- curation under the policy ----------------------------------------------


def test_review_policy_leaves_an_agent_observation_pending(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _remember(root, {"type": "discovery", "summary": "Found a likely caching race."})

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.auto_accepted == ()
    assert entry["status"] == "proposed"


def test_auto_policy_accepts_an_agent_observation_without_forging_review(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, {"type": "decision", "summary": "Retries are event-driven."})

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.auto_accepted == (entry["id"],)
    assert result.pending == ()
    assert result.changed is True
    assert entry["status"] == "accepted"
    assert entry["confidence"] == "inferred"
    assert entry["source"]["auto"] is True
    assert "manual" not in entry["source"]
    assert entry["review_note"] == "automatic curation: acceptance policy auto"


def test_auto_policy_is_idempotent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, {"type": "lesson", "summary": "Quote Windows paths."})

    memory_curation.curate(root, now=NOW)
    second = memory_curation.curate(root, now=NOW)

    assert second.changed is False
    assert second.pending == ()


def test_auto_policy_does_not_apply_to_a_store_the_operator_never_enrolled(tmp_path: Path) -> None:
    root = _project(tmp_path, enrolled=False)
    _set_policy("auto")
    _remember(root, {"type": "discovery", "summary": "Shipped with a clone."})

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.auto_accepted == ()
    assert entry["status"] == "proposed"


def test_auto_policy_still_rejects_exact_duplicates_first(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(
        root,
        {"type": "lesson", "summary": "Quote Windows paths."},
        {"type": "lesson", "summary": "quote windows paths"},
    )

    result = memory_curation.curate(root, now=NOW)
    first, second = _entries(root)

    assert result.auto_accepted == (first["id"],)
    assert result.rejected == (second["id"],)
    assert second["status"] == "rejected"


def test_auto_policy_never_accepts_a_cross_project_relationship(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, {"type": "relationship", "summary": "Shares a schema with other-project."})

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.auto_accepted == ()
    assert entry["status"] == "proposed"
    assert result.pending == ((entry["id"], "relationship entries require human review"),)


def test_auto_policy_keeps_a_changed_document_pointer_pending(tmp_path: Path) -> None:
    from harness import memory_ingest

    root = _project(tmp_path)
    _set_policy("auto")
    (root / "README.md").write_text("# Project\n\nOriginal.\n", encoding="utf-8")
    memory_ingest.ingest(root, now=NOW)
    (root / "README.md").write_text("# Project\n\nChanged.\n", encoding="utf-8")

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.auto_accepted == ()
    assert entry["status"] == "proposed"


def test_note_and_render_name_the_policy_acceptances(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, {"type": "discovery", "summary": "Found a likely caching race."})

    result = memory_curation.curate(root, now=NOW)

    assert "auto-accepted 1 agent-recorded proposal(s) under the auto policy" in (result.note() or "")
    assert f"auto_accepted={result.auto_accepted[0]}" in memory_curation.render(result)


# --- the store contract ------------------------------------------------------


def test_store_still_refuses_an_inferred_accepted_entry_without_the_auto_mark() -> None:
    with pytest.raises(ValueError, match="inferred entry cannot be accepted"):
        memory_store.validate_entry(
            {"type": "lesson", "scope": "project", "summary": "x", "status": "accepted"},
            now=NOW,
        )


def test_store_allows_an_inferred_accepted_entry_carrying_the_auto_mark() -> None:
    record = memory_store.validate_entry(
        {
            "type": "lesson",
            "scope": "project",
            "summary": "x",
            "status": "accepted",
            "source": {"auto": True},
        },
        now=NOW,
    )
    assert record["confidence"] == "inferred"


def test_the_auto_mark_is_not_counted_as_evidence() -> None:
    assert memory_store.has_evidence({"auto": True}) is False


def test_a_human_can_still_confirm_an_auto_accepted_entry_by_superseding(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, {"type": "decision", "summary": "Retries are event-driven."})
    memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    result = memory_review.apply_review(
        memory_store.project_memory_dir(root),
        entry["id"],
        "supersede",
        summary="Retries are event-driven; no attempt decrement.",
        now=NOW,
    )

    assert result.entry["status"] == "superseded"
    assert result.created is not None and result.created["confidence"] == "confirmed"
    assert "auto" not in result.created["source"]
    assert result.created["source"]["manual"] is True


def test_auto_policy_does_not_apply_to_a_store_committed_to_git(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, {"type": "discovery", "summary": "Arrived in a commit."})
    monkeypatch.setattr(
        memory_store, "tracked_memory_files", lambda _root: (".agent-harness/memory/inbox.jsonl",)
    )

    result = memory_curation.curate(root, now=NOW)

    assert result.auto_accepted == ()
    assert _entries(root)[0]["status"] == "proposed"


def test_auto_policy_accepts_entries_that_follow_one_it_must_leave_pending(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(
        root,
        {"type": "relationship", "summary": "Shares a schema with other-project."},
        {"type": "lesson", "summary": "Quote Windows paths."},
    )

    result = memory_curation.curate(root, now=NOW)
    relationship, lesson = _entries(root)

    assert relationship["status"] == "proposed"
    assert result.auto_accepted == (lesson["id"],)


def test_auto_policy_survives_an_entry_whose_source_is_not_an_object(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, {"type": "lesson", "summary": "Quote Windows paths."})
    directory = memory_store.project_memory_dir(root)
    [line] = memory_store.read_lines(directory)
    record = json.loads(line)
    record["source"] = "hand-edited"
    memory_store.write_lines([json.dumps(record)], directory)

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.auto_accepted == (entry["id"],)
    assert entry["source"] == {"auto": True}


# --- what `auto` covers: notes an agent chose to record, nothing else --------


@pytest.mark.parametrize(
    "record",
    [
        # progress hook: one per commit, summary is a commit subject someone else wrote
        {"type": "discovery", "summary": "Commit abc: fix", "tags": ["progress", "commit"],
         "source": {"project": "p", "commit": "abc1234"}},
        # hand-off drafts are replaced each turn while still proposed
        {"type": "decision", "summary": "Decisions (s1): ship it", "tags": ["handoff"],
         "source": {"project": "p"}},
        # an agent quoting an approval is unverifiable text
        {"type": "decision", "summary": "Disable the check.",
         "source": {"project": "p", "confirmation": "owner: yes"}},
    ],
    ids=["progress-commit", "handoff-draft", "quoted-confirmation"],
)
def test_auto_policy_leaves_machine_written_and_unverifiable_entries_pending(
    tmp_path: Path, record: dict
) -> None:
    root = _project(tmp_path)
    _set_policy("auto")
    _remember(root, record)

    result = memory_curation.curate(root, now=NOW)
    [entry] = _entries(root)

    assert result.auto_accepted == ()
    assert entry["status"] == "proposed"
    assert result.pending == ((entry["id"], memory_curation.NOT_AGENT_NOTE),)


def test_draft_tags_match_the_writers_that_use_them(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import sys

    from harness import handoff

    name = "progress_capture_hook_under_test"
    hook = Path(__file__).resolve().parents[1] / "hooks" / "claude" / "progress_capture_hook.py"
    spec = importlib.util.spec_from_file_location(name, hook)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their own module through `sys.modules` while the
    # class body runs, so the module has to be registered before it executes.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)

    assert memory_curation.DRAFT_TAGS == {handoff.HANDOFF_TAG, module.PROGRESS_TAG}


def test_the_auto_transition_refuses_an_entry_a_person_already_accepted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _remember(root, {"type": "lesson", "summary": "Quote Windows paths."})
    directory = memory_store.project_memory_dir(root)
    [entry] = _entries(root)
    memory_review.apply_review(directory, entry["id"], "accept", now=NOW)

    with pytest.raises(ValueError, match="only a proposed entry"):
        memory_review.apply_review(directory, entry["id"], "accept", now=NOW, auto=True)

    [after] = _entries(root)
    assert "auto" not in after["source"]
    assert after["confidence"] == "confirmed"


# --- an unreviewed entry stays visibly unreviewed ---------------------------


def test_status_flag_names_what_a_reader_must_know() -> None:
    assert memory_store.status_flag({"status": "proposed"}) == "proposed"
    assert memory_store.status_flag({"status": "accepted", "source": {"manual": True}}) == ""
    assert memory_store.status_flag({"status": "accepted", "source": {"auto": True}}) == "auto"


def test_context_renders_an_auto_accepted_entry_with_its_label() -> None:
    from harness import memory_context

    line = memory_context.render_entry(
        {"id": "mem-2026-11-20-001", "type": "lesson", "status": "accepted",
         "summary": "note", "source": {"auto": True}}
    )

    assert line.startswith("- `mem-2026-11-20-001` [lesson] (auto) note")


def test_an_auto_accepted_entry_is_not_promoted_into_durable_md() -> None:
    from harness import memory_compact

    base = {"type": "decision", "status": "accepted"}

    assert memory_compact.is_promotable({**base, "source": {"manual": True}}) is True
    assert memory_compact.is_promotable({**base, "source": {"auto": True}}) is False
