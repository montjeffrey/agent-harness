"""Tests for `harness.mcp_gate`, the on-demand MCP gate.

The gate's whole promise is about processes: a gated server costs nothing
until one of its tools is called, and goes back to costing nothing once it has
sat idle. So the lifecycle tests below check real child processes (by PID)
started from `tests/fixtures/fake_mcp_server.py`, not the gate's own flags.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the optional `mcp` SDK is not installed")

import anyio

from harness import mcp_gate

CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
FAKE_SERVER = CHECKOUT_ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


def _pid_alive(pid: int) -> bool:
    """True while `pid` is a running process (stdlib only, Windows and POSIX)."""
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _write_config(path: Path, servers: dict, idle: float = 600.0) -> Path:
    path.write_text(
        json.dumps({"idle_timeout_seconds": idle, "servers": servers}), encoding="utf-8"
    )
    return path


@pytest.fixture()
def pid_file(tmp_path: Path) -> Path:
    return tmp_path / "fake.pid"


@pytest.fixture()
def fake_server_entry(pid_file: Path) -> dict:
    return {
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "env": {"FAKE_MCP_PID_FILE": str(pid_file)},
        "description": "fake test server",
    }


@pytest.fixture()
def config(tmp_path: Path, fake_server_entry: dict) -> mcp_gate.GateConfig:
    path = _write_config(tmp_path / "gate.json", {"fake": fake_server_entry})
    return mcp_gate.load_config(path, cache_dir=tmp_path / "cache")


# --------------------------------------------------------------------- config


def test_load_config_reads_servers_and_defaults(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "gate.json",
        {
            "alpha": {"command": "node", "args": ["a.js"]},
            "beta": {"command": "npx", "idle_timeout_seconds": 30},
        },
        idle=120,
    )
    cfg = mcp_gate.load_config(path, cache_dir=tmp_path / "cache")

    assert sorted(cfg.servers) == ["alpha", "beta"]
    assert cfg.servers["alpha"].args == ("a.js",)
    assert cfg.servers["alpha"].idle_timeout_seconds == 120
    assert cfg.servers["beta"].args == ()
    assert cfg.servers["beta"].idle_timeout_seconds == 30


@pytest.mark.parametrize(
    ("servers", "fragment"),
    [
        ({"Bad_Name": {"command": "x"}}, "Bad_Name"),
        ({"a__b": {"command": "x"}}, "a__b"),
        ({"ok": {"args": []}}, "command"),
        ({"ok": {"command": "x", "args": "not-a-list"}}, "args"),
    ],
)
def test_load_config_rejects_invalid_entries(tmp_path: Path, servers: dict, fragment: str) -> None:
    path = _write_config(tmp_path / "gate.json", servers)
    with pytest.raises(mcp_gate.GateConfigError, match=fragment):
        mcp_gate.load_config(path, cache_dir=tmp_path / "cache")


def test_missing_config_is_an_empty_gate(tmp_path: Path) -> None:
    cfg = mcp_gate.load_config(tmp_path / "absent.json", cache_dir=tmp_path / "cache")
    assert cfg.servers == {}


# ---------------------------------------------------------------- tool naming


def test_public_names_round_trip_and_keep_tool_separators() -> None:
    public = mcp_gate.public_name("chrome-devtools", "take__snapshot")
    assert public == "chrome-devtools__take__snapshot"
    assert mcp_gate.split_public_name(public) == ("chrome-devtools", "take__snapshot")
    assert mcp_gate.split_public_name("gate_status") is None


# ------------------------------------------------------------- manifest cache


def test_manifest_cache_round_trips_and_invalidates_on_spec_change(
    config: mcp_gate.GateConfig,
) -> None:
    spec = config.servers["fake"]
    cache = mcp_gate.ManifestCache(config.cache_dir)
    tools = [{"name": "echo", "description": "d", "inputSchema": {"type": "object"}}]

    assert cache.load(spec) is None
    cache.store(spec, tools)
    assert cache.load(spec) == tools

    changed = spec.__class__(**{**spec.__dict__, "args": (*spec.args, "--extra")})
    assert cache.load(changed) is None


def test_corrupt_manifest_is_treated_as_missing(config: mcp_gate.GateConfig) -> None:
    spec = config.servers["fake"]
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    (config.cache_dir / "fake.json").write_text("{not json", encoding="utf-8")
    assert mcp_gate.ManifestCache(config.cache_dir).load(spec) is None


# ------------------------------------------------------------------ lifecycle


@pytest.mark.anyio
async def test_cached_tools_are_listed_without_starting_anything(
    config: mcp_gate.GateConfig, pid_file: Path
) -> None:
    mcp_gate.ManifestCache(config.cache_dir).store(
        config.servers["fake"],
        [{"name": "echo", "description": "Return the text.", "inputSchema": {"type": "object"}}],
    )
    async with mcp_gate.Gate.running(config) as gate:
        names = {tool.name for tool in gate.list_tools()}

    assert "fake__echo" in names
    assert {"gate_status", "gate_open", "gate_close"} <= names
    assert not pid_file.exists(), "listing tools must not start the downstream server"


@pytest.mark.anyio
async def test_first_call_starts_server_and_caches_its_tools(
    config: mcp_gate.GateConfig, pid_file: Path
) -> None:
    async with mcp_gate.Gate.running(config) as gate:
        assert not pid_file.exists()
        result = await gate.call_tool("fake__echo", {"text": "hi"})
        assert not result.isError
        assert result.content[0].text == "echo:hi"
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert _pid_alive(pid)

    cached = mcp_gate.ManifestCache(config.cache_dir).load(config.servers["fake"])
    assert cached is not None
    assert {tool["name"] for tool in cached} == {"echo", "pid"}
    assert not _pid_alive(pid), "leaving the gate must stop every downstream"


@pytest.mark.anyio
async def test_idle_server_is_stopped_and_restarts_on_next_call(
    tmp_path: Path, fake_server_entry: dict, pid_file: Path
) -> None:
    path = _write_config(tmp_path / "gate.json", {"fake": fake_server_entry}, idle=0.3)
    cfg = mcp_gate.load_config(path, cache_dir=tmp_path / "cache")

    async with mcp_gate.Gate.running(cfg, reap_interval=0.1) as gate:
        await gate.call_tool("fake__echo", {"text": "one"})
        first = int(pid_file.read_text(encoding="utf-8"))

        deadline = time.monotonic() + 15
        while _pid_alive(first) and time.monotonic() < deadline:
            await anyio.sleep(0.1)
        assert not _pid_alive(first), "an idle server must be stopped"
        assert gate.status()[0]["state"] == "asleep"

        result = await gate.call_tool("fake__echo", {"text": "two"})
        assert result.content[0].text == "echo:two"
        second = int(pid_file.read_text(encoding="utf-8"))
        assert second != first and _pid_alive(second)


@pytest.mark.anyio
async def test_open_and_close_by_hand(config: mcp_gate.GateConfig, pid_file: Path) -> None:
    async with mcp_gate.Gate.running(config) as gate:
        message = await gate.open_server("fake")
        assert "2 tools" in message
        pid = int(pid_file.read_text(encoding="utf-8"))
        assert gate.status()[0]["state"] == "awake"

        await gate.close_server("fake")
        assert gate.status()[0]["state"] == "asleep"
        assert not _pid_alive(pid)


@pytest.mark.anyio
async def test_unknown_tool_and_broken_server_return_errors_not_crashes(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "gate.json",
        {"broken": {"command": str(tmp_path / "does-not-exist.exe"), "args": []}},
    )
    cfg = mcp_gate.load_config(path, cache_dir=tmp_path / "cache")

    async with mcp_gate.Gate.running(cfg) as gate:
        unknown = await gate.call_tool("nope__tool", {})
        assert unknown.isError and "nope" in unknown.content[0].text

        broken = await gate.call_tool("broken__anything", {})
        assert broken.isError and "broken" in broken.content[0].text
        assert gate.status()[0]["state"] == "asleep"


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ({"command": "x", "arg": ["typo"]}, "arg"),
        ({"command": "x", "idle_timeout_seconds": float("nan")}, "idle_timeout_seconds"),
    ],
)
def test_load_config_rejects_typos_and_non_finite_numbers(
    tmp_path: Path, entry: dict, fragment: str
) -> None:
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({"servers": {"ok": entry}}), encoding="utf-8")
    with pytest.raises(mcp_gate.GateConfigError, match=fragment):
        mcp_gate.load_config(path, cache_dir=tmp_path / "cache")


def test_relative_cwd_resolves_against_the_config_folder(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "gate.json", {"ok": {"command": "x", "cwd": "tools"}})
    cfg = mcp_gate.load_config(path, cache_dir=tmp_path / "cache")
    assert Path(cfg.servers["ok"].cwd) == tmp_path / "tools"


def test_only_transport_failures_count_as_a_dead_server() -> None:
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED, INVALID_PARAMS, ErrorData

    assert mcp_gate._is_transport_failure(anyio.ClosedResourceError())
    assert mcp_gate._is_transport_failure(anyio.BrokenResourceError())
    assert mcp_gate._is_transport_failure(McpError(ErrorData(code=CONNECTION_CLOSED, message="x")))
    assert not mcp_gate._is_transport_failure(McpError(ErrorData(code=INVALID_PARAMS, message="x")))
    assert not mcp_gate._is_transport_failure(ValueError("x"))


@pytest.mark.anyio
async def test_a_bad_cached_record_does_not_break_tools_list(config: mcp_gate.GateConfig) -> None:
    mcp_gate.ManifestCache(config.cache_dir).store(
        config.servers["fake"],
        [
            {"name": "good", "description": "", "inputSchema": {"type": "object"}},
            {"name": "bad", "inputSchema": {"type": "object"}, "annotations": {"title": 5}},
        ],
    )
    async with mcp_gate.Gate.running(config) as gate:
        names = {tool.name for tool in gate.list_tools()}
    assert "fake__good" in names and "fake__bad" not in names
    assert "gate_status" in names


@pytest.mark.anyio
async def test_a_killed_server_is_restarted_on_the_next_call(
    config: mcp_gate.GateConfig, pid_file: Path
) -> None:
    async with mcp_gate.Gate.running(config) as gate:
        await gate.call_tool("fake__echo", {"text": "one"})
        first = int(pid_file.read_text(encoding="utf-8"))
        if sys.platform == "win32":
            await anyio.run_process(["taskkill", "/F", "/PID", str(first)], check=False)
        else:
            os.kill(first, 9)
        deadline = time.monotonic() + 10
        while _pid_alive(first) and time.monotonic() < deadline:
            await anyio.sleep(0.1)

        result = await gate.call_tool("fake__echo", {"text": "two"})
        assert not result.isError, result.content[0].text
        assert result.content[0].text == "echo:two"
        assert int(pid_file.read_text(encoding="utf-8")) != first


@pytest.mark.anyio
async def test_a_failed_start_fails_fast_until_the_backoff_passes(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "gate.json",
        {"broken": {"command": str(tmp_path / "does-not-exist.exe")}},
    )
    cfg = mcp_gate.load_config(path, cache_dir=tmp_path / "cache")
    async with mcp_gate.Gate.running(cfg) as gate:
        first = await gate.call_tool("broken__anything", {})
        assert first.isError
        started = time.monotonic()
        second = await gate.call_tool("broken__anything", {})
        assert second.isError and "recently" in second.content[0].text
        assert time.monotonic() - started < 1.0


def test_warm_exits_non_zero_when_a_server_fails(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "gate.json",
        {"broken": {"command": str(tmp_path / "does-not-exist.exe")}},
    )
    code = mcp_gate.main(["--config", str(path), "--cache-dir", str(tmp_path / "c"), "warm"])
    assert code != 0


# ------------------------------------------------------------ stdio end-to-end


@pytest.mark.anyio
async def test_gate_serves_proxied_tools_over_stdio(
    config: mcp_gate.GateConfig, tmp_path: Path
) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config_path = tmp_path / "gate.json"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "harness.mcp_gate",
            "serve",
            "--config",
            str(config_path),
            "--cache-dir",
            str(config.cache_dir),
        ],
        env={**os.environ, "PYTHONPATH": str(CHECKOUT_ROOT)},
        cwd=str(CHECKOUT_ROOT),
    )
    with anyio.fail_after(120):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = {tool.name for tool in (await session.list_tools()).tools}
            assert "gate_open" in listed

            result = await session.call_tool("fake__echo", {"text": "wire"})
            assert result.content[0].text == "echo:wire"

            relisted = {tool.name for tool in (await session.list_tools()).tools}
            assert {"fake__echo", "fake__pid"} <= relisted
