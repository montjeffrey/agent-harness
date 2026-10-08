"""Tests for `harness.memory_maintain`: index drift from the build manifest,
and the due-aware maintenance pass.

The load-bearing guarantees: drift is decided without ever opening the
semantic index, `maintain` is idempotent inside the compaction interval, and
`--check` writes nothing. Everything runs through a fake chromadb client so
the suite never downloads a model, and every test's home is a tmp path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from harness import memory_compact, memory_index, memory_maintain, memory_store

NOW = datetime(2026, 9, 11, 9, 0, 0, tzinfo=timezone.utc)


class FakeCollection:
    """Enough of a chromadb collection for `build_index` to run against."""

    def __init__(self) -> None:
        self.docs: dict[str, str] = {}
        self.metas: dict[str, dict] = {}

    def get(self, include: Any = None) -> dict:
        ids = list(self.docs)
        return {"ids": ids, "metadatas": [self.metas[i] for i in ids]}

    def add(self, ids, documents, metadatas) -> None:
        for i, d, m in zip(ids, documents, metadatas):
            self.docs[i] = d
            self.metas[i] = m

    upsert = add

    def delete(self, ids) -> None:
        for i in ids:
            self.docs.pop(i, None)
            self.metas.pop(i, None)


class FakeClient:
    """Stand-in for a chromadb PersistentClient, one store per index dir.

    Shared with `test_enroll` and `test_memory_cli`, which need to drive the
    real `build_indexes` path without the `semantic` extra.
    """

    stores: dict[str, FakeCollection] = {}

    def __init__(self, index_dir: Path) -> None:
        self.key = str(index_dir)

    def get_or_create_collection(self, name: str, **kwargs) -> FakeCollection:
        return FakeClient.stores.setdefault(f"{self.key}::{name}", FakeCollection())

    def get_collection(self, name: str, **kwargs) -> FakeCollection:
        try:
            return FakeClient.stores[f"{self.key}::{name}"]
        except KeyError:
            raise RuntimeError("collection does not exist") from None

    def get_max_batch_size(self) -> int:
        return 2


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv(memory_maintain.DRIFT_THRESHOLD_ENV, raising=False)
    monkeypatch.delenv(memory_compact.COMPACT_INTERVAL_ENV, raising=False)
    FakeClient.stores.clear()
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    memory_store.init_project(root, now=NOW)
    return root


def _dir(project: Path) -> Path:
    return memory_store.project_memory_dir(project)


def _add(project: Path, summary: str, **overrides) -> str:
    entry = {"type": "decision", "scope": "project", "summary": summary, **overrides}
    memory_store.append_entries([entry], directory=_dir(project), now=NOW)
    return list(memory_store.iter_entries(_dir(project)))[-1]["id"]


def _build(project: Path) -> list[memory_maintain.BuildReport]:
    return memory_maintain.build_indexes(project, client_factory=FakeClient, now=NOW)


# --- threshold ------------------------------------------------------------


def test_the_drift_threshold_defaults_and_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert memory_maintain.drift_threshold() == memory_maintain.DRIFT_THRESHOLD
    monkeypatch.setenv(memory_maintain.DRIFT_THRESHOLD_ENV, "3")
    assert memory_maintain.drift_threshold() == 3


@pytest.mark.parametrize("value", ["", "lots", "-4"])
def test_an_unusable_threshold_falls_back_or_floors_at_zero(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Same contract as AGENT_HARNESS_ADAPTIVE_MIN_ENTRIES: garbage means the
    default, and a negative number floors at zero rather than raising."""
    monkeypatch.setenv(memory_maintain.DRIFT_THRESHOLD_ENV, value)
    result = memory_maintain.drift_threshold()
    assert result == (0 if value == "-4" else memory_maintain.DRIFT_THRESHOLD)


# --- manifest -------------------------------------------------------------


def test_the_manifest_round_trips_keys_and_hashes(project: Path) -> None:
    _add(project, "Alpha.")
    entries = list(memory_store.iter_entries(_dir(project)))
    index_dir = project / "idx"
    memory_maintain.write_manifest(index_dir, entries, now=NOW)

    built_at, hashes = memory_maintain.read_manifest(index_dir)
    assert built_at == memory_store.format_ts(NOW)
    assert hashes == {
        memory_index.index_key(entries[0]): memory_index.entry_content_hash(entries[0])
    }


def test_a_corrupt_manifest_reads_as_absent(tmp_path: Path) -> None:
    index_dir = tmp_path / "idx"
    index_dir.mkdir()
    memory_maintain.manifest_path(index_dir).write_text("{not json", encoding="utf-8")
    assert memory_maintain.read_manifest(index_dir) is None


def test_the_manifest_omits_entries_the_index_would_skip(tmp_path: Path) -> None:
    """An entry with no embeddable text never enters the index, so recording
    it would report drift the index can never close."""
    index_dir = tmp_path / "idx"
    entries = [{"id": "mem-2026-09-11-001", "scope": "project", "summary": "", "tags": []}]
    memory_maintain.write_manifest(index_dir, entries, now=NOW)
    assert memory_maintain.read_manifest(index_dir)[1] == {}


