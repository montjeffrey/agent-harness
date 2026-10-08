"""Tests for the test suite's own machine-store isolation.

The defect these guard against is not a bug in a feature -- it is the suite
enrolling its throwaway `tmp_path` projects in the operator's real
`~/.agent-harness/memory/registry.json`. That has happened, it is silent
(every test still passes), and the evidence only surfaces weeks later as
tmp paths accumulating in a file nobody reads. So the isolation itself gets
tests, and the alarm on it gets tests too.
"""

from __future__ import annotations

import json
from pathlib import Path

import conftest

from harness import memory, memory_store


def test_path_home_inside_a_test_is_not_the_operators_home() -> None:
    assert Path.home() != conftest.REAL_REGISTRY.parent.parent.parent


def test_machine_memory_dir_resolves_under_the_isolated_home(
    isolated_home: Path,
) -> None:
    assert memory_store.machine_memory_dir() == (
        isolated_home / ".agent-harness" / "memory"
    )


def test_initialising_a_project_writes_only_the_isolated_registry(
    tmp_path: Path, isolated_home: Path
) -> None:
    before = (
        conftest.REAL_REGISTRY.read_bytes()
        if conftest.REAL_REGISTRY.is_file()
        else None
    )
    root = tmp_path / "throwaway"
    root.mkdir()

    assert memory.main(["init", str(root)]) == 0

    isolated = isolated_home / ".agent-harness" / "memory" / "registry.json"
    assert memory_store.project_id_for(root) in json.loads(
        isolated.read_text(encoding="utf-8")
    )["projects"]

    after = (
        conftest.REAL_REGISTRY.read_bytes()
        if conftest.REAL_REGISTRY.is_file()
        else None
    )
    assert after == before, "the real machine registry was written by a test"


# --- the alarm ------------------------------------------------------------


def _write_registry(path: Path, ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "projects": {pid: {"path": "x"} for pid in ids}}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_registered_ids_reports_none_when_there_is_no_registry(
    tmp_path: Path,
) -> None:
    assert conftest._registered_ids(tmp_path / "absent.json") is None


def test_registered_ids_reads_the_project_ids(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    _write_registry(path, ["alpha-0000", "beta-1111"])
    assert conftest._registered_ids(path) == frozenset(
        {"alpha-0000", "beta-1111"}
    )


def test_registered_ids_treats_a_corrupt_registry_as_empty(tmp_path: Path) -> None:
    """A corrupt file must not crash the guard -- it is a canary, not a
    validator, and an unreadable registry is nothing the suite added to."""
    path = tmp_path / "registry.json"
    path.write_text("{ broken", encoding="utf-8")
    assert conftest._registered_ids(path) == frozenset()


def test_the_alarm_notices_a_newly_leaked_project(tmp_path: Path) -> None:
    """The comparison the session guard makes, exercised directly."""
    path = tmp_path / "registry.json"
    _write_registry(path, ["real-0000"])
    before = conftest._registered_ids(path)

    _write_registry(path, ["real-0000", "tmpproj-9999"])
    after = conftest._registered_ids(path)

    assert after - before == frozenset({"tmpproj-9999"})


def test_the_alarm_ignores_a_legitimate_last_seen_refresh(tmp_path: Path) -> None:
    """A harness command run in another window updates `last_seen` in the
    real registry. That is not a leak, and must not redden an unrelated run --
    which is why the guard compares ids rather than bytes."""
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"schema": 1, "projects": {"real-0000": {"last_seen": "a"}}}),
        encoding="utf-8",
    )
    before = conftest._registered_ids(path)

    path.write_text(
        json.dumps({"schema": 1, "projects": {"real-0000": {"last_seen": "b"}}}),
        encoding="utf-8",
    )
    after = conftest._registered_ids(path)

    assert not after - before
