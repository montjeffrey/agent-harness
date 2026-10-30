"""Configuration for the on-demand MCP gate (`harness.mcp_gate`).

`~/.agent-harness/gate.json` (or the path named by `AGENT_HARNESS_GATE_CONFIG`)
holds one `command`/`args`/`env` entry per gated server plus a few gate-wide
settings:

* `idle_timeout_seconds` -- keep-warm time for servers that set none. Without
  it, the machine's memory tier decides (300 s on 16 GB, 600 s on 32 GB,
  900 s above; see `harness.gate_accounting.TIERS`). Explicit config wins.
* `min_idle_seconds` (default 60) -- keep-warm time while free RAM is below
  the low watermark.
* `low_watermark_mb` / `critical_watermark_mb` -- override the tier's
  free-RAM watermarks.
* `accounting` (default true) -- write gate records to the telemetry folder.

Unknown keys, at the top level or in a server entry, are rejected: a typo
must fail loudly instead of silently running with defaults.

This module also owns the tool-naming scheme and the on-disk tool manifest
cache, so listing tools never needs a running server.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import gate_accounting, procinfo
from harness.atomicwrite import atomic_sibling

CONFIG_ENV = "AGENT_HARNESS_GATE_CONFIG"
SEPARATOR = "__"
DEFAULT_IDLE_TIMEOUT_SECONDS = 600.0
DEFAULT_MIN_IDLE_SECONDS = 60.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0
# A live but wedged downstream must not hold a call (and, through `inflight`,
# the reaper) forever. Generous because browser tools legitimately run long.
DEFAULT_CALL_TIMEOUT_SECONDS = 900.0
MIB = 1024 * 1024

CONTROL_TOOLS = ("gate_status", "gate_open", "gate_close")
_SERVER_KEYS = frozenset(
    {
        "command",
        "args",
        "env",
        "cwd",
        "description",
        "idle_timeout_seconds",
        "startup_timeout_seconds",
        "call_timeout_seconds",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "servers",
        "idle_timeout_seconds",
        "min_idle_seconds",
        "low_watermark_mb",
        "critical_watermark_mb",
        "accounting",
    }
)

# Lowercase words joined by single hyphens. No underscores at all, so a
# server name can never contain SEPARATOR and `split_public_name` stays exact
# even when the downstream tool's own name contains "__".
_SERVER_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# `load_config(total_memory=...)` default: probe the machine at call time.
_PROBE: Any = object()


class GateConfigError(ValueError):
    """The gate configuration file is unreadable or describes an invalid server."""


@dataclass(frozen=True)
class ServerSpec:
    """One gated downstream server, exactly as an agent's MCP config would launch it."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    description: str = ""
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS
    call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS

    def digest(self) -> str:
        """Identity of the launch command; a cached manifest is valid only for this."""
        payload = json.dumps([self.command, list(self.args), list(self.env), self.cwd])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GateConfig:
    servers: dict[str, ServerSpec]
    cache_dir: Path
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS
    low_watermark_bytes: int | None = None
    critical_watermark_bytes: int | None = None
    accounting: bool = True
    # Total RAM seen when the config was loaded; picks the memory tier.
    total_memory_bytes: int | None = None

    @property
    def tier(self) -> gate_accounting.MemoryTier:
        return gate_accounting.tier_for(self.total_memory_bytes)


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-harness" / "gate.json"