# --- drift ----------------------------------------------------------------


def test_an_empty_store_with_no_index_is_not_stale(project: Path) -> None:
    drift = memory_maintain.project_drift(project)
    assert drift["project"].exists is False
    assert drift["project"].stale is False
    assert "nothing to index" in drift["project"].describe()


def test_entries_with_no_index_yet_are_stale(project: Path) -> None:
    _add(project, "Alpha.")
    drift = memory_maintain.project_drift(project)["project"]
    assert drift.stale is True
    assert "no semantic index" in drift.describe()


def test_a_fresh_build_reports_no_drift(project: Path) -> None:
    _add(project, "Alpha.")
    _build(project)
    drift = memory_maintain.project_drift(project)["project"]
    assert drift.pending == 0
    assert drift.stale is False
    assert "up to date" in drift.describe()


def test_drift_counts_added_removed_and_reworded_entries(project: Path) -> None:
    first = _add(project, "Alpha.")
    second = _add(project, "Beta.")
    _build(project)

    # Add a third *before* dropping `second`: `next_entry_id` continues past
    # the highest id present, so removing 002 first would hand 002 to Gamma
    # and the manifest would see a reword where the store saw a swap.
    _add(project, "Gamma.")
    lines = memory_store.read_lines(_dir(project))
    kept = []
    for line in lines:
        rec = json.loads(line)
        if rec["id"] == second:
            continue
        if rec["id"] == first:
            rec["summary"] = "Alpha, restated."
        kept.append(json.dumps(rec))
    memory_store.write_lines(kept, _dir(project))

    drift = memory_maintain.project_drift(project)["project"]
    assert (drift.added, drift.removed, drift.changed) == (1, 1, 1)
    assert drift.pending == 3
    assert "1 new" in drift.describe()
    assert "1 removed" in drift.describe()
    assert "1 reworded" in drift.describe()


def test_drift_below_the_threshold_is_not_stale(project: Path) -> None:
    _add(project, "Alpha.")
    _build(project)
    _add(project, "Beta.")
    assert memory_maintain.project_drift(project)["project"].stale is False


