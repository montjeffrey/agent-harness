"""Stdlib process and memory probes for the MCP gate's accounting and cleanup.

The gate needs four facts about the machine and its own downstream servers:
how much RAM is free (to scale idle timeouts under pressure), how much a
server's process tree holds (to account for it), which processes descend from
a server it launched (to prove a stop cleaned up), and how to kill a survivor
without ever killing a stranger that happens to reuse its PID.

Everything here is stdlib only -- `ctypes` against kernel32 on Windows,
`/proc` on Linux, `ps`/`sysctl`/`vm_stat` on macOS -- and every public
function is a probe: it returns None, [], or False when the platform is
unsupported, the process is gone, or a call fails, and never raises into the
gate. An unexpected failure is reported as one line on stderr; a process that
simply does not exist is an ordinary answer, not an error.

A process is identified by `(pid, create_time)`, never by PID or name alone.
`terminate` acts only when the live process still carries the recorded
creation time; on Windows (and Linux with pidfd) the check and the kill go
through one handle, so the PID cannot be recycled between them.
"""

from __future__ import annotations

import functools
import os
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")

_PS_TIMEOUT_SECONDS = 5.0
# How long `terminate` waits for a killed process to be reported gone.
_TERMINATE_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class ProcessIdentity:
    """One specific process: a PID plus the creation time that pins it down.

    `create_time` is only ever compared for equality with another value from
    this module on the same machine; its epoch differs by platform.
    """

    pid: int
    create_time: float
    exe: str | None


