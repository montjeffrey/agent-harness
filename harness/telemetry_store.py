"""Local-only, never-transmitted telemetry event store.

One JSON object per line (JSONL), UTF-8, appended to month-sharded files
under ``~/.agent-harness/telemetry/events-YYYYMM.jsonl``. Nothing in this
module opens a network socket, and no record is ever deleted automatically
in this phase (retention policy: none yet -- left for a future phase).

Schema (version 2) -- one JSON object per line:

    schema          int, required; ``2`` for new records, ``1`` still read
    ts              str, required, ISO 8601 UTC with a ``Z`` suffix
    agent           str, required, one of AGENTS
    event           str, required, one of EVENTS
    project         str | None, optional
    session_id      str | None, optional
    input_tokens    int, optional -- omitted (not zero) when unknown
    output_tokens   int, optional -- omitted (not zero) when unknown
    cost_usd        float, optional -- only when the source reports cost
                    directly; this toolkit never estimates cost from a
                    price table
    model           str, optional
    limit_remaining int | float, optional
    limit_kind      str, optional ("requests" | "tokens" | "percent") --
                    required whenever limit_remaining is present
    source          str, optional -- local file path only

Added in version 2, all optional, so a version-1 record is still a valid
version-2 record once its ``schema`` number is ignored:

    machine            str -- random id from ``~/.agent-harness/machine.json``
                       (see `machine_identity`); never the hostname
    cache_write_tokens int -- the part of ``input_tokens`` written to cache
    cache_read_tokens  int -- the part of ``input_tokens`` read from cache
    tool_calls         int -- tool_use blocks in the response
    harness_tool_calls int -- the subset named ``mcp__agent-harness__*``
    harness_tools      dict[str, int] -- that subset by short tool name
                       (``memory_remember``), omitted when empty
    subagent           bool -- ``true`` for a subagent turn, omitted otherwise

``input_tokens`` keeps its version-1 meaning: every input token of the turn,
cached or not. The cache fields split it rather than add to it, so a
version-1 reader summing ``input_tokens`` gets the same total as before.

Unknown extra fields on a record are preserved (forward-compat for a future
schema version); readers skip any record whose ``schema`` is not a version
this module understands, and skip malformed lines outright, rather than
raising -- a corrupt or partially-written line must never crash a reader.

Config files elsewhere in this toolkit (``~/.agent-harness/config.json``)
get an ownership/permission trust check because their content can drive
shell execution (`key_command`). Telemetry files carry no such risk -- they
are only ever read and written as inert JSON by this module -- so no trust
check is performed here.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from harness import atomicwrite

SCHEMA_VERSION = 2

# Every version a reader in this module accepts. Version 2 only added
# optional fields, so nothing reads differently between the two.
SUPPORTED_SCHEMAS: tuple[int, ...] = (1, 2)

_COUNT_FIELDS: tuple[str, ...] = (
    "cache_write_tokens",
    "cache_read_tokens",
    "tool_calls",
    "harness_tool_calls",
)

AGENTS: tuple[str, ...] = ("claude-code", "codex", "gemini")

EVENTS: tuple[str, ...] = (
    "session_start",
    "session_end",
    "usage",
    "rate_limit",
    "collector_error",
)

_VALID_LIMIT_KINDS: tuple[str, ...] = ("requests", "tokens", "percent")


def telemetry_dir() -> Path:
    """Root directory for all telemetry files, created lazily on first write."""
    return Path.home() / ".agent-harness" / "telemetry"


def machine_path() -> Path:
    return Path.home() / ".agent-harness" / "machine.json"


def _read_machine_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
        return None
    return data


def _write_machine_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomicwrite.atomic_sibling(path) as tmp:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def machine_identity(create: bool = True) -> dict | None:
    """This machine's telemetry identity: ``{"id": ..., "label": ...}``.

    The id is random, made once and kept in `machine_path()`. It is never
    derived from the hostname, user name or hardware, because exported
    records leave this machine (to a synced folder) and the id is all that
    has to tie them together. `label` is whatever the user set with
    `set_machine_label`, or absent.

    Returns None only when `create` is False and no identity exists yet.
    """
    path = machine_path()
    data = _read_machine_file(path)
    if data is not None or not create:
        return data
    data = {"id": "m-" + secrets.token_hex(6), "created": format_ts(datetime.now(timezone.utc))}
    _write_machine_file(path, data)
    return data


def set_machine_label(label: str | None) -> dict:
    """Set (or, with a blank `label`, clear) the user label on this machine."""
    data = dict(machine_identity(create=True) or {})
    label = (label or "").strip()
    if label:
        data["label"] = label
    else:
        data.pop("label", None)
    _write_machine_file(machine_path(), data)
    return data


def _month_shard_path(now: datetime) -> Path:
    return telemetry_dir() / f"events-{now.strftime('%Y%m')}.jsonl"


def format_ts(dt: datetime) -> str:
    """Render a datetime as ISO 8601 UTC with a literal ``Z`` suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    """Parse an ISO 8601 ``...Z`` timestamp into an aware UTC datetime.

    Raises ValueError on anything that doesn't parse -- callers that need to
    tolerate bad input (e.g. `iter_events`) catch that themselves.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_record(record: dict, now: datetime | None = None) -> dict:
    """Validate one record against the schema contract and return a
    normalized copy (never mutates the input).

    - Fills in `schema` (SCHEMA_VERSION) and `ts` (from `now`) when absent.
    - Raises ValueError for a missing/unknown `agent` or `event`, an
      unsupported `schema` version, or `limit_remaining` present without a
      matching `limit_kind`.
    - Unknown extra fields are preserved as-is.
    """
    if not isinstance(record, dict):
        raise ValueError(f"record must be a dict, got {type(record).__name__}")

    rec = dict(record)
    now = now or datetime.now(timezone.utc)

    schema = rec.setdefault("schema", SCHEMA_VERSION)
    if schema not in SUPPORTED_SCHEMAS:
        raise ValueError(f"unsupported schema version: {schema!r}")

    agent = rec.get("agent")
    if agent not in AGENTS:
        raise ValueError(f"unknown or missing agent: {agent!r} (expected one of {AGENTS})")

    event = rec.get("event")
    if event not in EVENTS:
        raise ValueError(f"unknown or missing event: {event!r} (expected one of {EVENTS})")

    ts = rec.get("ts")
    if not ts:
        rec["ts"] = format_ts(now)
    elif not isinstance(ts, str):
        raise ValueError(f"ts must be a string, got {type(ts).__name__}")
    else:
        try:
            parse_ts(ts)
        except ValueError as exc:
            raise ValueError(f"ts is not valid ISO 8601: {ts!r}") from exc

    for key in _COUNT_FIELDS:
        value = rec.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{key} must be a non-negative int, got {value!r}")

    machine = rec.get("machine")
    if machine is not None and not isinstance(machine, str):
        raise ValueError(f"machine must be a string, got {type(machine).__name__}")

    if "limit_remaining" in rec and rec["limit_remaining"] is not None:
        limit_kind = rec.get("limit_kind")
        if limit_kind not in _VALID_LIMIT_KINDS:
            raise ValueError(
                "limit_remaining present but limit_kind missing/invalid "
                f"(got {limit_kind!r}, expected one of {_VALID_LIMIT_KINDS})"
            )

    return rec


def append_events(records: Iterable[dict], now: datetime | None = None) -> int:
    """Validate and append `records` as JSONL to the current month's shard.

    All records are validated before anything is written -- a single invalid
    record raises ValueError and nothing is appended. Returns the number of
    records written.
    """
    now = now or datetime.now(timezone.utc)
    normalized = [validate_record(r, now=now) for r in records]
    if not normalized:
        return 0

    telemetry_dir().mkdir(parents=True, exist_ok=True)
    path = _month_shard_path(now)
    with path.open("a", encoding="utf-8") as fh:
        for rec in normalized:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")
    return len(normalized)


def iter_events(
    since: datetime | None = None,
    agent: str | None = None,
    project: str | None = None,
    directory: Path | None = None,
) -> Iterator[dict]:
    """Yield parsed, schema-valid records across all month shards, oldest
    file first, in on-disk line order within each file.

    Malformed lines (bad JSON, wrong type, unknown/missing schema version)
    are silently skipped -- never raised -- so one corrupt line never blocks
    reading the rest of the store.

    `directory` defaults to `telemetry_dir()`; callers that need to point at
    an explicit path (e.g. doctor's telemetry check) can override it.
    """
    directory = directory if directory is not None else telemetry_dir()
    yield from iter_shards(sorted(directory.glob("events-*.jsonl")) if directory.is_dir() else [],
                           since=since, agent=agent, project=project)


def iter_shards(
    paths: Iterable[Path],
    since: datetime | None = None,
    agent: str | None = None,
    project: str | None = None,
) -> Iterator[dict]:
    """`iter_events` over an explicit list of shard files, in the given order.

    The export tree (one folder per machine) is read through this, so it
    gets exactly the tolerant parsing the local store does.
    """
    since_utc = since
    if since_utc is not None and since_utc.tzinfo is None:
        since_utc = since_utc.replace(tzinfo=timezone.utc)

    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("schema") not in SUPPORTED_SCHEMAS:
                continue
            if agent is not None and rec.get("agent") != agent:
                continue
            if project is not None and rec.get("project") != project:
                continue
            if since_utc is not None:
                ts = rec.get("ts")
                if not isinstance(ts, str):
                    continue
                try:
                    if parse_ts(ts) < since_utc:
                        continue
                except ValueError:
                    continue
            yield rec
