"""On-demand MCP gate: heavy MCP servers sleep until an agent needs them.

Every agent session that registers an MCP server directly starts its own copy
of that server and keeps it alive for the whole session -- whether or not a
single tool is ever called. With a dozen sessions open, a handful of browser
and file-system servers each cost gigabytes of RAM to sit idle (measured on a
32 GB Windows workstation, 2026-10-29: ~5.6 GB of working set across 16
sessions, plus their `npx`/`cmd`/`conhost` wrappers).

The gate is one small stdio MCP server that stands in for all of them:

* **Asleep by default.** It advertises each gated server's tools from a
  cached manifest, so listing tools starts nothing.
* **Wakes on first use.** The first call to `<server>__<tool>` starts that
  server, forwards the call, and returns the downstream result unchanged.
* **Goes back to sleep.** A server idle for `idle_timeout_seconds` is stopped,
  process tree and all, and wakes again on the next call.
* **Manual control.** `gate_status`, `gate_open`, and `gate_close` let an
  agent (or a person) see and drive the state explicitly.

Configuration lives in `~/.agent-harness/gate.json` (or the path named by
`AGENT_HARNESS_GATE_CONFIG`); each entry is the same `command`/`args`/`env`
triple an agent's own MCP config would hold. The server works with any MCP
client -- Claude Code, Codex, Gemini -- because it is an ordinary stdio server.

Like `harness.mcp_server`, importing this module never requires the optional
`mcp` SDK: every `mcp`/`anyio` import is local, so `main` can explain a missing
extra instead of crashing on import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.atomicwrite import atomic_sibling

CONFIG_ENV = "AGENT_HARNESS_GATE_CONFIG"
SEPARATOR = "__"
DEFAULT_IDLE_TIMEOUT_SECONDS = 600.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 60.0
# A live but wedged downstream must not hold a call (and, through `inflight`,
# the reaper) forever. Generous because browser tools legitimately run long.
DEFAULT_CALL_TIMEOUT_SECONDS = 900.0
# A killed downstream does not always surface as EOF on Windows: measured
# 2026-10-29, the SDK's stdout reader stayed parked after `taskkill /F`, so a
# forwarded call waited forever. A ping before each call is answered in
# milliseconds by a live server and turns a dead one into a restart.
PING_TIMEOUT_SECONDS = 5.0
# After a failed start, further calls fail fast instead of each queueing for a
# full startup timeout; `gate_open` bypasses this to retry on demand.
FAILURE_BACKOFF_SECONDS = 30.0
# How long to wait for a stopped server's runner to unwind. The SDK itself
# escalates from closing stdin to terminating the process tree within ~2 s.
STOP_WAIT_SECONDS = 15.0
MAX_REAP_INTERVAL_SECONDS = 30.0
MIN_REAP_INTERVAL_SECONDS = 0.05
# Claude's API rejects tool names over 64 characters, and Claude Code adds an
# `mcp__<server>__` prefix; a longer public name is warned about, not dropped.
CLIENT_TOOL_NAME_LIMIT = 64
CLIENT_PREFIX_ESTIMATE = len("mcp__gate__")

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

# Lowercase words joined by single hyphens. No underscores at all, so a
# server name can never contain SEPARATOR and `split_public_name` stays exact
# even when the downstream tool's own name contains "__".
_SERVER_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class GateConfigError(ValueError):
    """The gate configuration file is unreadable or describes an invalid server."""


class GateError(RuntimeError):
    """A gate operation was asked for something that does not exist."""


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


def load_config(path: Path | None = None, cache_dir: Path | None = None) -> GateConfig:
    """Read the gate config. A missing file is a valid, empty gate."""
    path = Path(path) if path is not None else default_config_path()
    cache_dir = Path(cache_dir) if cache_dir is not None else path.parent / "gate-cache"
    if not path.is_file():
        return GateConfig(servers={}, cache_dir=cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateConfigError(f"gate config {path} is unreadable: {exc}") from exc
    if not isinstance(data, Mapping):
        raise GateConfigError(f"gate config {path} must be a JSON object")
    default_idle = _positive_number(
        data.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS),
        "'idle_timeout_seconds'",
    )
    servers = data.get("servers", {})
    if not isinstance(servers, Mapping):
        raise GateConfigError(f"gate config {path}: 'servers' must be an object")
    parsed = {
        name: _parse_server(name, entry, default_idle, path.parent)
        for name, entry in servers.items()
    }
    return GateConfig(servers=parsed, cache_dir=cache_dir, idle_timeout_seconds=default_idle)


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


def _describe(exc: BaseException) -> str:
    """One readable line for an error, unwrapping anyio exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _is_transport_failure(exc: BaseException) -> bool:
    """True when the downstream connection is gone, not when the server said no.

    A JSON-RPC error from a healthy server (bad arguments, unknown tool) is an
    `McpError` too; restarting the server for it would kill every other
    in-flight call on it for nothing.
    """
    import anyio
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED

    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError)):
        return True
    return isinstance(exc, McpError) and exc.error.code == CONNECTION_CLOSED


