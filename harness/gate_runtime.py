"""Process accounting and verified cleanup for the MCP gate's downstream servers.

`harness.mcp_gate` speaks MCP; this module watches the processes behind it:

* **Attribution.** The SDK's `stdio_client` does not expose the PID it
  launched, so the gate snapshots its own direct children just before and
  just after the spawn (spawns are serialized by `GateRuntime.spawn_lock`).
  Exactly one new child is the downstream's root; anything else leaves the
  root unknown, which degrades accounting but never behaviour.
* **Trees.** A downstream's tree is sampled from its root only while the root
  is still the process the gate launched (same PID *and* creation time), plus
  every identity seen earlier that is still that same process. A recycled
  root PID therefore never pulls a stranger's tree in.
* **Verified cleanup.** After a stop, every identity of the tree must be gone
  within `STOP_VERIFY_SECONDS`; survivors are terminated through
  `procinfo.terminate`, which re-checks the identity, and anything still
  running is reported as leaked.
* **Pressure.** `GateRuntime` samples free RAM, holds the memory tier, the
  gate's ledger, and its PID registry.

Every probe here runs in a worker thread and never raises into the gate: a
failure costs one stderr line and degrades to "unknown".
"""

from __future__ import annotations

import functools
import os
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from harness import gate_accounting, procinfo
from harness.gate_config import GateConfig

# How long a stopped tree may take to disappear before survivors are killed.
# The SDK itself escalates to terminating the tree within ~2 s.
STOP_VERIFY_SECONDS = 3.0
_VERIFY_POLL_SECONDS = 0.1
MIB = 1024 * 1024
SWEEP_SERVER = "*"
# `GateRuntime.start(total_memory=...)` default: use what the config saw.
UNSET: Any = object()

Identity = procinfo.ProcessIdentity


def warn(message: str) -> None:
    print(f"[mcp-gate] {message}", file=sys.stderr)


def default_registry_dir() -> Path:
    """Where running gates record their downstream PIDs (resolved at call time)."""
    return Path.home() / ".agent-harness" / "gate-state"


def new_session_id() -> str:
    return f"{os.getpid()}-{int(time.time())}"


