"""Review follow-ups for the store write lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import memory_review, memory_store, memory_tasks

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
REPO_ROOT = Path(memory_store.__file__).resolve().parent.parent


def _discovery(summary: str) -> dict:
    return {"type": "discovery", "scope": "project", "summary": summary, "source": {"manual": True}}


def test_a_nested_acquire_on_a_not_yet_existing_dotdot_path_does_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key was computed before mkdir, so the nested call resolved to a
    different key and waited the full timeout on a lock this process held."""
    monkeypatch.setattr(memory_store, "WRITE_LOCK_TIMEOUT_SECONDS", 3.0)
    store = tmp_path / "a" / ".." / "store"
    done: list[object] = []

    def run() -> None:
        try:
            memory_tasks.confirm(store, "Plan.", "the human said yes", tasks=["step"], now=NOW)
            done.append(True)
        except BaseException as exc:  # noqa: BLE001
            done.append(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=15)
    assert done == [True]


def test_a_write_lock_path_that_is_a_directory_fails_with_its_name(tmp_path: Path) -> None:
    store = tmp_path / "memory"
    (store / memory_store.WRITE_LOCK_NAME).mkdir(parents=True)

    with pytest.raises(OSError, match=r"memory write lock at .*\.write\.lock"):
        memory_store.append_entries([_discovery("x")], store, now=NOW)


_HOLDER = chr(10).join([
    "import sys, time",
    "from pathlib import Path",
    "from harness import memory_store",
    "d, flag = Path(sys.argv[1]), Path(sys.argv[2])",
    "with memory_store.writer_lock(d):",
    "    flag.write_text('held')",
    "    time.sleep(2.0)",
])


@pytest.mark.parametrize("which", ["review", "claim"])
def test_read_modify_rewrite_waits_for_a_writer_holding_the_lock(tmp_path: Path, which: str) -> None:
    """`apply_review` and the claim writers read the inbox and then replaced
    it without the write lock, so an append from another process in between
    was lost. They must now wait for the holder."""
    import time

    from harness import memory_claims

    store = tmp_path / "memory"
    memory_store.append_entries(
        [{"type": "task", "scope": "project", "summary": "work", "source": {"manual": True}}],
        store, now=NOW,
    )
    flag = tmp_path / "flag"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(store), str(flag)], cwd=REPO_ROOT, env=env
    )
    try:
        deadline = time.monotonic() + 30
        while not flag.exists():
            assert time.monotonic() < deadline and holder.poll() is None
            time.sleep(0.02)
        started = time.monotonic()
        entry_id = "mem-2026-11-14-001"
        if which == "review":
            memory_review.apply_review(store, entry_id, "reject", note="n", now=NOW)
        else:
            memory_claims.claim(store, entry_id, "claude", now=NOW)
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=60)

    assert waited > 0.8, f"{which} did not wait for the write lock ({waited:.2f}s)"