def test_drift_at_the_threshold_is_stale(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(memory_maintain.DRIFT_THRESHOLD_ENV, "2")
    _add(project, "Alpha.")
    _build(project)
    _add(project, "Beta.")
    assert memory_maintain.project_drift(project)["project"].stale is False
    _add(project, "Gamma.")
    assert memory_maintain.project_drift(project)["project"].stale is True


def test_a_zero_threshold_makes_any_change_stale(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(memory_maintain.DRIFT_THRESHOLD_ENV, "0")
    _add(project, "Alpha.")
    _build(project)
    assert memory_maintain.project_drift(project)["project"].stale is False
    _add(project, "Beta.")
    assert memory_maintain.project_drift(project)["project"].stale is True


def test_a_status_change_alone_is_not_drift(project: Path) -> None:
    """Accepting an entry changes metadata, not the embedded document, so the
    index is not behind and must not be reported as such."""
    from harness import memory_review

    entry_id = _add(project, "Alpha.")
    _build(project)
    memory_review.apply_review(_dir(project), entry_id, "accept", now=NOW)
    assert memory_maintain.project_drift(project)["project"].pending == 0


def test_drift_never_opens_the_index(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the manifest: drift is a file comparison, so the
    hot path never pays for a chromadb client."""
    _add(project, "Alpha.")
    _build(project)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("drift must not open the semantic index")

    monkeypatch.setattr(memory_index, "_default_client", forbidden)
    monkeypatch.setattr(memory_index, "build_index", forbidden)
    monkeypatch.setattr(memory_index, "probe_index", forbidden)
    memory_maintain.project_drift(project)
    memory_maintain.status_lines(project, now=NOW)
    memory_maintain.maintain(project, now=NOW, check=True)


# --- building -------------------------------------------------------------


def test_build_indexes_writes_a_manifest_per_built_scope(project: Path) -> None:
    _add(project, "Project note.")
    memory_store.append_entries(
        [{"type": "lesson", "scope": "machine", "summary": "Machine lesson."}],
        directory=memory_store.machine_memory_dir(), now=NOW,
    )
    reports = _build(project)
    assert [(r.scope, r.count) for r in reports] == [("project", 1), ("machine", 1)]
    for report in reports:
        assert memory_maintain.manifest_path(report.index_dir).is_file()
        assert report.stats.added == 1


def test_build_indexes_skips_an_empty_scope_rather_than_emptying_it(project: Path) -> None:
    _add(project, "Project note.")
    reports = _build(project)
    assert [r.scope for r in reports] == ["project"]
    assert not memory_maintain.manifest_path(memory_index.machine_index_dir()).exists()


def test_a_rebuild_refreshes_the_manifest(project: Path) -> None:
    _add(project, "Alpha.")
    _build(project)
    _add(project, "Beta.")
    later = NOW + timedelta(hours=1)
    memory_maintain.build_indexes(project, client_factory=FakeClient, now=later)
    built_at, hashes = memory_maintain.read_manifest(
        memory_index.index_dir_for(memory_store.project_id_for(project))
    )
    assert built_at == memory_store.format_ts(later)
    assert len(hashes) == 2
    assert memory_maintain.project_drift(project)["project"].pending == 0


# --- maintain -------------------------------------------------------------


def test_a_never_compacted_project_is_due_and_gets_compacted(project: Path) -> None:
    result = memory_maintain.maintain(project, now=NOW)
    assert result.due.due is True
    assert "never" in result.due.reason
    assert result.ran is True
    assert memory_compact.last_compacted(_dir(project)) == NOW


def test_maintain_is_idempotent_inside_the_interval(project: Path) -> None:
    memory_maintain.maintain(project, now=NOW)
    active = (_dir(project) / "active.md").read_bytes()

    again = memory_maintain.maintain(project, now=NOW + timedelta(days=1))
    assert again.due.due is False
    assert again.ran is False
    assert (_dir(project) / "active.md").read_bytes() == active


def test_maintain_runs_again_once_the_interval_has_passed(project: Path) -> None:
    memory_maintain.maintain(project, now=NOW)
    later = memory_maintain.maintain(
        project, now=NOW + timedelta(days=memory_compact.COMPACT_INTERVAL_DAYS)
    )
    assert later.due.due is True
    assert later.ran is True


def test_check_reports_without_writing(project: Path) -> None:
    before = (_dir(project) / "active.md").read_bytes()
    result = memory_maintain.maintain(project, now=NOW, check=True)
    assert result.due.due is True
    assert result.ran is False
    assert result.compact is None
    assert (_dir(project) / "active.md").read_bytes() == before
    assert not (_dir(project) / ".compact.lock").exists()


def test_force_compacts_when_not_due(project: Path) -> None:
    memory_maintain.maintain(project, now=NOW)
    forced = memory_maintain.maintain(project, now=NOW + timedelta(hours=1), force=True)
    assert forced.due.due is False
    assert forced.ran is True
    assert forced.forced is True


def test_maintain_reports_the_index_stale_after_compaction_removes_entries(project: Path) -> None:
    from harness import memory_review

    entry_id = _add(project, "Alpha.")
    _build(project)
    memory_review.apply_review(_dir(project), entry_id, "reject", now=NOW)

    result = memory_maintain.maintain(project, now=NOW)
    assert result.compact is not None and result.compact.archived == 1
    assert result.index_stale is True
    assert any("index" in line for line in result.advice())


def test_maintain_counts_the_backlog_and_never_touches_it(project: Path) -> None:
    _add(project, "Alpha.")
    _add(project, "Beta.")
    result = memory_maintain.maintain(project, now=NOW)
    assert result.backlog == 2
    assert all(e["status"] == "proposed" for e in memory_store.iter_entries(_dir(project)))
    assert any("triage" in line for line in result.advice())


def test_maintain_never_builds_the_index(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`index` is the only command allowed to touch chromadb."""
    _add(project, "Alpha.")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("maintain must not build the index")

    monkeypatch.setattr(memory_index, "build_index", forbidden)
    result = memory_maintain.maintain(project, now=NOW)
    assert result.ran is True
    assert result.index_stale is True


def test_maintain_propagates_the_compaction_lock(project: Path) -> None:
    with memory_compact.compact_lock(_dir(project), now=NOW):
        with pytest.raises(memory_compact.CompactLockError):
            memory_maintain.maintain(project, now=NOW)


# --- rendering ------------------------------------------------------------


def test_status_lines_name_every_owed_action_with_its_command(project: Path) -> None:
    _add(project, "Alpha.")
    text = "\n".join(memory_maintain.status_lines(project, now=NOW))
    assert "compaction: DUE" in text
    assert memory_maintain.MAINTAIN_COMMAND in text
    assert "index:      STALE" in text
    assert memory_maintain.INDEX_COMMAND in text
    assert "1 proposed" in text
    assert memory_maintain.TRIAGE_COMMAND in text


def test_status_lines_are_quiet_when_nothing_is_owed(project: Path) -> None:
    memory_maintain.maintain(project, now=NOW)
    text = "\n".join(memory_maintain.status_lines(project, now=NOW))
    assert "compaction: ok" in text
    assert "STALE" not in text
    assert "nothing awaiting review" in text
    assert "run `" not in text


def test_render_shows_what_ran_and_what_is_still_owed(project: Path) -> None:
    _add(project, "Alpha.")
    result = memory_maintain.maintain(project, now=NOW)
    text = memory_maintain.render(result, project)
    assert "compaction: due -- never compacted" in text
    assert "compacted (2026-W37)" in text
    assert "the stamp was refreshed" in text
    assert "next:" in text
    assert memory_maintain.TRIAGE_COMMAND in text
