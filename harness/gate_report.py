"""Per-server usage report for the on-demand MCP gate.

Reads the gate's own accounting records (``gate-*.jsonl``, written by
`harness.gate_accounting`) and answers the keep/remove questions the operator
actually has: which servers are woken and never used, which are woken so often
that their idle timeout is too short, which are slow to start for a fixable
reason, which leave processes behind, and which are being stopped because the
machine is short on memory.

Everything here is a pure function over plain dicts, so a report can be built
from any iterable of records. Only `run_report` touches the accounting module,
and it imports it lazily so this module loads without it. Malformed records
are skipped and counted (one line on stderr), never raised: one bad line in a
month of telemetry must not hide the rest of the report.
"""

from __future__ import annotations

import json
import math
import ntpath
import posixpath
import statistics
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.telemetry_store import parse_ts

SCHEMA_VERSION = 1
# Mirrors `mcp_gate.DEFAULT_IDLE_TIMEOUT_SECONDS`; not imported so the gate's
# CLI can import this module without a cycle.
DEFAULT_IDLE_SECONDS = 600.0
REWAKES_PER_DAY_LIMIT = 3.0
REWAKE_IDLE_FACTOR = 2.0
SLOW_STARTUP_MS = 10_000.0
SWEEP_SERVER = "*"
# Events a summary counts; `gate_sweep` and any future event are left out.
_SUMMARY_EVENTS = frozenset({"gate_wake", "gate_wake_failed", "gate_call", "gate_sleep"})
_NPX_NAMES = frozenset({"npx", "npx.cmd", "npx.exe"})
_NPX_FLAGS_WITH_VALUE = frozenset({"-p", "--package", "-c", "--call"})


@dataclass(frozen=True)
class ServerSummary:
    """One server's activity over the report window."""

    server: str
    wakes: int = 0
    wake_failures: int = 0
    calls: int = 0
    failed_calls: int = 0
    sessions: int = 0
    awake_s: float = 0.0
    median_startup_ms: float | None = None
    median_tree_mb: float | None = None
    mb_hours: float = 0.0
    sleep_reasons: dict[str, int] = field(default_factory=dict)
    leaked: int = 0
    recommendations: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Event:
    """A validated record: only the fields the report uses."""

    at: datetime
    event: str
    server: str
    session: str | None
    startup_ms: float | None = None
    tree_mb: float | None = None
    ok: bool = True
    reason: str | None = None
    awake_s: float = 0.0
    leaked: int = 0


class _Malformed(ValueError):
    pass


# --- record validation -------------------------------------------------------