def _positive_number(value: Any, where: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise GateConfigError(f"{where} must be a positive number, got {value!r}")
    return float(value)


def _parse_server(name: str, entry: Any, default_idle: float, base_dir: Path) -> ServerSpec:
    where = f"gate server '{name}'"
    if not _SERVER_NAME.match(name):
        raise GateConfigError(
            f"{where}: name must be lowercase letters, digits, and single hyphens"
        )
    if not isinstance(entry, Mapping):
        raise GateConfigError(f"{where}: entry must be an object")
    unknown = sorted(set(entry) - _SERVER_KEYS)
    if unknown:
        raise GateConfigError(
            f"{where}: unknown keys {', '.join(unknown)} (allowed: {', '.join(sorted(_SERVER_KEYS))})"
        )
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise GateConfigError(f"{where}: 'command' must be a non-empty string")
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise GateConfigError(f"{where}: 'args' must be a list of strings")
    env = entry.get("env", {})
    if not isinstance(env, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise GateConfigError(f"{where}: 'env' must map strings to strings")
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise GateConfigError(f"{where}: 'cwd' must be a string")
    if cwd is not None and not Path(cwd).is_absolute():
        # Relative to the config file, not to whichever folder the agent
        # happened to launch the gate from.
        cwd = os.path.normpath(base_dir / cwd)
    description = entry.get("description", "")
    if not isinstance(description, str):
        raise GateConfigError(f"{where}: 'description' must be a string")
    idle = entry.get("idle_timeout_seconds", default_idle)
    startup = entry.get("startup_timeout_seconds", DEFAULT_STARTUP_TIMEOUT_SECONDS)
    call = entry.get("call_timeout_seconds", DEFAULT_CALL_TIMEOUT_SECONDS)
    return ServerSpec(
        name=name,
        command=command,
        args=tuple(args),
        env=tuple(sorted(env.items())),
        cwd=cwd,
        description=description,
        idle_timeout_seconds=_positive_number(idle, f"{where}: 'idle_timeout_seconds'"),
        startup_timeout_seconds=_positive_number(
            startup, f"{where}: 'startup_timeout_seconds'"
        ),
        call_timeout_seconds=_positive_number(call, f"{where}: 'call_timeout_seconds'"),
    )


def _watermark_bytes(data: Mapping, key: str) -> int | None:
    if key not in data:
        return None
    return int(_positive_number(data[key], f"'{key}'") * MIB)


def _gate_settings(data: Mapping, path: Path) -> dict[str, Any]:
    """The validated gate-wide keys (everything but `servers` and the idle default)."""
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise GateConfigError(
            f"gate config {path}: unknown keys {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(_TOP_LEVEL_KEYS))})"
        )
    low = _watermark_bytes(data, "low_watermark_mb")
    critical = _watermark_bytes(data, "critical_watermark_mb")
    if low is not None and critical is not None and critical > low:
        raise GateConfigError(
            f"gate config {path}: 'critical_watermark_mb' must not exceed 'low_watermark_mb'"
        )
    accounting = data.get("accounting", True)
    if not isinstance(accounting, bool):
        raise GateConfigError(f"gate config {path}: 'accounting' must be true or false")
    return {
        "min_idle_seconds": _positive_number(
            data.get("min_idle_seconds", DEFAULT_MIN_IDLE_SECONDS), "'min_idle_seconds'"
        ),
        "low_watermark_bytes": low,
        "critical_watermark_bytes": critical,
        "accounting": accounting,
    }


def load_config(
    path: Path | None = None, cache_dir: Path | None = None, *, total_memory: Any = _PROBE
) -> GateConfig:
    """Read the gate config. A missing file is a valid, empty gate.

    `total_memory` (bytes, or None for unknown) picks the memory tier; by
    default the machine is probed now.
    """
    path = Path(path) if path is not None else default_config_path()
    cache_dir = Path(cache_dir) if cache_dir is not None else path.parent / "gate-cache"
    total = procinfo.total_memory_bytes() if total_memory is _PROBE else total_memory
    tier_idle = gate_accounting.tier_for(total).default_idle_seconds
    if not path.is_file():
        return GateConfig(
            servers={}, cache_dir=cache_dir, idle_timeout_seconds=tier_idle, total_memory_bytes=total
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"gate config {path} is unreadable: {exc}") from exc
    if not isinstance(data, Mapping):
        raise GateConfigError(f"gate config {path} must be a JSON object")
    settings = _gate_settings(data, path)
    default_idle = _positive_number(
        data.get("idle_timeout_seconds", tier_idle), "'idle_timeout_seconds'"
    )
    servers = data.get("servers", {})
    if not isinstance(servers, Mapping):
        raise GateConfigError(f"gate config {path}: 'servers' must be an object")
    parsed = {
        name: _parse_server(name, entry, default_idle, path.parent)
        for name, entry in servers.items()
    }
    return GateConfig(
        servers=parsed,
        cache_dir=cache_dir,
        idle_timeout_seconds=default_idle,
        total_memory_bytes=total,
        **settings,
    )


def public_name(server: str, tool: str) -> str:
    return f"{server}{SEPARATOR}{tool}"


def split_public_name(name: str) -> tuple[str, str] | None:
    server, sep, tool = name.partition(SEPARATOR)
    if not sep or not server or not tool:
        return None
    return server, tool


class ManifestCache:
    """Each gated server's tool list on disk, so listing tools starts nothing."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, spec: ServerSpec) -> Path:
        return self.directory / f"{spec.name}.json"

    def load(self, spec: ServerSpec) -> list[dict] | None:
        try:
            data = json.loads(self._path(spec).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, Mapping) or data.get("digest") != spec.digest():
            return None
        tools = data.get("tools")
        if not isinstance(tools, list) or not all(
            isinstance(t, Mapping) and isinstance(t.get("name"), str) for t in tools
        ):
            return None
        return tools

    def store(self, spec: ServerSpec, tools: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "digest": spec.digest(),
            "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "tools": tools,
        }
        target = self._path(spec)
        with atomic_sibling(target) as tmp:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
