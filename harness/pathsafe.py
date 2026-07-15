"""Shared safe-rendering helpers: argv-safe shell quoting and JSON-string
escaping.

Both the installer (`install.py`) and the graph lifecycle tooling
(`harness/graph_refresh.py`, `harness/graph_check.py`) need to render a path
or value into copy-pasteable command text or an already-open JSON string
literal. This module is the single place that logic lives, so both callers
share one safe-rendering contract instead of each reimplementing it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess


def shell_quote(value: str) -> str:
    """Quote `value` to survive argv tokenization on the current platform.

    Leaves already-safe values (no spaces/specials) bare rather than adding
    unnecessary quoting. On POSIX, `shlex.quote` covers full `sh`/`bash`
    metacharacter safety. On Windows, `subprocess.list2cmdline` only
    implements the MSVCRT/CreateProcess argv-splitting rules (spaces, tabs,
    quotes, backslashes) -- it does NOT escape cmd.exe shell metacharacters
    (`% ^ & | < > ( )`). Callers of this function render installer- or
    harness-controlled paths (repo checkout location, `sys.executable`), not
    untrusted input, so this is intentionally scoped to argv-splitting
    safety rather than full interactive-shell metacharacter escaping.
    """
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def json_string_escape(value: str) -> str:
    """Escape `value` for splicing into an already-open JSON string literal."""
    return json.dumps(value)[1:-1]
