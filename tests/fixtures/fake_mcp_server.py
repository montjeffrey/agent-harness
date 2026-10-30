"""A minimal stdio MCP server the gate tests use as a downstream.

It records its own PID in the file named by `FAKE_MCP_PID_FILE` (when set) so
a test can prove the gate really started -- and really stopped -- a process,
rather than trusting the gate's own bookkeeping.
"""

from __future__ import annotations

import os

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


if __name__ == "__main__":
    pid_file = os.environ.get("FAKE_MCP_PID_FILE")
    if pid_file:
        with open(pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    app.run(transport="stdio")
