"""Tests for the retrieval eval harness.

The harness exists to make later ranking claims checkable, so its own
failure modes are the ones worth pinning: a case set that quietly loses a
line, a pool that is not the pool production ranks, an unavailable backend
that scores as a bad one, and a bare entry id that lets a machine hit be
counted as a project hit.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory, memory_eval, memory_index, memory_store, work_store

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _add(project: Path, summary: str, scope: str = "project", **overrides) -> None:
    directory = (
        memory_store.project_memory_dir(project)
        if scope == "project"
        else memory_store.machine_memory_dir()
    )
    memory_store.append_entries(
        [
            {
                "type": "decision",
                "scope": scope,
                "status": "accepted",
                "confidence": "confirmed",
                "source": {"manual": True},
                "summary": summary,
                **overrides,
            }
        ],
        directory=directory,
        now=NOW,
    )


def _keys(project: Path) -> dict[str, str]:
    """summary -> index key, so tests name entries by meaning, not by id."""
    entries = list(
        memory_store.iter_entries(memory_store.project_memory_dir(project))
    ) + list(memory_store.iter_entries(memory_store.machine_memory_dir()))
    return {e["summary"]: memory_index.index_key(e) for e in entries}


# --- case parsing ---------------------------------------------------------


def test_bare_entry_ids_are_rejected() -> None:
    """Ids are unique per store; a bare id scores a machine hit as a project one."""
    with pytest.raises(memory_eval.EvalCaseError, match="not a 'scope:id' index key"):
        memory_eval.parse_case(
            {"query": "anything", "relevant": ["mem-2026-08-07-001"]}, "cases:1"
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"relevant": ["project:mem-2026-08-07-001"]},
        {"query": "   ", "relevant": ["project:mem-2026-08-07-001"]},
        {"query": "ok", "relevant": []},
        {"query": "ok", "relevant": "project:mem-2026-08-07-001"},
        {"query": "ok", "relevant": ["project:x"], "name": 7},
    ],
)
def test_malformed_cases_raise_rather_than_being_skipped(payload) -> None:
    with pytest.raises(memory_eval.EvalCaseError):
        memory_eval.parse_case(payload, "cases:1")


def test_case_name_defaults_to_the_query() -> None:
    case = memory_eval.parse_case(
        {"query": "how did we redo login", "relevant": ["project:mem-2026-08-07-001"]},
        "cases:1",
    )
    assert case.name == "how did we redo login"


def test_duplicate_relevant_keys_collapse() -> None:
    """Otherwise recall's denominator counts the same entry twice."""
    case = memory_eval.parse_case(
        {"query": "q", "relevant": ["project:a", "project:a", "project:b"]}, "cases:1"
    )
    assert case.relevant == ("project:a", "project:b")


def test_load_cases_skips_blanks_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "# a comment\n"
        "\n"
        '{"query": "one", "relevant": ["project:mem-2026-08-07-001"]}\n'
        '{"query": "two", "relevant": ["machine:mem-2026-08-07-001"]}\n',
        encoding="utf-8",
    )
    assert [c.query for c in memory_eval.load_cases(path)] == ["one", "two"]


def test_load_cases_names_the_offending_line(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"query": "one", "relevant": ["project:mem-2026-08-07-001"]}\n'
        "{not json\n",
        encoding="utf-8",
    )
    with pytest.raises(memory_eval.EvalCaseError, match="cases.jsonl:2"):
        memory_eval.load_cases(path)


