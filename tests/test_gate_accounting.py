"""Tests for `harness.gate_accounting`: memory tiers, the gate ledger, and the
crash safety net (`PidRegistry` + `sweep_orphans`).

Nothing here starts or kills a real process. Every `harness.procinfo` probe
the code under test reaches is monkeypatched, so these tests pin the
accounting logic -- in particular that the sweep only ever asks to terminate
an identity it has just confirmed -- independently of the platform probes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness import gate_accounting, procinfo, telemetry_store
from harness.gate_accounting import (
    GateLedger,
    MemoryTier,
    PidRegistry,
    effective_idle_seconds,
    iter_gate_events,
    pressure_level,
    sweep_orphans,
    tier_for,
)

GIB = 1024**3


def _identity(pid: int, create_time: float = 1000.5, exe: str | None = "C:/x/node.exe"):
    return procinfo.ProcessIdentity(pid=pid, create_time=create_time, exe=exe)


# --------------------------------------------------------------------- tiers


def test_tier_table_matches_the_design_defaults() -> None:
    sixteen = tier_for(16 * GIB)
    thirty_two = tier_for(32 * GIB)
    large = tier_for(64 * GIB)

    assert (sixteen.low_watermark_bytes, sixteen.critical_watermark_bytes) == (
        3 * GIB,
        int(1.5 * GIB),
    )
    assert sixteen.default_idle_seconds == 300.0
    assert (thirty_two.low_watermark_bytes, thirty_two.critical_watermark_bytes) == (
        5 * GIB,
        int(2.5 * GIB),
    )
    assert thirty_two.default_idle_seconds == 600.0
    assert (large.low_watermark_bytes, large.critical_watermark_bytes) == (8 * GIB, 4 * GIB)
    assert large.default_idle_seconds == 900.0
    assert all(isinstance(bound, int) for bound, _ in gate_accounting.TIERS)


def test_a_16gb_box_reporting_less_than_16_gib_lands_in_the_16gb_tier() -> None:
    assert tier_for(int(15.7 * GIB)) == tier_for(16 * GIB)
    assert tier_for(int(15.7 * GIB)).default_idle_seconds == 300.0


def test_tier_bounds_include_slack_and_are_inclusive() -> None:
    assert tier_for(17 * GIB).default_idle_seconds == 300.0
    assert tier_for(17 * GIB + 1).default_idle_seconds == 600.0
    assert tier_for(int(31.8 * GIB)).default_idle_seconds == 600.0
    assert tier_for(33 * GIB).default_idle_seconds == 600.0
    assert tier_for(33 * GIB + 1).default_idle_seconds == 900.0
    assert tier_for(512 * GIB).default_idle_seconds == 900.0


@pytest.mark.parametrize("total", [None, 0, -5, True])
def test_unknown_total_memory_falls_back_to_the_32gb_tier(total) -> None:
    assert tier_for(total) == tier_for(32 * GIB)


# ------------------------------------------------------------------ pressure


@pytest.fixture
def tier() -> MemoryTier:
    return MemoryTier(
        name="test",
        low_watermark_bytes=3000,
        critical_watermark_bytes=1500,
        default_idle_seconds=300.0,
    )


def test_pressure_level_boundaries(tier: MemoryTier) -> None:
    assert pressure_level(10_000, tier) == "normal"
    assert pressure_level(3000, tier) == "normal"
    assert pressure_level(2999, tier) == "low"
    assert pressure_level(1500, tier) == "low"
    assert pressure_level(1499, tier) == "critical"
    assert pressure_level(0, tier) == "critical"


def test_unknown_available_memory_is_normal_pressure(tier: MemoryTier) -> None:
    assert pressure_level(None, tier) == "normal"


def test_overrides_replace_each_watermark(tier: MemoryTier) -> None:
    assert pressure_level(4000, tier, low_override_bytes=5000) == "low"
    assert pressure_level(2000, tier, low_override_bytes=1800) == "normal"
    assert pressure_level(2000, tier, critical_override_bytes=2500) == "critical"
    assert pressure_level(1400, tier, critical_override_bytes=1000) == "low"
    assert (
        pressure_level(900, tier, low_override_bytes=1000, critical_override_bytes=500) == "low"
    )


def test_a_single_override_never_inverts_the_watermarks() -> None:
    sixteen = tier_for(16 * GIB)  # low 3 GiB, critical 1.5 GiB
    # Only low lowered to 1 GiB: 1.2 GiB free is above the operator's low mark.
    assert pressure_level(int(1.2 * GIB), sixteen, low_override_bytes=GIB) == "normal"
    assert pressure_level(int(0.9 * GIB), sixteen, low_override_bytes=GIB) == "critical"
    # Only critical raised to 4 GiB: the tier's 3 GiB low mark rises to meet it.
    assert pressure_level(int(4.5 * GIB), sixteen, critical_override_bytes=4 * GIB) == "normal"
    assert pressure_level(int(3.5 * GIB), sixteen, critical_override_bytes=4 * GIB) == "critical"


def test_effective_idle_per_level() -> None:
    assert effective_idle_seconds(600.0, 60.0, "normal") == 600.0
    assert effective_idle_seconds(600.0, 60.0, "low") == 60.0
    # A server already configured shorter than min_idle keeps its own value.
    assert effective_idle_seconds(30.0, 60.0, "low") == 30.0
    assert effective_idle_seconds(600.0, 60.0, "critical") == 0.0


# -------------------------------------------------------------------- ledger


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_ledger_writes_parseable_lines_with_common_fields_to_a_month_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate_accounting, "_now", lambda: datetime(2026, 10, 30, 12, 0, 5, tzinfo=UTC))
    directory = tmp_path / "telemetry"
    ledger = GateLedger(session="sess-1", directory=directory)
    ledger.client = "claude-code"

    ledger.record("gate_wake", "playwright", reason="call", startup_ms=812, root_pid=42, tree_mb=96.5)
    ledger.record("gate_call", "playwright", tool="navigate", duration_ms=40, ok=True, error_kind=None)

    shard = directory / "gate-202610.jsonl"
    assert [p.name for p in directory.iterdir()] == ["gate-202610.jsonl"]
    wake, call = _read_lines(shard)
    assert wake == {
        "schema": 1,
        "ts": "2026-10-30T12:00:05Z",
        "event": "gate_wake",
        "server": "playwright",
        "session": "sess-1",
        "client": "claude-code",
        "reason": "call",
        "startup_ms": 812,
        "root_pid": 42,
        "tree_mb": 96.5,
    }
    assert call["event"] == "gate_call"
    assert call["ok"] is True
    assert call["error_kind"] is None


def test_ledger_client_defaults_to_none(tmp_path: Path) -> None:
    ledger = GateLedger(session="s", directory=tmp_path)
    ledger.record("gate_sweep", "*", pids=[])
    (record,) = list(iter_gate_events(directory=tmp_path))
    assert record["client"] is None
    assert record["pids"] == []


def test_ledger_common_fields_cannot_be_overridden(tmp_path: Path) -> None:
    ledger = GateLedger(session="real", directory=tmp_path)
    ledger.record("gate_sleep", "fs", schema=99, session="fake", ts="nope", client="x")
    (record,) = list(iter_gate_events(directory=tmp_path))
    assert record["schema"] == 1
    assert record["session"] == "real"
    assert record["client"] is None
    assert record["ts"].endswith("Z")


def test_ledger_defaults_to_the_telemetry_directory() -> None:
    assert GateLedger(session="s").directory == telemetry_store.telemetry_dir()


def test_ledger_never_raises_when_the_directory_is_unwritable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_dir = tmp_path / "plain-file"
    not_a_dir.write_text("x", encoding="utf-8")
    ledger = GateLedger(session="s", directory=not_a_dir)

    ledger.record("gate_wake", "fs", reason="call", startup_ms=1)

    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "gate_wake" in err


def test_ledger_never_raises_on_an_unserialisable_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    loop: list = []
    loop.append(loop)
    GateLedger(session="s", directory=tmp_path).record("gate_call", "fs", tool=loop)
    assert capsys.readouterr().err.strip()


def test_disabled_ledger_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "telemetry"
    GateLedger(session="s", directory=target, enabled=False).record("gate_wake", "fs")
    assert not target.exists()


def test_gate_records_stay_out_of_the_agent_event_store(tmp_path: Path) -> None:
    GateLedger(session="s", directory=tmp_path).record("gate_wake", "fs")
    assert list(telemetry_store.iter_events(directory=tmp_path)) == []
    assert not list(tmp_path.glob("events-*.jsonl"))


# ------------------------------------------------------------ event reading


def test_iter_gate_events_skips_garbage_and_other_schemas(tmp_path: Path) -> None:
    good = {"schema": 1, "ts": "2026-10-01T00:00:00Z", "event": "gate_wake", "server": "fs"}
    lines = [
        json.dumps(good),
        "{not json",
        "",
        json.dumps(["a", "list"]),
        json.dumps({**good, "schema": 2}),
        json.dumps({**good, "event": 7}),
        json.dumps({**good, "server": "second"}),
    ]
    (tmp_path / "gate-202610.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "gate-202609.jsonl").write_bytes(b"\xff\xfe garbage \x00\n")
    (tmp_path / "events-202610.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")

    servers = [record["server"] for record in iter_gate_events(directory=tmp_path)]

    assert servers == ["fs", "second"]


def test_iter_gate_events_filters_by_since(tmp_path: Path) -> None:
    old = {"schema": 1, "ts": "2026-09-01T00:00:00Z", "event": "gate_wake", "server": "old"}
    new = {"schema": 1, "ts": "2026-10-20T00:00:00Z", "event": "gate_wake", "server": "new"}
    bad_ts = {"schema": 1, "ts": "yesterday", "event": "gate_wake", "server": "bad"}
    (tmp_path / "gate-202610.jsonl").write_text(
        "\n".join(json.dumps(r) for r in (old, new, bad_ts)), encoding="utf-8"
    )

    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert [r["server"] for r in iter_gate_events(since=since, directory=tmp_path)] == ["new"]


def test_iter_gate_events_on_a_missing_directory_yields_nothing(tmp_path: Path) -> None:
    assert list(iter_gate_events(directory=tmp_path / "absent")) == []


# --------------------------------------------------------------- pid registry


def _registry_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("owner-*.json"))


def test_registry_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[Path] = []
    real_sibling = gate_accounting.atomic_sibling

    def recording_sibling(dst: Path):
        writes.append(Path(dst))
        return real_sibling(dst)

    monkeypatch.setattr(gate_accounting, "atomic_sibling", recording_sibling)
    owner = _identity(100, create_time=1730000000.123456, exe=None)
    registry = PidRegistry(tmp_path, owner)

    registry.track("fs", [_identity(201), _identity(202, exe=None)])
    registry.track("browser", [_identity(301)])
    registry.track("fs", [_identity(203)])

    (path,) = _registry_files(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["owner"] == {"pid": 100, "create_time": 1730000000.123456, "exe": None}
    assert data["servers"] == {
        "fs": [{"pid": 203, "create_time": 1000.5, "exe": "C:/x/node.exe"}],
        "browser": [{"pid": 301, "create_time": 1000.5, "exe": "C:/x/node.exe"}],
    }

    registry.untrack("browser")
    assert json.loads(path.read_text(encoding="utf-8"))["servers"] == {
        "fs": [{"pid": 203, "create_time": 1000.5, "exe": "C:/x/node.exe"}]
    }

    assert writes and all(w == path for w in writes)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    registry.close()
    assert _registry_files(tmp_path) == []
    registry.track("fs", [_identity(204)])
    assert _registry_files(tmp_path) == []


def test_registry_file_name_is_unique_per_owner_identity(tmp_path: Path) -> None:
    PidRegistry(tmp_path, _identity(100, create_time=1.0)).track("fs", [_identity(201)])
    PidRegistry(tmp_path, _identity(100, create_time=2.0)).track("fs", [_identity(202)])
    assert len(_registry_files(tmp_path)) == 2


def test_registry_never_raises_when_the_directory_is_unwritable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_dir = tmp_path / "plain-file"
    not_a_dir.write_text("x", encoding="utf-8")
    registry = PidRegistry(not_a_dir, _identity(100))

    registry.track("fs", [_identity(201)])
    registry.untrack("fs")
    registry.close()

    assert capsys.readouterr().err.strip()


# ------------------------------------------------------------- orphan sweep


class _FakeProcs:
    """Stand-in for the procinfo probes: a set of live identities and a kill log."""

    def __init__(self, alive: set[tuple[int, float]], *, kill_works: bool = True) -> None:
        self.alive = set(alive)
        self.kill_works = kill_works
        self.terminated: list = []

    def is_same_process(self, identity) -> bool:
        return (identity.pid, identity.create_time) in self.alive

    def process_state(self, identity) -> str:
        return "alive" if self.is_same_process(identity) else "gone"

    def terminate(self, identity) -> bool:
        self.terminated.append(identity)
        if not self.kill_works:
            return False
        if (identity.pid, identity.create_time) not in self.alive:
            return False
        self.alive.discard((identity.pid, identity.create_time))
        return True


@pytest.fixture
def fake_procs(monkeypatch: pytest.MonkeyPatch):
    def install(alive: set[tuple[int, float]], **kwargs) -> _FakeProcs:
        procs = _FakeProcs(alive, **kwargs)
        monkeypatch.setattr(procinfo, "is_same_process", procs.is_same_process)
        monkeypatch.setattr(procinfo, "terminate", procs.terminate)
        monkeypatch.setattr(procinfo, "process_state", procs.process_state, raising=False)
        return procs

    return install


def test_sweep_terminates_only_orphans_of_dead_owners(tmp_path: Path, fake_procs) -> None:
    dead_owner, live_owner = _identity(10, 1.0), _identity(20, 2.0)
    orphan, already_gone = _identity(11, 3.0), _identity(12, 4.0)
    live_child = _identity(21, 5.0)
    PidRegistry(tmp_path, dead_owner).track("fs", [orphan, already_gone])
    PidRegistry(tmp_path, live_owner).track("browser", [live_child])
    live_file = [p for p in _registry_files(tmp_path) if "-20-" in p.name]
    live_bytes = live_file[0].read_bytes()
    procs = fake_procs({(20, 2.0), (11, 3.0), (21, 5.0)})

    swept = sweep_orphans(tmp_path)

    assert swept == [orphan]
    assert procs.terminated == [orphan]
    assert (21, 5.0) in procs.alive
    assert _registry_files(tmp_path) == live_file
    assert live_file[0].read_bytes() == live_bytes


def test_sweep_never_terminates_when_identity_does_not_match(tmp_path: Path, fake_procs) -> None:
    # The recorded PIDs are "running", but with a different creation time: a
    # recycled PID that now belongs to some unrelated process.
    PidRegistry(tmp_path, _identity(10, 1.0)).track("fs", [_identity(11, 3.0), _identity(12, 4.0)])
    procs = fake_procs({(11, 99.0), (12, 99.0)})

    assert sweep_orphans(tmp_path) == []
    assert procs.terminated == []
    assert _registry_files(tmp_path) == []


def test_sweep_keeps_the_file_when_a_kill_fails(
    tmp_path: Path, fake_procs, capsys: pytest.CaptureFixture[str]
) -> None:
    PidRegistry(tmp_path, _identity(10, 1.0)).track("fs", [_identity(11, 3.0)])
    fake_procs({(11, 3.0)}, kill_works=False)

    assert sweep_orphans(tmp_path) == []
    assert len(_registry_files(tmp_path)) == 1
    assert capsys.readouterr().err.strip()


def test_sweep_leaves_unreadable_files_alone_and_never_raises(
    tmp_path: Path, fake_procs, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "owner-1-abc.json"
    corrupt.write_text("{truncated", encoding="utf-8")
    bad_entry = tmp_path / "owner-2-def.json"
    bad_entry.write_text(
        json.dumps(
            {
                "schema": 1,
                "owner": {"pid": 2, "create_time": 1.0, "exe": None},
                "servers": {"fs": [{"pid": "eleven", "create_time": 3.0, "exe": None}]},
            }
        ),
        encoding="utf-8",
    )
    procs = fake_procs({(11, 3.0)})

    assert sweep_orphans(tmp_path) == []
    assert procs.terminated == []
    assert corrupt.exists()
    assert capsys.readouterr().err.strip()


def test_sweep_skips_a_live_owner_whose_identity_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Gate 10 is alive, but reading it fails (a `ps` timeout, an access error):
    # that is "unknown", not "crashed", so its running server is left alone.
    owner, server = _identity(10, 1.0), _identity(11, 3.0)
    PidRegistry(tmp_path, owner).track("fs", [server])

    def identity_probe(pid: int):
        if pid == owner.pid:
            raise TimeoutError("ps timed out")
        return server if pid == server.pid else None

    monkeypatch.setattr(procinfo, "_platform", lambda: "linux")
    monkeypatch.setattr(procinfo, "_linux_process_identity", identity_probe)
    terminated: list = []
    monkeypatch.setattr(procinfo, "terminate", lambda i: terminated.append(i) or True)

    assert sweep_orphans(tmp_path) == []
    assert terminated == []
    assert len(_registry_files(tmp_path)) == 1
    assert capsys.readouterr().err.strip()


def test_sweep_never_terminates_identities_from_a_previous_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_procs
) -> None:
    # Linux creation times count from boot, so after a reboot a new process can
    # carry a recorded (pid, create_time) by chance. Nothing from the previous
    # boot can be alive: the record is dropped, nothing is killed.
    monkeypatch.setattr(procinfo, "boot_id", lambda: "boot-a", raising=False)
    PidRegistry(tmp_path, _identity(10, 1.0)).track("fs", [_identity(11, 3.0)])
    procs = fake_procs({(11, 3.0)})
    monkeypatch.setattr(procinfo, "boot_id", lambda: "boot-b", raising=False)

    assert sweep_orphans(tmp_path) == []
    assert procs.terminated == []
    assert _registry_files(tmp_path) == []


def test_sweep_of_a_missing_directory_is_empty(tmp_path: Path, fake_procs) -> None:
    fake_procs(set())
    assert sweep_orphans(tmp_path / "absent") == []
