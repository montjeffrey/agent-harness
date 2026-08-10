"""Tests for context packets: deterministic selection, budgets, and rendering.

The two guarantees under test are the ones an agent depends on: the packet
never exceeds its ceiling, and it never cuts a record in half. Selector
choice may change *which* entries appear; it may never change either of
those.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness import memory, memory_context, memory_store

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)

FILLED_SCOPE = """# Project scope

> Guidance the agent should not waste budget on.

## Purpose

Portable memory for AI-assisted development.

## Boundaries

Markdown and JSONL only.

## Exclusions

No prompt text, no source bodies.

## Acceptance criteria

Tests green on Windows, macOS, and Linux.

## Active objective

Ship the context command with token budgets.
"""


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


@pytest.fixture
def scoped(project: Path) -> Path:
    (project / ".agent-harness" / "scope.md").write_text(FILLED_SCOPE, encoding="utf-8")
    return project


def _add(project: Path, summary: str, now: datetime = NOW, **overrides) -> None:
    entry = {
        "type": "decision",
        "scope": "project",
        "status": "accepted",
        "confidence": "confirmed",
        "source": {"manual": True},
        "summary": summary,
        **overrides,
    }
    memory_store.append_entries(
        [entry], directory=memory_store.project_memory_dir(project), now=now
    )


def _section(packet, key: str):
    """Look a section up by key -- positional indexing breaks whenever the
    packet gains a section, which it has."""
    return next(s for s in packet.sections if s.key == key)


# --- scope and active prose ----------------------------------------------


def test_scope_section_carries_the_filled_in_headings(scoped: Path) -> None:
    packet = memory_context.build_packet(scoped, now=NOW)
    text = "\n".join(_section(packet, "scope").units)
    assert "**Purpose:**" in text
    assert "Portable memory for AI-assisted development." in text


def test_template_placeholders_and_guidance_never_reach_the_packet(
    project: Path,
) -> None:
    """An untouched scope.md must contribute nothing but a warning."""
    packet = memory_context.build_packet(project, now=NOW)
    assert _section(packet, "scope").units == ()
    assert any("untouched template" in w for w in packet.warnings)


def test_the_active_objective_becomes_the_default_query(scoped: Path) -> None:
    assert memory_context.active_objective(scoped) == (
        "Ship the context command with token budgets."
    )


# --- section ordering and priority ---------------------------------------


def test_sections_appear_in_the_designed_order(scoped: Path) -> None:
    packet = memory_context.build_packet(scoped, now=NOW)
    assert [s.key for s in packet.sections] == list(memory_context.SECTION_ORDER)


def test_open_work_leads_the_active_section_not_the_knowledge_section(
    scoped: Path,
) -> None:
    _add(scoped, "A blocker that must surface early.", type="blocker",
         status="proposed", confidence="inferred", source=None)
    _add(scoped, "Some settled architectural fact.")
    packet = memory_context.build_packet(scoped, now=NOW)
    active = "\n".join(_section(packet, "active").units)
    knowledge = "\n".join(_section(packet, "project_durable").units)
    assert "must surface early" in active
    assert "must surface early" not in knowledge


def test_closed_entries_are_left_out_of_the_packet(scoped: Path) -> None:
    _add(scoped, "A fact that was later rejected.", status="rejected")
    _add(scoped, "A fact that still holds.")
    packet = memory_context.build_packet(scoped, now=NOW)
    rendered = memory_context.render_markdown(packet)
    assert "still holds" in rendered
    assert "later rejected" not in rendered


def test_pinned_ids_lead_their_section(scoped: Path) -> None:
    for n in range(5):
        _add(scoped, f"Ordinary fact number {n}.")
    entries = list(memory_store.iter_entries(memory_store.project_memory_dir(scoped)))
    target = entries[-1]["id"]
    packet = memory_context.build_packet(scoped, include_ids=[target], now=NOW)
    assert _section(packet, "project_durable").units[0].startswith(f"- `{target}`")


def test_a_pinned_id_survives_even_when_it_ranks_below_the_candidate_limit(
    scoped: Path,
) -> None:
    """Asking for an id by name must never lose to the selector's cut-off."""
    _add(scoped, "The pinned entry, matching nothing in the query.")
    pinned = list(
        memory_store.iter_entries(memory_store.project_memory_dir(scoped))
    )[0]["id"]
    for n in range(memory_context.CANDIDATE_LIMIT + 10):
        _add(scoped, f"Caching and retries, note number {n}.", tags=["caching"])

    packet = memory_context.build_packet(
        scoped, query="caching retries", include_ids=[pinned],
        max_chars=100_000, now=NOW,
    )
    assert _section(packet, "project_durable").units[0].startswith(f"- `{pinned}`")


