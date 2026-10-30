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
* **Pressure, accounting, verified cleanup.** Below the machine's free-RAM
  watermarks idle servers are stopped sooner (or at once); every wake, call,
  and stop is recorded with the downstream tree's memory; a stop is checked
  until the whole tree is gone (see `harness.gate_runtime`).

Configuration lives in `~/.agent-harness/gate.json` (or the path named by
`AGENT_HARNESS_GATE_CONFIG`; see `harness.gate_config`); each entry is the same
`command`/`args`/`env` triple an agent's own MCP config would hold. The server
works with any MCP client -- Claude Code, Codex, Gemini -- because it is an
ordinary stdio server.

Like `harness.mcp_server`, importing this module never requires the optional
`mcp` SDK: every `mcp`/`anyio` import is local, so `main` can explain a missing
extra instead of crashing on import.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from harness import gate_runtime
from harness.gate_config import (
    CONFIG_ENV,
    CONTROL_TOOLS,
    DEFAULT_CALL_TIMEOUT_SECONDS,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MIN_IDLE_SECONDS,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    SEPARATOR,
    GateConfig,
    GateConfigError,
    ManifestCache,
    ServerSpec,
    default_config_path,
    load_config,
    public_name,
    split_public_name,
)

# Config lives in `harness.gate_config`; these names are re-exported so
# existing callers of `harness.mcp_gate` keep working.
__all__ = [
    "CONFIG_ENV",
    "CONTROL_TOOLS",
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_MIN_IDLE_SECONDS",
    "DEFAULT_STARTUP_TIMEOUT_SECONDS",
    "SEPARATOR",
    "Gate",
    "GateConfig",
    "GateConfigError",
    "GateError",
    "ManifestCache",
    "ServerSpec",
    "build_server",
    "default_config_path",
    "load_config",
    "main",
    "public_name",
    "serve",
    "split_public_name",
    "warm",
]

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