def test_load_cases_rejects_a_file_with_no_cases(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text("# nothing but a comment\n", encoding="utf-8")
    with pytest.raises(memory_eval.EvalCaseError, match="no cases found"):
        memory_eval.load_cases(path)


def test_missing_case_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        memory_eval.load_cases(tmp_path / "absent.jsonl")


def test_cases_round_trip_through_jsonl(tmp_path: Path) -> None:
    cases = [memory_eval.EvalCase("n", "q", ("project:mem-2026-08-07-001",))]
    path = memory_eval.write_cases(tmp_path / "c.jsonl", cases)
    assert memory_eval.load_cases(path) == cases


# --- scoring --------------------------------------------------------------


def test_score_rewards_a_perfect_answer() -> None:
    assert memory_eval.score(["a", "b"], ["a", "b"]) == (1.0, 1.0, 1.0)


def test_score_reports_a_miss_as_zero_across_the_board() -> None:
    assert memory_eval.score(["x", "y"], ["a"]) == (0.0, 0.0, 0.0)


def test_precision_divides_by_what_was_returned_not_by_the_limit() -> None:
    """A two-entry store must not be scored as if it failed to return forty."""
    precision, recall, _ = memory_eval.score(["a", "b"], ["a"])
    assert precision == 0.5
    assert recall == 1.0


def test_reciprocal_rank_falls_when_the_right_entry_is_buried() -> None:
    """Precision and recall can both look fine while an agent pays for depth."""
    _, _, first = memory_eval.score(["a", "x", "y"], ["a"])
    _, _, third = memory_eval.score(["x", "y", "a"], ["a"])
    assert first == 1.0
    assert third == pytest.approx(1 / 3)


# --- candidate pool -------------------------------------------------------


def test_pool_matches_what_a_packet_actually_ranks(project: Path) -> None:
    _add(project, "a live decision")
    _add(project, "a superseded decision", status="superseded")
    _add(project, "an open task", type="task", status="accepted")
    _add(project, "a link", type="relationship")
    _add(project, "a machine lesson", scope="machine", type="lesson")

    summaries = {e["summary"] for e in memory_eval.candidate_pool(project)}
    assert summaries == {"a live decision", "a machine lesson"}


def test_pool_is_empty_before_anything_is_recorded(project: Path) -> None:
    assert memory_eval.candidate_pool(project) == []


# --- evaluate -------------------------------------------------------------


def test_a_matching_entry_ranks_first_deterministically(project: Path) -> None:
    _add(project, "rewrote the login flow", tags=["auth"])
    _add(project, "upgraded the invoice printer", tags=["billing"])
    keys = _keys(project)

    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("auth", "login auth", (keys["rewrote the login flow"],))],
        strategies=("deterministic",),
        now=NOW,
    )
    result = report.cases[0].results[0]
    assert result.strategy == "deterministic"
    assert result.selected[0] == keys["rewrote the login flow"]
    assert result.reciprocal_rank == 1.0
    assert result.recall == 1.0


def test_semantic_is_reported_unavailable_never_silently_swapped(
    project: Path,
) -> None:
    """A benchmark that ran deterministic under a `semantic` heading is a lie."""
    _add(project, "rewrote the login flow")
    keys = _keys(project)

    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("auth", "login", (keys["rewrote the login flow"],))],
        strategies=("deterministic", "semantic"),
        now=NOW,
    )
    strategies = {r.strategy for r in report.cases[0].results}
    assert strategies == {"deterministic"}
    assert any(n.startswith("semantic: unavailable") for n in report.notes)


def test_aggregate_omits_a_strategy_that_never_ran(project: Path) -> None:
    """Averaging in an unmeasured zero makes an absent backend look broken."""
    _add(project, "rewrote the login flow")
    keys = _keys(project)

    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("auth", "login", (keys["rewrote the login flow"],))],
        strategies=("deterministic", "semantic"),
        now=NOW,
    )
    assert set(report.aggregate()) == {"deterministic"}
    assert report.aggregate()["deterministic"]["cases"] == 1.0


def test_report_records_the_pool_size_and_limit(project: Path) -> None:
    _add(project, "one")
    _add(project, "two")
    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("n", "one", ("project:mem-2026-08-07-001",))],
        limit=5,
        strategies=("deterministic",),
        now=NOW,
    )
    assert report.candidates == 2
    assert report.limit == 5
    assert report.generated_at == "2026-08-07T12:00:00Z"


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_is_refused(project: Path, limit: int) -> None:
    """`[:−1]` drops the last entry and reports a score for a cut nobody asked for."""
    with pytest.raises(ValueError, match="limit must be positive"):
        memory_eval.evaluate(
            project,
            [memory_eval.EvalCase("n", "q", ("project:mem-2026-08-07-001",))],
            limit=limit,
            strategies=("deterministic",),
            now=NOW,
        )


def test_an_empty_store_says_so_rather_than_reporting_a_silent_zero(
    project: Path,
) -> None:
    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("n", "q", ("project:mem-2026-08-07-001",))],
        strategies=("deterministic",),
        now=NOW,
    )
    assert any("no durable entries" in n for n in report.notes)


# --- rendering ------------------------------------------------------------


def test_markdown_carries_the_aggregate_and_the_case(project: Path) -> None:
    _add(project, "rewrote the login flow")
    keys = _keys(project)
    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("auth", "login", (keys["rewrote the login flow"],))],
        strategies=("deterministic",),
        now=NOW,
    )
    text = memory_eval.render_markdown(report)
    assert "## Aggregate" in text
    assert "### auth" in text
    assert "deterministic" in text


def test_json_is_parseable_and_carries_the_schema(project: Path) -> None:
    _add(project, "rewrote the login flow")
    keys = _keys(project)
    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("auth", "login", (keys["rewrote the login flow"],))],
        strategies=("deterministic",),
        now=NOW,
    )
    payload = json.loads(memory_eval.render_json(report))
    assert payload["schema"] == memory_store.SCHEMA_VERSION
    assert payload["cases"][0]["name"] == "auth"
    assert payload["aggregate"]["deterministic"]["cases"] == 1.0


