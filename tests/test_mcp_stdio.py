"""End-to-end latency guard for the stdio MCP transport.

This file exists because of a bug that **cannot** be reproduced in-process.
Inside `harness.mcp_server` running over stdio, every `subprocess.run(["git",
...])` blocked before git even started -- GIT_TRACE wrote nothing -- until the
call's own `timeout` fired, because the git child inherited the server's live
JSON-RPC stdin pipe. `memory_context` reaches two such calls through
`memory_graph.build_annotator` -> `graph_location.report_health`, so it cost a
flat 20 s per call; one live session hung for half an hour on it.

The same git commands took 0.03 s in-process, from the CLI, and from a plain
script launched with the same process-creation flags. Only a real server,
driven over a real stdio pipe, shows the failure -- hence this file. The cheap
deterministic guards on the fix itself (`stdin=subprocess.DEVNULL` at every
call site) live in tests/test_graph_location.py and tests/test_work.py; this
is the test that proves the fix actually holds where it matters.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the optional `mcp` SDK is not installed")

# Launch the server from this checkout so `-m harness.mcp_server` resolves to
# the working tree rather than any installed copy.
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent

# The budget the tool must meet. With the fix a `memory_context` call over
# stdio lands around 0.1 s; with the bug it is 20 s. Two seconds is far enough
# above the former and far enough below the latter that neither Windows
# process-spawn jitter nor a slow CI box can flip the verdict.
BUDGET_SECONDS = 2.0

# A hard cap so a regression *fails* rather than wedging the suite. Generous
# on purpose: it is a deadlock guard, not a second latency assertion.
HARD_TIMEOUT_SECONDS = 120.0


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture()
def graph_repo(tmp_path: Path) -> Path:
    """A committed, clean git repo holding a `graphify-out/graph.json`.

    Clean and non-stale on purpose: `report_health` short-circuits on a dirty
    tree after a single git call, which would leave the second call site
    (`git rev-parse HEAD`) unexercised. `graphify-out/` is gitignored so the
    graph itself does not dirty the tree.
    """
    repo = tmp_path / "graph-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("graphify-out/\n", encoding="utf-8")

    _git(repo.parent, "init", repo.name)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    head = _git(repo, "rev-parse", "HEAD")

    out = repo / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(
        json.dumps(
            {
                "built_at_commit": head,
                "nodes": [
                    {
                        "id": "n1",
                        "label": "main",
                        "source_file": "src/app.py",
                        "source_location": "1-2",
                        "community_name": "app",
                        "metadata": {"kind": "function"},
                    }
                ],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    return repo


async def _time_memory_context(repo: Path) -> tuple[float, float, str]:
    """Drive a real server over stdio; return (first, second, text).

    Both calls are timed. The first is the one that catches an inherited-stdin
    regression, since it is the call that actually shells out to git. The
    second catches a regression in the server's health cache, which is what
    keeps a *burst* of tool calls from re-paying for those git subprocesses.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_server"],
        # Inherit the test environment: tests/conftest.py's `isolated_home`
        # sets HOME/USERPROFILE, and the child must honour them so nothing is
        # written into the developer's real ~/.agent-harness.
        env=dict(os.environ),
        cwd=str(CHECKOUT_ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Warm-up on a tool that touches no git: it pays the one-time
            # lazy import of the harness.memory_* modules so that cost is not
            # charged to the measurement below.
            await session.call_tool("memory_status", {"project": str(repo)})

            arguments = {"project": str(repo), "max_chars": 1000}

            start = time.monotonic()
            result = await session.call_tool("memory_context", arguments)
            first = time.monotonic() - start

            start = time.monotonic()
            await session.call_tool("memory_context", arguments)
            second = time.monotonic() - start

            assert not getattr(result, "isError", False), result
            text = "\n".join(
                getattr(block, "text", "") for block in (result.content or [])
            )
            return first, second, text


def test_memory_context_over_stdio_returns_promptly(graph_repo: Path) -> None:
    """`memory_context` must not pay a git timeout per call.

    Deliberately unmarked: `--strict-markers` is on and this repo registers no
    custom markers, so a `slow` mark would be a collection error. It is the
    slowest test in the suite (it spawns a real interpreter and MCP server),
    at a few seconds -- worth it for the only coverage of a 20 s production
    hang that no in-process test can see.

    With the inherited-stdin bug this took ~20 s (two 10 s git timeouts). The
    assertion is a latency budget rather than an exact figure because the only
    failure mode being guarded against is off by two orders of magnitude.
    """
    first, second, text = asyncio.run(
        asyncio.wait_for(_time_memory_context(graph_repo), HARD_TIMEOUT_SECONDS)
    )

    assert text.strip(), "memory_context returned an empty packet"
    assert first < BUDGET_SECONDS, (
        f"memory_context took {first:.2f}s over stdio (budget {BUDGET_SECONDS}s). "
        "A git subprocess is almost certainly inheriting the server's JSON-RPC "
        "stdin pipe again -- check stdin=subprocess.DEVNULL in "
        "harness/graph_location.py and harness/work.py."
    )
    assert second < BUDGET_SECONDS, (
        f"a repeat memory_context took {second:.2f}s over stdio "
        f"(budget {BUDGET_SECONDS}s); the graph-health cache in "
        "harness/mcp_server.py may have regressed."
    )


# --- the topic-scoped tools over the same transport -----------------------


async def _time_topic_tools(repo: Path) -> dict[str, tuple[float, str]]:
    """Drive `memory_context` with filters and `memory_search` across all
    projects over real stdio; return `{tool: (seconds, text)}`.

    `memory_search` with `projects="all"` is the call that reads the registry
    and, for each registered project, resolves a selector -- the path most
    likely to grow a lazy native import or a per-project subprocess. The
    isolated home has no registry, so the expected answer is the current
    project alone plus a note saying why.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "harness.mcp_server"],
        env=dict(os.environ),
        cwd=str(CHECKOUT_ROOT),
    )
    timings: dict[str, tuple[float, str]] = {}
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await session.call_tool("memory_status", {"project": str(repo)})
            calls = {
                "memory_context": {
                    "project": str(repo), "max_chars": 1000, "query": "retry",
                    "tags": "retry", "types": "decision",
                },
                "memory_search": {
                    "project": str(repo), "query": "retry backoff", "projects": "all",
                },
            }
            for tool, arguments in calls.items():
                start = time.monotonic()
                result = await session.call_tool(tool, arguments)
                elapsed = time.monotonic() - start
                assert not getattr(result, "isError", False), result
                text = "\n".join(getattr(b, "text", "") for b in (result.content or []))
                timings[tool] = (elapsed, text)
    return timings


def test_filtered_context_and_cross_project_search_over_stdio(graph_repo: Path) -> None:
    from harness import memory_store

    memory_store.init_project(graph_repo)
    memory_store.append_entries(
        [
            {"type": "decision", "scope": "project", "status": "accepted",
             "confidence": "confirmed", "source": {"manual": True},
             "summary": "Retry policy uses exponential backoff", "tags": ["retry"]},
            {"type": "lesson", "scope": "project", "status": "accepted",
             "confidence": "confirmed", "source": {"manual": True},
             "summary": "Logging is local JSONL", "tags": ["logging"]},
        ],
        directory=memory_store.project_memory_dir(graph_repo),
    )

    timings = asyncio.run(
        asyncio.wait_for(_time_topic_tools(graph_repo), HARD_TIMEOUT_SECONDS)
    )

    context_seconds, context = timings["memory_context"]
    assert "exponential backoff" in context and "local JSONL" not in context
    assert "filters applied (tags: retry; types: decision); 1 entry excluded" in context
    assert context_seconds < BUDGET_SECONDS, f"filtered memory_context took {context_seconds:.2f}s"

    search_seconds, search = timings["memory_search"]
    assert search.startswith("# Memory search: retry backoff")
    assert "exponential backoff" in search
    # `init_project` registered this repo alone in the isolated registry, so
    # "all" resolves to the current project with a note saying so.
    assert "no other projects registered on this machine" in search
    assert search_seconds < BUDGET_SECONDS, f"memory_search took {search_seconds:.2f}s"
