"""Tests for the shared safe-rendering helpers (harness.pathsafe), used by
both install.py and harness/graph_refresh.py.
"""

from __future__ import annotations

import os

from harness import pathsafe


def test_shell_quote_leaves_plain_values_bare() -> None:
    assert pathsafe.shell_quote("plainvalue") == "plainvalue"


def test_shell_quote_quotes_values_with_spaces() -> None:
    quoted = pathsafe.shell_quote("path with spaces")
    assert "path with spaces" in quoted
    assert quoted != "path with spaces"


def test_shell_quote_matches_platform_convention() -> None:
    import shlex
    import subprocess

    value = "a value/with spaces"
    quoted = pathsafe.shell_quote(value)
    if os.name == "nt":
        assert quoted == subprocess.list2cmdline([value])
    else:
        assert quoted == shlex.quote(value)


def test_json_string_escape_escapes_quotes_and_backslashes() -> None:
    escaped = pathsafe.json_string_escape('He said "hi"\\path')
    assert '"' not in escaped.replace('\\"', "")  # embedded quotes are escaped
    assert escaped == 'He said \\"hi\\"\\\\path'
