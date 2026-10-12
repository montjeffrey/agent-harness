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

from harness import memory, memory_compact, memory_context, memory_store

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


# --- compaction pressure notes -------------------------------------------


def _machine_add(summary: str, now: datetime = NOW, **overrides) -> None:
    memory_store.append_entries(
        [{
            "type": "discovery",
            "scope": "machine",
            "status": "proposed",
            "confidence": "inferred",
            "summary": summary,
            **overrides,
        }],
        directory=memory_store.machine_memory_dir(),
        now=now,
    )


def _notes(packet) -> str:
    return "\n".join(packet.notes)


def test_a_clean_store_adds_no_pressure_note(scoped: Path) -> None:
    _add(scoped, "Live knowledge.")
    packet = memory_context.build_packet(scoped, now=NOW)
    assert not any("[memory]" in n for n in packet.notes)


def test_closed_project_work_asks_for_compaction(scoped: Path) -> None:
    _add(scoped, "Shipped.", type="task", status="resolved")
    packet = memory_context.build_packet(scoped, now=NOW)
    assert any("run `agent-harness-memory compact`" in n for n in packet.notes)


def test_pressure_counts_terminal_entries_the_packet_itself_filters_out(
    scoped: Path,
) -> None:
    """The durable sections drop closed entries through `_is_live`, so pressure
    has to be computed over the raw pool. Reading it off the selected records
    would report zero on exactly the store that needs compacting most."""
    for n in range(3):
        _add(scoped, f"Shipped {n}.", type="task", status="resolved")
    packet = memory_context.build_packet(scoped, now=NOW)
    assert any("3 closed" in n for n in packet.notes)


def test_machine_and_project_pressure_are_measured_separately(scoped: Path) -> None:
    """A combined count could not tell the operator which store to act on, and
    only one of the two has a working `compact`."""
    _add(scoped, "Shipped.", type="task", status="resolved")
    for n in range(6):
        _machine_add(f"Machine observation {n}.", now=NOW - timedelta(days=23))

    packet = memory_context.build_packet(scoped, now=NOW)
    text = _notes(packet)

    assert "run `agent-harness-memory compact`" in text
    assert "6 machine proposals unreviewed" in text
    assert "run `agent-harness-memory review --scope machine`" in text


def test_caller_supplied_notes_survive_alongside_pressure_notes(scoped: Path) -> None:
    _add(scoped, "Shipped.", type="task", status="resolved")
    packet = memory_context.build_packet(scoped, now=NOW, notes=["semantic index unavailable"])
    assert "semantic index unavailable" in packet.notes
    assert any("[memory]" in n for n in packet.notes)


def test_the_pressure_note_survives_into_both_renderers(scoped: Path) -> None:
    """The CLI reads Markdown, the MCP tool reads JSON, and the session-start
    hook reads whichever it was configured for. A note that only made it into
    one of them would be invisible to the other two consumers."""
    _add(scoped, "Shipped.", type="task", status="resolved")
    packet = memory_context.build_packet(scoped, now=NOW)

    assert "run `agent-harness-memory compact`" in memory_context.render_markdown(packet)

    payload = json.loads(memory_context.render_json(packet))
    assert any("run `agent-harness-memory compact`" in n for n in payload["notes"])


# --- active.md degrades per entry ----------------------------------------
#
# `compact` fills active.md up to ACTIVE_MAX_CHARS (4000) while the packet
# gives the active section DEFAULT_BUDGETS["active"] (2000). While the whole
# bullet list parsed as one atomic unit, anything compaction wrote between
# those two numbers was dropped whole and the section read empty. The budgets
# are allowed to disagree; the list just has to give way one entry at a time.


def _active_section(project: Path):
    return _section(memory_context.build_packet(project, now=NOW), "active")


def test_a_compacted_active_file_degrades_entry_by_entry(project: Path) -> None:
    """The regression: a real compaction must not blank the active section."""
    for i in range(20):
        _add(project, f"Decision {i:02d}: " + "budgeted retrieval detail " * 6)
    memory_compact.compact(project, now=NOW)

    rendered = (memory_store.project_memory_dir(project) / "active.md").read_text(
        encoding="utf-8"
    )
    assert len(rendered) > memory_context.DEFAULT_BUDGETS["active"], (
        "fixture too small to reproduce the bug"
    )

    section = _active_section(project)
    assert section.units, "the whole active list vanished instead of degrading"
    assert section.omitted, "nothing dropped, so the budget was not the binding constraint"
    assert section.chars <= memory_context.DEFAULT_BUDGETS["active"]


