"""Cross-project relationship proposals: deterministic, precise, never accepted.

Every test registers throwaway projects in the isolated home the autouse
``isolated_home`` fixture provides (see conftest), so nothing here can touch
the operator's real registry or machine store.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_relate, memory_review, memory_store

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _add(root: Path, *summaries: str, tags: list[str] | None = None, status: str = "proposed",
         type: str = "decision") -> list[str]:
    """Append entries to `root`'s project store and return their ids."""
    directory = memory_store.project_memory_dir(root)
    before = {e["id"] for e in memory_store.iter_entries(directory)}
    entries = []
    for summary in summaries:
        entry: dict = {
            "type": type,
            "scope": "project",
            "summary": summary,
            "status": status,
            "source": {"project": memory_store.project_id_for(root)},
        }
        if status == "accepted":
            entry["confidence"] = "confirmed"
            entry["source"]["manual"] = True
        if tags:
            entry["tags"] = list(tags)
        entries.append(entry)
    memory_store.append_entries(entries, directory, now=NOW)
    return [e["id"] for e in memory_store.iter_entries(directory) if e["id"] not in before]


def _pid(root: Path) -> str:
    return memory_store.project_id_for(root)


def _machine_relationships() -> list[dict]:
    return list(memory_store.iter_entries(memory_store.machine_memory_dir(), type="relationship"))


def _candidate(result: memory_relate.RelateResult, a: Path, b: Path) -> memory_relate.Candidate:
    pair = tuple(sorted((_pid(a), _pid(b))))
    for candidate in result.candidates:
        if candidate.pair == pair:
            return candidate
    raise AssertionError(f"no candidate for {pair}")