def _warn(where: str, exc: BaseException) -> None:
    print(f"[procinfo] {where} failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _probe(where: str, default: T, fn: Callable[[], T]) -> T:
    """Run a platform probe; any unexpected exception becomes `default` plus a stderr line."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a probe must never raise into the gate
        _warn(where, exc)
        return default


def _platform() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return "other"


# -- Windows ---------------------------------------------------------------

_PROCESS_TERMINATE = 0x0001
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87
_TH32CS_SNAPPROCESS = 0x00000002
_WAIT_OBJECT_0 = 0
_MAX_PATH_CHARS = 32768


@functools.lru_cache(maxsize=1)
def _win() -> Any:
    """kernel32 with explicit prototypes, on a private WinDLL instance.

    A private instance keeps these argtypes from leaking into other code that
    uses the shared `ctypes.windll.kernel32`. HANDLE is pointer-sized; leaving
    restype at the default `int` truncates handles and once returned zeroed
    memory counters.
    """
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    prototypes: dict[str, tuple[list[Any], Any]] = {
        "OpenProcess": ([dword, boolean, dword], handle),
        "CloseHandle": ([handle], boolean),
        "GetExitCodeProcess": ([handle, ctypes.POINTER(dword)], boolean),
        "GetProcessTimes": ([handle] + [ctypes.POINTER(FILETIME)] * 4, boolean),
        "QueryFullProcessImageNameW": (
            [handle, dword, wintypes.LPWSTR, ctypes.POINTER(dword)],
            boolean,
        ),
        "TerminateProcess": ([handle, wintypes.UINT], boolean),
        "WaitForSingleObject": ([handle, dword], dword),
        "GlobalMemoryStatusEx": ([ctypes.POINTER(MEMORYSTATUSEX)], boolean),
        "K32GetProcessMemoryInfo": (
            [handle, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), dword],
            boolean,
        ),
        "CreateToolhelp32Snapshot": ([dword, dword], handle),
        "Process32FirstW": ([handle, ctypes.POINTER(PROCESSENTRY32W)], boolean),
        "Process32NextW": ([handle, ctypes.POINTER(PROCESSENTRY32W)], boolean),
    }
    for name, (argtypes, restype) in prototypes.items():
        fn = getattr(k32, name)
        fn.argtypes = argtypes
        fn.restype = restype
    k32.FILETIME = FILETIME
    k32.MEMORYSTATUSEX = MEMORYSTATUSEX
    k32.PROCESS_MEMORY_COUNTERS_EX = PROCESS_MEMORY_COUNTERS_EX
    k32.PROCESSENTRY32W = PROCESSENTRY32W
    k32.INVALID_HANDLE = ctypes.c_void_p(-1).value
    return k32


def _win_memory_status() -> Any:
    import ctypes

    k32 = _win()
    status = k32.MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not k32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return status


def _win_open(pid: int, access: int, *, warn: bool = True) -> Any:
    """A process handle, or None when the PID is gone or not ours to open.

    A missing PID (ERROR_INVALID_PARAMETER) is an ordinary answer; any other
    refusal, such as access denied, is reported on stderr when `warn` is set.
    """
    import ctypes

    handle = _win().OpenProcess(access, False, pid)
    if handle:
        return handle
    error = ctypes.get_last_error()
    if warn and error != _ERROR_INVALID_PARAMETER:
        _warn(f"OpenProcess({pid}, {access:#x})", ctypes.WinError(error))
    return None


def _win_alive(handle: Any) -> bool:
    import ctypes
    from ctypes import wintypes

    code = wintypes.DWORD()
    if not _win().GetExitCodeProcess(handle, ctypes.byref(code)):
        raise ctypes.WinError(ctypes.get_last_error())
    return code.value == _STILL_ACTIVE


def _win_create_time(handle: Any) -> float:
    """Creation time as seconds since 1601-01-01 (FILETIME), from an open handle."""
    import ctypes

    k32 = _win()
    times = [k32.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    created = times[0]
    return ((created.high << 32) | created.low) / 1e7


def _win_exe(handle: Any) -> str | None:
    import ctypes
    from ctypes import wintypes

    buffer = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
    size = wintypes.DWORD(_MAX_PATH_CHARS)
    if not _win().QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value or None


def _win_identity_from(pid: int, handle: Any) -> ProcessIdentity | None:
    if not _win_alive(handle):
        return None
    return ProcessIdentity(pid, _win_create_time(handle), _win_exe(handle))


def _win_process_identity(pid: int) -> ProcessIdentity | None:
    """None only when the PID is gone; any other refusal raises (state unknown)."""
    import ctypes

    handle = _win().OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return None
        raise ctypes.WinError(error)
    try:
        return _win_identity_from(pid, handle)
    finally:
        _win().CloseHandle(handle)


def _win_children_map() -> dict[int, list[int]]:
    import ctypes

    k32 = _win()
    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == k32.INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    mapping: dict[int, list[int]] = {}
    try:
        entry = k32.PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            mapping.setdefault(entry.th32ParentProcessID, []).append(
                entry.th32ProcessID
            )
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return mapping


def _win_working_set(pid: int) -> int | None:
    import ctypes

    k32 = _win()
    # VM_READ is refused for some processes; QUERY_LIMITED alone suffices on Windows 8.1+.
    handle = _win_open(
        pid, _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, warn=False
    )
    handle = handle or _win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        return None
    try:
        if not _win_alive(handle):
            return None
        counters = k32.PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not k32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.WorkingSetSize)
    finally:
        k32.CloseHandle(handle)


def _win_terminate(identity: ProcessIdentity) -> bool:
    """Check identity and kill through one handle, so the PID cannot be reused in between."""
    import ctypes

    k32 = _win()
    access = _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE
    handle = _win_open(identity.pid, access)
    if handle is None:
        return False
    try:
        if not _matches(identity, _win_identity_from(identity.pid, handle)):
            return False
        if not k32.TerminateProcess(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        # TerminateProcess only starts the kill; wait so callers see it gone.
        k32.WaitForSingleObject(handle, int(_TERMINATE_WAIT_SECONDS * 1000))
        return True
    finally:
        k32.CloseHandle(handle)


# -- Linux -----------------------------------------------------------------


def _read_text(path: str) -> str | None:
    """File contents, or None when the file (usually a /proc entry) is gone."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
        return None


def _kb_field(text: str, key: str) -> int | None:
    """`Key:   1234 kB` from /proc/meminfo or /proc/<pid>/status, in bytes."""
    for line in text.splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1]) * 1024
    return None