def test_each_entry_in_active_becomes_its_own_unit(project: Path) -> None:
    _add(project, "First accepted decision.")
    _add(project, "Second accepted decision.")
    memory_compact.compact(project, now=NOW)

    units = _active_section(project).units
    assert sum(u.lstrip().startswith("- ") for u in units) == 2
    assert sum("First accepted decision." in u for u in units) == 1
    assert sum("Second accepted decision." in u for u in units) == 1


def test_a_bullet_unit_carries_its_own_id_so_it_survives_alone(project: Path) -> None:
    """Units are dropped independently, so each must still identify itself."""
    _add(project, "Standalone decision.")
    memory_compact.compact(project, now=NOW)

    unit = next(u for u in _active_section(project).units if "Standalone" in u)
    assert unit.lstrip().startswith("- ")
    assert "mem-" in unit


def test_scope_prose_still_forms_one_unit_per_heading(scoped: Path) -> None:
    """scope.md has no bullet lists, so per-entry splitting must not touch it."""
    units = _section(memory_context.build_packet(scoped, now=NOW), "scope").units
    assert len(units) == 5
    assert all(u.startswith("**") for u in units)


def test_the_active_objective_still_joins_prose_across_lines(project: Path) -> None:
    """Splitting the parser's body must not shred a multi-line objective."""
    (project / ".agent-harness" / "scope.md").write_text(
        "# Project scope\n\n## Active objective\n\nImprove retrieval\nacross two lines.\n",
        encoding="utf-8",
    )
    assert memory_context.active_objective(project) == "Improve retrieval across two lines."


def test_active_units_and_records_stay_parallel(project: Path) -> None:
    """`build_packet` slices records by `len(kept)`, so order must match 1:1."""
    for i in range(6):
        _add(project, f"Parallel decision {i}.")
    memory_compact.compact(project, now=NOW)

    section = _active_section(project)
    assert len(section.units) >= 6, "too few units to detect a misalignment"
    assert len(section.units) == len(section.records)
    for unit, record in zip(section.units, section.records):
        assert record["body"] in unit


# --- open work comes from the inbox, not the snapshot ---------------------
#
# `compact` writes an `## Open work` list into active.md and `build_packet`
# also derives open work from the live entries, so every task and blocker
# reached the active section twice. The inbox is the store of record; the
# snapshot is as old as the last compaction, so the snapshot copy is the one
# that goes.


def test_open_work_reaches_the_active_section_exactly_once(project: Path) -> None:
    _add(project, "UNIQUEBLOCKER needs a decision.", type="blocker")
    memory_compact.compact(project, now=NOW)

    units = _active_section(project).units
    assert sum("UNIQUEBLOCKER" in u for u in units) == 1


def test_the_surviving_copy_is_the_entry_not_the_snapshot(project: Path) -> None:
    """The entry rendering carries status and blast radius; the bullet does not."""
    _add(project, "UNIQUEBLOCKER needs a decision.", type="blocker")
    memory_compact.compact(project, now=NOW)

    unit = next(u for u in _active_section(project).units if "UNIQUEBLOCKER" in u)
    assert "[blocker]" in unit, "kept compaction's snapshot instead of the live entry"


def test_a_task_resolved_since_the_last_compaction_is_not_shown_as_open(
    project: Path,
) -> None:
    """The case that rules out deduplicating by id.

    A resolved task still sits in the snapshot but is no longer open work, so
    an id-based dedupe would find nothing to match it against and leave the
    stale bullet in -- reporting finished work as outstanding.
    """
    _add(project, "STALETASK was finished last week.", type="task", status="resolved")
    active = memory_store.project_memory_dir(project) / "active.md"
    active.write_text(
        "# Active memory\n\n## Open work\n\n"
        "- `mem-2026-08-07-001` task: STALETASK was finished last week.\n",
        encoding="utf-8",
    )

    packet = memory_context.build_packet(project, now=NOW)
    assert "STALETASK" not in memory_context.render_markdown(packet)


def test_recent_decisions_still_reach_the_packet(project: Path) -> None:
    """Only `## Open work` is dropped -- this fix must not widen past it."""
    _add(project, "KEPTDECISION was settled.")
    memory_compact.compact(project, now=NOW)

    units = _active_section(project).units
    assert any("KEPTDECISION" in u for u in units)