class GateError(RuntimeError):
    """A gate operation was asked for something that does not exist."""


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

    `account` follows each awake period, from wake to verified stop: the root
    process the gate launched, its tree, and the calls it served.
    """

    def __init__(
        self,
        spec: ServerSpec,
        cache: ManifestCache,
        task_group: Any,
        runtime: gate_runtime.GateRuntime,
    ) -> None:
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
        self.account = gate_runtime.ServerAccount(spec.name, runtime)

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

    async def wake(self, *, force: bool = False, reason: str = "call") -> bool:
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
            await self.account.finish("dead")  # a runner that unwound on its own
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
            started = time.monotonic()
            try:
                with anyio.fail_after(timeout):
                    await self._task_group.start(self._run)
            except Exception as exc:
                timed_out = isinstance(exc, TimeoutError)
                self._failed_at = time.monotonic()
                self.last_error = (
                    f"did not start within {timeout:g}s ({self.spec.command})"
                    if timed_out
                    else _describe(exc)
                )
                self.account.start_failed(started, reason, self.last_error)
                if timed_out:
                    raise GateError(f"gated server '{self.spec.name}' {self.last_error}") from None
                raise
            self._failed_at = None
            self.last_error = None
            self.last_used = time.monotonic()
            await self.account.begin(reason, started)
            return self.tools != before

    async def refresh_tree(self) -> None:
        """Re-record the awake tree for crash cleanup; skipped while a wake or stop runs."""
        if self._lock.locked() or not self.awake:
            return
        async with self._lock:
            if self.awake:
                await self.account.refresh()

    async def finish_if_dead(self) -> None:
        """Account for a server whose process exited without being asked to stop."""
        if not self.stopped or self.account.woke_at is None or self._lock.locked():
            return
        async with self._lock:
            if self.stopped:
                await self.account.finish("dead")

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
            async with AsyncExitStack() as stack:
                read, write = await self.account.spawn(stack, stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
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

    def is_idle(self, now: float, idle: float | None = None) -> bool:
        """No call in flight and unused for `idle` seconds (default: the configured timeout)."""
        limit = self.spec.idle_timeout_seconds if idle is None else idle
        return self.inflight == 0 and now - self.last_used >= limit

    async def sleep(
        self, *, reason: str = "manual", only_if_idle: bool = False, idle: float | None = None
    ) -> None:
        """Stop the server (process tree included). A no-op when already asleep.

        `only_if_idle` re-checks idleness (against `idle`) under the lock: the
        reaper decides before it gets the lock, and a call may have started in
        between. The tree is sampled just before the stop, so children spawned
        after the wake are verified too.
        """
        import anyio

        async with self._lock:
            if self._session is None:
                await self.account.finish("dead")
                return
            if only_if_idle and not self.is_idle(time.monotonic(), idle):
                return
            tree, tree_mb = await self.account.sample(self.account.tree)
            self._stop.set()
            with anyio.move_on_after(STOP_WAIT_SECONDS):
                await self._stopped.wait()
            if self._session is not None:
                self.last_error = f"did not stop within {STOP_WAIT_SECONDS:g}s"
            await self.account.finish(reason, tree, tree_mb)


def _default_reap_interval(config: GateConfig) -> float:
    timeouts = [spec.idle_timeout_seconds for spec in config.servers.values()]
    if timeouts:
        # Under low memory the shorter `min_idle_seconds` applies instead.
        timeouts.append(config.min_idle_seconds)
    smallest = min(timeouts, default=MAX_REAP_INTERVAL_SECONDS * 2)
    return max(MIN_REAP_INTERVAL_SECONDS, min(MAX_REAP_INTERVAL_SECONDS, smallest / 2))


class Gate:
    """All gated servers for one agent session."""

    def __init__(
        self,
        config: GateConfig,
        task_group: Any,
        *,
        reap_interval: float,
        runtime: gate_runtime.GateRuntime,
    ) -> None:
        cache = ManifestCache(config.cache_dir)
        self.config = config
        self._reap_interval = reap_interval
        self._runtime = runtime
        self._tools_changed = False
        self._warned_long_names: set[str] = set()
        self._downstreams = {
            name: _Downstream(spec, cache, task_group, runtime)
            for name, spec in sorted(config.servers.items())
        }

    @classmethod
    @asynccontextmanager
    async def running(
        cls,
        config: GateConfig,
        *,
        reap_interval: float | None = None,
        memory_sampler: Callable[[], int | None] | None = None,
        total_memory: Any = gate_runtime.UNSET,
        ledger: Any = None,
        registry_dir: Path | None = None,
    ) -> AsyncIterator[Gate]:
        """Run a gate; on exit every awake server is stopped.

        Starting first sweeps processes that a crashed gate left running. The
        keywords after `reap_interval` are seams (defaults in parentheses): a
        free-RAM sampler (`procinfo.available_memory_bytes`), total RAM for
        the tier (what `load_config` saw), a `GateLedger` (the telemetry
        folder), and the PID registry folder (`~/.agent-harness/gate-state`).
        """
        import anyio

        interval = reap_interval if reap_interval is not None else _default_reap_interval(config)
        runtime = await gate_runtime.GateRuntime.start(
            config,
            memory_sampler=memory_sampler,
            total_memory=total_memory,
            ledger=ledger,
            registry_dir=registry_dir,
        )
        async with anyio.create_task_group() as task_group:
            gate = cls(config, task_group, reap_interval=interval, runtime=runtime)
            task_group.start_soon(gate._reap_forever)
            try:
                yield gate
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        await gate.close_all()
                    finally:
                        runtime.close()
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
            try:
                await self.reap(time.monotonic())
            except Exception as exc:  # noqa: BLE001 -- one bad tick must not end the gate
                gate_runtime.warn(f"reaper tick failed: {_describe(exc)}")

    async def reap(self, now: float) -> None:
        """Stop every awake server idle past its effective timeout.

        Free RAM is sampled first: below the low watermark the timeout drops
        to `min_idle_seconds`, below the critical one to zero, so every awake
        server with no call in flight stops now (reason "pressure").
        """
        await self._runtime.sample_pressure()
        for downstream in self._downstreams.values():
            if not downstream.awake:
                await downstream.finish_if_dead()
                continue
            idle = self._runtime.effective_idle(downstream.spec.idle_timeout_seconds)
            if downstream.is_idle(now, idle):
                reason = "idle" if downstream.is_idle(now) else "pressure"
                await downstream.sleep(reason=reason, only_if_idle=True, idle=idle)
            else:
                await downstream.refresh_tree()

    async def open_server(self, server: str, *, reason: str = "open") -> str:
        downstream = self._get(server)
        if await downstream.wake(force=True, reason=reason):
            self._tools_changed = True
        count = len(downstream.tools or [])
        idle = self._runtime.effective_idle(downstream.spec.idle_timeout_seconds)
        return f"'{server}' is awake with {count} tools; it goes back to sleep after {idle:g}s idle."

    async def close_server(self, server: str) -> str:
        downstream = self._get(server)
        await downstream.sleep(reason="manual")
        if not downstream.stopped:
            raise GateError(f"'{server}' {downstream.last_error}; it is still stopping")
        return f"'{server}' is asleep."

    async def close_all(self) -> None:
        for downstream in self._downstreams.values():
            await downstream.sleep(reason="exit")

    def pop_tools_changed(self) -> bool:
        """True once after any wake changed an advertised tool list."""
        changed, self._tools_changed = self._tools_changed, False
        return changed

    def note_client(self, session: Any) -> None:
        """Label accounting records with the MCP client's name (best effort)."""
        info = getattr(getattr(session, "client_params", None), "clientInfo", None)
        self._runtime.set_client(getattr(info, "name", None))

    def status(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "server": name,
                "state": "awake" if downstream.awake else "asleep",
                "tools": None if downstream.tools is None else len(downstream.tools),
                "idle_seconds": round(now - downstream.last_used, 1) if downstream.awake else None,
                "idle_timeout_seconds": downstream.spec.idle_timeout_seconds,
                "effective_idle_seconds": self._runtime.effective_idle(
                    downstream.spec.idle_timeout_seconds
                ),
                "tree_mb": downstream.account.tree_mb if downstream.awake else None,
                "last_error": downstream.last_error,
            }
            for name, downstream in self._downstreams.items()
        ]

    async def status_report(self) -> dict:
        """`gate_status`: machine tier and pressure, then each server with fresh tree MB."""
        await self._runtime.sample_pressure()
        for downstream in self._downstreams.values():
            if downstream.awake:
                await downstream.account.sample_mb()
        return {"summary": self._runtime.summary(), "servers": self.status()}

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
                return _text_result(json.dumps(await self.status_report(), indent=2))
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
        """Forward one call and account for it (`gate_call`) once it has an outcome."""
        started = time.monotonic()
        downstream.inflight += 1
        try:
            result, error_kind = await self._dispatch(downstream, tool, arguments)
        finally:
            downstream.inflight -= 1
            downstream.last_used = time.monotonic()
        if error_kind is None and getattr(result, "isError", False):
            error_kind = "server_error"
        self._runtime.record(
            "gate_call", downstream.spec.name,
            tool=tool,
            duration_ms=gate_runtime.ms_since(started),
            ok=error_kind is None,
            error_kind=error_kind,
        )
        return result

    async def _dispatch(
        self, downstream: _Downstream, tool: str, arguments: dict
    ) -> tuple[Any, str | None]:
        """Wake if needed, check the server answers, forward; restart once if it died.

        Returns the result and its error kind (None when the call went through).
        """
        import anyio

        server = downstream.spec.name
        for _attempt in (1, 2):
            try:
                if await downstream.wake():
                    self._tools_changed = True
            except GateError as exc:
                return _error_result(str(exc)), "start_failed"
            except Exception as exc:  # noqa: BLE001 -- reported to the caller, not raised
                message = f"gated server '{server}' failed to start: {_describe(exc)}"
                return _error_result(message), "start_failed"
            if tool not in {record["name"] for record in downstream.tools or []}:
                return _error_result(f"gated server '{server}' has no tool '{tool}'"), "not_found"
            # A dead server is safe to restart: nothing was sent to it yet.
            if not await downstream.ping():
                downstream.last_error = "stopped answering; restarted"
                await downstream.sleep(reason="dead")
                continue
            timeout = downstream.spec.call_timeout_seconds
            downstream.account.calls += 1
            try:
                with anyio.fail_after(timeout):
                    return await downstream.session.call_tool(tool, arguments), None
            except TimeoutError:
                downstream.last_error = f"call to '{tool}' exceeded {timeout:g}s"
                await downstream.sleep(reason="timeout")
                message = (
                    f"gated server '{server}' did not answer '{tool}' within {timeout:g}s "
                    "and was stopped"
                )
                return _error_result(message), "timeout"
            except Exception as exc:  # noqa: BLE001 -- classified below
                if not _is_transport_failure(exc):
                    message = f"gated server '{server}' error: {_describe(exc)}"
                    return _error_result(message), "server_error"
                downstream.last_error = _describe(exc)
                await downstream.sleep(reason="transport")
                # The request may already have run, so it is not retried.
                message = f"gated server '{server}' failed: {downstream.last_error}"
                return _error_result(message), "transport"
        message = f"gated server '{server}' is not answering after a restart"
        return _error_result(message), "transport"


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
        gate_runtime.safely("client name", None, lambda: gate.note_client(server.request_context.session))
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
            try:
                outcomes.append((True, await gate.open_server(name, reason="warm")))
            except Exception as exc:  # noqa: BLE001 -- reported per server, as gate_open would
                outcomes.append((False, str(exc) if isinstance(exc, GateError) else _describe(exc)))
            await gate.close_server(name)
    return outcomes


def main(argv: list[str] | None = None) -> int:
    """`agent-harness-gate`; the command line lives in `harness.gate_cli`."""
    from harness.gate_cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