def test_report_filename_matches_the_report_clock(project: Path) -> None:
    """One clock read: a filename that disagrees with its contents is a trap."""
    report = memory_eval.evaluate(
        project,
        [memory_eval.EvalCase("n", "q", ("project:mem-2026-08-07-001",))],
        strategies=("deterministic",),
        now=NOW,
    )
    assert memory_eval.report_path(project, report).name == "2026-08-07T120000Z.json"


# --- CLI ------------------------------------------------------------------


def test_cli_scores_and_prints(project: Path, capsys) -> None:
    _add(project, "rewrote the login flow")
    keys = _keys(project)
    cases = work_store.ensure_work_path(project, "eval", "default.jsonl")
    memory_eval.write_cases(
        cases,
        [memory_eval.EvalCase("auth", "login", (keys["rewrote the login flow"],))],
    )

    code = memory.main(
        ["eval", "--project", str(project), "--strategy", "deterministic"]
    )
    assert code == 0
    assert "Retrieval eval" in capsys.readouterr().out


def test_cli_exits_one_when_there_are_no_cases(project: Path, capsys) -> None:
    code = memory.main(["eval", "--project", str(project)])
    assert code == 1
    assert "no eval case file" in capsys.readouterr().err


def test_cli_exits_two_on_a_malformed_case(project: Path, capsys) -> None:
    cases = work_store.ensure_work_path(project, "eval", "default.jsonl")
    cases.write_text('{"query": "q", "relevant": ["bare-id"]}\n', encoding="utf-8")
    code = memory.main(["eval", "--project", str(project)])
    assert code == 2
    assert "index key" in capsys.readouterr().err


def test_cli_exits_two_on_a_non_positive_limit(project: Path, capsys) -> None:
    cases = work_store.ensure_work_path(project, "eval", "default.jsonl")
    memory_eval.write_cases(
        cases, [memory_eval.EvalCase("n", "q", ("project:mem-2026-08-07-001",))]
    )
    code = memory.main(["eval", "--project", str(project), "--limit", "-1"])
    assert code == 2
    assert "limit must be positive" in capsys.readouterr().err


def test_cli_exits_one_when_no_strategy_could_be_scored(project: Path, capsys) -> None:
    """Nothing measured is not success.

    `semantic` alone with no index resolves nothing, so the run scores no
    case at all. Exiting 0 would let automation record a measured-nothing
    run as a baseline -- the same failure `aggregate` avoids by omitting a
    strategy that never ran rather than averaging in an unmeasured zero.
    The report still prints: the note is the diagnosis.
    """
    _add(project, "rewrote the login flow")
    keys = _keys(project)
    cases = work_store.ensure_work_path(project, "eval", "default.jsonl")
    memory_eval.write_cases(
        cases,
        [memory_eval.EvalCase("auth", "login", (keys["rewrote the login flow"],))],
    )

    code = memory.main(["eval", "--project", str(project), "--strategy", "semantic"])
    assert code == 1
    assert "no strategy could be resolved" in capsys.readouterr().out


def test_cli_exits_zero_when_the_pool_is_empty_but_a_strategy_ran(
    project: Path, capsys
) -> None:
    """An empty store is a measured zero, not an unavailable capability.

    The counterpart to the test above, and the line between them: there,
    nothing ran; here, deterministic ran and honestly scored zero against a
    store with nothing in it. Collapsing the two would make `exit 1` mean
    "unavailable or just empty", which no caller could branch on.
    """
    cases = work_store.ensure_work_path(project, "eval", "default.jsonl")
    memory_eval.write_cases(
        cases, [memory_eval.EvalCase("n", "q", ("project:mem-2026-08-07-001",))]
    )

    code = memory.main(
        ["eval", "--project", str(project), "--strategy", "deterministic"]
    )
    assert code == 0
    assert "zero by construction" in capsys.readouterr().out


def test_cli_save_lands_inside_the_ignored_store(project: Path) -> None:
    _add(project, "rewrote the login flow")
    keys = _keys(project)
    cases = work_store.ensure_work_path(project, "eval", "default.jsonl")
    memory_eval.write_cases(
        cases,
        [memory_eval.EvalCase("auth", "login", (keys["rewrote the login flow"],))],
    )

    assert memory.main(["eval", "--project", str(project), "--save"]) == 0

    results = work_store.work_dir(project) / "eval" / "results"
    saved = list(results.glob("*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8"))["cases"][0]["name"] == "auth"
    assert work_store.is_ignored(project)