def test_open_work_leads_the_active_section(project: Path) -> None:
    """Trimming keeps a prefix, so the tail is evicted first.

    Open work is the design's second priority tier and must not be the first
    thing budget pressure discards.
    """
    for i in range(4):
        _add(project, f"Ordinary decision {i}.")
    _add(project, "LEADINGBLOCKER outranks the decisions.", type="blocker")
    memory_compact.compact(project, now=NOW)

    units = _active_section(project).units
    # The entry form, not compaction's snapshot: the snapshot's own `## Open
    # work` also happens to sort first, so asserting on the text alone would
    # pass without the ordering ever being fixed.
    assert "LEADINGBLOCKER" in units[0]
    assert "[blocker]" in units[0]
# --- filters: narrowing happens before ranking ----------------------------


def test_parse_filter_lowercases_trims_and_deduplicates() -> None:
    f = memory_context.parse_filter(" Retry, http ,retry", "Decision", "mem-2026-08-07-001, HTTP")
    assert f.tags == ("retry", "http")
    assert f.types == ("decision",)
    assert f.exclude == ("mem-2026-08-07-001", "http")
    assert not f.is_empty
    assert memory_context.parse_filter("", "", "").is_empty


def test_parse_filter_rejects_an_unknown_type_instead_of_matching_nothing() -> None:
    with pytest.raises(ValueError, match="unknown type"):
        memory_context.parse_filter(types="decsion")


def test_entry_filter_semantics() -> None:
    f = memory_context.EntryFilter(tags=("retry",), types=("decision",), exclude=("mem-x", "legacy"))
    base = {"id": "mem-1", "type": "decision", "tags": ["Retry"]}
    assert f.matches(base)
    assert not f.matches({**base, "tags": ["http"]})
    assert not f.matches({**base, "type": "lesson"})
    assert not f.matches({**base, "id": "mem-x"})
    assert not f.matches({**base, "tags": ["retry", "legacy"]})
    assert memory_context.EntryFilter().matches({"id": "anything"})


def test_a_filter_removes_entries_before_ranking_and_says_so(scoped: Path) -> None:
    _add(scoped, "Retry policy uses exponential backoff", tags=["retry"])
    _add(scoped, "Logging is local JSONL", tags=["logging"])
    _add(scoped, "An open retry task", type="task", status="proposed",
         confidence="inferred", source=None, tags=["retry"])
    packet = memory_context.build_packet(
        scoped, entry_filter=memory_context.parse_filter(tags="retry"), now=NOW
    )
    text = memory_context.render_markdown(packet)
    assert "exponential backoff" in text
    assert "An open retry task" in text  # open work is filtered too, not just knowledge
    assert "local JSONL" not in text
    assert "_Note: filters applied (tags: retry); 1 entry excluded before ranking_" in text


def test_warnings_describe_the_store_not_the_filtered_slice(scoped: Path) -> None:
    _add(scoped, "A proposal about logging", status="proposed", confidence="inferred",
         source=None, tags=["logging"])
    _add(scoped, "A retry decision", tags=["retry"])
    packet = memory_context.build_packet(
        scoped, entry_filter=memory_context.parse_filter(tags="retry"), now=NOW
    )
    assert any("1 entry awaiting review" in w for w in packet.warnings)


def test_a_filter_matching_nothing_is_a_visible_warning(scoped: Path) -> None:
    _add(scoped, "A retry decision", tags=["retry"])
    packet = memory_context.build_packet(
        scoped, entry_filter=memory_context.parse_filter(tags="nosuchtag"), now=NOW
    )
    assert any("filters matched no entries" in w for w in packet.warnings)
    assert "1 entry excluded before ranking" in " ".join(packet.notes)


def test_an_empty_filter_leaves_the_packet_byte_identical(scoped: Path) -> None:
    _add(scoped, "A retry decision", tags=["retry"])
    plain = memory_context.render_markdown(memory_context.build_packet(scoped, now=NOW))
    filtered = memory_context.render_markdown(
        memory_context.build_packet(scoped, entry_filter=memory_context.EntryFilter(), now=NOW)
    )
    assert plain == filtered