def test_machine_entries_land_in_the_machine_section(scoped: Path) -> None:
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted",
          "confidence": "confirmed", "source": {"manual": True},
          "summary": "Always quote OneDrive paths."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    packet = memory_context.build_packet(scoped, now=NOW)
    assert "OneDrive" in "\n".join(_section(packet, "machine_durable").units)


# --- budgets --------------------------------------------------------------


def test_the_packet_never_exceeds_its_hard_ceiling(scoped: Path) -> None:
    for n in range(200):
        _add(scoped, f"Fact {n}: " + "detail " * 40)
    packet = memory_context.build_packet(scoped, max_chars=1500, now=NOW)
    assert packet.total_chars <= 1500


def test_a_section_never_exceeds_its_own_budget(scoped: Path) -> None:
    for n in range(200):
        _add(scoped, f"Fact {n}: " + "detail " * 40)
    packet = memory_context.build_packet(
        scoped, budgets={"project_durable": 400}, now=NOW
    )
    durable = next(s for s in packet.sections if s.key == "project_durable")
    assert durable.chars <= 400


def test_omitted_entries_are_counted_not_silently_dropped(scoped: Path) -> None:
    for n in range(50):
        _add(scoped, f"Fact {n}: " + "detail " * 20)
    packet = memory_context.build_packet(scoped, max_chars=900, now=NOW)
    assert packet.omitted > 0
    assert "more omitted for budget" in memory_context.render_markdown(packet)


def test_no_record_is_ever_cut_in_half(scoped: Path) -> None:
    """Every rendered entry line must contain its whole summary, byte for byte."""
    for n in range(40):
        _add(scoped, f"Fact {n}: " + "detail " * 20)
    packet = memory_context.build_packet(scoped, max_chars=1200, now=NOW)
    durable = next(s for s in packet.sections if s.key == "project_durable")
    for unit, record in zip(durable.units, durable.records):
        assert record["summary"].strip() in unit


def test_a_zero_budget_yields_an_empty_but_valid_packet(scoped: Path) -> None:
    _add(scoped, "Something worth remembering.")
    packet = memory_context.build_packet(scoped, max_chars=0, now=NOW)
    assert packet.total_chars == 0
    assert packet.omitted > 0
    assert memory_context.render_markdown(packet).startswith("# Project context:")


# --- deterministic selection ---------------------------------------------


def test_selection_is_stable_across_identical_runs(scoped: Path) -> None:
    for n in range(30):
        _add(scoped, f"Fact {n} about caching and retries.")
    first = memory_context.render_markdown(memory_context.build_packet(scoped, now=NOW))
    second = memory_context.render_markdown(
        memory_context.build_packet(scoped, now=NOW)
    )
    assert first == second


def test_query_keywords_outrank_recency(scoped: Path) -> None:
    _add(scoped, "Rate limiting uses a token bucket.", now=NOW - timedelta(days=5),
         tags=["ratelimit"])
    _add(scoped, "Unrelated note about spreadsheet formatting.", now=NOW)
    packet = memory_context.build_packet(scoped, query="rate limiting", now=NOW)
    assert "token bucket" in _section(packet, "project_durable").units[0]


def test_a_shared_stopword_is_not_treated_as_relevance(scoped: Path) -> None:
    """Observed for real: a shared "with" outranked recency over an unrelated
    entry, making keyword selection look meaningful when it was noise."""
    _add(scoped, "Rate limiting a queue works best with a token bucket.",
         now=NOW - timedelta(days=5))
    _add(scoped, "Refactored the invoice PDF template.", now=NOW)
    packet = memory_context.build_packet(
        scoped, query="avoid overwhelming a service with too many requests", now=NOW
    )
    # No content word is shared, so this must fall through to recency --
    # the newer invoice entry -- rather than matching on "with".
    assert "invoice" in _section(packet, "project_durable").units[0]


def test_stopwords_are_dropped_from_keyword_extraction() -> None:
    assert memory_context._keywords("with the and from that") == set()
    assert memory_context._keywords("token bucket with queue") == {
        "token", "bucket", "queue",
    }