def _number(value: object, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Malformed(f"not a number: {value!r}")
    if not math.isfinite(value):
        raise _Malformed(f"not finite: {value!r}")
    return float(value)


def _string(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise _Malformed(f"not a non-empty string: {value!r}")
    return value


def _pid_count(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, list) or not all(
        isinstance(p, int) and not isinstance(p, bool) for p in value
    ):
        raise _Malformed(f"not a list of pids: {value!r}")
    return len(value)


def _event_fields(event: str, rec: Mapping) -> dict:
    """The per-event fields, validated; raises `_Malformed` on a bad one."""
    if event == "gate_wake":
        return {
            "startup_ms": _number(rec.get("startup_ms")),
            "tree_mb": _number(rec.get("tree_mb"), nullable=True),
        }
    if event == "gate_call":
        if not isinstance(rec.get("ok"), bool):
            raise _Malformed(f"'ok' is not a bool: {rec.get('ok')!r}")
        return {"ok": rec["ok"]}
    if event == "gate_sleep":
        return {
            "reason": _string(rec.get("reason")),
            "awake_s": _number(rec.get("awake_s")),
            "tree_mb": _number(rec.get("tree_mb"), nullable=True),
            "leaked": _pid_count(rec.get("leaked_pids")),
        }
    return {}


def _parse(rec: object) -> _Event:
    """Validate one record; raises `_Malformed` (never anything else)."""
    if not isinstance(rec, Mapping):
        raise _Malformed(f"record is a {type(rec).__name__}")
    if rec.get("schema") != SCHEMA_VERSION:
        raise _Malformed(f"schema {rec.get('schema')!r}")
    try:
        at = parse_ts(_string(rec.get("ts")))
    except (TypeError, ValueError) as exc:
        raise _Malformed(f"bad ts: {exc}") from exc
    event = _string(rec.get("event"))
    return _Event(
        at=at,
        event=event,
        server=_string(rec.get("server")),
        session=_string(rec.get("session"), nullable=True),
        **_event_fields(event, rec),
    )


def _window(events: Iterable[object], since: datetime) -> list[_Event]:
    """Valid, in-window, per-server events in timestamp order (stable)."""
    kept: list[_Event] = []
    malformed = 0
    for rec in events:
        try:
            ev = _parse(rec)
        except _Malformed:
            malformed += 1
            continue
        if ev.at >= since and ev.event in _SUMMARY_EVENTS and ev.server != SWEEP_SERVER:
            kept.append(ev)
    if malformed:
        print(f"[gate-report] skipped {malformed} malformed gate record(s)", file=sys.stderr)
    return sorted(kept, key=lambda e: e.at)


# --- configured server access ------------------------------------------------


def _spec_value(spec: object, key: str) -> object:
    """Read `key` from a `ServerSpec`-like object or a raw gate.json entry."""
    if isinstance(spec, Mapping):
        return spec.get(key)
    return getattr(spec, key, None)


def _idle_seconds(spec: object) -> float:
    try:
        idle = _number(_spec_value(spec, "idle_timeout_seconds"))
    except _Malformed:
        return DEFAULT_IDLE_SECONDS
    return idle if idle and idle > 0 else DEFAULT_IDLE_SECONDS


def _command_line(spec: object) -> list[str]:
    command = _spec_value(spec, "command")
    args = _spec_value(spec, "args") or ()
    if not isinstance(command, str) or not isinstance(args, (list, tuple)):
        return []
    return [command, *(a for a in args if isinstance(a, str))]


def _basename(path: str) -> str:
    # Configs written on Windows use backslashes; split on both separators.
    return posixpath.basename(ntpath.basename(path)).lower()


def _npx_args(argv: list[str]) -> list[str] | None:
    """The arguments after `npx`, including via `cmd /c npx ...`; None if not npx."""
    if argv and _basename(argv[0]) in _NPX_NAMES:
        return argv[1:]
    if len(argv) >= 3 and _basename(argv[0]) in ("cmd", "cmd.exe") and argv[1].lower() == "/c":
        return _npx_args(argv[2:])
    return None


def _npx_package(args: list[str]) -> str | None:
    """The package npx will install: `--package` if given, else the first positional."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--package="):
            return arg.split("=", 1)[1]
        if arg in ("-p", "--package") and i + 1 < len(args):
            return args[i + 1]
        if arg in _NPX_FLAGS_WITH_VALUE:
            i += 2
            continue
        if not arg.startswith("-"):
            return arg
        i += 1
    return None


def _unpinned_npx_package(spec: object) -> str | None:
    """The npx package name when it is `@latest` or carries no version at all."""
    args = _npx_args(_command_line(spec))
    package = _npx_package(args) if args is not None else None
    if not package:
        return None
    name = package.removeprefix("@")
    version = name.split("@", 1)[1] if "@" in name else ""
    return package if version in ("", "latest") else None


# --- recommendations ---------------------------------------------------------


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _quick_rewakes(events: list[_Event], idle: float) -> tuple[int, int]:
    """(re-wakes after an idle stop, those within 2x idle of that stop).

    Only an `idle` stop is one a longer timeout would have avoided; a wake
    after a session exit or a pressure stop is not counted either way. Each
    session is its own gate process with its own idle timer, so a wake is
    paired only with a stop from the same session.
    """
    rewakes = quick = 0
    last_sleep: dict[str | None, _Event] = {}
    for ev in events:
        if ev.event == "gate_sleep":
            last_sleep[ev.session] = ev
        elif ev.event == "gate_wake":
            stop = last_sleep.pop(ev.session, None)
            if stop is not None and stop.reason == "idle":
                rewakes += 1
                gap = (ev.at - stop.at).total_seconds()
                quick += gap <= REWAKE_IDLE_FACTOR * idle
    return rewakes, quick


def _idle_advice(events: list[_Event], days: int, spec: object) -> str | None:
    idle = _idle_seconds(spec)
    rewakes, quick = _quick_rewakes(events, idle)
    # Per day of same-session re-wakes: each session's first wake is one no
    # timeout could avoid, so counting every wake would overstate the rate.
    per_day = rewakes / days
    if per_day <= REWAKES_PER_DAY_LIMIT or not rewakes or quick * 2 <= rewakes:
        return None
    return (
        f"re-woken {per_day:.1f}x/day, {quick} of {rewakes} re-wakes within "
        f"{REWAKE_IDLE_FACTOR * idle:g}s of an idle stop: raise idle_timeout_seconds "
        f"(now {idle:g}s)"
    )


def _recommend(
    s: ServerSummary, events: list[_Event], *, configured: bool, spec: object, days: int
) -> tuple[str, ...]:
    """The keep/remove advice for one server, one line each."""
    out: list[str] = []
    if configured and s.calls == 0:
        out.append(
            f"no calls in the last {_plural(days, 'day')}: remove it from gate.json"
            " (or turn it off in the client)"
        )
    idle = _idle_advice(events, days, spec)
    if idle:
        out.append(idle)
    package = _unpinned_npx_package(spec) if spec is not None else None
    if package and s.median_startup_ms is not None and s.median_startup_ms > SLOW_STARTUP_MS:
        out.append(
            f"median startup {s.median_startup_ms / 1000:.1f}s from unpinned npx package "
            f"{package}: pin the package version so warm starts hit the npx cache"
        )
    if s.leaked:
        out.append(
            f"{_plural(s.leaked, 'process', 'processes')} survived a stop: the server leaves "
            "processes behind on stop; investigate"
        )
    sleeps = sum(s.sleep_reasons.values())
    pressure = s.sleep_reasons.get("pressure", 0)
    if sleeps and pressure * 2 > sleeps:
        out.append(
            f"{pressure} of {sleeps} stops were for memory pressure: the machine is "
            "short on RAM while it is in use"
        )
    return tuple(out)


# --- summary -----------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _tally(server: str, events: list[_Event]) -> ServerSummary:
    """Counts and sums for one server (no recommendations yet)."""
    wakes = [e for e in events if e.event == "gate_wake"]
    calls = [e for e in events if e.event == "gate_call"]
    sleeps = [e for e in events if e.event == "gate_sleep"]
    trees = [e.tree_mb for e in wakes + sleeps if e.tree_mb is not None]
    return ServerSummary(
        server=server,
        wakes=len(wakes),
        wake_failures=sum(e.event == "gate_wake_failed" for e in events),
        calls=len(calls),
        failed_calls=sum(not e.ok for e in calls),
        sessions=len({e.session for e in events if e.session is not None}),
        awake_s=sum((e.awake_s for e in sleeps), 0.0),
        median_startup_ms=_median([e.startup_ms for e in wakes if e.startup_ms is not None]),
        median_tree_mb=_median(trees),
        mb_hours=sum(((e.tree_mb or 0.0) * e.awake_s / 3600.0 for e in sleeps), 0.0),
        sleep_reasons=dict(sorted(Counter(e.reason for e in sleeps if e.reason).items())),
        leaked=sum(e.leaked for e in sleeps),
    )


def summarize(
    events: Iterable[dict],
    *,
    configured: Mapping[str, object] | None = None,
    days: int,
    now: datetime,
) -> list[ServerSummary]:
    """Summarize gate records from the last `days` days before `now`.

    `configured` maps server name to its config (a `ServerSpec` or a raw
    gate.json entry); configured servers with no events still get a row, so
    an unused one can be recommended for removal. Rows are ordered by
    MB-hours held, largest first, then by name.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError(f"days must be a positive integer, got {days!r}")
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    configured = configured or {}
    by_server: dict[str, list[_Event]] = {name: [] for name in configured}
    for ev in _window(events, now - timedelta(days=days)):
        by_server.setdefault(ev.server, []).append(ev)
    summaries = []
    for server, evs in by_server.items():
        tally = _tally(server, evs)
        recs = _recommend(
            tally, evs, configured=server in configured, spec=configured.get(server), days=days
        )
        summaries.append(_with_recommendations(tally, recs))
    return sorted(summaries, key=lambda s: (-s.mb_hours, s.server))


def _with_recommendations(s: ServerSummary, recs: tuple[str, ...]) -> ServerSummary:
    return ServerSummary(**{**asdict(s), "recommendations": recs})


# --- rendering ---------------------------------------------------------------

_COLUMNS = (
    ("wakes", 5), ("fail", 4), ("calls", 5), ("err", 4), ("sess", 4),
    ("warm_min", 8), ("start_ms", 8), ("tree_mb", 7), ("mb_h", 7),
)


def _fmt(value: float | None, places: int) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _cells(s: ServerSummary) -> tuple[str, ...]:
    return (
        str(s.wakes), str(s.wake_failures), str(s.calls), str(s.failed_calls),
        str(s.sessions), _fmt(s.awake_s / 60.0, 1), _fmt(s.median_startup_ms, 0),
        _fmt(s.median_tree_mb, 1), _fmt(s.mb_hours, 1),
    )


def _row(name: str, width: int, cells: tuple[str, ...]) -> str:
    parts = [f"{name:<{width}}"]
    parts += [f"{cell:>{w}}" for cell, (_, w) in zip(cells, _COLUMNS, strict=True)]
    return "  ".join(parts)


def render_text(summaries: Iterable[ServerSummary], *, days: int) -> str:
    """A compact table, largest holder first, then one line per recommendation."""
    rows = list(summaries)
    if not rows:
        return f"No gate activity recorded in the last {_plural(days, 'day')}.\n"
    width = max(len("server"), *(len(s.server) for s in rows))
    lines = [
        f"Gate usage over the last {_plural(days, 'day')} (by MB-hours held)",
        _row("server", width, tuple(name for name, _ in _COLUMNS)),
        *(_row(s.server, width, _cells(s)) for s in rows),
    ]
    advice = [f"  - {s.server}: {rec}" for s in rows for rec in s.recommendations]
    lines += ["", "Recommendations:", *advice] if advice else ["", "Recommendations: none"]
    return "\n".join(lines) + "\n"


def _json_value(value: object) -> object:
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, tuple):
        return list(value)
    return value


def render_json(summaries: Iterable[ServerSummary]) -> str:
    """The summaries as stable JSON (sorted keys, floats rounded to 3 places)."""
    servers = [{k: _json_value(v) for k, v in asdict(s).items()} for s in summaries]
    return json.dumps({"servers": servers}, indent=2, sort_keys=True)


def run_report(
    *,
    days: int,
    as_json: bool,
    configured: Mapping[str, object] | None,
    directory: Path | None = None,
) -> str:
    """Read the gate records for the last `days` days and render the report."""
    from harness.gate_accounting import iter_gate_events

    now = datetime.now(UTC)
    events = iter_gate_events(since=now - timedelta(days=days), directory=directory)
    summaries = summarize(events, configured=configured, days=days, now=now)
    return render_json(summaries) if as_json else render_text(summaries, days=days)