def test_load_entries_counts_what_the_gate_can_rank(scoped: Path, fake_home: Path) -> None:
    for n in range(5):
        _add(scoped, f"retry fact {n}", tags=["retry"])
    _add(scoped, "unrelated", tags=["other"])
    machine = fake_home / ".agent-harness" / "memory"
    machine.mkdir(parents=True, exist_ok=True)
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "status": "accepted", "confidence": "confirmed",
          "source": {"manual": True}, "summary": "machine retry lesson", "tags": ["retry"]}],
        directory=machine, now=NOW,
    )
    loaded = memory_context.load_entries(scoped, memory_context.parse_filter(tags="retry"))
    assert loaded.count == 6
    assert loaded.excluded == 1
    assert len(loaded.project_all) == 6 and len(loaded.machine_all) == 1

    project_only = memory_context.load_entries(scoped, include_machine=False)
    assert project_only.machine == () and project_only.machine_all == ()
    assert project_only.count == 6


def test_a_preloaded_store_is_used_instead_of_reading_again(scoped: Path) -> None:
    _add(scoped, "A retry decision", tags=["retry"])
    loaded = memory_context.load_entries(scoped, memory_context.parse_filter(tags="retry"))
    _add(scoped, "Recorded after the load", tags=["retry"])
    packet = memory_context.build_packet(scoped, loaded=loaded, now=NOW)
    text = memory_context.render_markdown(packet)
    assert "A retry decision" in text
    assert "Recorded after the load" not in text


# --- focused search -------------------------------------------------------


def _entry(id: str, summary: str, **overrides) -> dict:
    return {
        "id": id, "type": "decision", "scope": "project", "status": "accepted",
        "confidence": "confirmed", "summary": summary, "ts": "2026-08-07T12:00:00Z",
        **overrides,
    }


def test_search_ranks_by_the_query_and_labels_each_hit_with_its_source() -> None:
    result = memory_context.search(
        "retry backoff",
        [
            ("alpha", [_entry("mem-1", "Logging is JSONL"), _entry("mem-2", "Retry uses backoff")]),
            ("beta", [_entry("mem-3", "Beta retry backoff too")]),
        ],
        limit=5,
    )
    assert [(h.source, h.entry["id"]) for h in result.hits] == [
        ("alpha", "mem-2"), ("beta", "mem-3"), ("alpha", "mem-1"),
    ]
    text = memory_context.render_search_markdown(result)
    assert "- [alpha] `mem-2` [decision] Retry uses backoff" in text
    assert "- [beta] `mem-3`" in text
    assert "ranked: alpha=deterministic, beta=deterministic" in text
    assert "## " not in text  # no packet sections


def test_search_merges_sources_by_rank_current_project_first() -> None:
    result = memory_context.search(
        "x",
        [
            ("current", [_entry("c1", "x one"), _entry("c2", "x two")]),
            ("other", [_entry("o1", "x uno"), _entry("o2", "x dos")]),
        ],
        limit=3,
    )
    assert [h.entry["id"] for h in result.hits] == ["c1", "o1", "c2"]


def test_search_limit_is_clamped_and_the_clamp_is_stated() -> None:
    entries = [_entry(f"mem-{n}", f"fact {n}") for n in range(40)]
    result = memory_context.search("fact", [("p", entries)], limit=100, max_chars=100_000)
    assert len(result.hits) == memory_context.SEARCH_MAX_LIMIT
    assert any("capped at 25" in n for n in result.notes)
    assert result.limit == memory_context.SEARCH_MAX_LIMIT


def test_search_never_shows_a_closed_entry_or_a_partial_line() -> None:
    entries = [
        _entry("mem-1", "resolved task", type="task", status="resolved"),
        _entry("mem-2", "superseded", status="superseded"),
        _entry("mem-3", "rejected", status="rejected"),
        _entry("mem-4", "live " + "x" * 200),
        _entry("mem-5", "also live " + "y" * 200),
    ]
    result = memory_context.search("live", [("p", entries)], limit=8, max_chars=250)
    assert [h.entry["id"] for h in result.hits] == ["mem-4"]
    assert result.omitted == 1
    text = memory_context.render_search_markdown(result)
    assert "_... 1 more omitted for budget._" in text
    assert "mem-5" not in text


def test_search_says_when_nothing_matched_and_skips_empty_sources() -> None:
    calls: list[str] = []

    def resolver(source: str, count: int):
        calls.append(source)
        return memory_context.deterministic_selector, "deterministic", ["ranked plainly"]

    result = memory_context.search(
        "anything", [("empty", []), ("full", [_entry("m", "one")])], resolver=resolver
    )
    assert calls == ["full"]
    assert ("empty", "no live entries") in result.strategies
    assert result.notes == ("full: ranked plainly",)
    nothing = memory_context.render_search_markdown(memory_context.search("q", [("empty", [])]))
    assert "No live entries matched" in nothing


