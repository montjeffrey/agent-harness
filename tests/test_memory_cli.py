"""Tests for the `agent-harness-memory` CLI: init, remember, status, prune, and relate.

Exit codes are part of the contract (automation reads them): 0 success,
1 "nothing to do / not initialized", 2 rejected input.

Every test monkeypatches HOME/USERPROFILE to `tmp_path` so nothing here ever
touches the real `~/.agent-harness/memory/` directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harness import memory, memory_store


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
    return root


def _entries(project: Path) -> list[dict]:
    return list(memory_store.iter_entries(memory_store.project_memory_dir(project)))


# --- init -----------------------------------------------------------------


def test_init_creates_layout_and_reports_the_project_id(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    out = capsys.readouterr().out
    assert memory_store.project_id_for(project) in out
    assert (project / ".agent-harness" / "memory" / "durable.md").is_file()


def test_init_is_idempotent(project: Path) -> None:
    assert memory.main(["init", str(project)]) == 0
    assert memory.main(["init", str(project)]) == 0


def test_init_defaults_to_the_current_directory(project: Path, monkeypatch) -> None:
    monkeypatch.chdir(project)
    assert memory.main(["init"]) == 0
    assert (project / ".agent-harness" / "scope.md").is_file()


# --- remember -------------------------------------------------------------


def test_remember_appends_a_proposed_entry(project: Path) -> None:
    memory.main(["init", str(project)])
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Chose JSONL over SQLite for the inbox."]
    )
    assert code == 0
    [rec] = _entries(project)
    assert rec["type"] == "decision"
    assert rec["status"] == "proposed"
    assert rec["confidence"] == "inferred"
    assert rec["id"].startswith("mem-")


def test_remember_works_without_an_explicit_init(project: Path) -> None:
    """Capture must never be blocked by forgetting to initialize first."""
    assert memory.main(
        ["remember", "--project", str(project), "--type", "task",
         "--summary", "Wire the memory CLI into the packaging manifest."]
    ) == 0
    assert len(_entries(project)) == 1


def test_remember_splits_comma_separated_tags(project: Path) -> None:
    memory.main(
        ["remember", "--project", str(project), "--type", "lesson",
         "--summary", "Rebase before merging long-lived branches.",
         "--tags", "git, workflow"]
    )
    [rec] = _entries(project)
    assert rec["tags"] == ["git", "workflow"]


def test_remember_records_commit_and_file_as_source(project: Path) -> None:
    memory.main(
        ["remember", "--project", str(project), "--type", "discovery",
         "--summary", "Telemetry collect was double-counting sessions.",
         "--commit", "9248b7a", "--file", "harness/telemetry.py"]
    )
    [rec] = _entries(project)
    assert rec["source"]["commit"] == "9248b7a"
    assert rec["source"]["file"] == "harness/telemetry.py"


def test_remember_can_accept_a_confirmed_entry_with_manual_confirmation(project: Path) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Gemini telemetry ships in Phase 2.",
         "--confirmed", "--accept", "--manual"]
    )
    assert code == 0
    [rec] = _entries(project)
    assert rec["status"] == "accepted"
    assert rec["confidence"] == "confirmed"


def test_remember_refuses_to_accept_an_inferred_entry(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Something the agent merely guessed.", "--accept"]
    )
    assert code == 2
    assert "inferred" in capsys.readouterr().err
    assert _entries(project) == []


def test_remember_refuses_to_accept_without_a_source(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "decision",
         "--summary", "Confirmed but unsourced.", "--confirmed", "--accept"]
    )
    assert code == 2
    assert "source" in capsys.readouterr().err


def test_remember_rejects_a_probable_secret_and_writes_nothing(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "discovery",
         "--summary", "token ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]
    )
    assert code == 2
    assert "secret" in capsys.readouterr().err
    assert _entries(project) == []


def test_remember_rejects_an_oversized_summary(project: Path, capsys) -> None:
    code = memory.main(
        ["remember", "--project", str(project), "--type", "task",
         "--summary", "x" * (memory_store.MAX_SUMMARY_CHARS + 1)]
    )
    assert code == 2
    assert "summary" in capsys.readouterr().err


def test_remember_rejects_an_unknown_type_at_the_argparse_layer(project: Path) -> None:
    with pytest.raises(SystemExit):
        memory.main(["remember", "--project", str(project), "--type", "vibe",
                     "--summary", "nope"])


# --- status ---------------------------------------------------------------


def test_status_on_an_uninitialized_project_says_so(project: Path, capsys) -> None:
    code = memory.main(["status", "--project", str(project)])
    assert code == 1
    assert "init" in capsys.readouterr().out.lower()


def test_status_reports_counts_by_status(project: Path, capsys) -> None:
    memory.main(["init", str(project)])
    memory.main(["remember", "--project", str(project), "--type", "task",
                 "--summary", "Implement weekly compaction."])
    memory.main(["remember", "--project", str(project), "--type", "blocker",
                 "--summary", "Compaction needs a project-scoped lock."])

    assert memory.main(["status", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "2" in out
    assert "proposed" in out


def test_status_warns_while_scope_is_still_the_untouched_template(project: Path, capsys) -> None:
    memory.main(["init", str(project)])
    assert memory.main(["status", "--project", str(project)]) == 0
    assert "scope" in capsys.readouterr().out.lower()


# --- compact --------------------------------------------------------------


def _remember(project: Path, summary: str, *, type: str = "decision") -> None:
    assert memory.main([
        "remember", "--project", str(project),
        "--type", type, "--summary", summary,
    ]) == 0


def test_compact_reports_nothing_to_do_on_a_fresh_project(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    assert memory.main(["compact", "--project", str(project)]) == 1
    assert "nothing to compact" in capsys.readouterr().out


def test_compact_promotes_accepted_knowledge_and_exits_zero(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    _remember(project, "We chose JSONL for the inbox.")
    entry_id = _entries(project)[0]["id"]
    assert memory.main([
        "review", "accept", entry_id, "--project", str(project), "--commit", "abc1234",
    ]) == 0

    assert memory.main(["compact", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "promoted: 1" in out
    durable = (project / ".agent-harness" / "memory" / "durable.md").read_text(encoding="utf-8")
    assert "We chose JSONL for the inbox." in durable


def test_compact_tells_the_operator_to_rebuild_the_index_after_removing_entries(
    project: Path, capsys
) -> None:
    """The whole point of compaction is fresher retrieval, and removals do not
    reach the semantic index until `index` is re-run."""
    assert memory.main(["init", str(project)]) == 0
    _remember(project, "This one gets rejected.")
    entry_id = _entries(project)[0]["id"]
    assert memory.main(["review", "reject", entry_id, "--project", str(project)]) == 0

    assert memory.main(["compact", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "agent-harness-memory index" in out


def test_compact_refuses_with_exit_two_while_another_run_holds_the_lock(
    project: Path, capsys
) -> None:
    from harness import memory_compact

    assert memory.main(["init", str(project)]) == 0
    directory = memory_store.project_memory_dir(project)
    with memory_compact.compact_lock(directory):
        assert memory.main(["compact", "--project", str(project)]) == 2
    assert "compaction refused" in capsys.readouterr().err


def test_compact_defaults_to_the_current_directory(project: Path, monkeypatch) -> None:
    assert memory.main(["init", str(project)]) == 0
    monkeypatch.chdir(project)
    assert memory.main(["compact"]) in (0, 1)


# --- prune ----------------------------------------------------------------


def _registered() -> dict:
    return memory_store.load_registry()["projects"]


def test_prune_removes_registry_entries_whose_directory_is_gone(
    tmp_path: Path, capsys
) -> None:
    alive = tmp_path / "alive"
    dead = tmp_path / "dead"
    alive.mkdir()
    dead.mkdir()
    assert memory.main(["init", str(alive)]) == 0
    assert memory.main(["init", str(dead)]) == 0
    dead_id = memory_store.project_id_for(dead)
    shutil.rmtree(dead)

    assert memory.main(["prune"]) == 0

    out = capsys.readouterr().out
    assert dead_id in out
    assert "path is gone" in out
    assert set(_registered()) == {memory_store.project_id_for(alive)}


def test_prune_dry_run_reports_without_writing(tmp_path: Path, capsys) -> None:
    dead = tmp_path / "dead"
    dead.mkdir()
    assert memory.main(["init", str(dead)]) == 0
    dead_id = memory_store.project_id_for(dead)
    shutil.rmtree(dead)

    assert memory.main(["prune", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would remove 1" in out
    assert "the registry was not written" in out
    assert set(_registered()) == {dead_id}


def test_prune_removes_an_operator_named_project_that_still_exists(
    project: Path, capsys
) -> None:
    assert memory.main(["init", str(project)]) == 0
    project_id = memory_store.project_id_for(project)

    assert memory.main(["prune", "--id", project_id]) == 0

    assert "named by --id" in capsys.readouterr().out
    assert _registered() == {}
    assert (project / ".agent-harness").is_dir(), "prune must not touch the project"


def test_prune_reports_nothing_to_do_with_exit_one(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    assert memory.main(["prune"]) == 1
    assert "nothing to prune" in capsys.readouterr().out
    assert set(_registered()) == {memory_store.project_id_for(project)}


def test_prune_rejects_an_unknown_id_with_exit_two(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0

    assert memory.main(["prune", "--id", "ghost-00000000"]) == 2

    assert "prune rejected" in capsys.readouterr().err
    assert set(_registered()) == {memory_store.project_id_for(project)}


def test_prune_accepts_several_named_ids(tmp_path: Path) -> None:
    roots = []
    for name in ("one", "two", "three"):
        root = tmp_path / name
        root.mkdir()
        assert memory.main(["init", str(root)]) == 0
        roots.append(root)

    assert memory.main([
        "prune",
        "--id", memory_store.project_id_for(roots[0]),
        "--id", memory_store.project_id_for(roots[2]),
    ]) == 0

    assert set(_registered()) == {memory_store.project_id_for(roots[1])}


# --- registry freshness ---------------------------------------------------


def test_a_command_refreshes_last_seen_for_a_registered_project(
    project: Path, monkeypatch
) -> None:
    assert memory.main(["init", str(project)]) == 0
    project_id = memory_store.project_id_for(project)
    registry = memory_store.load_registry()
    stale = "2020-01-01T00:00:00Z"
    registry["projects"][project_id]["first_seen"] = stale
    registry["projects"][project_id]["last_seen"] = stale
    memory_store.save_registry(registry)

    assert memory.main(["status", "--project", str(project)]) == 0

    record = memory_store.load_registry()["projects"][project_id]
    assert record["first_seen"] == stale, "first_seen must survive a touch"
    assert record["last_seen"] > stale


def test_a_command_on_an_unregistered_project_registers_nothing(
    project: Path,
) -> None:
    """`status` on a bare directory must not enrol it -- only init/enroll do."""
    assert memory.main(["status", "--project", str(project)]) == 1
    assert _registered() == {}


def test_a_registry_that_cannot_be_written_does_not_fail_the_command(
    project: Path, monkeypatch
) -> None:
    assert memory.main(["init", str(project)]) == 0
    registry = memory_store.load_registry()
    registry["projects"][memory_store.project_id_for(project)]["last_seen"] = (
        "2020-01-01T00:00:00Z"
    )
    memory_store.save_registry(registry)

    def explode(_registry: dict) -> None:
        raise OSError("read-only home")

    monkeypatch.setattr(memory_store, "save_registry", explode)
    assert memory.main(["status", "--project", str(project)]) == 0


# --- relate ---------------------------------------------------------------


def _related_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Two registered projects where one names the other by registered name."""
    alpha = tmp_path / "alpha-engine"
    beta = tmp_path / "beta-tools"
    for root in (alpha, beta):
        root.mkdir()
        assert memory.main(["init", str(root)]) == 0
    memory.main(["remember", "--project", str(alpha), "--type", "decision",
                 "--summary", "Reuse the ingestion pipeline from beta-tools."])
    memory.main(["remember", "--project", str(beta), "--type", "decision",
                 "--summary", "Ingestion pipeline is the stable surface."])
    return alpha, beta


