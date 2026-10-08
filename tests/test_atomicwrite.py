"""Tests for `harness.atomicwrite`: the retrying atomic-replace helper.

The failure these cover is real and observed, not hypothetical: on Windows a
`replace` onto a path another process holds open raises
`PermissionError` (winerror 5, errno 13). Antivirus and the search indexer
both open files opportunistically right after they are written, which is
exactly the moment the temp-then-replace shape performs its replace. The
contention is transient, so a bounded retry converts a hard failure into a
short wait.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness import atomicwrite, install_cli, memory_compact, memory_store, telemetry


def test_replaces_on_the_first_attempt_when_uncontended(tmp_path: Path) -> None:
    """The uncontended path must not retry, sleep, or alter replace semantics."""
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    atomicwrite.replace_with_retry(src, dst)

    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_retries_a_transient_permission_error_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two contended attempts followed by a success must land the write.

    This is the shape of the observed failure: the holder releases the file
    within milliseconds, so the replace succeeds on a later attempt.
    """
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")

    real_replace = os.replace
    calls: list[int] = []

    def flaky(a: object, b: object) -> None:
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "Access is denied")
        real_replace(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    atomicwrite.replace_with_retry(src, dst)

    assert len(calls) == 3
    assert dst.read_text(encoding="utf-8") == "new"


def test_reraises_after_the_attempt_bound_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely locked file must still fail, not hang or pass silently.

    Retrying forever would turn a permanent lock into a hung agent; the
    caller's error handling stays reachable.
    """
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")

    calls: list[int] = []

    def always_denied(a: object, b: object) -> None:
        calls.append(1)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os, "replace", always_denied)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        atomicwrite.replace_with_retry(src, dst, attempts=4)

    assert len(calls) == 4


def test_does_not_retry_errors_outside_the_contention_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing source is a bug, not contention -- it must surface at once.

    Retrying it would only delay the report by the full backoff budget.
    """
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")

    calls: list[int] = []

    def missing(a: object, b: object) -> None:
        calls.append(1)
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(os, "replace", missing)

    with pytest.raises(FileNotFoundError):
        atomicwrite.replace_with_retry(src, dst)

    assert len(calls) == 1


def test_backs_off_between_attempts_within_a_bounded_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waits must grow and stay short.

    A flat retry can land inside the same lock window repeatedly; an unbounded
    one blocks the agent. Growing-but-bounded is what makes the retry useful
    against AV/indexer contention without becoming a stall.
    """
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")

    slept: list[float] = []
    monkeypatch.setattr(
        os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(13, "denied"))
    )
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    with pytest.raises(PermissionError):
        atomicwrite.replace_with_retry(src, dst, attempts=5, delay=0.01)

    # One sleep fewer than attempts: no wait after the final failure.
    assert len(slept) == 4
    assert slept == sorted(slept), "backoff must not shrink"
    assert sum(slept) < 1.0, "total wait must stay short enough to not stall an agent"


# --- real contention, against the production write paths ------------------
#
# Everything above monkeypatches `os.replace`, which proves the retry loop but
# not that the four production writers actually route through it. These drive
# the real functions while a background thread holds a genuine OS handle on the
# destination -- the same thing an antivirus scanner or the search indexer does
# in the moment after a file is written.

# Comfortably inside the ~150ms default retry budget, so the holder releases
# while attempts remain. Short enough that the suite does not slow down.
HOLD_SECONDS = 0.03


def _hold_open(path: Path, seconds: float = HOLD_SECONDS) -> threading.Thread:
    """Hold a real read handle on `path`; returns the thread to join."""
    ready = threading.Event()

    def run() -> None:
        with path.open("r", encoding="utf-8") as fh:
            ready.set()
            time.sleep(seconds)
            fh.read()

    thread = threading.Thread(target=run)
    thread.start()
    ready.wait()  # do not race until the handle is genuinely open
    return thread


@pytest.mark.skipif(
    sys.platform != "win32", reason="file-replace contention is a Windows behaviour"
)
def test_a_held_handle_really_does_deny_a_bare_replace(tmp_path: Path) -> None:
    """Guards the cases below from becoming vacuous.

    If Windows ever stopped denying a replace onto a held file, the tests that
    follow would pass without exercising anything. This one fails first and
    says why.
    """
    src, dst = tmp_path / "src.txt", tmp_path / "dst.txt"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    thread = _hold_open(dst)
    try:
        with pytest.raises(PermissionError):
            os.replace(src, dst)
    finally:
        thread.join()


def test_write_lines_survives_a_held_inbox(tmp_path: Path) -> None:
    """`memory_store.write_lines` must land the inbox despite contention."""
    memory_store.write_lines(['{"id": "e-0001"}'], tmp_path)
    inbox = tmp_path / "inbox.jsonl"

    thread = _hold_open(inbox)
    try:
        written = memory_store.write_lines(['{"id": "e-0002"}'], tmp_path)
    finally:
        thread.join()

    assert written == 1
    assert "e-0002" in inbox.read_text(encoding="utf-8")


def test_save_registry_survives_a_held_registry() -> None:
    """`memory_store.save_registry` must land despite contention.

    Runs against the autouse `isolated_home` fixture, so this touches a tmp
    home rather than the developer's real registry.
    """
    memory_store.save_registry({"projects": {}})
    registry = memory_store.machine_memory_dir() / "registry.json"
    assert registry.exists(), "precondition: the first save must have created it"

    thread = _hold_open(registry)
    try:
        memory_store.save_registry({"projects": {"demo": {"root": "/tmp/demo"}}})
    finally:
        thread.join()

    assert "demo" in registry.read_text(encoding="utf-8")


def test_compaction_archive_write_survives_a_held_target(tmp_path: Path) -> None:
    """Compaction's archive write is the one with a real data-loss window."""
    archive = tmp_path / "2026-W33.md"
    memory_compact._atomic_write(archive, "first\n")

    thread = _hold_open(archive)
    try:
        memory_compact._atomic_write(archive, "second\n")
    finally:
        thread.join()

    assert archive.read_text(encoding="utf-8") == "second\n"


def test_installer_write_survives_a_held_target(tmp_path: Path) -> None:
    """The installer's byte writer covers settings/hook files it rewrites."""
    target = tmp_path / "settings.json"
    install_cli._atomic_write_bytes(target, b'{"a": 1}')

    thread = _hold_open(target)
    try:
        install_cli._atomic_write_bytes(target, b'{"a": 2}')
    finally:
        thread.join()

    assert target.read_bytes() == b'{"a": 2}'


def test_telemetry_state_write_survives_a_held_target() -> None:
    """The watermark map routes through the same retry as the other writers.

    Unlike the four cases above this one also passes against the bare
    `write_text` it replaced -- Windows permits a write to a file another
    process holds open for *reading*, so there was no denial here to fix. It
    earns its place as the guard that temp-then-replace did not introduce one:
    the replace is the step that contends, and this writer now performs one.

    Runs against the autouse `isolated_home` fixture, so `telemetry_dir()`
    resolves under a tmp home rather than the developer's real one.
    """
    telemetry._save_collect_state({"claude-code": "2026-01-01T00:00:00Z"})
    state = telemetry._collect_state_path()
    assert state.exists(), "precondition: the first save must have created it"

    thread = _hold_open(state)
    try:
        telemetry._save_collect_state({"claude-code": "2026-02-02T00:00:00Z"})
    finally:
        thread.join()

    assert telemetry._load_collect_state() == {"claude-code": "2026-02-02T00:00:00Z"}


# --- temp names two writers cannot share ----------------------------------
#
# Surviving contention is not enough if both contenders scribble in the same
# scratch file. Three writers derived the temp name from the destination
# alone, so any two writing one target picked one path: the first's replace
# moves it out from under the second, and interleaved writes can leave a
# blend of both payloads where a whole file belongs.


def test_atomic_sibling_never_hands_two_writers_the_same_path(tmp_path: Path) -> None:
    """The property the fixed-name scheme could not have.

    Guards the production-writer cases below from going vacuous: if the names
    ever collide again, this fails first and names the property that broke.
    """
    dst = tmp_path / "inbox.jsonl"

    with atomicwrite.atomic_sibling(dst) as first:
        with atomicwrite.atomic_sibling(dst) as second:
            assert first != second
            assert first.parent == second.parent == dst.parent
            assert first.suffix == second.suffix == ".tmp"
            second.write_text("second", encoding="utf-8")
        first.write_text("first", encoding="utf-8")

    # Whole-file semantics survive the overlap: the later replace wins
    # entirely rather than the two payloads mixing.
    assert dst.read_text(encoding="utf-8") == "first"


def test_atomic_sibling_leaves_the_temp_uncreated_for_the_caller(
    tmp_path: Path,
) -> None:
    """Allocating a name, not a file, is what preserves each writer's mode.

    `mkstemp` would create it 0600 and silently tighten files that are 0644
    today. The caller's own `open`/`write_text` creates it instead.
    """
    dst = tmp_path / "inbox.jsonl"

    with atomicwrite.atomic_sibling(dst) as tmp:
        assert not tmp.exists()
        tmp.write_text("content", encoding="utf-8")

    assert dst.read_text(encoding="utf-8") == "content"


def test_a_failed_body_strands_no_temp_and_leaves_the_target_alone(
    tmp_path: Path,
) -> None:
    """Unique names cannot self-heal by being overwritten, so they must clean up.

    The destination must also be untouched: a body that failed halfway has no
    business landing anything.
    """
    dst = tmp_path / "inbox.jsonl"
    dst.write_text("original", encoding="utf-8")
    allocated: list[Path] = []

    with pytest.raises(RuntimeError):
        with atomicwrite.atomic_sibling(dst) as tmp:
            allocated.append(tmp)
            tmp.write_text("half a payload", encoding="utf-8")
            raise RuntimeError("writer failed mid-payload")

    assert not allocated[0].exists()
    assert dst.read_text(encoding="utf-8") == "original"


def test_cleanup_does_not_mask_the_failure_that_triggered_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unlink can hit the same denial as the replace it cleans up after."""
    dst = tmp_path / "inbox.jsonl"

    def denied(_self: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "unlink", denied)

    with pytest.raises(RuntimeError, match="the real failure"):
        with atomicwrite.atomic_sibling(dst) as tmp:
            tmp.write_text("x", encoding="utf-8")
            raise RuntimeError("the real failure")


def test_two_inbox_writers_do_not_share_one_temp_file(tmp_path: Path) -> None:
    """A second `write_lines` completes while the first is still streaming.

    No monkeypatching here: the interleave point is a line generator that runs
    the nested write between yields, which is the overlap two processes
    produce. Under the fixed `inbox.jsonl.tmp` the two collide on one scratch
    path and one of them raises -- `PermissionError` on Windows, where the
    outer's open handle denies the inner's replace, and `FileNotFoundError`
    elsewhere, where the inner's rename leaves the outer no source.
    """

    def outer_lines() -> Iterator[str]:
        yield '{"id": "outer-1"}'
        memory_store.write_lines(['{"id": "inner-1"}'], tmp_path)
        yield '{"id": "outer-2"}'

    written = memory_store.write_lines(outer_lines(), tmp_path)

    assert written == 2
    inbox = (tmp_path / "inbox.jsonl").read_text(encoding="utf-8")
    assert "outer-1" in inbox and "outer-2" in inbox
    assert "inner-1" not in inbox, "a whole file must land, not a blend of two"


def _overlap_at_the_replace(
    monkeypatch: pytest.MonkeyPatch, second_writer: object
) -> list[int]:
    """Run `second_writer` once, at the only moment a rival could get in.

    `_atomic_write` and `save_registry` write and close inside one call, so
    unlike the streamed inbox they offer no natural interleave point. The
    overlap goes where a second process's would land: after the temp is
    written, before it is replaced. The flag makes it fire once, so the nested
    writer's own replace runs straight through instead of recursing.
    """
    fired: list[int] = []
    real_replace = atomicwrite.replace_with_retry

    def replace_after_a_second_writer_finishes(src: Path, dst: Path) -> None:
        if not fired:
            fired.append(1)
            second_writer()  # type: ignore[operator]
        real_replace(src, dst)

    monkeypatch.setattr(
        atomicwrite, "replace_with_retry", replace_after_a_second_writer_finishes
    )
    return fired


def test_two_archive_writers_do_not_share_one_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compaction's writer, overlapped at the moment before its replace."""
    archive = tmp_path / "2026-W33.md"
    fired = _overlap_at_the_replace(
        monkeypatch, lambda: memory_compact._atomic_write(archive, "inner\n")
    )

    memory_compact._atomic_write(archive, "outer\n")

    assert fired, "precondition: the two writers must actually have overlapped"
    assert archive.read_text(encoding="utf-8") == "outer\n"


def test_two_registry_writers_do_not_share_one_temp_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One registry serves every project, so concurrent enrols collide here.

    Runs against the autouse `isolated_home` fixture.
    """
    fired = _overlap_at_the_replace(
        monkeypatch, lambda: memory_store.save_registry({"projects": {"inner": {}}})
    )

    memory_store.save_registry({"projects": {"outer": {}}})

    registry = (memory_store.machine_memory_dir() / "registry.json").read_text(
        encoding="utf-8"
    )
    assert fired, "precondition: the two writers must actually have overlapped"
    assert "outer" in registry
    assert "inner" not in registry, "the later replace must win whole"