@pytest.fixture
def trio(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Three registered projects with distinctive names."""
    return (
        _project(tmp_path, "alpha-engine"),
        _project(tmp_path, "beta-tools"),
        _project(tmp_path, "gamma-site"),
    )


# --- shared tags ----------------------------------------------------------


def test_shared_tags_are_weighted_by_rarity_across_the_machine(trio) -> None:
    alpha, beta, gamma = trio
    for root in trio:
        _add(root, "Uses git worktrees.", "Rebases before merging.", tags=["git"])
    _add(alpha, "Odds parser one.", "Odds parser two.", tags=["odds"])
    _add(beta, "Odds feed one.", "Odds feed two.", tags=["odds"])

    result = memory_relate.relate(dry_run=True)
    candidate = _candidate(result, alpha, beta)
    [evidence] = candidate.evidence

    assert evidence.kind == memory_relate.KIND_SHARED_TAG
    assert evidence.detail == "odds"
    assert evidence.weight == pytest.approx(round(math.log(3 / 2), 3))
    assert "git" not in {e.detail for e in candidate.evidence}, "a tag on every project weighs nothing"


def test_a_single_shared_tag_never_qualifies_whatever_the_score(trio) -> None:
    """One word in common is a coincidence, not a relationship."""
    alpha, beta, gamma = trio
    _add(alpha, "First.", "Second.", "Third.", tags=["zebra"])
    _add(beta, "Fourth.", "Fifth.", "Sixth.", tags=["zebra"])
    _add(gamma, "Unrelated.", tags=["site"])

    result = memory_relate.relate(dry_run=True, min_score=0)

    candidate = _candidate(result, alpha, beta)
    assert len(candidate.evidence) == 1
    assert not candidate.qualifies(0)
    assert result.proposed == ()
    assert result.below_threshold == (candidate,)


def test_a_tag_used_by_one_entry_on_a_side_is_not_that_projects_vocabulary(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Only once.", tags=["build"])
    _add(beta, "One.", "Two.", "Three.", tags=["build"])

    result = memory_relate.relate(dry_run=True)

    assert all(c.pair != tuple(sorted((_pid(alpha), _pid(beta)))) for c in result.candidates)


def test_schema_words_used_as_tags_are_ignored(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "A.", "B.", tags=["decision", "proposed"])
    _add(beta, "C.", "D.", tags=["decision", "proposed"])

    result = memory_relate.relate(dry_run=True)

    assert result.candidates == ()


def test_two_rare_tags_with_support_qualify(trio) -> None:
    alpha, beta, gamma = trio
    _add(alpha, "A1.", "A2.", tags=["odds", "parlay"])
    _add(beta, "B1.", "B2.", tags=["odds", "parlay"])
    _add(gamma, "Unrelated.", tags=["site"])

    result = memory_relate.relate(dry_run=True, min_score=0.5)

    candidate = _candidate(result, alpha, beta)
    assert candidate.kinds == (memory_relate.KIND_SHARED_TAG,)
    assert candidate.qualifies(0.5)
    assert result.proposed == (candidate,)


# --- cross-references -----------------------------------------------------


def test_cross_reference_by_registered_name_carries_the_referencing_id(trio) -> None:
    alpha, beta, _ = trio
    [ref_id] = _add(alpha, "Reuse the ingestion pipeline from beta-tools rather than forking it.")
    _add(beta, "Ingestion pipeline is the stable surface.")

    result = memory_relate.relate(dry_run=True)
    candidate = _candidate(result, alpha, beta)
    [evidence] = candidate.evidence

    assert evidence.kind == memory_relate.KIND_CROSS_REFERENCE
    assert evidence.detail == f"{_pid(alpha)}->{_pid(beta)}"
    assert evidence.weight == memory_relate.CROSS_REFERENCE_WEIGHT
    assert candidate.left_ids == (ref_id,)
    assert candidate.right_ids == ()
    assert candidate.qualifies()


def test_cross_reference_by_registered_id(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Anchor.")
    [ref_id] = _add(beta, f"Depends on the memory store shipped by {_pid(alpha)}.")

    result = memory_relate.relate(dry_run=True)
    candidate = _candidate(result, alpha, beta)
    [evidence] = candidate.evidence

    assert evidence.detail == f"{_pid(beta)}->{_pid(alpha)}"
    assert evidence.right_ids == (ref_id,)


def test_each_referencing_entry_is_its_own_evidence(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Anchor.")
    _add(beta, "Talks to alpha-engine.", "Also alpha-engine.", "And alpha-engine again.")

    candidate = _candidate(memory_relate.relate(dry_run=True), alpha, beta)

    assert candidate.score == 3 * memory_relate.CROSS_REFERENCE_WEIGHT


def test_a_short_registered_name_is_never_matched_as_prose(tmp_path: Path) -> None:
    api = _project(tmp_path, "api")
    other = _project(tmp_path, "other-service")
    _add(api, "Anchor.")
    _add(other, "The api layer is thin.")

    result = memory_relate.relate(dry_run=True)

    assert result.candidates == ()


def test_a_name_match_is_case_insensitive_and_bounded(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Anchor.")
    _add(beta, "See Alpha-Engineering for the fork.", "Talk to ALPHA-ENGINE about it.")

    candidate = _candidate(memory_relate.relate(dry_run=True), alpha, beta)

    # Only the bounded, case-folded mention counts, not the longer word.
    assert candidate.score == 1.0


def test_with_only_two_projects_holding_memory_tags_cannot_distinguish(trio) -> None:
    """Every shared tag is then on every project; only references and symbols apply."""
    alpha, beta, _ = trio
    _add(alpha, "A1.", "A2.", tags=["odds"])
    _add(beta, "B1.", "B2.", tags=["odds"])

    assert memory_relate.relate(dry_run=True).candidates == ()


def test_closed_entries_are_not_signal(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Anchor.")
    _add(beta, "Talks to alpha-engine.", status="rejected")

    assert memory_relate.relate(dry_run=True).candidates == ()


# --- shared symbols -------------------------------------------------------


def test_shared_symbols_are_weaker_textual_evidence_and_filenames_are_dropped(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Moved `parse_odds` behind a cache; README.md explains it.")
    _add(beta, "Calls parse_odds() once per game; see README.md.")

    candidate = _candidate(memory_relate.relate(dry_run=True), alpha, beta)
    [evidence] = candidate.evidence

    assert evidence.kind == memory_relate.KIND_SHARED_SYMBOL
    assert evidence.detail.lower().rstrip("()") == "parse_odds"
    assert evidence.weight == memory_relate.TEXTUAL_SYMBOL_WEIGHT
    assert "weaker" in evidence.note
    assert not candidate.qualifies()


def test_a_symbol_resolved_uniquely_in_a_code_graph_counts_double(trio) -> None:
    alpha, beta, _ = trio
    graph = alpha / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps({
        "nodes": [
            {"id": "alpha/odds.py::parse_odds", "label": "parse_odds()", "source_file": "odds.py"},
            {"id": "alpha/odds.py::helper", "label": "helper()", "source_file": "odds.py"},
        ],
        "links": [],
    }), encoding="utf-8")
    _add(alpha, "Moved `parse_odds` behind a cache.")
    _add(beta, "Calls `parse_odds` once per game.")

    [evidence] = _candidate(memory_relate.relate(dry_run=True), alpha, beta).evidence

    assert evidence.weight == memory_relate.RESOLVED_SYMBOL_WEIGHT
    assert "resolved" in evidence.note


# --- proposal writing -----------------------------------------------------


def test_relate_writes_a_proposed_inferred_relationship_into_machine_memory(trio) -> None:
    alpha, beta, _ = trio
    [ref_id] = _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Ingestion pipeline is stable.")
    project_counts = {
        root: len(list(memory_store.iter_entries(memory_store.project_memory_dir(root))))
        for root in trio
    }

    result = memory_relate.relate(now=NOW)

    [entry] = _machine_relationships()
    assert result.proposed_ids == (entry["id"],)
    assert entry["type"] == "relationship"
    assert entry["scope"] == "machine"
    assert entry["status"] == "proposed"
    assert entry["confidence"] == "inferred"
    assert entry["source"]["detector"] == memory_relate.DETECTOR
    assert entry["source"]["projects"] == sorted((_pid(alpha), _pid(beta)))
    assert entry["source"]["evidence_class"] == "cross-reference"
    assert entry["source"]["evidence"][0]["left_ids"] == [ref_id]
    assert "cross-project" in entry["tags"]
    assert "alpha-engine" in entry["summary"] and "beta-tools" in entry["summary"]
    assert len(entry["summary"]) <= memory_store.MAX_SUMMARY_CHARS
    assert "\\" not in json.dumps(entry["source"]), "a proposal must never carry a path"
    for root, count in project_counts.items():
        assert len(list(memory_store.iter_entries(memory_store.project_memory_dir(root)))) == count


def test_dry_run_writes_nothing(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")

    result = memory_relate.relate(dry_run=True)

    assert result.dry_run and result.proposed
    assert result.proposed_ids == ()
    assert _machine_relationships() == []
    assert not (memory_store.machine_memory_dir() / "inbox.jsonl").exists()


def test_rerunning_does_not_repropose_a_live_pair(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")

    first = memory_relate.relate(now=NOW)
    second = memory_relate.relate(now=NOW)

    assert first.changed and not second.changed
    [(candidate, reason)] = second.skipped
    assert candidate.pair == tuple(sorted((_pid(alpha), _pid(beta))))
    assert reason == f"already proposed as {first.proposed_ids[0]}"
    assert len(_machine_relationships()) == 1


def test_an_accepted_relationship_covers_the_pair(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")
    [entry_id] = memory_relate.relate(now=NOW).proposed_ids
    memory_review.apply_review(
        memory_store.machine_memory_dir(), entry_id, "accept", file="docs/links.md", now=NOW
    )

    result = memory_relate.relate(now=NOW)

    assert not result.changed
    assert result.skipped[0][1] == f"already accepted as {entry_id}"


def test_new_evidence_kinds_are_proposed_but_a_superset_prior_covers_fewer(trio) -> None:
    alpha, beta, gamma = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")
    _add(gamma, "Unrelated.", tags=["site"])
    memory_relate.relate(now=NOW)
    _add(alpha, "A1.", "A2.", tags=["odds"])
    _add(beta, "B1.", "B2.", tags=["odds"])

    grown = memory_relate.relate(now=NOW)
    assert grown.changed
    classes = {e["source"]["evidence_class"] for e in _machine_relationships()}
    assert classes == {"cross-reference", "cross-reference+shared-tag"}

    # A candidate whose kinds a live prior already carries is covered.
    prior = memory_relate.ExistingRelationship(
        "mem-x", "proposed", grown.proposed[0].pair, "cross-reference+shared-tag", None
    )
    smaller = memory_relate.Candidate(
        *grown.proposed[0].pair,
        tuple(e for e in grown.proposed[0].evidence if e.kind == "cross-reference"),
    )
    to_propose, skipped = memory_relate.plan([smaller], [prior])
    assert to_propose == [] and skipped[0][1] == "already proposed as mem-x"


def test_a_rejected_proposal_is_not_resurrected_until_the_evidence_changes(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")
    [entry_id] = memory_relate.relate(now=NOW).proposed_ids
    memory_review.apply_review(memory_store.machine_memory_dir(), entry_id, "reject", now=NOW)

    unchanged = memory_relate.relate(now=NOW)
    assert not unchanged.changed
    assert "rejected" in unchanged.skipped[0][1]
    assert len(_machine_relationships()) == 1

    _add(beta, "The parser here mirrors alpha-engine's.")
    changed = memory_relate.relate(now=NOW)
    assert changed.changed
    assert len(_machine_relationships()) == 2


def test_a_hand_written_live_relationship_covers_the_pair_for_every_class(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")
    memory_store.append_entries(
        [{"type": "relationship", "scope": "machine",
          "summary": "alpha-engine consumes beta-tools' ingestion output nightly."}],
        memory_store.machine_memory_dir(), now=NOW,
    )

    result = memory_relate.relate(now=NOW)

    assert not result.changed
    assert "already proposed" in result.skipped[0][1]
    assert len(_machine_relationships()) == 1


def test_candidates_are_ranked_best_first_and_only_restricts_pairs(trio) -> None:
    alpha, beta, gamma = trio
    _add(alpha, "Anchor.")
    _add(beta, "Uses alpha-engine.", "Uses alpha-engine twice.")
    _add(gamma, "Uses alpha-engine.", "Uses beta-tools.")

    ranked = memory_relate.relate(dry_run=True)
    assert [c.score for c in ranked.candidates] == sorted(
        (c.score for c in ranked.candidates), reverse=True
    )
    assert ranked.candidates[0].pair == tuple(sorted((_pid(alpha), _pid(beta))))

    only = memory_relate.relate(dry_run=True, only=[_pid(alpha)])
    assert all(_pid(alpha) in c.pair for c in only.candidates)
    assert len(only.candidates) == 2


def test_an_unregistered_project_id_is_rejected(trio) -> None:
    with pytest.raises(LookupError):
        memory_relate.relate(dry_run=True, only=["ghost-00000000"])


def test_a_negative_min_score_is_rejected(trio) -> None:
    with pytest.raises(ValueError):
        memory_relate.relate(dry_run=True, min_score=-1)


def test_a_vanished_registered_path_is_noted_and_skipped(tmp_path: Path, trio) -> None:
    import shutil

    alpha, beta, gamma = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")
    shutil.rmtree(gamma)

    result = memory_relate.relate(dry_run=True)

    assert any(_pid(gamma) in note and "missing" in note for note in result.notes)
    assert result.proposed


def test_fewer_than_two_populated_projects_proposes_nothing(trio) -> None:
    alpha, _, _ = trio
    _add(alpha, "Talks to beta-tools.")

    result = memory_relate.relate(dry_run=True)

    assert result.candidates == ()
    assert any("at least two" in note for note in result.notes)


def test_render_is_bounded_and_names_the_evidence(trio) -> None:
    alpha, beta, _ = trio
    _add(alpha, "Reuse the ingestion pipeline from beta-tools.")
    _add(beta, "Anchor.")

    text = memory_relate.render(memory_relate.relate(dry_run=True))

    assert "would propose 1 relationship(s)" in text
    assert "alpha-engine <-> beta-tools" in text
    assert "cross-reference:" in text
    assert "dry run -- nothing was written." in text
