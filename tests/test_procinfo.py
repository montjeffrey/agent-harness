"""Tests for `harness.procinfo`, the stdlib process and memory probes.

The gate leans on these probes to account for downstream servers and to prove
they were cleaned up, and on `terminate` to stop a survivor. So the tests use
real processes this file starts itself (by PID), and the one destructive call
is checked from both sides: it kills the exact process it was given, and it
refuses a process whose recorded identity no longer matches.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from harness import procinfo

SLEEPER = "import time; time.sleep(60)"
# The child starts its own sleeping child, reports that grandchild's PID on
# stdout, then sleeps itself, so the test sees a real three-level tree.
SPAWNER = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(60)\n"
)
# Far above Linux's pid_max and never handed out in practice on Windows.
MISSING_PID = 2**30 + 4
MB = 1024 * 1024


def _spawn(code: str, **kwargs: object) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code], **kwargs)  # type: ignore[call-overload]


def _wait_dead(identity: procinfo.ProcessIdentity, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not procinfo.is_same_process(identity):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen]:
    proc = _spawn(SLEEPER)
    try:
        yield proc
    finally:
        # Only this test's own child, through its own Popen handle.
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def spawner() -> Iterator[tuple[subprocess.Popen, int]]:
    proc = _spawn(SPAWNER, stdout=subprocess.PIPE, text=True)
    grandchild_identity = None
    try:
        assert proc.stdout is not None
        grandchild = int(proc.stdout.readline().strip())
        grandchild_identity = procinfo.process_identity(grandchild)
        yield proc, grandchild
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
        # The grandchild was started by our child; stop it only by the
        # identity captured right after it reported in, never by PID alone.
        if grandchild_identity is not None:
            procinfo.terminate(grandchild_identity)


# -- memory ----------------------------------------------------------------


def test_total_and_available_memory_are_plausible() -> None:
    total = procinfo.total_memory_bytes()
    available = procinfo.available_memory_bytes()
    assert total is not None and total > 512 * MB
    assert available is not None and 0 < available <= total


# -- identity --------------------------------------------------------------


def test_identity_of_current_process() -> None:
    identity = procinfo.process_identity(os.getpid())
    assert identity is not None
    assert identity.pid == os.getpid()
    assert identity.create_time > 0
    assert procinfo.is_same_process(identity)
    # Stable: a second read gives the identical value, so equality works.
    assert procinfo.process_identity(os.getpid()) == identity


def test_identity_reports_executable_on_windows() -> None:
    if sys.platform != "win32":
        pytest.skip("image path assertions are Windows-specific")
    identity = procinfo.process_identity(os.getpid())
    assert identity is not None and identity.exe is not None
    assert identity.exe.lower().endswith(".exe")
    assert os.path.isfile(identity.exe)


def test_child_identity_then_terminate(sleeper: subprocess.Popen) -> None:
    identity = procinfo.process_identity(sleeper.pid)
    assert identity is not None
    assert identity.pid == sleeper.pid
    assert procinfo.is_same_process(identity)

    assert procinfo.terminate(identity) is True
    assert _wait_dead(identity)
    assert procinfo.is_same_process(identity) is False
    sleeper.wait(timeout=10)
    assert procinfo.process_identity(sleeper.pid) is None
    # Already gone: nothing is killed now.
    assert procinfo.terminate(identity) is False


def test_forged_identity_is_not_terminated(sleeper: subprocess.Popen) -> None:
    identity = procinfo.process_identity(sleeper.pid)
    assert identity is not None
    forged = dataclasses.replace(identity, create_time=identity.create_time - 1.0)

    assert procinfo.is_same_process(forged) is False
    assert procinfo.terminate(forged) is False
    time.sleep(0.3)
    assert sleeper.poll() is None, "a process with a mismatched identity was killed"
    assert procinfo.is_same_process(identity)


# -- trees -----------------------------------------------------------------


def test_children_map_contains_child(sleeper: subprocess.Popen) -> None:
    mapping = procinfo.children_map()
    assert sleeper.pid in mapping.get(os.getpid(), [])


def test_descendants_finds_grandchild(spawner: tuple[subprocess.Popen, int]) -> None:
    child, grandchild = spawner
    found = procinfo.descendants(child.pid)
    assert grandchild in found
    assert child.pid not in found

    mine = procinfo.descendants(os.getpid())
    assert child.pid in mine and grandchild in mine

    tree = procinfo.process_tree(child.pid)
    assert tree[0].pid == child.pid
    assert grandchild in [identity.pid for identity in tree]


def test_tree_working_set_covers_root(spawner: tuple[subprocess.Popen, int]) -> None:
    child, _ = spawner
    root = procinfo.working_set_bytes(child.pid)
    total = procinfo.tree_working_set_bytes(child.pid)
    assert root is not None and total is not None
    assert total >= root


def _fake_probes(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[int, list[int]],
    created: dict[int, float],
) -> None:
    monkeypatch.setattr(procinfo, "children_map", lambda: mapping)
    monkeypatch.setattr(
        procinfo,
        "process_identity",
        lambda pid: (
            procinfo.ProcessIdentity(pid, created[pid], None)
            if pid in created
            else None
        ),
    )


def test_descendants_excludes_reused_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    # 30 names 20 as its parent, but 30 started before 20 existed: 30's real
    # parent died and PID 20 was reused by an unrelated, younger process.
    _fake_probes(
        monkeypatch,
        {10: [20], 20: [30, 40]},
        {10: 100.0, 20: 200.0, 30: 150.0, 40: 250.0},
    )
    assert sorted(procinfo.descendants(10)) == [20, 40]


def test_descendants_is_cycle_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_probes(
        monkeypatch,
        {1: [1, 2], 2: [3], 3: [1, 2]},
        {1: 10.0, 2: 10.0, 3: 10.0},
    )
    assert sorted(procinfo.descendants(1)) == [2, 3]


def test_unreadable_node_does_not_admit_stale_ppid_strangers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 500 was the root's short-lived child and exited before it could be read.
    # 7 (think explorer.exe) still names a long-dead parent that also had PID
    # 500, and it predates the root: it must never join the tree.
    _fake_probes(monkeypatch, {10: [500], 500: [7]}, {10: 500.0, 7: 100.0})
    assert procinfo.descendants(10) == []
    assert [i.pid for i in procinfo.process_tree(10)] == [10]


def test_root_exiting_mid_walk_admits_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    # The root is readable once, then gone: every process that names its PID
    # as parent must not be accepted unchecked.
    root = procinfo.ProcessIdentity(10, 500.0, None)
    probes = iter([root])
    monkeypatch.setattr(procinfo, "children_map", lambda: {10: [7]})
    monkeypatch.setattr(
        procinfo,
        "process_identity",
        lambda pid: next(probes, None) if pid == 10 else procinfo.ProcessIdentity(pid, 100.0, None),
    )
    assert [i.pid for i in procinfo.process_tree(10)] in ([], [10])


def test_identity_tree_of_a_recycled_root_admits_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    # The recorded root exited and PID 10 now belongs to a newer, unrelated
    # process whose child 7 is younger than the recorded root. Age alone would
    # admit 7; only re-checking the root's identity keeps a stranger's tree
    # out of anything the gate might kill.
    recorded_root = procinfo.ProcessIdentity(10, 500.0, None)
    _fake_probes(monkeypatch, {10: [7]}, {10: 900.0, 7: 950.0})
    assert procinfo.identity_tree(recorded_root) == []


def test_descendant_older_than_the_root_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_probes(monkeypatch, {10: [20], 20: [30]}, {10: 500.0, 20: 600.0, 30: 550.0})
    assert procinfo.descendants(10) == [20]


# -- process state (alive / gone / unknown) ---------------------------------


def _fake_platform_identity(monkeypatch: pytest.MonkeyPatch, impl) -> None:
    monkeypatch.setattr(procinfo, "_platform", lambda: "linux")
    monkeypatch.setattr(procinfo, "_linux_process_identity", impl)


def test_process_state_is_tri_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def impl(pid: int):
        if pid == 1:
            return procinfo.ProcessIdentity(1, 10.0, None)
        if pid == 2:
            return None
        raise PermissionError("probe refused")

    _fake_platform_identity(monkeypatch, impl)
    assert procinfo.process_state(procinfo.ProcessIdentity(1, 10.0, None)) == "alive"
    assert procinfo.process_state(procinfo.ProcessIdentity(1, 11.0, None)) == "gone"
    assert procinfo.process_state(procinfo.ProcessIdentity(2, 10.0, None)) == "gone"
    assert procinfo.process_state(procinfo.ProcessIdentity(3, 10.0, None)) == "unknown"


# -- Linux creation time ---------------------------------------------------


def test_linux_create_time_survives_a_wall_clock_step(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two gate processes may read btime from /proc/stat a second apart after a
    # clock step; the recorded identity must still match in the later one.
    fields = ["S", "1"] + ["0"] * 17 + ["123456"] + ["0"] * 10
    monkeypatch.setattr(procinfo, "_linux_stat_fields", lambda pid: fields)
    monkeypatch.setattr(os, "sysconf", lambda name: 100, raising=False)
    monkeypatch.setattr(procinfo, "_linux_boot_time", lambda: 1_700_000_000.0, raising=False)
    first = procinfo._linux_process_identity(4321)
    monkeypatch.setattr(procinfo, "_linux_boot_time", lambda: 1_700_000_001.0, raising=False)
    second = procinfo._linux_process_identity(4321)
    assert first is not None and first == second


# -- macOS children map ----------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout, self.stderr, self.returncode = stdout, "", 0


class _FakePs:
    """`ps -A` that, like BSD ps, lists its own row (pid 4242) under the caller."""

    pid = 4242
    returncode = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    @staticmethod
    def listing() -> str:
        return f"    1     0\n 4242 {os.getpid()}\n   77 {os.getpid()}\n"

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.listing(), ""


def test_mac_children_map_drops_its_own_ps_row(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = type(
        "FakeSubprocess",
        (),
        {
            "run": staticmethod(lambda *a, **k: _FakeCompleted(_FakePs.listing())),
            "Popen": _FakePs,
            "PIPE": subprocess.PIPE,
            "TimeoutExpired": subprocess.TimeoutExpired,
        },
    )
    monkeypatch.setattr(procinfo, "subprocess", fake)
    assert procinfo._mac_children_map()[os.getpid()] == [77]


# -- working set -----------------------------------------------------------


def test_working_set_of_current_process_is_plausible() -> None:
    size = procinfo.working_set_bytes(os.getpid())
    assert size is not None and size > 5 * MB
    # Not compared with `size`: the tree re-reads the root later, and a live,
    # allocating process may have released memory in between.
    tree = procinfo.tree_working_set_bytes(os.getpid())
    assert tree is not None and tree > 5 * MB


# -- missing processes -----------------------------------------------------


@pytest.mark.parametrize("pid", [MISSING_PID, 0, -1])
def test_missing_pid_returns_empty_results(pid: int) -> None:
    assert procinfo.process_identity(pid) is None
    assert procinfo.working_set_bytes(pid) is None
    assert procinfo.tree_working_set_bytes(pid) is None
    assert procinfo.process_tree(pid) == []
    assert procinfo.descendants(pid) == []
    forged = procinfo.ProcessIdentity(pid=pid, create_time=1.0, exe=None)
    assert procinfo.is_same_process(forged) is False
    assert procinfo.terminate(forged) is False


def test_exited_child_has_no_identity() -> None:
    proc = _spawn("pass")
    proc.wait(timeout=30)
    # On Windows the Popen handle is still open, so the process object exists
    # but has exited; it must not be reported as a live identity.
    assert procinfo.process_identity(proc.pid) is None
    assert procinfo.working_set_bytes(proc.pid) is None