def test_confirmed_entries_outrank_inferred_ones(scoped: Path) -> None:
    _add(scoped, "Merely inferred guess.", status="proposed", confidence="inferred",
         source=None, now=NOW)
    _add(scoped, "Confirmed and accepted fact.", now=NOW - timedelta(days=1))
    packet = memory_context.build_packet(scoped, query=None, now=NOW)
    assert "Confirmed and accepted" in _section(packet, "project_durable").units[0]


def test_newer_entries_come_first_when_nothing_else_separates_them(
    scoped: Path,
) -> None:
    _add(scoped, "Older fact.", now=NOW - timedelta(days=3))
    _add(scoped, "Newer fact.", now=NOW)
    packet = memory_context.build_packet(scoped, query=None, now=NOW)
    assert "Newer fact" in _section(packet, "project_durable").units[0]


# --- warnings -------------------------------------------------------------


def test_stale_memory_raises_a_freshness_warning(scoped: Path) -> None:
    _add(scoped, "A fact recorded a long time ago.",
         now=NOW - timedelta(days=memory_context.STALE_AFTER_DAYS + 5))
    packet = memory_context.build_packet(scoped, now=NOW)
    assert any("stale" in w for w in packet.warnings)


def test_pending_proposals_are_flagged_as_not_authoritative(scoped: Path) -> None:
    _add(scoped, "Something not yet reviewed.", status="proposed",
         confidence="inferred", source=None)
    packet = memory_context.build_packet(scoped, now=NOW)
    assert any("awaiting review" in w for w in packet.warnings)


def test_an_empty_project_says_so_rather_than_rendering_nothing(project: Path) -> None:
    packet = memory_context.build_packet(project, now=NOW)
    assert any("no project memory" in w for w in packet.warnings)


# --- rendering ------------------------------------------------------------


def test_markdown_names_the_strategy_that_produced_the_packet(scoped: Path) -> None:
    packet = memory_context.build_packet(scoped, strategy="semantic", now=NOW)
    assert "selection: semantic" in memory_context.render_markdown(packet)


def test_json_output_is_parseable_and_carries_structured_records(scoped: Path) -> None:
    _add(scoped, "A fact with structure.", tags=["alpha"])
    packet = memory_context.build_packet(scoped, now=NOW)
    payload = json.loads(memory_context.render_json(packet))
    assert payload["schema"] == memory_store.SCHEMA_VERSION
    durable = next(s for s in payload["sections"] if s["key"] == "project_durable")
    assert durable["records"][0]["tags"] == ["alpha"]


def test_entry_lines_carry_the_id_so_an_agent_can_act_on_them(scoped: Path) -> None:
    _add(scoped, "Actionable fact.")
    packet = memory_context.build_packet(scoped, now=NOW)
    assert _section(packet, "project_durable").units[0].startswith("- `mem-")


def test_proposed_status_is_visible_in_the_rendered_line(scoped: Path) -> None:
    _add(scoped, "Not yet reviewed.", type="task", status="proposed",
         confidence="inferred", source=None)
    packet = memory_context.build_packet(scoped, now=NOW)
    assert "(proposed)" in "\n".join(_section(packet, "active").units)


# --- CLI ------------------------------------------------------------------


def test_context_cli_emits_markdown_by_default(scoped: Path, capsys) -> None:
    _add(scoped, "A recorded decision.")
    assert memory.main(["context", "--project", str(scoped)]) == 0
    assert "# Project context:" in capsys.readouterr().out