def _tool_record(tool: Any) -> dict:
    record = {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.inputSchema or {"type": "object"},
    }
    if getattr(tool, "annotations", None) is not None:
        record["annotations"] = tool.annotations.model_dump(exclude_none=True)
    return record


class _Downstream:
    """One gated server: asleep (no process) or awake (a live client session).

    The client session is entered and exited inside a single runner task,
    because anyio cancel scopes must be left by the task that entered them. A
    call from any other task only uses the live session object.
    """

    def __init__(self, spec: ServerSpec, cache: ManifestCache, task_group: Any) -> None:
        import anyio

        self.spec = spec
        self._cache = cache
        self._task_group = task_group
        self._lock = anyio.Lock()
        self._session: Any = None
        self._stop: Any = None
        self._stopped: Any = None
        self.tools: list[dict] | None = cache.load(spec)
        self.inflight = 0
        self.last_used = time.monotonic()
        self.last_error: str | None = None
        self._failed_at: float | None = None

    @property
    def awake(self) -> bool:
        """Running and not asked to stop.

        A stop flips this at once: the process tree dies quickly, but the SDK
        finishes its own cleanup after that, and status must not lag behind
        what is actually holding memory.
        """
        return self._session is not None and not self._stop.is_set()

    @property
    def stopped(self) -> bool:
        """The runner has fully unwound (no process, no session)."""
        return self._session is None

    @property
    def session(self) -> Any:
        if not self.awake:
            raise GateError(f"gated server '{self.spec.name}' is asleep")
        return self._session

    async def wake(self, *, force: bool = False) -> bool:
        """Start the server if it is asleep. Returns True when its tool list changed.

        Within FAILURE_BACKOFF_SECONDS of a failed start this raises GateError
        at once (unless `force`), so concurrent callers do not each wait out
        a full startup timeout on a server that just failed.
        """
        import anyio

        async with self._lock:
            if self.awake:
                return False
            if not self.stopped:
                raise GateError(
                    f"gated server '{self.spec.name}' is still stopping ({self.last_error})"
                )
            if (
                not force
                and self._failed_at is not None
                and time.monotonic() - self._failed_at < FAILURE_BACKOFF_SECONDS
            ):
                raise GateError(
                    f"gated server '{self.spec.name}' failed to start recently "
                    f"({self.last_error}); it retries after {FAILURE_BACKOFF_SECONDS:g}s, "
                    "or call gate_open to retry now"
                )
            before = self.tools
            self._stop = anyio.Event()
            self._stopped = anyio.Event()
            timeout = self.spec.startup_timeout_seconds
            try:
                with anyio.fail_after(timeout):
                    await self._task_group.start(self._run)
            except TimeoutError:
                self._failed_at = time.monotonic()
                self.last_error = f"did not start within {timeout:g}s ({self.spec.command})"
                raise GateError(f"gated server '{self.spec.name}' {self.last_error}") from None
            except Exception as exc:
                self._failed_at = time.monotonic()
                self.last_error = _describe(exc)
                raise
            self._failed_at = None
            self.last_error = None
            self.last_used = time.monotonic()
            return self.tools != before

    async def ping(self) -> bool:
        """True when the awake server answers a ping within PING_TIMEOUT_SECONDS."""
        import anyio

        if not self.awake:
            return False
        session = self._session
        with anyio.move_on_after(PING_TIMEOUT_SECONDS):
            try:
                await session.send_ping()
            except Exception:  # noqa: BLE001 -- any failure means "not answering"
                return False
            return True
        return False

    async def _run(self, *, task_status: Any) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.spec.command,
            args=list(self.spec.args),
            env={**os.environ, **dict(self.spec.env)},
            cwd=self.spec.cwd,
        )
        started = False
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools: list[dict] = []
                cursor: str | None = None
                while True:
                    listed = await session.list_tools(cursor)
                    tools.extend(_tool_record(tool) for tool in listed.tools)
                    cursor = listed.nextCursor
                    if not cursor:
                        break
                try:
                    self._cache.store(self.spec, tools)
                except OSError as exc:
                    # A cloud-sync lock on the cache must not fail a healthy wake.
                    print(
                        f"[mcp-gate] '{self.spec.name}' tool cache not saved: {_describe(exc)}",
                        file=sys.stderr,
                    )
                self.tools = tools
                self._session = session
                started = True
                task_status.started()
                await self._stop.wait()
        except Exception as exc:
            if not started:
                raise
            # After startup the runner lives in the gate's task group; an
            # escaping error would take the whole gate down with it.
            self.last_error = _describe(exc)
            print(f"[mcp-gate] '{self.spec.name}' stopped: {self.last_error}", file=sys.stderr)
        finally:
            self._session = None
            self._stopped.set()

    def is_idle(self, now: float) -> bool:
        return self.inflight == 0 and now - self.last_used >= self.spec.idle_timeout_seconds

    async def sleep(self, *, only_if_idle: bool = False) -> None:
        """Stop the server (process tree included). A no-op when already asleep.

        `only_if_idle` re-checks idleness under the lock: the reaper decides
        before it gets the lock, and a call may have started in between.
        """
        import anyio

        async with self._lock:
            if self._session is None:
                return
            if only_if_idle and not self.is_idle(time.monotonic()):
                return
            self._stop.set()
            with anyio.move_on_after(STOP_WAIT_SECONDS):
                await self._stopped.wait()
            if self._session is not None:
                self.last_error = f"did not stop within {STOP_WAIT_SECONDS:g}s"


