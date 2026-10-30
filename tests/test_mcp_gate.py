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


# ===================================================== phase 1: usage-aware gate
#
# Pressure, accounting, verified cleanup, and the crash sweep. Like the
# lifecycle tests above, these drive real fake-server processes; the only
# injected input is the free-RAM reading, so a pressure level can be forced.

GIB = 1024**3


def _records(directory: Path, event: str | None = None) -> list[dict]:
    from harness.gate_accounting import iter_gate_events

    return [r for r in iter_gate_events(directory=directory) if event in (None, r["event"])]


def _ledger(tmp_path: Path):
    from harness.gate_accounting import GateLedger

    return GateLedger(session="test-session", directory=tmp_path / "ledger")


async def _wait_until_gone(pid: int, seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while _pid_alive(pid) and time.monotonic() < deadline:
        await anyio.sleep(0.1)


def _sleeper():
    """A long-sleeping process this test starts (and may therefore kill)."""
    import subprocess

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_identity(pid: int):
    from harness import procinfo

    deadline = time.monotonic() + 10
    identity = procinfo.process_identity(pid)
    while identity is None and time.monotonic() < deadline:
        time.sleep(0.05)
        identity = procinfo.process_identity(pid)
    assert identity is not None, f"pid {pid} never became readable"
    return identity


def _gate_config(tmp_path: Path, entry: dict, **top: object) -> mcp_gate.GateConfig:
    path = tmp_path / "gate.json"
    path.write_text(json.dumps({**top, "servers": {"fake": entry}}), encoding="utf-8")
    return mcp_gate.load_config(path, cache_dir=tmp_path / "cache", total_memory=32 * GIB)


# --------------------------------------------------------------- config keys


def test_gate_wide_keys_are_parsed(tmp_path: Path) -> None:
    cfg = _gate_config(
        tmp_path,
        {"command": "x"},
        min_idle_seconds=30,
        low_watermark_mb=4096,
        critical_watermark_mb=1024,
        accounting=False,
    )
    assert cfg.min_idle_seconds == 30
    assert cfg.low_watermark_bytes == 4096 * 1024 * 1024
    assert cfg.critical_watermark_bytes == 1024 * 1024 * 1024
    assert cfg.accounting is False
    assert cfg.tier.name == "32GB"


def test_defaults_for_gate_wide_keys(tmp_path: Path) -> None:
    cfg = _gate_config(tmp_path, {"command": "x"})
    assert cfg.min_idle_seconds == 60
    assert cfg.low_watermark_bytes is None and cfg.critical_watermark_bytes is None
    assert cfg.accounting is True


@pytest.mark.parametrize(
    ("top", "fragment"),
    [
        ({"idle_timeout": 5}, "idle_timeout"),
        ({"accounting": "yes"}, "accounting"),
        ({"min_idle_seconds": 0}, "min_idle_seconds"),
        ({"low_watermark_mb": 100, "critical_watermark_mb": 200}, "critical_watermark_mb"),
    ],
)
def test_bad_gate_wide_keys_are_rejected(tmp_path: Path, top: dict, fragment: str) -> None:
    with pytest.raises(mcp_gate.GateConfigError, match=fragment):
        _gate_config(tmp_path, {"command": "x"}, **top)


def test_tier_supplies_the_idle_default_and_explicit_config_wins(tmp_path: Path) -> None:
    path = tmp_path / "gate.json"
    servers = {"a": {"command": "x"}, "b": {"command": "x", "idle_timeout_seconds": 42}}
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")

    small = mcp_gate.load_config(path, cache_dir=tmp_path / "c", total_memory=16 * GIB)
    large = mcp_gate.load_config(path, cache_dir=tmp_path / "c", total_memory=64 * GIB)
    unknown = mcp_gate.load_config(path, cache_dir=tmp_path / "c", total_memory=None)

    assert small.servers["a"].idle_timeout_seconds == 300
    assert large.servers["a"].idle_timeout_seconds == 900
    assert unknown.servers["a"].idle_timeout_seconds == 600
    assert small.servers["b"].idle_timeout_seconds == 42

    path.write_text(json.dumps({"idle_timeout_seconds": 77, "servers": servers}), encoding="utf-8")
    top = mcp_gate.load_config(path, cache_dir=tmp_path / "c", total_memory=16 * GIB)
    assert top.servers["a"].idle_timeout_seconds == 77


# ------------------------------------------------------------------ pressure


@pytest.mark.anyio
async def test_low_memory_shortens_the_idle_timeout(
    tmp_path: Path, fake_server_entry: dict, pid_file: Path
) -> None:
    cfg = _gate_config(tmp_path, fake_server_entry, idle_timeout_seconds=600, min_idle_seconds=0.3)
    async with mcp_gate.Gate.running(
        cfg,
        reap_interval=0.1,
        memory_sampler=lambda: 4 * GIB,  # between the 32 GB tier's 2.5 and 5 GiB marks
        total_memory=32 * GIB,
        ledger=_ledger(tmp_path),
        registry_dir=tmp_path / "state",
    ) as gate:
        await gate.call_tool("fake__echo", {"text": "x"})
        pid = int(pid_file.read_text(encoding="utf-8"))
        await _wait_until_gone(pid)
        assert not _pid_alive(pid), "under low memory the 0.3 s minimum idle applies"

    sleeps = _records(tmp_path / "ledger", "gate_sleep")
    assert sleeps[0]["reason"] == "pressure"
    assert sleeps[0]["pressure"] == "low"
    assert sleeps[0]["available_mb"] == pytest.approx(4096)


@pytest.mark.anyio
async def test_critical_memory_stops_an_idle_server_at_once(
    tmp_path: Path, fake_server_entry: dict, pid_file: Path
) -> None:
    free = {"bytes": 16 * GIB}
    cfg = _gate_config(tmp_path, fake_server_entry, idle_timeout_seconds=600, min_idle_seconds=600)
    async with mcp_gate.Gate.running(
        cfg,
        reap_interval=0.1,
        memory_sampler=lambda: free["bytes"],
        total_memory=32 * GIB,
        ledger=_ledger(tmp_path),
        registry_dir=tmp_path / "state",
    ) as gate:
        await gate.call_tool("fake__echo", {"text": "x"})
        pid = int(pid_file.read_text(encoding="utf-8"))
        await anyio.sleep(0.5)
        assert _pid_alive(pid), "normal pressure keeps the server for its full idle timeout"

        free["bytes"] = 1 * GIB  # below the 2.5 GiB critical mark
        await _wait_until_gone(pid)
        assert not _pid_alive(pid)
        assert gate.status()[0]["state"] == "asleep"

    sleeps = _records(tmp_path / "ledger", "gate_sleep")
    assert (sleeps[0]["reason"], sleeps[0]["pressure"]) == ("pressure", "critical")


# ---------------------------------------------------------------- accounting


@pytest.mark.anyio
async def test_wake_call_and_sleep_are_accounted(
    config: mcp_gate.GateConfig, tmp_path: Path, pid_file: Path
) -> None:
    async with mcp_gate.Gate.running(
        config, ledger=_ledger(tmp_path), registry_dir=tmp_path / "state"
    ) as gate:
        await gate.call_tool("fake__echo", {"text": "a"})
        await gate.call_tool("fake__nope", {})
        pid = int(pid_file.read_text(encoding="utf-8"))
        await gate.close_server("fake")

    wake, *_ = _records(tmp_path / "ledger", "gate_wake")
    assert (wake["server"], wake["reason"], wake["session"]) == ("fake", "call", "test-session")
    assert isinstance(wake["startup_ms"], int) and 0 < wake["startup_ms"] < 120_000
    assert wake["root_pid"] == pid
    assert wake["tree_mb"] is not None and wake["tree_mb"] > 1

    calls = _records(tmp_path / "ledger", "gate_call")
    assert [(c["tool"], c["ok"], c["error_kind"]) for c in calls] == [
        ("echo", True, None),
        ("nope", False, "not_found"),
    ]
    assert all(isinstance(c["duration_ms"], int) for c in calls)

    sleep, *_ = _records(tmp_path / "ledger", "gate_sleep")
    assert (sleep["reason"], sleep["calls"], sleep["leaked_pids"]) == ("manual", 1, [])
    assert pid in sleep["tree_pids"]
    assert sleep["awake_s"] > 0 and sleep["tree_mb"] > 1
    assert not list((tmp_path / "state").glob("owner-*")), "a clean exit removes its registry"

    from datetime import UTC, datetime

    from harness import gate_report

    rows = gate_report.summarize(_records(tmp_path / "ledger"), days=1, now=datetime.now(UTC))
    assert (rows[0].server, rows[0].wakes, rows[0].calls) == ("fake", 1, 2)


@pytest.mark.anyio
async def test_accounting_false_writes_no_records(tmp_path: Path, fake_server_entry: dict) -> None:
    from harness import telemetry_store

    cfg = _gate_config(tmp_path, fake_server_entry, accounting=False)
    async with mcp_gate.Gate.running(cfg, registry_dir=tmp_path / "state") as gate:
        result = await gate.call_tool("fake__echo", {"text": "a"})
        assert not result.isError
    assert not list(telemetry_store.telemetry_dir().glob("gate-*.jsonl"))


@pytest.mark.anyio
async def test_gate_status_reports_tier_pressure_and_tree(
    config: mcp_gate.GateConfig, tmp_path: Path
) -> None:
    async with mcp_gate.Gate.running(
        config,
        memory_sampler=lambda: 3 * GIB,
        total_memory=16 * GIB,
        ledger=_ledger(tmp_path),
        registry_dir=tmp_path / "state",
    ) as gate:
        await gate.open_server("fake")
        result = await gate.call_tool("gate_status", {})
    payload = json.loads(result.content[0].text)
    assert payload["summary"]["tier"] == "16GB"
    assert payload["summary"]["pressure"] == "normal"
    assert payload["summary"]["available_mb"] == pytest.approx(3072)
    (row,) = payload["servers"]
    assert row["state"] == "awake" and row["effective_idle_seconds"] == 600
    assert row["tree_mb"] > 1


# ----------------------------------------------------------- verified cleanup


@pytest.mark.anyio
async def test_stop_cleans_up_and_accounts_for_a_grandchild(
    tmp_path: Path, fake_server_entry: dict, pid_file: Path
) -> None:
    from harness import procinfo

    child_file = tmp_path / "grandchild.pid"
    env = {**fake_server_entry["env"], "FAKE_MCP_SPAWN_CHILD_PID_FILE": str(child_file)}
    cfg = _gate_config(tmp_path, {**fake_server_entry, "env": env})
    grandchild = None
    try:
        async with mcp_gate.Gate.running(
            cfg, ledger=_ledger(tmp_path), registry_dir=tmp_path / "state"
        ) as gate:
            await gate.open_server("fake")
            grandchild = _wait_for_identity(int(child_file.read_text(encoding="utf-8")))
            await gate.close_server("fake")
            # Identity-based, like the gate's own check (a zombie counts as
            # gone), and polled: a POSIX SIGKILL returns before the exit lands.
            deadline = time.monotonic() + 5.0
            while procinfo.is_same_process(grandchild) and time.monotonic() < deadline:
                await anyio.sleep(0.05)
            assert not procinfo.is_same_process(grandchild), (
                "the downstream's whole tree must be gone"
            )
    finally:
        # Identity-checked: only the grandchild this test's fixture started.
        if grandchild is not None and procinfo.is_same_process(grandchild):
            procinfo.terminate(grandchild)

    sleep, *_ = _records(tmp_path / "ledger", "gate_sleep")
    assert grandchild.pid in sleep["tree_pids"]
    assert int(pid_file.read_text(encoding="utf-8")) in sleep["tree_pids"]
    assert sleep["leaked_pids"] == []


@pytest.mark.anyio
async def test_cleanup_terminates_a_survivor_only_by_exact_identity() -> None:
    from harness import gate_runtime, procinfo

    survivor, stranger = _sleeper(), _sleeper()
    try:
        exact = _wait_for_identity(survivor.pid)
        real = _wait_for_identity(stranger.pid)
        forged = procinfo.ProcessIdentity(real.pid, real.create_time + 1.0, real.exe)

        reaped, leaked = await gate_runtime.verify_gone([exact, forged], wait=0.3)

        assert reaped == [survivor.pid] and leaked == []
        assert survivor.wait(timeout=10) is not None
        assert stranger.poll() is None, "a mismatched identity must never be terminated"
    finally:
        for proc in (survivor, stranger):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def _bare_account(config: mcp_gate.GateConfig, tmp_path: Path):
    """A `ServerAccount` mid-awake-period, with no real process behind it."""
    from harness import gate_runtime

    runtime = gate_runtime.GateRuntime(
        config,
        memory_sampler=lambda: None,
        total_memory=None,
        ledger=_ledger(tmp_path),
        registry=None,
    )
    account = gate_runtime.ServerAccount("fake", runtime)
    account.woke_at = time.monotonic() - 5.0
    return account


@pytest.mark.anyio
async def test_a_cancelled_stop_still_finishes_verified_cleanup(
    config: mcp_gate.GateConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import gate_runtime, procinfo

    account = _bare_account(config, tmp_path)
    survivor = procinfo.ProcessIdentity(4242, 1.0, None)
    verified: list = []

    async def slow_verify(identities, wait=gate_runtime.STOP_VERIFY_SECONDS):
        # Stands in for the real poll-then-terminate; no process is touched.
        await anyio.sleep(0.3)
        verified.append(list(identities))
        return [], [survivor.pid]

    monkeypatch.setattr(gate_runtime, "verify_gone", slow_verify)
    with anyio.move_on_after(0.05):  # the client cancels the request mid-verify
        await account.finish("idle", tree=(survivor,), tree_mb=10.0)

    assert verified == [[survivor]]
    (sleep,) = _records(tmp_path / "ledger", "gate_sleep")
    assert sleep["leaked_pids"] == [survivor.pid]


@pytest.mark.anyio
async def test_a_dead_servers_sleep_keeps_its_last_known_tree_size(
    config: mcp_gate.GateConfig, tmp_path: Path
) -> None:
    account = _bare_account(config, tmp_path)
    account.tree_mb = 96.5  # sampled at wake; the tree has exited since

    await account.finish("dead")

    (sleep,) = _records(tmp_path / "ledger", "gate_sleep")
    assert sleep["tree_mb"] == 96.5 and sleep["awake_s"] >= 5.0


@pytest.mark.anyio
async def test_start_sweeps_processes_left_by_a_crashed_gate(
    config: mcp_gate.GateConfig, tmp_path: Path
) -> None:
    from harness import gate_accounting, procinfo

    orphan = _sleeper()
    try:
        identity = _wait_for_identity(orphan.pid)
        me = procinfo.process_identity(os.getpid())
        # A gate that "crashed": its recorded identity names no live process.
        dead_owner = procinfo.ProcessIdentity(me.pid, me.create_time - 1000.0, me.exe)
        gate_accounting.PidRegistry(tmp_path / "state", dead_owner).track("fake", [identity])

        async with mcp_gate.Gate.running(
            config, ledger=_ledger(tmp_path), registry_dir=tmp_path / "state"
        ):
            assert orphan.wait(timeout=10) is not None
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=10)

    (sweep,) = _records(tmp_path / "ledger", "gate_sweep")
    assert sweep["server"] == "*" and sweep["pids"] == [orphan.pid]
    assert not list((tmp_path / "state").glob("owner-*"))


# -------------------------------------------------------------------- report


def _report_config(tmp_path: Path) -> Path:
    path = tmp_path / "gate.json"
    servers = {"fake": {"command": "x"}, "idle": {"command": "y"}}
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


def test_report_cli_summarizes_the_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from harness import gate_accounting, telemetry_store

    ledger = gate_accounting.GateLedger(session="s", directory=telemetry_store.telemetry_dir())
    ledger.record("gate_wake", "fake", reason="call", startup_ms=900, root_pid=1, tree_mb=50.0)
    ledger.record("gate_call", "fake", tool="echo", duration_ms=5, ok=True, error_kind=None)
    path = _report_config(tmp_path)
    args = ["--config", str(path), "--cache-dir", str(tmp_path / "c"), "report"]

    assert mcp_gate.main([*args, "--days", "7"]) == 0
    text = capsys.readouterr().out
    assert "fake" in text and "idle" in text and "remove it from gate.json" in text

    assert mcp_gate.main([*args, "--json"]) == 0
    servers = {row["server"]: row for row in json.loads(capsys.readouterr().out)["servers"]}
    assert servers["fake"]["calls"] == 1 and servers["idle"]["calls"] == 0


def test_report_cli_rejects_a_non_positive_window(tmp_path: Path) -> None:
    path = _report_config(tmp_path)
    with pytest.raises(SystemExit) as exc:
        mcp_gate.main(["--config", str(path), "report", "--days", "0"])
    assert exc.value.code == 2