def test_context_cli_emits_json_on_request(scoped: Path, capsys) -> None:
    _add(scoped, "A recorded decision.")
    assert memory.main(["context", "--project", str(scoped), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["strategy"] == "deterministic"


def test_context_cli_honours_max_chars(scoped: Path, capsys) -> None:
    for n in range(100):
        _add(scoped, f"Fact {n}: " + "detail " * 30)
    assert memory.main(
        ["context", "--project", str(scoped), "--json", "--max-chars", "800"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["total_chars"] <= 800


def test_context_cli_works_on_an_uninitialized_project(tmp_path: Path, capsys) -> None:
    """Context must degrade to a warning, never crash on a bare directory."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert memory.main(["context", "--project", str(bare)]) == 0
    assert "no project memory" in capsys.readouterr().out


# --- graph annotation seam ------------------------------------------------
#
# `memory_context` never imports the graph; it accepts an annotator callable
# (`harness.memory_graph` supplies the real one). These tests use a stub, so
# the packet's contract is verified independently of graph availability.

IMPACT = {
    "symbol": "build_packet",
    "node_id": "ctx_build_packet",
    "source_file": "harness/memory_context.py",
    "dependents": 4,
    "files": ["harness/memory.py"],
    "line": "    graph: `build_packet` -- 4 dependents in 1 file (harness/memory.py)",
}


def _stub_annotator(entry):
    return (IMPACT,)


def test_without_an_annotator_the_packet_is_byte_identical(scoped: Path) -> None:
    _add(scoped, "Rewrote build_packet to take an annotator.")
    plain = memory_context.render_markdown(memory_context.build_packet(scoped, now=NOW))
    explicit = memory_context.render_markdown(
        memory_context.build_packet(scoped, annotator=None, now=NOW)
    )
    assert plain == explicit
    assert "graph:" not in plain


def test_an_annotation_rides_inside_the_entrys_own_unit(scoped: Path) -> None:
    """One unit, not two -- budgeting is unit-atomic, so a separate unit could
    be dropped while its entry survives, leaving a claim with no evidence."""
    _add(scoped, "Rewrote build_packet.")
    packet = memory_context.build_packet(scoped, annotator=_stub_annotator, now=NOW)
    units = _section(packet, "project_durable").units
    assert len(units) == 1
    assert units[0].startswith("- `mem-")
    assert IMPACT["line"] in units[0]


def test_an_annotated_entry_is_included_or_omitted_whole(scoped: Path) -> None:
    """A budget with room for the entry but not its annotation drops both."""
    _add(scoped, "Rewrote build_packet.")
    bare = _section(memory_context.build_packet(scoped, now=NOW), "project_durable")
    budget = len(bare.units[0]) + 1  # exactly enough for the unannotated entry

    fits = memory_context.build_packet(
        scoped, budgets={"project_durable": budget}, now=NOW
    )
    assert len(_section(fits, "project_durable").units) == 1

    annotated = _section(
        memory_context.build_packet(
            scoped,
            budgets={"project_durable": budget},
            annotator=_stub_annotator,
            now=NOW,
        ),
        "project_durable",
    )
    assert annotated.units == ()
    assert annotated.omitted == 1


def test_records_carry_the_structured_impact_without_mutating_the_entry(
    scoped: Path,
) -> None:
    _add(scoped, "Rewrote build_packet.")
    packet = memory_context.build_packet(scoped, annotator=_stub_annotator, now=NOW)
    record = _section(packet, "project_durable").records[0]
    assert record["graph_impact"] == [IMPACT]
    # The stored entry is untouched: the packet holds an annotated copy.
    stored = next(
        memory_store.iter_entries(memory_store.project_memory_dir(scoped))
    )
    assert "graph_impact" not in stored


def test_json_output_carries_the_impact_for_machine_consumers(scoped: Path) -> None:
    _add(scoped, "Rewrote build_packet.")
    packet = memory_context.build_packet(scoped, annotator=_stub_annotator, now=NOW)
    payload = json.loads(memory_context.render_json(packet))
    durable = next(s for s in payload["sections"] if s["key"] == "project_durable")
    assert durable["records"][0]["graph_impact"][0]["dependents"] == 4


def test_open_work_is_annotated_too_not_just_durable_knowledge(scoped: Path) -> None:
    _add(scoped, "Finish build_packet.", type="task", status="proposed",
         confidence="inferred", source=None)
    packet = memory_context.build_packet(scoped, annotator=_stub_annotator, now=NOW)
    assert IMPACT["line"] in "\n".join(_section(packet, "active").units)


def test_an_annotator_returning_nothing_changes_nothing(scoped: Path) -> None:
    _add(scoped, "A fact with no resolvable symbol.")
    plain = memory_context.render_markdown(memory_context.build_packet(scoped, now=NOW))
    empty = memory_context.render_markdown(
        memory_context.build_packet(scoped, annotator=lambda e: (), now=NOW)
    )
    assert plain == empty


def test_context_cli_stays_quiet_when_the_project_has_no_graph(
    scoped: Path, capsys
) -> None:
    """Graph annotation is on by default and must degrade to silence."""
    _add(scoped, "Rewrote build_packet.")
    assert memory.main(["context", "--project", str(scoped)]) == 0
    out = capsys.readouterr().out
    assert "# Project context:" in out
    assert "graph:" not in out


def test_context_cli_accepts_no_graph(scoped: Path, capsys) -> None:
    _add(scoped, "Rewrote build_packet.")
    assert memory.main(["context", "--project", str(scoped), "--no-graph"]) == 0
    assert "# Project context:" in capsys.readouterr().out