def test_identical_source_notes_are_stated_once_with_every_source_named() -> None:
    def resolver(source: str, count: int):
        return memory_context.deterministic_selector, "deterministic", ["semantic selection skipped"]

    result = memory_context.search(
        "q", [("a", [_entry("1", "q")]), ("b", [_entry("2", "q")])], resolver=resolver
    )
    assert result.notes == ("a, b: semantic selection skipped",)


# --- cross-project resolution --------------------------------------------


def _registry(*projects: tuple[str, Path]) -> dict:
    return {
        "schema": 1,
        "projects": {pid: {"path": str(path), "name": path.name} for pid, path in projects},
    }


def test_current_is_the_project_alone_without_touching_the_registry(project: Path) -> None:
    scope = memory_context.resolve_search_projects("current", project, None)  # type: ignore[arg-type]
    assert scope.projects == ((memory_store.project_id_for(project), project),)
    assert scope.notes == ()


def test_an_empty_or_missing_registry_narrows_to_current_with_a_note(project: Path) -> None:
    empty = {"schema": 1, "projects": {}}  # what load_registry returns for missing/corrupt too
    for spec in ("all", "related", "someone-11111111"):
        scope = memory_context.resolve_search_projects(spec, project, empty)
        assert len(scope.projects) == 1
        assert any("registry missing, unreadable, or empty" in n for n in scope.notes)


def test_all_with_only_the_current_project_registered_says_so(project: Path) -> None:
    scope = memory_context.resolve_search_projects("all", project, memory_store.load_registry())
    assert len(scope.projects) == 1
    assert any("no other projects registered on this machine" in n for n in scope.notes)


def test_all_reads_every_registered_project_and_reports_vanished_paths(
    project: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    gone = tmp_path / "gone"
    registry = _registry(
        (memory_store.project_id_for(project), project),
        ("other-11111111", other),
        ("gone-22222222", gone),
    )
    scope = memory_context.resolve_search_projects("all", project, registry)
    assert [pid for pid, _ in scope.projects] == [
        memory_store.project_id_for(project), "other-11111111"
    ]
    assert any("gone-22222222" in n and "no longer exists" in n for n in scope.notes)


def test_an_explicit_list_skips_unknown_ids_visibly(project: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    registry = _registry(("other-11111111", other))
    scope = memory_context.resolve_search_projects(
        "other-11111111, nope-00000000, other-11111111", project, registry
    )
    assert [pid for pid, _ in scope.projects][1:] == ["other-11111111"]
    assert any("'nope-00000000' is not registered" in n for n in scope.notes)


def test_related_resolves_through_accepted_relationships_only(
    project: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    third = tmp_path / "third"
    third.mkdir()
    current_id = memory_store.project_id_for(project)
    registry = _registry(
        (current_id, project), ("other-11111111", other), ("third-33333333", third)
    )
    relationships = [
        {"type": "relationship", "status": "accepted", "scope": "machine",
         "summary": "shares a client", "projects": [current_id, "other-11111111"]},
        {"type": "relationship", "status": "proposed", "scope": "machine",
         "summary": "maybe linked to third-33333333"},
    ]
    scope = memory_context.resolve_search_projects("related", project, registry, relationships)
    assert [pid for pid, _ in scope.projects] == [current_id, "other-11111111"]

    none = memory_context.resolve_search_projects("related", project, registry, [])
    assert len(none.projects) == 1
    assert any("no accepted relationship" in n for n in none.notes)


def test_related_project_ids_reads_every_plausible_field() -> None:
    registered = ["me-00000000", "a-11111111", "b-22222222", "c-33333333", "d-44444444"]
    entries = [
        {"type": "relationship", "status": "accepted", "summary": "x",
         "source": {"project": "me-00000000", "related_project": "a-11111111"}},
        {"type": "relationship", "status": "accepted", "summary": "depends on b-22222222"},
        {"type": "relationship", "status": "accepted", "summary": "x", "tags": ["c-33333333"]},
        {"type": "relationship", "status": "accepted", "summary": "x", "projects": ["unknown-99999999"]},
        {"type": "decision", "status": "accepted", "summary": "names d-44444444 but is not a relationship"},
    ]
    assert memory_context.related_project_ids(entries, registered, "me-00000000") == [
        "a-11111111", "b-22222222", "c-33333333"
    ]
