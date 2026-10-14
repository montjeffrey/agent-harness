"""Cross-machine export of the local telemetry store.

`export_store` copies the local store into ``<dir>/<machine>/events-YYYYMM.jsonl``
so several machines can be compared. Point ``<dir>`` at a synced folder
(OneDrive, Dropbox, a network share) and the sync client moves the data;
nothing here opens a socket.

Two fields change on the way out, because the export is meant to be read on
other machines:

- `source` (a local transcript path) is dropped.
- `project` (a local path) becomes a stable key that is the same on every
  machine: ``git:<hash>`` of the normalised git remote URL when the project
  is a git checkout with a remote, else ``dir:<hash>`` of the directory
  basename. Remote URLs are read from ``.git/config`` directly; no git
  process is started (a git subprocess stalls inside the MCP server, and a
  hook should not pay for one either).

Keys are cached in ``~/.agent-harness/telemetry/project_keys.json`` the first
time a path is seen, so a project keeps its key after its checkout is
deleted. Without the cache a removed clone would fall back from ``git:`` to
``dir:`` and split its history in two.

Each export rewrites this machine's month files in full from the local store
(the store is append-only and keeps all history), so running it twice never
duplicates a record. Files whose content would not change are left alone, so
an idle machine causes no sync traffic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from harness import atomicwrite, telemetry_store

KEY_PREFIXES: tuple[str, ...] = ("git:", "dir:")

# Fields that only mean something on the machine that wrote them.
_EXPORT_DROP: tuple[str, ...] = ("source",)

_SAFE_DIR = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_REMOTE_SECTION = re.compile(r'^\s*\[\s*remote\s+"([^"]+)"\s*\]', re.IGNORECASE)
_ANY_SECTION = re.compile(r"^\s*\[")
_URL_LINE = re.compile(r"^\s*url\s*=\s*(.+?)\s*$", re.IGNORECASE)
# scp-like syntax: [user@]host:path, but not a Windows drive (C:\ or C:/).
_SCP_LIKE = re.compile(r"^(?:[^@/\\]+@)?([^:/\\]{2,}):(?!//)(.+)$")


# --- project keys -------------------------------------------------------------


def is_project_key(value: object) -> bool:
    return isinstance(value, str) and value.startswith(KEY_PREFIXES)


def _find_git_dir(start: Path) -> Path | None:
    """The git directory holding `start`'s config, or None.

    Follows a worktree's ``.git`` file to its ``commondir``, so every
    worktree of a repository resolves to the main checkout's config (and so
    to the same remote and the same key).
    """
    try:
        candidates = (start, *start.parents)
    except (TypeError, ValueError):
        return None
    for directory in candidates:
        dot_git = directory / ".git"
        try:
            if dot_git.is_dir():
                return dot_git
            if not dot_git.is_file():
                continue
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        git_dir = Path(text[len("gitdir:"):].strip())
        if not git_dir.is_absolute():
            git_dir = directory / git_dir
        common = git_dir / "commondir"
        try:
            if common.is_file():
                target = Path(common.read_text(encoding="utf-8").strip())
                git_dir = target if target.is_absolute() else git_dir / target
        except OSError:
            pass
        return git_dir
    return None


def _remote_urls(config_text: str) -> dict[str, str]:
    """``{remote name: url}`` from git config text, first url per remote.

    Hand-rolled because `configparser` reads git's tab-indented keys as
    continuation lines.
    """
    urls: dict[str, str] = {}
    current: str | None = None
    for line in config_text.splitlines():
        match = _REMOTE_SECTION.match(line)
        if match:
            current = match.group(1)
            continue
        if _ANY_SECTION.match(line):
            current = None
            continue
        if current is not None and current not in urls:
            match = _URL_LINE.match(line)
            if match:
                urls[current] = match.group(1)
    return urls


def git_remote_url(project: Path) -> str | None:
    """The ``origin`` remote URL of the checkout holding `project` (else the
    first remote listed), or None when there is no checkout or no remote."""
    git_dir = _find_git_dir(project)
    if git_dir is None:
        return None
    try:
        text = (git_dir / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    urls = _remote_urls(text)
    if not urls:
        return None
    return urls.get("origin") or next(iter(urls.values()))


def normalize_remote(url: str) -> str:
    """One spelling per repository: ``host/owner/repo``, lower case.

    ``git@github.com:me/x.git``, ``https://github.com/me/x`` and
    ``ssh://git@github.com/me/x.git`` all become ``github.com/me/x``, so a
    machine that cloned over SSH and one that cloned over HTTPS agree. User
    names, passwords and ports are discarded with the rest of the netloc.
    """
    text = url.strip()
    if "://" in text:
        parts = urlsplit(text)
        host, path = (parts.hostname or ""), parts.path
    else:
        match = _SCP_LIKE.match(text)
        host, path = (match.group(1), match.group(2)) if match else ("", text)
    path = path.replace("\\", "/").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}/{path}".lower()


def _basename(project: str) -> str:
    return re.split(r"[\\/]", project.rstrip("\\/"))[-1].lower()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def compute_project_key(project: str | None) -> str | None:
    """The cross-machine key for a local project path (uncached)."""
    if not project or not isinstance(project, str):
        return None
    if is_project_key(project):
        return project
    url = git_remote_url(Path(project))
    if url:
        return "git:" + _digest(normalize_remote(url))
    base = _basename(project)
    return "dir:" + _digest(base) if base else None


def _key_cache_path() -> Path:
    return telemetry_store.telemetry_dir() / "project_keys.json"


class ProjectKeys:
    """Path -> key lookups, cached on disk, plus a key -> local label map.

    Labels (the directory basename) are for this machine's own reports
    only; they are never written into an export.
    """

    def __init__(self, cache: dict[str, str] | None = None) -> None:
        self._cache: dict[str, str] = dict(cache or {})
        self._dirty = False
        self.labels: dict[str, str] = {}
        for path, key in self._cache.items():
            self.labels.setdefault(key, _basename(path))

    @classmethod
    def load(cls) -> ProjectKeys:
        try:
            data = json.loads(_key_cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        keys = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(keys, dict):
            keys = {}
        return cls({p: k for p, k in keys.items() if isinstance(p, str) and is_project_key(k)})

    def key(self, project: str | None) -> str | None:
        if not project or not isinstance(project, str):
            return None
        if is_project_key(project):
            return project
        key = self._cache.get(project)
        if key is None:
            key = compute_project_key(project)
            if key is None:
                return None
            self._cache[project] = key
            self._dirty = True
        self.labels.setdefault(key, _basename(project))
        return key

    def save(self) -> None:
        if not self._dirty:
            return
        path = _key_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomicwrite.atomic_sibling(path) as tmp:
            tmp.write_text(json.dumps({"keys": self._cache}, indent=1, sort_keys=True), encoding="utf-8")
        self._dirty = False


# --- export -------------------------------------------------------------------


def _month(ts: object) -> str | None:
    if not isinstance(ts, str):
        return None
    try:
        return telemetry_store.parse_ts(ts).strftime("%Y%m")
    except ValueError:
        return None


def _long_path(path: Path) -> Path:
    """`path` in extended-length form on Windows when it is near MAX_PATH.

    A synced folder can sit deep in a profile, and the atomic write's temp
    sibling adds ~50 characters to the shard name; past 260 the write fails
    with a misleading FileNotFoundError.
    """
    text = os.path.abspath(path)
    if os.name == "nt" and len(text) >= 200 and not text.startswith("\\\\?\\"):
        return Path("\\\\?\\" + text)
    return path


def _write_if_changed(path: Path, content: str) -> bool:
    path = _long_path(path)
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomicwrite.atomic_sibling(path) as tmp:
        tmp.write_text(content, encoding="utf-8")
    return True


def export_record(rec: dict, machine: str, keys: ProjectKeys) -> dict:
    """`rec` as it goes into an export: machine stamped, `source` dropped,
    `project` replaced by its cross-machine key."""
    out = {k: v for k, v in rec.items() if k not in _EXPORT_DROP}
    out["machine"] = rec.get("machine") or machine
    if "project" in out:
        out["project"] = keys.key(out["project"])
    return out


def export_store(to: Path) -> dict:
    """Write this machine's store into `to`; return a short summary.

    `collector_error` records are not exported: they describe this
    machine's collector, not usage, and their text is a local diagnostic.
    """
    identity = telemetry_store.machine_identity(create=True) or {}
    machine = identity["id"]
    keys = ProjectKeys.load()

    shards: dict[tuple[str, str], list[str]] = defaultdict(list)
    exported = 0
    for rec in telemetry_store.iter_events():
        if rec.get("event") == "collector_error":
            continue
        month = _month(rec.get("ts"))
        if month is None:
            continue
        out = export_record(rec, machine, keys)
        if not _SAFE_DIR.match(str(out["machine"])):
            continue
        shards[(out["machine"], month)].append(json.dumps(out, ensure_ascii=False, sort_keys=True))
        exported += 1
    keys.save()

    written = unchanged = 0
    for (owner, month), lines in sorted(shards.items()):
        changed = _write_if_changed(to / owner / f"events-{month}.jsonl", "\n".join(lines) + "\n")
        written += changed
        unchanged += not changed

    card = {"id": machine}
    if identity.get("label"):
        card["label"] = identity["label"]
    _write_if_changed(to / machine / "machine.json", json.dumps(card, indent=2, sort_keys=True) + "\n")

    return {
        "machine": machine,
        "records": exported,
        "files_written": written,
        "files_unchanged": unchanged,
        "dir": str(to / machine),
    }


# --- reading an export tree ---------------------------------------------------


def export_machines(root: Path) -> dict[str, str | None]:
    """``{machine id: label or None}`` for every machine folder under `root`."""
    machines: dict[str, str | None] = {}
    root = _long_path(root)
    if not root.is_dir():
        return machines
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        label = None
        try:
            card = json.loads((sub / "machine.json").read_text(encoding="utf-8"))
            if isinstance(card, dict) and isinstance(card.get("label"), str):
                label = card["label"]
        except (OSError, ValueError):
            pass
        machines[sub.name] = label
    return machines


def iter_export(root: Path) -> Iterator[dict]:
    """Every record in an export tree, machine by machine, oldest month first.

    A record without a `machine` field takes its folder's name, so a shard
    copied in by hand is still attributable.
    """
    for machine in export_machines(root):
        shards = sorted(_long_path(root / machine).glob("events-*.jsonl"))
        for rec in telemetry_store.iter_shards(shards):
            rec.setdefault("machine", machine)
            yield rec
