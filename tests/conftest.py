"""Shared pytest fixtures.

The autouse ``isolated_home`` fixture below is a safety net: it guarantees no
test can write into the developer's real ``~/.agent-harness``. Individual
modules still define their own ``fake_home`` fixtures where a test needs a
handle on the home directory; those re-point home to their own tmp path and
shadow this one harmlessly.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the home directory to a per-test tmp path.

    Machine-scope memory resolves through ``Path.home()`` at call time
    (``memory_store.machine_memory_dir`` -> ``_registry_path``), so without
    this a test that initialises a project appends a throwaway path to the
    real ``~/.agent-harness/memory/registry.json``.

    Both the environment variables and the ``Path.home`` attribute are
    patched: the attribute covers in-process lookups, and ``HOME`` /
    ``USERPROFILE`` propagate to console scripts run in subprocesses.

    ``PIP_CACHE_DIR`` and ``XDG_CACHE_HOME`` are pinned as well. Moving home
    out from under the ``pip install`` subprocess in the packaging tests
    otherwise makes pip fall back to a cache directory relative to the
    working directory, littering ``pip/cache`` into the repository root.
    """
    home = tmp_path / "isolated-home"
    home.mkdir()
    cache = home / "cache"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("PIP_CACHE_DIR", str(cache / "pip"))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home