def _default_reap_interval(config: GateConfig) -> float:
    timeouts = [spec.idle_timeout_seconds for spec in config.servers.values()]
    smallest = min(timeouts, default=MAX_REAP_INTERVAL_SECONDS * 2)
    return max(MIN_REAP_INTERVAL_SECONDS, min(MAX_REAP_INTERVAL_SECONDS, smallest / 2))


class Gate:
    """All gated servers for one agent session."""

    def __init__(self, config: GateConfig, task_group: Any, *, reap_interval: float) -> None:
        cache = ManifestCache(config.cache_dir)
        self.config = config
        self._reap_interval = reap_interval
        self._tools_changed = False
        self._warned_long_names: set[str] = set()
        self._downstreams = {
            name: _Downstream(spec, cache, task_group)
            for name, spec in sorted(config.servers.items())
        }

    @classmethod
    @asynccontextmanager
    async def running(
        cls, config: GateConfig, *, reap_interval: float | None = None
    ) -> AsyncIterator[Gate]:
        """Run a gate; on exit every awake server is stopped."""
        import anyio

        interval = reap_interval if reap_interval is not None else _default_reap_interval(config)
        async with anyio.create_task_group() as task_group:
            gate = cls(config, task_group, reap_interval=interval)
            task_group.start_soon(gate._reap_forever)
            try:
                yield gate
            finally:
                with anyio.CancelScope(shield=True):
                    await gate.close_all()
                task_group.cancel_scope.cancel()

    # ------------------------------------------------------------ lifecycle

    def _get(self, server: str) -> _Downstream:
        downstream = self._downstreams.get(server)
        if downstream is None:
            known = ", ".join(self._downstreams) or "none configured"
            raise GateError(f"unknown gated server '{server}' (servers: {known})")
        return downstream

    async def _reap_forever(self) -> None:
        import anyio

        while True:
            await anyio.sleep(self._reap_interval)
            await self.reap(time.monotonic())

    async def reap(self, now: float) -> None:
        """Stop every awake server that has been idle past its timeout."""
        for downstream in self._downstreams.values():
            if downstream.awake and downstream.is_idle(now):
                await downstream.sleep(only_if_idle=True)

    async def open_server(self, server: str) -> str:
        downstream = self._get(server)
        if await downstream.wake(force=True):
            self._tools_changed = True
        count = len(downstream.tools or [])
        return (
            f"'{server}' is awake with {count} tools; it goes back to sleep after "
            f"{downstream.spec.idle_timeout_seconds:g}s idle."
        )

    async def close_server(self, server: str) -> str:
        downstream = self._get(server)
        await downstream.sleep()
        if not downstream.stopped:
            raise GateError(f"'{server}' {downstream.last_error}; it is still stopping")
        return f"'{server}' is asleep."

    async def close_all(self) -> None:
        for downstream in self._downstreams.values():
            await downstream.sleep()

    def pop_tools_changed(self) -> bool:
        """True once after any wake changed an advertised tool list."""
        changed, self._tools_changed = self._tools_changed, False
        return changed

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "server": name,
                "state": "awake" if downstream.awake else "asleep",
                "tools": None if downstream.tools is None else len(downstream.tools),
                "idle_seconds": round(now - downstream.last_used, 1) if downstream.awake else None,
                "idle_timeout_seconds": downstream.spec.idle_timeout_seconds,
                "last_error": downstream.last_error,
            }
            for name, downstream in self._downstreams.items()
        ]

    # ---------------------------------------------------------------- tools

    def list_tools(self) -> list[Any]:
        from mcp import types

        names = list(self._downstreams)
        server_arg = {
            "type": "object",
            "properties": {"server": {"type": "string", "enum": names}},
            "required": ["server"],
        }
        tools = [
            types.Tool(
                name="gate_status",
                description=(
                    "Show every gated MCP server: asleep or awake, how many tools it "
                    "offers, and how long it has been idle."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="gate_open",
                description=(
                    "Wake a gated MCP server now (otherwise it wakes on its first tool "
                    "call). Use this for a server whose tools are not listed yet."
                ),
                inputSchema=server_arg,
            ),
            types.Tool(
                name="gate_close",
                description="Put a gated MCP server back to sleep now, freeing its memory.",
                inputSchema=server_arg,
            ),
        ]
        for name, downstream in self._downstreams.items():
            for record in downstream.tools or []:
                tool = self._public_tool(name, record)
                if tool is not None:
                    tools.append(tool)
        return tools

    def _public_tool(self, server: str, record: Mapping[str, Any]) -> Any:
        """One cached record as a public Tool, or None when the record is unusable.

        A single bad or newer-format record must not fail the whole listing,
        control tools included.
        """
        from mcp import types

        try:
            annotations = record.get("annotations")
            tool = types.Tool(
                name=public_name(server, record["name"]),
                description=f"[{server}, starts on first use] {record.get('description', '')}",
                inputSchema=record.get("inputSchema") or {"type": "object"},
                annotations=types.ToolAnnotations(**annotations) if annotations else None,
            )
        except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError and friends
            print(
                f"[mcp-gate] skipping cached tool '{server}/{record.get('name')}': {_describe(exc)}",
                file=sys.stderr,
            )
            return None
        if (
            len(tool.name) + CLIENT_PREFIX_ESTIMATE > CLIENT_TOOL_NAME_LIMIT
            and tool.name not in self._warned_long_names
        ):
            self._warned_long_names.add(tool.name)
            print(
                f"[mcp-gate] '{tool.name}' may exceed a client's {CLIENT_TOOL_NAME_LIMIT}-character "
                "tool-name limit; use a shorter server name",
                file=sys.stderr,
            )
        return tool

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        try:
            if name == "gate_status":
                return _text_result(json.dumps(self.status(), indent=2))
            if name == "gate_open":
                return _text_result(await self.open_server(str(arguments.get("server", ""))))
            if name == "gate_close":
                return _text_result(await self.close_server(str(arguments.get("server", ""))))
        except GateError as exc:
            return _error_result(str(exc))
        except Exception as exc:  # noqa: BLE001 -- a broken server must not crash the gate
            return _error_result(f"gate operation failed: {_describe(exc)}")

        parts = split_public_name(name)
        if parts is None or parts[0] not in self._downstreams:
            return _error_result(f"unknown gated tool '{name}'")
        server, tool = parts
        return await self._forward(self._downstreams[server], tool, dict(arguments))

    async def _forward(self, downstream: _Downstream, tool: str, arguments: dict) -> Any:
        """Wake if needed, check the server answers, forward; restart once if it died."""
        import anyio

        server = downstream.spec.name
        downstream.inflight += 1
        try:
            for attempt in (1, 2):
                try:
                    if await downstream.wake():
                        self._tools_changed = True
                except GateError as exc:
                    return _error_result(str(exc))
                except Exception as exc:  # noqa: BLE001 -- reported to the caller, not raised
                    return _error_result(
                        f"gated server '{server}' failed to start: {_describe(exc)}"
                    )
                if tool not in {record["name"] for record in downstream.tools or []}:
                    return _error_result(f"gated server '{server}' has no tool '{tool}'")
                # A dead server is safe to restart: nothing was sent to it yet.
                if not await downstream.ping():
                    downstream.last_error = "stopped answering; restarted"
                    await downstream.sleep()
                    continue
                timeout = downstream.spec.call_timeout_seconds
                try:
                    with anyio.fail_after(timeout):
                        return await downstream.session.call_tool(tool, arguments)
                except TimeoutError:
                    downstream.last_error = f"call to '{tool}' exceeded {timeout:g}s"
                    await downstream.sleep()
                    return _error_result(
                        f"gated server '{server}' did not answer '{tool}' within {timeout:g}s "
                        "and was stopped"
                    )
                except Exception as exc:  # noqa: BLE001 -- classified below
                    if not _is_transport_failure(exc):
                        return _error_result(f"gated server '{server}' error: {_describe(exc)}")
                    downstream.last_error = _describe(exc)
                    await downstream.sleep()
                    # The request may already have run, so it is not retried.
                    return _error_result(f"gated server '{server}' failed: {downstream.last_error}")
            return _error_result(f"gated server '{server}' is not answering after a restart")
        finally:
            downstream.inflight -= 1
            downstream.last_used = time.monotonic()


def _text_result(text: str) -> Any:
    from mcp import types

    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def _error_result(text: str) -> Any:
    from mcp import types

    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


GATE_INSTRUCTIONS = (
    "This server is a gate in front of heavier MCP servers. Their tools are listed as "
    "<server>__<tool> and each server starts only when one of its tools is first "
    "called, then stops again after it has been idle. Call gate_status to see which "
    "servers exist; call gate_open for a server whose tools are not listed yet."
)


def build_server(gate: Gate) -> Any:
    from mcp.server.lowlevel import Server

    server = Server("agent-harness-gate", instructions=GATE_INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return gate.list_tools()

    # Downstream servers validate their own input; validating here against a
    # possibly stale cached schema would only reject calls the server accepts.
    # SDKs older than the `validate_input` flag never validated at all.
    try:
        call_tool_decorator = server.call_tool(validate_input=False)
    except TypeError:
        call_tool_decorator = server.call_tool()

    @call_tool_decorator
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
        result = await gate.call_tool(name, arguments or {})
        if gate.pop_tools_changed():
            try:
                await server.request_context.session.send_tool_list_changed()
            except Exception as exc:  # noqa: BLE001 -- the call itself succeeded
                print(f"[mcp-gate] tools/list_changed not sent: {_describe(exc)}", file=sys.stderr)
        return result

    return server


async def serve(config: GateConfig, reap_interval: float | None = None) -> None:
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.stdio import stdio_server

    async with Gate.running(config, reap_interval=reap_interval) as gate:
        server = build_server(gate)
        options = server.create_initialization_options(
            notification_options=NotificationOptions(tools_changed=True)
        )
        async with stdio_server() as (read, write):
            await server.run(read, write, options)


async def warm(config: GateConfig, names: list[str]) -> list[tuple[bool, str]]:
    """Wake each server once to capture its tool manifest, then stop it.

    Returns `(ok, message)` per server so a bootstrap script can tell a
    captured manifest from a server that never started.
    """
    outcomes = []
    async with Gate.running(config) as gate:
        for name in names:
            result = await gate.call_tool("gate_open", {"server": name})
            outcomes.append((not result.isError, result.content[0].text))
            await gate.close_server(name)
    return outcomes


def _status_lines(config: GateConfig) -> list[str]:
    if not config.servers:
        return [f"no gated servers configured (config: {default_config_path()})"]
    cache = ManifestCache(config.cache_dir)
    lines = []
    for name, spec in sorted(config.servers.items()):
        tools = cache.load(spec)
        cached = "not cached (run warm)" if tools is None else f"{len(tools)} tools cached"
        lines.append(f"{name}: {cached}; idle timeout {spec.idle_timeout_seconds:g}s")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-harness-gate",
        description="Serve heavy MCP servers on demand: asleep until used, stopped when idle.",
    )
    # Accepted before or after the subcommand: agent MCP configs naturally
    # write `serve --config ...`. SUPPRESS keeps a subcommand from resetting a
    # value given before it.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    for target in (parser, common):
        target.add_argument(
            "--config", type=Path, help="gate config (default: ~/.agent-harness/gate.json)"
        )
        target.add_argument("--cache-dir", type=Path, help="tool manifest cache directory")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", parents=[common], help="run the gate as a stdio MCP server (default)")
    sub.add_parser(
        "status", parents=[common], help="list gated servers and their cached tool manifests"
    )
    warm_parser = sub.add_parser(
        "warm", parents=[common], help="capture tool manifests (wakes each server once)"
    )
    warm_parser.add_argument("servers", nargs="*", help="servers to warm (default: all)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        config = load_config(args.config, args.cache_dir)
    except GateConfigError as exc:
        print(f"agent-harness-gate: {exc}", file=sys.stderr)
        return 2

    if args.command == "status":
        print("\n".join(_status_lines(config)))
        return 0

    try:
        import anyio
        import mcp  # noqa: F401
    except ImportError:
        print(
            "agent-harness-gate requires the optional 'mcp' Python SDK.\n"
            'from this checkout install with: python -m pip install -e ".[mcp]"',
            file=sys.stderr,
        )
        return 1

    if args.command == "warm":
        names = args.servers or sorted(config.servers)
        unknown = [name for name in names if name not in config.servers]
        if unknown:
            print(f"agent-harness-gate: unknown servers: {', '.join(unknown)}", file=sys.stderr)
            return 2
        outcomes = anyio.run(warm, config, names)
        for ok, message in outcomes:
            print(message if ok else f"FAILED: {message}")
        return 0 if all(ok for ok, _ in outcomes) else 1

    anyio.run(serve, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
