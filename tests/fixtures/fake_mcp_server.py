"""A minimal stdio MCP server the gate tests use as a downstream.

It records its own PID in the file named by `FAKE_MCP_PID_FILE` (when set) so
a test can prove the gate really started -- and really stopped -- a process,
rather than trusting the gate's own bookkeeping.

With `FAKE_MCP_SPAWN_CHILD_PID_FILE` set it also starts one long-sleeping
child process before serving and records that child's PID there, the way a
browser server spawns a browser: the gate's verified cleanup must find and
account for the whole tree, not just the process it launched.
"""

from __future__ import annotations

import os
import subprocess
import sys

from mcp.server.fastmcp import FastMCP

app = FastMCP("fake")


@app.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"echo:{text}"


@app.tool()
def pid() -> int:
    """Return this server's process id."""
    return os.getpid()


def _write_pid(path: str, value: int) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(value))


if __name__ == "__main__":
    child_pid_file = os.environ.get("FAKE_MCP_SPAWN_CHILD_PID_FILE")
    if child_pid_file:
        # DEVNULL handles: the child must not hold this server's MCP pipes
        # open, or the client would never see EOF when this server exits.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _write_pid(child_pid_file, child.pid)
    pid_file = os.environ.get("FAKE_MCP_PID_FILE")
    if pid_file:
        _write_pid(pid_file, os.getpid())
    app.run(transport="stdio")