def _machine_relationships() -> list[dict]:
    return list(memory_store.iter_entries(memory_store.machine_memory_dir(), type="relationship"))


def test_relate_dry_run_prints_candidates_and_writes_nothing(tmp_path: Path, capsys) -> None:
    _related_pair(tmp_path)

    assert memory.main(["relate", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would propose 1 relationship(s)" in out
    assert "alpha-engine <-> beta-tools" in out
    assert "cross-reference:" in out
    assert "dry run -- nothing was written." in out
    assert _machine_relationships() == []


def test_relate_proposes_into_machine_scope_and_exits_one_on_rerun(tmp_path: Path, capsys) -> None:
    alpha, beta = _related_pair(tmp_path)

    assert memory.main(["relate"]) == 0
    [entry] = _machine_relationships()
    assert entry["status"] == "proposed"
    assert entry["confidence"] == "inferred"
    assert entry["source"]["projects"] == sorted(
        (memory_store.project_id_for(alpha), memory_store.project_id_for(beta))
    )
    assert entry["id"] in capsys.readouterr().out

    assert memory.main(["relate"]) == 1
    out = capsys.readouterr().out
    assert "already proposed" in out
    assert "nothing to propose" in out
    assert len(_machine_relationships()) == 1


def test_relate_reports_nothing_with_a_single_project(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    memory.main(["remember", "--project", str(project), "--type", "task", "--summary", "Alone."])

    assert memory.main(["relate"]) == 1

    out = capsys.readouterr().out
    assert "at least two" in out
    assert _machine_relationships() == []


def test_relate_min_score_raises_the_bar(tmp_path: Path, capsys) -> None:
    _related_pair(tmp_path)

    assert memory.main(["relate", "--min-score", "5"]) == 1

    assert "below threshold" in capsys.readouterr().out
    assert _machine_relationships() == []


def test_relate_project_filter_accepts_ids_and_rejects_unknown_ones(tmp_path: Path, capsys) -> None:
    alpha, _ = _related_pair(tmp_path)

    assert memory.main(["relate", "--dry-run", "--project", memory_store.project_id_for(alpha)]) == 0
    assert memory.main(["relate", "--project", "ghost-00000000"]) == 2

    assert "relate rejected" in capsys.readouterr().err
    assert _machine_relationships() == []


def test_relate_rejects_a_negative_min_score_with_exit_two(tmp_path: Path, capsys) -> None:
    _related_pair(tmp_path)

    assert memory.main(["relate", "--min-score", "-1"]) == 2

    assert "relate rejected" in capsys.readouterr().err


# --- maintain -------------------------------------------------------------


def test_maintain_on_an_uninitialized_project_exits_1(project: Path, capsys) -> None:
    assert memory.main(["maintain", "--project", str(project)]) == 1
    assert "init" in capsys.readouterr().err


def test_maintain_compacts_when_due_and_is_idempotent_within_the_interval(
    project: Path, capsys, monkeypatch
) -> None:
    monkeypatch.delenv("AGENT_HARNESS_COMPACT_INTERVAL_DAYS", raising=False)
    assert memory.main(["init", str(project)]) == 0
    _remember(project, "Something proposed.")

    assert memory.main(["maintain", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "compaction: due -- never compacted" in out
    assert "compacted (" in out
    assert "agent-harness-memory triage" in out

    assert memory.main(["maintain", "--project", str(project)]) == 1
    out = capsys.readouterr().out
    assert "not due" in out
    # The proposed entry survived both runs: maintain never reviews.
    assert _entries(project)[0]["status"] == "proposed"


def test_maintain_check_reports_due_ness_without_writing(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    active = project / ".agent-harness" / "memory" / "active.md"
    before = active.read_bytes()
    assert memory.main(["maintain", "--project", str(project), "--check"]) == 0
    assert active.read_bytes() == before
    assert memory.main(["maintain", "--project", str(project)]) == 0
    capsys.readouterr()
    assert memory.main(["maintain", "--project", str(project), "--check"]) == 1


def test_maintain_force_runs_when_not_due(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    assert memory.main(["maintain", "--project", str(project)]) == 0
    assert memory.main(["maintain", "--project", str(project), "--force"]) == 0
    assert "forced" in capsys.readouterr().out


def test_maintain_refuses_with_exit_two_while_compaction_holds_the_lock(
    project: Path, capsys
) -> None:
    from harness import memory_compact

    assert memory.main(["init", str(project)]) == 0
    with memory_compact.compact_lock(memory_store.project_memory_dir(project)):
        assert memory.main(["maintain", "--project", str(project)]) == 2
    assert "maintenance refused" in capsys.readouterr().err


def test_maintain_reports_removals_leave_the_index_stale(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    _remember(project, "Rejected soon.")
    entry_id = _entries(project)[0]["id"]
    assert memory.main(["review", "reject", entry_id, "--project", str(project)]) == 0
    assert memory.main(["maintain", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "archived: 1" in out
    assert "agent-harness-memory index" in out


# --- status surfaces maintenance ------------------------------------------


def test_status_surfaces_what_is_owed(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    _remember(project, "Awaiting review.")
    assert memory.main(["status", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "maintenance:" in out
    assert "compaction: DUE" in out
    assert "agent-harness-memory maintain" in out
    assert "index:      STALE" in out
    assert "backlog:    1 proposed" in out
    assert "agent-harness-memory triage" in out


def test_status_is_quiet_once_maintenance_is_done(project: Path, capsys) -> None:
    assert memory.main(["init", str(project)]) == 0
    assert memory.main(["maintain", "--project", str(project)]) == 0
    capsys.readouterr()
    assert memory.main(["status", "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "compaction: ok" in out
    assert "nothing awaiting review" in out


# --- index --if-stale ------------------------------------------------------


def test_index_if_stale_builds_only_past_the_drift_threshold(
    project: Path, capsys, monkeypatch
) -> None:
    from test_memory_maintain import FakeClient

    from harness import memory_index, memory_maintain

    monkeypatch.setenv(memory_maintain.DRIFT_THRESHOLD_ENV, "2")
    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "_default_client", FakeClient)
    FakeClient.stores.clear()
    assert memory.main(["init", str(project)]) == 0
    _remember(project, "First.")

    # Never built: stale, so it builds and leaves a manifest.
    assert memory.main(["index", "--project", str(project), "--if-stale"]) == 0
    assert "added=1" in capsys.readouterr().out
    index_dir = memory_index.index_dir_for(memory_store.project_id_for(project))
    assert memory_maintain.manifest_path(index_dir).is_file()

    # One new entry is below the threshold: fresh, nothing to do.
    _remember(project, "Second.")
    assert memory.main(["index", "--project", str(project), "--if-stale"]) == 1
    assert "index is fresh" in capsys.readouterr().out

    # Two new entries reach it: rebuild, embedding only what changed.
    _remember(project, "Third.")
    assert memory.main(["index", "--project", str(project), "--if-stale"]) == 0
    assert "added=2" in capsys.readouterr().out


def test_index_if_stale_decides_before_touching_chromadb(
    project: Path, capsys, monkeypatch
) -> None:
    """A fresh index must cost nothing: the drift check runs off the manifest
    before any client is constructed."""
    from test_memory_maintain import FakeClient

    from harness import memory_index

    monkeypatch.setattr(memory_index, "chromadb_available", lambda: True)
    monkeypatch.setattr(memory_index, "_default_client", FakeClient)
    FakeClient.stores.clear()
    assert memory.main(["init", str(project)]) == 0
    _remember(project, "Only.")
    assert memory.main(["index", "--project", str(project)]) == 0
    capsys.readouterr()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a fresh --if-stale run must not open the index")

    monkeypatch.setattr(memory_index, "_default_client", forbidden)
    monkeypatch.setattr(memory_index, "build_index", forbidden)
    assert memory.main(["index", "--project", str(project), "--if-stale"]) == 1
