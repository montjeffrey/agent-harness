"""One shared contract for the final step of a temp-then-replace write.

Four call sites in this package build a file by writing a temp sibling and
then atomically replacing the target: the installer's
`_atomic_write_bytes`, the memory store's `write_lines` and `save_registry`,
and compaction's `_atomic_write`. Each has its own reason to shape the
*write* differently -- binary via `mkstemp`, streamed lines, a permission
restriction pass -- so those stay where they are. Only the replace is
centralised here, because only the replace has a failure mode all four share.

That failure mode is Windows file-replace contention. `os.replace` onto a
path another process currently holds open raises `PermissionError`
(winerror 5 / errno 13, verified locally -- a sharing violation surfaces as
winerror 5, not 32, so catching `PermissionError` covers the family without
inspecting `winerror`). Antivirus and the search indexer both open files
opportunistically in the moment after they are written, which is precisely
when a temp-then-replace performs its replace. The contention is transient
and short, so a bounded retry turns a hard write failure into a brief wait.

Deliberately *not* platform-gated. The observed failure was on Windows, but
the retry is harmless elsewhere: POSIX `rename` does not fail this way, so
the loop simply never takes its second iteration.

The second shared failure mode is the temp file's *name*. Three writers
derived it from the destination alone -- `inbox.jsonl.tmp`,
`registry.json.tmp`, `<archive>.md.tmp` -- so two writers of the same target
picked the same scratch path and wrote into each other: the first's replace
moves the file out from under the second, whose own replace then fails on a
vanished source, and interleaved writes can leave a blend of both payloads
where a whole file belongs. `atomic_sibling` allocates a name that cannot
collide, which is why it lives here rather than being fixed three times over.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Five attempts at 10ms doubling waits ~0.15s in total before giving up.
# Long enough to outlast an AV or indexer file handle, short enough that a
# genuinely locked file still reports quickly instead of stalling an agent.
DEFAULT_ATTEMPTS = 5
DEFAULT_DELAY = 0.01


def replace_with_retry(
    src: Path,
    dst: Path,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
) -> None:
    """`os.replace(src, dst)`, retrying only transient permission denials.

    Retries `PermissionError` with exponential backoff and re-raises it once
    `attempts` is exhausted -- a permanent lock must still surface as an
    error rather than hang. Every other exception propagates on the first
    attempt: a missing source or a bad path is a bug, and retrying it would
    only delay the report by the whole backoff budget.

    Atomicity is unchanged. Each attempt is a single `os.replace`, so a
    reader still sees either the old file or the new one, never a blend.
    """
    wait = delay
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait)
            wait *= 2


@contextmanager
def atomic_sibling(dst: Path) -> Iterator[Path]:
    """Yield a private scratch path next to `dst`, then replace `dst` with it.

    The caller writes the temp file however it likes -- streamed lines, one
    `write_text`, a permission pass -- and this handles only the two parts
    every temp-then-replace shares: picking a name no other writer can pick,
    and not leaving that name behind when the body fails.

    The name is `.<dst.name>.<pid>-<uuid4>.tmp`, following the installer's
    existing convention so a leftover is recognisable as ours. Deliberately
    *not* created here: `mkstemp` would create it 0600 and silently tighten
    the mode of files that are 0644 today. The caller's own `open`/`write_text`
    creates it, so POSIX permissions stay exactly what they are now, and
    pid-plus-uuid makes the lost `O_EXCL` guarantee academic.

    Accepts the one cost of unique names: a fixed name self-heals because the
    next write overwrites it, whereas a process killed between create and
    replace strands one orphan. The installer already had that property and
    the shared prefix keeps orphans sweepable; adding machinery to collect
    them would cost more than the stray bytes do.
    """
    dst = Path(dst)
    tmp = dst.parent / f".{dst.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        yield tmp
        replace_with_retry(tmp, dst)
    except BaseException:
        # Cleanup must not mask the failure that brought us here: the same
        # contention that can deny a replace can deny this unlink, and an
        # exception raised here would replace the real one.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
