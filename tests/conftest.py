"""Shared pytest fixtures.

The autouse ``isolated_home`` fixture below is a safety net: it guarantees no
test can write into the developer's real ``~/.agent-harness``. Individual
modules still define their own ``fake_home`` fixtures where a test needs a
handle on the home directory; those re-point home to their own tmp path and
shadow this one harmlessly.

``real_registry_untouched`` is the alarm on that safety net. Isolation here
has been broken and re-fixed more than once, and the failure mode is silent:
the suite passes, and the only evidence is throwaway tmp paths accumulating
in the operator's real registry weeks later. The session fixture turns that
into a red run instead.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

# Captured at import, before any fixture patches ``Path.home``: this must be
# the operator's actual home, not a test's.
REAL_REGISTRY = Path.home() / ".agent-harness" / "memory" / "registry.json"


def _registered_ids(path: Path) -> frozenset[str] | None:
    """Project ids in the registry at `path`, or None when there is no file."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return frozenset(data["projects"])
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset()


@pytest.fixture(scope="session", autouse=True)
def real_registry_untouched() -> Iterator[None]:
    """Fail the run if the suite enrolled anything in the real registry.

    The set of project ids is what is compared, not the file's bytes. A
    ``last_seen`` refresh from a genuine harness command running in another
    window is legitimate and must not redden an unrelated test run; a *new*
    id, or a registry conjured into existence by a test, is the leak this
    guards.
    """
    before = _registered_ids(REAL_REGISTRY)
    yield
    after = _registered_ids(REAL_REGISTRY)

    if before is None and after is not None:
        pytest.fail(
            f"the test suite created the real machine registry at "
            f"{REAL_REGISTRY} -- home isolation is broken",
            pytrace=False,
        )
    if before is not None and after is not None and after - before:
        leaked = ", ".join(sorted(after - before))
        pytest.fail(
            f"the test suite registered {leaked} in the real machine "
            f"registry at {REAL_REGISTRY} -- home isolation is broken",
            pytrace=False,
        )


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
    # The SessionStart hook starts a detached background `collect` once a
    # day; a hook test must not leave one running against its tmp home.
    monkeypatch.setenv("AGENT_HARNESS_TELEMETRY_AUTO", "0")
    monkeypatch.setattr(Path, "home", lambda: home)
    return home
