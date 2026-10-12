"""Each Claude Code hook must import the harness package that ships beside it.

The bug these guard against: hooks are registered by their checkout path but
run under the pipx interpreter. When that pipx install was an older snapshot,
`from harness import ...` resolved to the stale copy first. Modules the
snapshot had loaded silently from the stale copy; a module it predated
(`memory_curation`) raised ImportError, and the fallback could not recover
cleanly because `harness` was already cached in `sys.modules` from the stale
location. The memory hook swallows every error by design, so the
session-start packet silently disappeared for weeks.

Each case runs in a fresh interpreter with a stale `harness` package -- some
modules present, `memory_curation` missing, as in the real snapshot -- placed
ahead of the checkout on `sys.path`, and asserts every module the hook's
importer returns comes from the checkout beside the hook.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

HOOK_IMPORTERS = [
    ("memory_context_hook.py", "_harness"),
    ("claim_guard.py", "_import_work"),
    ("progress_capture_hook.py", "_import_harness"),
]

PROBE = """
import importlib.util, sys
from pathlib import Path
hook_path, func = Path(sys.argv[1]), sys.argv[2]
spec = importlib.util.spec_from_file_location(hook_path.stem, hook_path)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
returned = getattr(mod, func)()
modules = returned if isinstance(returned, tuple) else (returned,)
import harness
for module in (harness, *modules):
    print(Path(module.__file__).resolve().parent)
"""

# What the real stale snapshot had: older copies of these, but no memory_curation.
STALE_MODULES = ("__init__", "work", "memory_store", "memory_context", "memory_index")


@pytest.fixture
def stale_install(tmp_path: Path) -> Path:
    """A `harness` package shaped like an outdated pipx snapshot."""
    package = tmp_path / "stale-site-packages" / "harness"
    package.mkdir(parents=True)
    for name in STALE_MODULES:
        (package / f"{name}.py").write_text("STALE = True\n", encoding="utf-8")
    return package.parent


@pytest.mark.parametrize(("hook", "importer"), HOOK_IMPORTERS)
def test_hook_imports_the_harness_beside_it_over_a_stale_install(
    hook: str, importer: str, stale_install: Path, tmp_path: Path
) -> None:
    env = {**os.environ, "PYTHONPATH": str(stale_install)}
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO_ROOT / "hooks" / "claude" / hook), importer],
        cwd=tmp_path,  # keep the working directory from supplying a harness package
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{hook} could not import the harness:\n{result.stderr}"
    sources = {Path(line) for line in result.stdout.splitlines() if line.strip()}
    assert sources == {REPO_ROOT / "harness"}, f"{hook} loaded harness code from: {sources}"