def _linux_meminfo(key: str) -> int | None:
    text = _read_text("/proc/meminfo")
    return None if text is None else _kb_field(text, key)


def _linux_stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the `(comm)` field: [state, ppid, ...]."""
    text = _read_text(f"/proc/{pid}/stat")
    if not text or ")" not in text:
        return None  # gone, or exited mid-read
    return text[text.rindex(")") + 2 :].split()


def _linux_process_identity(pid: int) -> ProcessIdentity | None:
    fields = _linux_stat_fields(pid)
    if fields is None or fields[0] in ("Z", "X"):
        return None
    # Seconds since boot, deliberately not offset by /proc/stat's btime: btime
    # moves when the wall clock is stepped, so a later gate would compute a
    # different value for the same process and its crash sweep would miss it.
    created = int(fields[19]) / os.sysconf("SC_CLK_TCK")
    try:
        exe: str | None = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        exe = None
    return ProcessIdentity(pid, created, exe)


def _linux_children_map() -> dict[int, list[int]]:
    mapping: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        fields = _linux_stat_fields(int(name))
        if fields is not None:
            mapping.setdefault(int(fields[1]), []).append(int(name))
    return mapping


def _linux_working_set(pid: int) -> int | None:
    text = _read_text(f"/proc/{pid}/status")
    return None if text is None else _kb_field(text, "VmRSS")


def _linux_terminate(identity: ProcessIdentity) -> bool:
    """Pin the process with a pidfd, verify it, then signal through the pidfd."""
    pidfd_open = getattr(os, "pidfd_open", None)
    send = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or send is None:
        return _posix_terminate(identity)
    try:
        fd = pidfd_open(identity.pid)
    except ProcessLookupError:
        return False
    try:
        if not _matches(identity, _linux_process_identity(identity.pid)):
            return False
        send(fd, signal.SIGKILL)
        return True
    finally:
        os.close(fd)


# -- macOS -----------------------------------------------------------------


def _run_with_pid(args: list[str]) -> tuple[str | None, int]:
    """(stdout, the command's own PID); stdout is None when it exits non-zero silently.

    Non-zero with nothing printed is `ps -p` saying "no such PID"; non-zero
    with output, or a timeout, raises: the answer is unknown, not "gone".
    """
    env = {**os.environ, "LC_ALL": "C"}
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    try:
        out, _ = proc.communicate(timeout=_PS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()  # our own child, through its own handle
        proc.communicate()
        raise
    if proc.returncode == 0:
        return out, proc.pid
    if out.strip():
        raise OSError(f"{args[0]} exited {proc.returncode}")
    return None, proc.pid


def _run(args: list[str]) -> str | None:
    """stdout of a short command, or None when it exits non-zero (e.g. no such PID)."""
    return _run_with_pid(args)[0]


def _mac_process_identity(pid: int) -> ProcessIdentity | None:
    out = _run(["ps", "-o", "stat=,lstart=,comm=", "-p", str(pid)])
    if not out or not out.strip():
        return None
    parts = out.split(None, 6)
    if parts[0].startswith("Z"):
        return None
    created = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
    exe = parts[6].strip() if len(parts) > 6 else None
    return ProcessIdentity(pid, created, exe or None)


def _mac_children_map() -> dict[int, list[int]]:
    out, ps_pid = _run_with_pid(["ps", "-A", "-o", "pid=,ppid="])
    if out is None:
        raise OSError("ps -A failed")
    mapping: dict[int, list[int]] = {}
    for line in out.splitlines():
        pid, ppid = line.split()
        # BSD `ps -A` lists itself as our child; left in, every spawn would
        # see two new children and never attribute the downstream's root.
        if int(pid) != ps_pid:
            mapping.setdefault(int(ppid), []).append(int(pid))
    return mapping


def _mac_working_set(pid: int) -> int | None:
    out = _run(["ps", "-o", "rss=", "-p", str(pid)])
    return int(out.strip()) * 1024 if out and out.strip() else None


def _mac_total_memory() -> int | None:
    out = _run(["sysctl", "-n", "hw.memsize"])
    return int(out.strip()) if out else None


def _mac_available_memory() -> int | None:
    out = _run(["vm_stat"])
    if out is None:
        return None
    page_size = int(out.split("page size of", 1)[1].split()[0])
    wanted = ("Pages free", "Pages inactive", "Pages speculative")
    pages = 0
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in wanted:
            pages += int(value.strip().rstrip("."))
    return pages * page_size


# -- POSIX -----------------------------------------------------------------


def _posix_terminate(identity: ProcessIdentity) -> bool:
    # Without a pidfd the check and the kill are two steps; the window is a
    # few microseconds and the creation-time check still rules out any PID
    # that was reused before this call.
    if not is_same_process(identity):
        return False
    try:
        os.kill(identity.pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


# -- public API ------------------------------------------------------------


def _matches(identity: ProcessIdentity, current: ProcessIdentity | None) -> bool:
    """Same live process: something runs at that PID with the recorded creation time.

    The image path is deliberately not compared: a POSIX `exec` keeps the PID
    and creation time but changes it, and treating that still-running process
    as gone would hide a leak. Callers that also require the path compare
    `identity.exe` themselves.
    """
    return current is not None and current.create_time == identity.create_time


def total_memory_bytes() -> int | None:
    """Physical RAM installed, in bytes."""
    impls: dict[str, Callable[[], int | None]] = {
        "win32": lambda: int(_win_memory_status().ullTotalPhys),
        "linux": lambda: _linux_meminfo("MemTotal"),
        "darwin": _mac_total_memory,
    }
    impl = impls.get(_platform())
    return None if impl is None else _probe("total_memory_bytes", None, impl)


def available_memory_bytes() -> int | None:
    """RAM that can be handed to new work without paging, in bytes."""
    impls: dict[str, Callable[[], int | None]] = {
        "win32": lambda: int(_win_memory_status().ullAvailPhys),
        "linux": lambda: _linux_meminfo("MemAvailable"),
        "darwin": _mac_available_memory,
    }
    impl = impls.get(_platform())
    return None if impl is None else _probe("available_memory_bytes", None, impl)


def boot_id() -> str | None:
    """This boot's identifier where creation times restart at boot (Linux), else None.

    Linux creation times count from boot, so an identity recorded before a
    reboot must never be compared with a process running after it.
    """
    if _platform() != "linux":
        return None

    def read() -> str | None:
        return (_read_text("/proc/sys/kernel/random/boot_id") or "").strip() or None

    return _probe("boot_id", None, read)


def _identity_impl() -> Callable[[int], ProcessIdentity | None] | None:
    """The platform's strict identity probe: None means gone, an exception means unknown."""
    impls = {
        "win32": _win_process_identity,
        "linux": _linux_process_identity,
        "darwin": _mac_process_identity,
    }
    return impls.get(_platform())


def process_identity(pid: int) -> ProcessIdentity | None:
    """The live process at `pid`, or None when there is none (or it cannot be read)."""
    impl = _identity_impl()
    if impl is None or pid <= 0:
        return None
    return _probe(f"process_identity({pid})", None, lambda: impl(pid))


def is_same_process(identity: ProcessIdentity) -> bool:
    """True while the process `identity` names is alive and has not been replaced."""
    return _matches(identity, process_identity(identity.pid))


def process_state(identity: ProcessIdentity) -> str:
    """Whether the process `identity` names is "alive", "gone", or "unknown".

    Unlike `is_same_process`, a failed probe (access denied, a `ps` timeout)
    is "unknown" rather than "gone", so a caller deciding whether another
    gate has crashed can leave a live one alone.
    """
    impl = _identity_impl()
    if impl is None:
        return "unknown"
    if identity.pid <= 0:
        return "gone"
    try:
        current = impl(identity.pid)
    except Exception as exc:  # noqa: BLE001 - a probe must never raise into the gate
        _warn(f"process_state({identity.pid})", exc)
        return "unknown"
    return "alive" if _matches(identity, current) else "gone"


def children_map() -> dict[int, list[int]]:
    """One snapshot of the process table: parent PID -> child PIDs."""
    impls = {
        "win32": _win_children_map,
        "linux": _linux_children_map,
        "darwin": _mac_children_map,
    }
    impl = impls.get(_platform())
    return {} if impl is None else _probe("children_map", {}, impl)


def _is_child_of(parent: ProcessIdentity, child: ProcessIdentity) -> bool:
    # A recorded parent PID can outlive the parent (Windows never updates it):
    # once that PID is reused, the newcomer "adopts" every orphan that names
    # it. A real child cannot predate its parent, so an older one is not
    # counted. Every admitted parent is itself no older than the root, so the
    # whole tree is no older than the root.
    return child.create_time >= parent.create_time


def identity_tree(root: ProcessIdentity) -> list[ProcessIdentity]:
    """`root` then every verified descendant, breadth first; [] once `root` is gone.

    The root is re-checked after the process-table snapshot: had its PID been
    reused before the snapshot, the newcomer's children would be listed
    under it. Nodes that cannot be read are neither included nor descended
    through.
    """
    mapping = children_map()
    if not is_same_process(root):
        return []
    seen = {root.pid}
    found = [root]
    queue = deque([root])
    while queue:
        parent = queue.popleft()
        for pid in mapping.get(parent.pid, []):
            if pid in seen:
                continue  # already seen: the root, a cycle, or a duplicate
            seen.add(pid)
            child = process_identity(pid)
            # Fail closed on an unreadable node: a missed descendant only
            # leaks, a wrongly admitted one could later be killed.
            if child is not None and _is_child_of(parent, child):
                found.append(child)
                queue.append(child)
    return found


def descendants(root_pid: int) -> list[int]:
    """Every process below `root_pid` (excluding the root itself), breadth first."""
    return [identity.pid for identity in process_tree(root_pid)[1:]]


def process_tree(root_pid: int) -> list[ProcessIdentity]:
    """The root's identity first, then each verified descendant's."""
    root = process_identity(root_pid)
    return [] if root is None else identity_tree(root)


def working_set_bytes(pid: int) -> int | None:
    """Resident memory of one process, in bytes (Windows working set / POSIX RSS)."""
    impls = {
        "win32": _win_working_set,
        "linux": _linux_working_set,
        "darwin": _mac_working_set,
    }
    impl = impls.get(_platform())
    if impl is None or pid <= 0:
        return None
    return _probe(f"working_set_bytes({pid})", None, lambda: impl(pid))


def tree_working_set_bytes(root_pid: int) -> int | None:
    """Resident memory of a whole process tree; None when the root itself is unreadable."""
    tree = process_tree(root_pid)
    if not tree:
        return None
    root_size = working_set_bytes(root_pid)
    if root_size is None:
        return None
    sizes = (working_set_bytes(identity.pid) for identity in tree[1:])
    return root_size + sum(size for size in sizes if size is not None)


def terminate(identity: ProcessIdentity) -> bool:
    """Kill exactly the process `identity` names; True only when it was killed by this call.

    Refuses (False) when that PID now belongs to a different process, when
    nothing runs there, and when asked to kill the calling process itself.
    """
    if identity.pid <= 0 or identity.pid == os.getpid():
        return False
    impls = {
        "win32": _win_terminate,
        "linux": _linux_terminate,
        "darwin": _posix_terminate,
    }
    impl = impls.get(_platform())
    if impl is None:
        return False
    return _probe(f"terminate({identity.pid})", False, lambda: impl(identity))
