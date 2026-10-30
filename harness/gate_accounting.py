"""Accounting, memory-pressure tiers, and crash cleanup for the MCP gate.

Phase 1 of the usage-aware resource gate (see
`docs/superpowers/specs/2026-10-30-usage-aware-resource-gate-design.md`):

* **Tiers and pressure.** A machine's total RAM picks a `MemoryTier`; free
  RAM against that tier's watermarks gives a pressure level, and the level
  shortens how long an idle server is kept warm. A 16 GB box therefore
  degrades by restarting tools more often, not by paging.
* **Ledger.** Every wake, call, and stop is appended as one JSON line to
  `gate-YYYYMM.jsonl` next to the agent telemetry shards. The gate records
  carry their own schema 1 and their own file prefix, so
  `telemetry_store`'s agent/event contract (and its readers) are unchanged.
* **Crash safety net.** `PidRegistry` keeps, per running gate, the exact
  identities of the downstream processes it has running. A gate that starts
  after another one crashed calls `sweep_orphans`, which terminates only
  processes whose pid *and* creation time still match the record, so a
  recycled PID is never killed.

Everything here is stdlib only and never raises into the gate: a failed
write or an unreadable file costs one line on stderr, never a tool call.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import procinfo, telemetry_store
from harness.atomicwrite import atomic_sibling

SCHEMA_VERSION = 1
GIB = 1024**3
PRESSURE_LEVELS = ("normal", "low", "critical")
_REGISTRY_GLOB = "owner-*.json"


@dataclass(frozen=True)
class MemoryTier:
    """Watermarks and keep-warm default for one class of machine."""

    name: str
    low_watermark_bytes: int
    critical_watermark_bytes: int
    default_idle_seconds: float


_TIER_16GB = MemoryTier("16GB", 3 * GIB, int(1.5 * GIB), 300.0)
_TIER_32GB = MemoryTier("32GB", 5 * GIB, int(2.5 * GIB), 600.0)
_TIER_LARGE = MemoryTier("large", 8 * GIB, 4 * GIB, 900.0)

# (max_total_bytes, tier), checked in order. The bounds carry 1 GiB of slack
# because a "16 GB" machine reports less than 16 GiB of usable physical memory
# (firmware and integrated graphics reserve some; ~15.7 GiB is typical).
TIERS: tuple[tuple[int, MemoryTier], ...] = (
    (17 * GIB, _TIER_16GB),
    (33 * GIB, _TIER_32GB),
    (sys.maxsize, _TIER_LARGE),
)


def _warn(message: str) -> None:
    print(f"[mcp-gate] {message}", file=sys.stderr)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def tier_for(total_bytes: int | None) -> MemoryTier:
    """The tier for a machine with `total_bytes` of RAM; unknown -> the 32 GB tier."""
    if not _is_number(total_bytes) or total_bytes <= 0:
        return _TIER_32GB
    for bound, tier in TIERS:
        if total_bytes <= bound:
            return tier
    return TIERS[-1][1]


def pressure_level(
    available_bytes: int | None,
    tier: MemoryTier,
    *,
    low_override_bytes: int | None = None,
    critical_override_bytes: int | None = None,
) -> str:
    """Pressure for `available_bytes` of free RAM under `tier`'s watermarks.

    "normal" at or above the low watermark, "critical" below the critical
    one, "low" in between. An unknown reading is "normal": a failed probe must
    never make the gate stop servers.
    """
    if not _is_number(available_bytes):
        return "normal"
    low = low_override_bytes if low_override_bytes is not None else tier.low_watermark_bytes
    critical = (
        critical_override_bytes
        if critical_override_bytes is not None
        else tier.critical_watermark_bytes
    )
    if critical > low:
        # One override crossed the tier's other watermark (the config only
        # checks the pair when both are set). The operator's explicit value
        # wins and the tier default yields to it, so "critical" never fires
        # above a low watermark they chose.
        if low_override_bytes is None:
            low = critical
        else:
            critical = low
    if available_bytes < critical:
        return "critical"
    if available_bytes < low:
        return "low"
    return "normal"


def effective_idle_seconds(configured: float, min_idle: float, level: str) -> float:
    """Keep-warm time under `level`: as configured, capped at `min_idle`, or none."""
    if level == "critical":
        return 0.0
    if level == "low":
        return min(configured, min_idle)
    return configured


# ---------------------------------------------------------------------- ledger


def _now() -> datetime:
    return datetime.now(UTC)


class GateLedger:
    """Appends the gate's accounting records to `<directory>/gate-YYYYMM.jsonl`."""

    def __init__(
        self, *, session: str, directory: Path | None = None, enabled: bool = True
    ) -> None:
        self.session = session
        self.directory = directory if directory is not None else telemetry_store.telemetry_dir()
        self.enabled = enabled
        self.client: str | None = None

    def record(self, event: str, server: str, **fields: Any) -> None:
        """Append one record. The common fields win over same-named `fields`."""
        if not self.enabled:
            return
        now = _now()
        common = {
            "schema": SCHEMA_VERSION,
            "ts": telemetry_store.format_ts(now),
            "event": event,
            "server": server,
            "session": self.session,
            "client": self.client,
        }
        record = {**common, **{k: v for k, v in fields.items() if k not in common}}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            self.directory.mkdir(parents=True, exist_ok=True)
            # One write per line: several gates append to the same shard.
            with (self.directory / f"gate-{now:%Y%m}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            _warn(f"accounting record {event} for '{server}' not written: {exc}")


def _parsed_after(record: dict, since: datetime | None) -> bool:
    if since is None:
        return True
    ts = record.get("ts")
    if not isinstance(ts, str):
        return False
    try:
        return telemetry_store.parse_ts(ts) >= since
    except ValueError:
        return False


def _gate_record(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("schema") != SCHEMA_VERSION:
        return None
    if not isinstance(record.get("event"), str):
        return None
    return record


def iter_gate_events(
    since: datetime | None = None, directory: Path | None = None
) -> Iterator[dict]:
    """Yield schema-1 gate records, oldest shard first; bad lines are skipped."""
    directory = directory if directory is not None else telemetry_store.telemetry_dir()
    if not directory.is_dir():
        return
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    for path in sorted(directory.glob("gate-*.jsonl")):
        try:
            # errors="replace": a torn or binary line fails JSON parsing below
            # instead of aborting the whole shard with UnicodeDecodeError.
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            _warn(f"gate ledger {path.name} unreadable: {exc}")
            continue
        for line in lines:
            record = _gate_record(line)
            if record is not None and _parsed_after(record, since):
                yield record


# ----------------------------------------------------------- crash safety net


def _identity_to_json(identity: procinfo.ProcessIdentity) -> dict:
    # create_time stays a raw float: JSON round-trips it exactly, and the
    # sweep compares it for equality.
    return {"pid": identity.pid, "create_time": identity.create_time, "exe": identity.exe}


def _identity_from_json(data: Any) -> procinfo.ProcessIdentity:
    """Rebuild a recorded identity; TypeError/ValueError on anything not exactly valid."""
    if not isinstance(data, dict):
        raise TypeError("identity is not an object")
    pid, create_time, exe = data.get("pid"), data.get("create_time"), data.get("exe")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError(f"bad pid {pid!r}")
    if not _is_number(create_time):
        raise ValueError(f"bad create_time {create_time!r}")
    if exe is not None and not isinstance(exe, str):
        raise ValueError(f"bad exe {exe!r}")
    return procinfo.ProcessIdentity(pid=pid, create_time=float(create_time), exe=exe)


def _registry_name(owner: procinfo.ProcessIdentity) -> str:
    # The creation time is part of the name, so a new gate that inherits a
    # crashed gate's recycled PID cannot overwrite the orphan record.
    stamp = hashlib.sha256(repr(owner.create_time).encode("ascii")).hexdigest()[:16]
    return f"owner-{owner.pid}-{stamp}.json"


class PidRegistry:
    """What this gate has running, on disk, so a later gate can clean up after a crash."""

    def __init__(self, directory: Path, owner: procinfo.ProcessIdentity) -> None:
        self.directory = Path(directory)
        self.owner = owner
        self.path = self.directory / _registry_name(owner)
        self._servers: dict[str, tuple[procinfo.ProcessIdentity, ...]] = {}
        self._lock = threading.Lock()
        self._closed = False

    def track(self, server: str, identities: list[procinfo.ProcessIdentity]) -> None:
        """Replace `server`'s recorded process identities."""
        with self._lock:
            if self._closed:
                return
            try:
                recorded = tuple(identities)
            except TypeError as exc:
                _warn(f"pid registry: identities for '{server}' not recorded: {exc}")
                return
            self._servers = {**self._servers, server: recorded}
            self._write()

    def untrack(self, server: str) -> None:
        with self._lock:
            if self._closed or server not in self._servers:
                return
            self._servers = {k: v for k, v in self._servers.items() if k != server}
            self._write()

    def close(self) -> None:
        """Remove this owner's file; later `track`/`untrack` calls do nothing."""
        with self._lock:
            self._closed = True
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                _warn(f"pid registry {self.path.name} not removed: {exc}")

    def _write(self) -> None:
        try:
            payload = {
                "schema": SCHEMA_VERSION,
                "boot": procinfo.boot_id(),
                "owner": _identity_to_json(self.owner),
                "servers": {
                    name: [_identity_to_json(i) for i in identities]
                    for name, identities in self._servers.items()
                },
            }
            text = json.dumps(payload)
            self.directory.mkdir(parents=True, exist_ok=True)
            with atomic_sibling(self.path) as tmp:
                tmp.write_text(text, encoding="utf-8")
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            _warn(f"pid registry {self.path.name} not written: {exc}")


def _load_registry(
    path: Path,
) -> tuple[str | None, procinfo.ProcessIdentity, list[procinfo.ProcessIdentity]]:
    """(boot id, owner, recorded identities); OSError/TypeError/ValueError when not valid."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        raise ValueError("not a schema-1 registry")
    boot = data.get("boot")
    if boot is not None and not isinstance(boot, str):
        raise ValueError(f"bad boot {boot!r}")
    servers = data.get("servers")
    if not isinstance(servers, dict) or not all(isinstance(v, list) for v in servers.values()):
        raise ValueError("servers is not a mapping of lists")
    owner = _identity_from_json(data.get("owner"))
    recorded = [_identity_from_json(item) for items in servers.values() for item in items]
    return boot, owner, recorded


def _from_another_boot(recorded_boot: str | None) -> bool:
    current = procinfo.boot_id()
    return recorded_boot is not None and current is not None and recorded_boot != current


def _terminate_recorded(
    recorded: list[procinfo.ProcessIdentity],
) -> tuple[list[procinfo.ProcessIdentity], list[procinfo.ProcessIdentity]]:
    """Kill each recorded identity that is still exactly that process: (killed, survivors)."""
    killed: list[procinfo.ProcessIdentity] = []
    survivors: list[procinfo.ProcessIdentity] = []
    for identity in recorded:
        # Checked here as well as inside `terminate`: an identity that no
        # longer matches (exited, or its PID recycled) is never handed to it.
        if not procinfo.is_same_process(identity):
            continue
        if procinfo.terminate(identity):
            killed.append(identity)
        elif procinfo.is_same_process(identity):
            survivors.append(identity)
    return killed, survivors


def _sweep_file(path: Path) -> list[procinfo.ProcessIdentity]:
    try:
        boot, owner, recorded = _load_registry(path)
    except (OSError, TypeError, ValueError) as exc:
        # Never act on a record that could not be validated; leave it for a person.
        _warn(f"pid registry {path.name} skipped: {exc}")
        return []
    if _from_another_boot(boot):
        # Nothing it names can still run, and a new process could match a
        # boot-relative creation time by chance: drop it without a kill.
        _remove(path)
        return []
    state = procinfo.process_state(owner)
    if state != "gone":
        # "unknown" (a failed probe) is not a crash: a live gate's servers
        # are in use. The file is left for a later sweep to decide.
        if state == "unknown":
            _warn(f"pid registry {path.name} skipped: owner {owner.pid} state unknown")
        return []
    killed, survivors = _terminate_recorded(recorded)
    if survivors:
        pids = ", ".join(str(i.pid) for i in survivors)
        _warn(f"orphaned processes still running after sweep ({pids}); {path.name} kept")
        return killed
    _remove(path)
    return killed


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _warn(f"pid registry {path.name} not removed: {exc}")


def sweep_orphans(directory: Path) -> list[procinfo.ProcessIdentity]:
    """Terminate what crashed gates left running; return the identities killed now.

    Files whose owner gate is still the same live process, or whose owner
    cannot be probed right now, are left alone.
    """
    directory = Path(directory)
    try:
        if not directory.is_dir():
            return []
        paths = sorted(directory.glob(_REGISTRY_GLOB))
    except OSError as exc:
        _warn(f"pid registry directory {directory} unreadable: {exc}")
        return []
    terminated: list[procinfo.ProcessIdentity] = []
    for path in paths:
        try:
            terminated.extend(_sweep_file(path))
        except Exception as exc:  # noqa: BLE001 -- a probe bug must not abort gate startup
            _warn(f"pid registry {path.name} sweep failed: {exc!r}")
    return terminated