def safely(what: str, default: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """`fn(*args, **kwargs)`, or `default` plus one stderr line if it raises."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- accounting must never break a call
        warn(f"{what} failed: {type(exc).__name__}: {exc}")
        return default


async def in_thread(what: str, default: Any, fn: Callable[..., Any], *args: Any) -> Any:
    """`safely`, in a worker thread: probes can block (Toolhelp, `ps`)."""
    import anyio

    return await anyio.to_thread.run_sync(functools.partial(safely, what, default, fn, *args))


def to_mb(size: int | None) -> float | None:
    return None if size is None else round(size / MIB, 1)


def _key(identity: Identity) -> tuple[int, float]:
    return identity.pid, identity.create_time


# ------------------------------------------------------------- process probes


def child_pids() -> frozenset[int]:
    """This gate's direct children right now."""
    return frozenset(procinfo.children_map().get(os.getpid(), []))


def new_child(before: frozenset[int], exclude: Iterable[int]) -> Identity | None:
    """The one child that appeared since `before`, or None when that is ambiguous.

    `exclude` holds other downstreams' roots, so a failed `before` snapshot
    can never make another live server look like the new one.
    """
    added = child_pids() - before - set(exclude)
    if len(added) != 1:
        return None
    return procinfo.process_identity(next(iter(added)))


def live_tree(root: Identity | None, known: Iterable[Identity]) -> tuple[Identity, ...]:
    """The downstream's tree now: from the root while it is still ours, plus known survivors."""
    found: dict[tuple[int, float], Identity] = {}
    if root is not None:
        # Walked from the recorded identity itself, never a fresh probe of its
        # PID: [] once the root is gone, and nothing older than it is admitted.
        found.update((_key(i), i) for i in procinfo.identity_tree(root))
    for identity in known:
        if _key(identity) not in found and procinfo.is_same_process(identity):
            found[_key(identity)] = identity
    return tuple(found.values())


def tree_bytes(tree: Iterable[Identity]) -> int | None:
    sizes = [size for size in (procinfo.working_set_bytes(i.pid) for i in tree) if size is not None]
    return sum(sizes) if sizes else None


def _still_running(identities: list[Identity]) -> list[Identity]:
    return [i for i in identities if procinfo.is_same_process(i)]


def _terminate_survivors(survivors: list[Identity]) -> tuple[list[int], list[int]]:
    reaped: list[int] = []
    leaked: list[int] = []
    for identity in survivors:
        if procinfo.terminate(identity):  # identity-checked: never a recycled PID
            reaped.append(identity.pid)
        elif procinfo.is_same_process(identity):
            leaked.append(identity.pid)
    return reaped, leaked


async def verify_gone(
    identities: Iterable[Identity], wait: float = STOP_VERIFY_SECONDS
) -> tuple[list[int], list[int]]:
    """Wait for a stopped tree to exit; kill exact survivors. Returns (reaped, leaked) PIDs."""
    import anyio

    pending = list(identities)
    deadline = time.monotonic() + wait
    while pending:
        pending = await in_thread("stop check", pending, _still_running, pending)
        if not pending or time.monotonic() >= deadline:
            break
        await anyio.sleep(_VERIFY_POLL_SECONDS)
    if not pending:
        return [], []
    unknown = ([], [i.pid for i in pending])
    return await in_thread("terminating survivors", unknown, _terminate_survivors, pending)


# --------------------------------------------------------------- the runtime


class GateRuntime:
    """Gate-wide accounting state: memory tier, pressure, ledger, and PID registry."""

    def __init__(
        self,
        config: GateConfig,
        *,
        memory_sampler: Callable[[], int | None],
        total_memory: int | None,
        ledger: gate_accounting.GateLedger,
        registry: gate_accounting.PidRegistry | None,
    ) -> None:
        import anyio

        self.config = config
        self.tier = gate_accounting.tier_for(total_memory)
        self.ledger = ledger
        self.registry = registry
        self.spawn_lock = anyio.Lock()
        self.roots: dict[str, Identity] = {}
        self.level = "normal"
        self.available_bytes: int | None = None
        self._sampler = memory_sampler

    @classmethod
    async def start(
        cls,
        config: GateConfig,
        *,
        memory_sampler: Callable[[], int | None] | None = None,
        total_memory: Any = UNSET,
        ledger: gate_accounting.GateLedger | None = None,
        registry_dir: Path | None = None,
    ) -> GateRuntime:
        """Sweep what crashed gates left behind, then open this gate's registry."""
        if ledger is None or not config.accounting:
            ledger = gate_accounting.GateLedger(
                session=new_session_id(), enabled=config.accounting
            )
        directory = registry_dir if registry_dir is not None else default_registry_dir()
        swept = await in_thread("orphan sweep", [], gate_accounting.sweep_orphans, directory)
        if swept:
            safely("accounting", None, ledger.record, "gate_sweep", SWEEP_SERVER,
                   pids=[i.pid for i in swept])
        owner = await in_thread("own identity", None, procinfo.process_identity, os.getpid())
        if owner is None:
            warn("own process identity unreadable; crash cleanup is off for this gate")
        runtime = cls(
            config,
            memory_sampler=memory_sampler or procinfo.available_memory_bytes,
            total_memory=config.total_memory_bytes if total_memory is UNSET else total_memory,
            ledger=ledger,
            registry=None if owner is None else gate_accounting.PidRegistry(directory, owner),
        )
        await runtime.sample_pressure()
        return runtime

    def record(self, event: str, server: str, **fields: Any) -> None:
        safely(f"accounting {event}", None, self.ledger.record, event, server, **fields)

    def set_client(self, name: object) -> None:
        if isinstance(name, str) and name and self.ledger.client is None:
            self.ledger.client = name

    async def sample_pressure(self) -> str:
        available = await in_thread("free memory probe", None, self._sampler)
        valid = isinstance(available, int) and not isinstance(available, bool)
        self.available_bytes = available if valid else None
        self.level = safely(
            "pressure level",
            "normal",
            gate_accounting.pressure_level,
            self.available_bytes,
            self.tier,
            low_override_bytes=self.config.low_watermark_bytes,
            critical_override_bytes=self.config.critical_watermark_bytes,
        )
        return self.level

    def effective_idle(self, configured: float) -> float:
        return gate_accounting.effective_idle_seconds(
            configured, self.config.min_idle_seconds, self.level
        )

    def track(self, server: str, tree: tuple[Identity, ...]) -> None:
        if self.registry is not None:
            safely("pid registry", None, self.registry.track, server, list(tree))

    def untrack(self, server: str) -> None:
        self.roots.pop(server, None)
        if self.registry is not None:
            safely("pid registry", None, self.registry.untrack, server)

    def close(self) -> None:
        if self.registry is not None:
            safely("pid registry", None, self.registry.close)

    def summary(self) -> dict:
        return {
            "tier": self.tier.name,
            "pressure": self.level,
            "available_mb": to_mb(self.available_bytes),
            "min_idle_seconds": self.config.min_idle_seconds,
        }


class ServerAccount:
    """One server's current awake period: its root, its tree as last seen, its calls.

    `begin` opens a period after a wake and records `gate_wake`; `finish`
    closes it after a stop, verifying the tree is gone and recording
    `gate_sleep`. The owning `_Downstream` serializes both under its lock.
    """

    def __init__(self, name: str, runtime: GateRuntime) -> None:
        self.name = name
        self._runtime = runtime
        self.root: Identity | None = None
        self.tree: tuple[Identity, ...] = ()
        self.tree_mb: float | None = None
        self.calls = 0
        self.woke_at: float | None = None

    async def spawn(self, stack: Any, client: Any) -> Any:
        """Enter `client` (which launches the process) and attribute its root PID.

        Spawns are serialized gate-wide, so the one new child of this process
        between the two snapshots is the downstream. Only the spawn is under
        the lock, not the (possibly slow) MCP startup that follows.
        """
        runtime = self._runtime
        async with runtime.spawn_lock:
            before = await in_thread("process snapshot", frozenset(), child_pids)
            streams = await stack.enter_async_context(client)
            others = [i.pid for name, i in runtime.roots.items() if name != self.name]
            self.root = await in_thread("downstream attribution", None, new_child, before, others)
        if self.root is not None:
            runtime.roots[self.name] = self.root
        return streams

    def start_failed(self, started: float, reason: str, error: str) -> None:
        self._runtime.untrack(self.name)
        self.root = None
        self._runtime.record(
            "gate_wake_failed", self.name,
            reason=reason, startup_ms=ms_since(started), error=error,
        )

    async def begin(self, reason: str, started: float) -> None:
        startup_ms = ms_since(started)
        self.woke_at = time.monotonic()
        self.calls = 0
        try:
            self.tree, self.tree_mb = await self.sample(())
            self._runtime.track(self.name, self.tree)
            self._runtime.record(
                "gate_wake", self.name,
                reason=reason,
                startup_ms=startup_ms,
                root_pid=None if self.root is None else self.root.pid,
                tree_mb=self.tree_mb,
            )
        except Exception as exc:  # noqa: BLE001 -- the server is up; only accounting failed
            warn(f"'{self.name}' wake accounting failed: {type(exc).__name__}: {exc}")

    async def sample(self, known: tuple[Identity, ...]) -> tuple[tuple[Identity, ...], float | None]:
        """(tree, MB) now: from the root while it is still ours, plus `known` survivors."""
        tree = await in_thread("process tree probe", known, live_tree, self.root, known)
        return tree, to_mb(await in_thread("tree memory probe", None, tree_bytes, tree))

    async def sample_mb(self) -> None:
        _, self.tree_mb = await self.sample(self.tree)

    async def refresh(self) -> None:
        """Re-record the tree: servers such as browsers spawn children late."""
        tree = await in_thread("process tree probe", self.tree, live_tree, self.root, self.tree)
        if {_key(i) for i in tree} != {_key(i) for i in self.tree}:
            self.tree = tree
            self._runtime.track(self.name, tree)

    async def finish(
        self, reason: str, tree: tuple[Identity, ...] | None = None, tree_mb: float | None = None
    ) -> None:
        """Verify the stopped tree is gone (killing exact survivors) and record the sleep.

        Shielded from the caller's cancellation (a client cancelling
        `gate_close`, say): an interrupted verify would untrack survivors
        without killing them. It is bounded by STOP_VERIFY_SECONDS plus the
        terminate wait.
        """
        import anyio

        if self.woke_at is None:
            return
        with anyio.CancelScope(shield=True):
            await self._finish(reason, tree, tree_mb)

    async def _finish(
        self, reason: str, tree: tuple[Identity, ...] | None, tree_mb: float | None
    ) -> None:
        try:
            if tree is None:
                tree, tree_mb = await self.sample(self.tree)
            if tree_mb is None:
                # The tree already exited (a crash, found by the reaper or a
                # wake): its last known size is better than counting zero.
                tree_mb = self.tree_mb
            reaped, leaked = await verify_gone(tree)
            if leaked:
                pids = ", ".join(str(pid) for pid in leaked)
                warn(f"'{self.name}' left processes running after its stop: {pids}")
            self._runtime.record(
                "gate_sleep", self.name,
                reason=reason,
                awake_s=round(time.monotonic() - self.woke_at, 3),
                calls=self.calls,
                tree_mb=tree_mb,
                pressure=self._runtime.level,
                available_mb=to_mb(self._runtime.available_bytes),
                reaped_pids=reaped,
                leaked_pids=leaked,
                tree_pids=[identity.pid for identity in tree],
            )
        except Exception as exc:  # noqa: BLE001 -- accounting must not fail a stop
            warn(f"'{self.name}' stop accounting failed: {type(exc).__name__}: {exc}")
        finally:
            self._runtime.untrack(self.name)
            self.woke_at = None
            self.root = None
            self.tree = ()
            self.tree_mb = None


def ms_since(started: float) -> int:
    return max(1, round((time.monotonic() - started) * 1000))
