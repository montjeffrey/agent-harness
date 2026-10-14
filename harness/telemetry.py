"""Local-only telemetry CLI: collect, tail, and aggregate agent session
events (see `harness.telemetry_store` for the on-disk schema).

Collection runs from the CLI, and at most once a day in the background from
the Claude Code SessionStart hook (`maybe_start_daily_collect`; set
AGENT_HARNESS_TELEMETRY_AUTO=0 to turn that off). Nothing in this module
opens a network socket: `collect` only reads local agent log files and
appends to the local JSONL store, and `export` only writes to a directory
you name -- point it at a synced folder to bring machines together.
`machine --export-to DIR` names that folder once, and from then on the
daily background run exports there after it collects.

Usage:
    python harness/telemetry.py collect [--agent claude-code|codex|gemini] [--quiet] [--then-export]
    python harness/telemetry.py tail [-n N] [--agent AGENT] [--project P]
    python harness/telemetry.py aggregate [--by agent|project] [--since ISO8601]
    python harness/telemetry.py export --to DIR
    python harness/telemetry.py baseline [--from DIR] [--days N] [--min-turns 3]
                                         [--by machine|project|month] [--json OUT]
    python harness/telemetry.py machine [--label TEXT] [--export-to DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import atomicwrite, telemetry_baseline, telemetry_export, telemetry_store
    from harness.collectors import run_collector
except ImportError:  # running as `python harness/telemetry.py`, not `-m harness.telemetry`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import atomicwrite, telemetry_baseline, telemetry_export, telemetry_store
    from harness.collectors import run_collector

AUTO_ENV = "AGENT_HARNESS_TELEMETRY_AUTO"
# "Once a day" for someone who starts work at roughly the same time each
# morning: a strict 24 h would skip every day the first session came a
# little earlier than yesterday's.
AUTO_INTERVAL = timedelta(hours=20)
# A collect holding the lock longer than this is assumed dead.
LOCK_STALE = timedelta(hours=2)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect", help="run collectors and append events to the local store")
    collect_p.add_argument(
        "--agent", choices=telemetry_store.AGENTS, default=None,
        help="collect only this agent (default: all of %s)" % (telemetry_store.AGENTS,),
    )
    collect_p.add_argument("--quiet", action="store_true", help="print nothing (for background runs)")
    collect_p.add_argument(
        "--then-export", action="store_true",
        help="afterwards, still holding the collect lock, export to the folder set with "
             "`machine --export-to` (nothing when none is set)",
    )

    tail_p = sub.add_parser("tail", help="print the most recent events")
    tail_p.add_argument("-n", type=int, default=20, help="number of records to print (default: 20)")
    tail_p.add_argument("--agent", choices=telemetry_store.AGENTS, default=None)
    tail_p.add_argument("--project", default=None)

    agg_p = sub.add_parser("aggregate", help="print per-group event/token/cost summaries")
    agg_p.add_argument("--by", choices=("agent", "project"), default="agent")
    agg_p.add_argument("--since", default=None, help="ISO 8601 timestamp; only include events at/after it")

    export_p = sub.add_parser(
        "export", help="write this machine's store to DIR/<machine>/ for cross-machine baselines"
    )
    export_p.add_argument("--to", required=True, type=Path, help="export root, e.g. a synced folder")

    base_p = sub.add_parser("baseline", help="per-session rollups and the K1-K6 efficiency KPIs")
    base_p.add_argument(
        "--from", dest="from_dir", type=Path, default=None,
        help="an export root holding one folder per machine (default: this machine's store)",
    )
    base_p.add_argument("--days", type=int, default=None, help="only the last N days (default: all)")
    base_p.add_argument(
        "--min-turns", type=int, default=3,
        help="minimum main-loop turns for a session to count as interactive (default: 3)",
    )
    base_p.add_argument("--by", choices=telemetry_baseline.GROUPINGS, default=None)
    base_p.add_argument(
        "--agent", choices=(*telemetry_store.AGENTS, "all"), default="claude-code",
        help="whose sessions to measure (default: claude-code, the only agent whose records carry "
             "tool and subagent counts, so the one K1-K5 are defined on; 'all' merges every agent)",
    )
    base_p.add_argument("--json", dest="json_out", type=Path, default=None, help="also write the report here")

    machine_p = sub.add_parser("machine", help="show this machine's telemetry id, or set its label")
    machine_p.add_argument("--label", default=None, help="a readable name for this machine (empty clears it)")
    machine_p.add_argument(
        "--export-to", default=None,
        help="a synced folder the daily background run exports to after it collects (empty turns it off)",
    )

    return parser.parse_args(argv)


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return telemetry_store.parse_ts(value)
    except ValueError:
        raise SystemExit(f"--since is not a valid ISO 8601 timestamp: {value!r}") from None


def _collect_state_path() -> Path:
    return telemetry_store.telemetry_dir() / ".collect_state.json"


def _load_collect_state() -> dict[str, str]:
    """Return the persisted per-agent watermark map (agent -> ISO 8601 ts).

    Missing file, unreadable file, or malformed JSON all degrade to "no
    watermarks yet" rather than raising -- the same tolerant-reader posture
    as the rest of this module.
    """
    try:
        text = _collect_state_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_collect_state(state: dict[str, str]) -> None:
    """Replace the watermark map atomically.

    A plain `write_text` truncates before it writes, and `_load_collect_state`
    reads a tolerant `{}` from anything it cannot parse -- so a process killed
    mid-write does not lose one watermark, it loses all of them silently, and
    the next `collect` re-ingests every agent's history from the beginning.
    Temp-then-replace makes the failure cost the update instead of the file,
    and picks up the contention retry the other writers already have.
    """
    path = _collect_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomicwrite.atomic_sibling(path) as tmp:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _record_ts(rec: dict) -> datetime | None:
    ts = rec.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return telemetry_store.parse_ts(ts)
    except ValueError:
        return None


def _lock_path() -> Path:
    return telemetry_store.telemetry_dir() / ".collect.lock"


def _acquire_lock() -> bool:
    """Take the collect lock, clearing one left by a dead run. False if held.

    Two collects at once would both read the same watermark and both append
    the same records. The background daily run makes that overlap possible
    (a manual `collect` while it is still reading), so it is excluded here
    rather than left to luck.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
            except OSError:
                continue
            if age < LOCK_STALE.total_seconds():
                return False
            try:
                path.unlink()
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def _release_lock() -> None:
    try:
        _lock_path().unlink()
    except OSError:
        pass


def cmd_collect(args: argparse.Namespace) -> int:
    quiet = getattr(args, "quiet", False)
    if not _acquire_lock():
        if not quiet:
            print("another collect is running; nothing done")
        return 0
    try:
        rc = _collect(args, quiet)
        # Under the same lock: an export reading the store while another
        # collect appends to it could publish a half-written month.
        if getattr(args, "then_export", False):
            rc = _export_to_target(quiet) or rc
        return rc
    finally:
        _release_lock()


def _collect(args: argparse.Namespace, quiet: bool) -> int:
    agents = [args.agent] if args.agent else list(telemetry_store.AGENTS)
    machine = (telemetry_store.machine_identity(create=True) or {}).get("id")
    state = _load_collect_state()
    total_written = 0
    for agent in agents:
        watermark_str = state.get(agent)
        since = telemetry_store.parse_ts(watermark_str) if watermark_str else None

        records = run_collector(agent, since)
        errors = sum(1 for r in records if r.get("event") == "collector_error")

        # Collectors filter inclusively (ts >= since), so the previously-seen
        # boundary record comes back too -- drop anything at or before the
        # watermark so a second run never re-appends already-stored events.
        if since is not None:
            new_records = [
                r for r in records
                if (rts := _record_ts(r)) is None or rts > since
            ]
        else:
            new_records = records
        if machine:
            new_records = [dict(r, machine=r.get("machine") or machine) for r in new_records]

        written = telemetry_store.append_events(new_records) if new_records else 0
        total_written += written

        latest = since
        for r in new_records:
            rts = _record_ts(r)
            if rts is not None and (latest is None or rts > latest):
                latest = rts
        if latest is not None:
            state[agent] = telemetry_store.format_ts(latest)

        if not quiet:
            print(f"{agent}: collected {len(records)} record(s), {errors} error(s), wrote {written}")
    _save_collect_state(state)
    if not quiet:
        print(f"total written: {total_written}")
    return 0


def _export_status_path() -> Path:
    return telemetry_store.telemetry_dir() / ".auto_export.json"


def _load_export_status() -> dict:
    try:
        data = json.loads(_export_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_export_status(status: dict) -> None:
    try:
        path = _export_status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomicwrite.atomic_sibling(path) as tmp:
            tmp.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _export_to_target(quiet: bool) -> int:
    """Export to the configured target, if any. Never raises.

    The background run has no console, so the outcome -- or the error, when
    the synced folder is gone or unwritable -- goes to `.auto_export.json`,
    which `machine` reads back.
    """
    target = telemetry_store.export_target()
    if target is None:
        if not quiet:
            print("no export target set (agent-harness-telemetry machine --export-to DIR); nothing exported")
        return 0
    status: dict = {"ts": telemetry_store.format_ts(datetime.now(timezone.utc)), "dir": str(target)}
    try:
        summary = telemetry_export.export_store(target)
    except Exception as exc:  # noqa: BLE001 -- the background run must stay silent; the status says why
        status["error"] = f"{type(exc).__name__}: {exc}"
        _save_export_status(status)
        if not quiet:
            print(f"export to {target} failed: {status['error']}", file=sys.stderr)
        return 1
    status.update(records=summary["records"], files_written=summary["files_written"])
    _save_export_status(status)
    if not quiet:
        print(
            f"exported {summary['records']} record(s) to {summary['dir']}: "
            f"{summary['files_written']} file(s) written, {summary['files_unchanged']} unchanged"
        )
    return 0


# --- background daily collect -------------------------------------------------


def _auto_state_path() -> Path:
    return telemetry_store.telemetry_dir() / ".auto_collect.json"


def _spawn_background(argv: list[str]) -> None:
    """Start `argv` detached: no console, no inherited pipes, not waited on.

    The hook that calls this must return in milliseconds and Claude Code
    waits on its output pipes, so the child gets DEVNULL for all three.
    """
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)  # noqa: S603 -- fixed argv: our own interpreter and script


def maybe_start_daily_collect(now: datetime | None = None, spawn=None) -> bool:
    """Start a background `collect --quiet` unless one started in the last
    AUTO_INTERVAL. Returns whether one was started. Never raises.

    With an export target set (`machine --export-to`), the run is
    `collect --quiet --then-export`, so the synced folder stays current
    without anyone running `export`.

    The timestamp is written *before* the spawn, so a burst of sessions
    starting together starts one collect, not one each; the collect lock
    covers whatever still overlaps.
    """
    try:
        if os.environ.get(AUTO_ENV, "").strip().lower() in ("0", "off", "false", "no"):
            return False
        now = now or datetime.now(timezone.utc)
        path = _auto_state_path()
        try:
            last = telemetry_store.parse_ts(json.loads(path.read_text(encoding="utf-8"))["last"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            last = None
        if last is not None and timedelta(0) <= now - last < AUTO_INTERVAL:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomicwrite.atomic_sibling(path) as tmp:
            tmp.write_text(json.dumps({"last": telemetry_store.format_ts(now)}), encoding="utf-8")
        argv = [sys.executable, str(Path(__file__).resolve()), "collect", "--quiet"]
        if telemetry_store.export_target() is not None:
            argv.append("--then-export")
        (spawn or _spawn_background)(argv)
        return True
    except Exception:  # noqa: BLE001 -- a hook nicety must never break session start
        return False


def _summarize_record(rec: dict) -> str:
    parts = [rec.get("ts", "?"), rec.get("agent", "?"), rec.get("event", "?")]
    for key in ("project", "session_id", "model"):
        value = rec.get(key)
        if value:
            parts.append(f"{key}={value}")
    for key in ("input_tokens", "output_tokens", "cost_usd", "limit_remaining", "limit_kind"):
        if key in rec and rec[key] is not None:
            parts.append(f"{key}={rec[key]}")
    return " ".join(str(p) for p in parts)


def cmd_tail(args: argparse.Namespace) -> int:
    records = list(telemetry_store.iter_events(agent=args.agent, project=args.project))
    tail = records[-args.n:] if args.n > 0 else []
    if not tail:
        print("no telemetry events recorded yet")
        return 0
    for rec in tail:
        print(_summarize_record(rec))
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    since = _parse_since(args.since)
    records = list(telemetry_store.iter_events(since=since))
    if not records:
        print("no telemetry events recorded yet")
        return 0

    groups: dict[str, dict] = {}
    for rec in records:
        key = rec.get(args.by) or "(unknown)"
        group = groups.setdefault(key, {
            "count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "cost_usd_n": 0,
            "latest_limit": None,
            "latest_limit_ts": None,
        })
        group["count"] += 1
        it = rec.get("input_tokens")
        if isinstance(it, (int, float)):
            group["input_tokens"] += it
        ot = rec.get("output_tokens")
        if isinstance(ot, (int, float)):
            group["output_tokens"] += ot
        cost = rec.get("cost_usd")
        if isinstance(cost, (int, float)):
            group["cost_usd"] += cost
            group["cost_usd_n"] += 1
        if "limit_remaining" in rec and rec["limit_remaining"] is not None:
            ts = rec.get("ts", "")
            if group["latest_limit_ts"] is None or ts >= group["latest_limit_ts"]:
                group["latest_limit_ts"] = ts
                group["latest_limit"] = (rec["limit_remaining"], rec.get("limit_kind"))

    for key in sorted(groups):
        g = groups[key]
        line = (
            f"{args.by}={key}: events={g['count']} "
            f"input_tokens={g['input_tokens']} output_tokens={g['output_tokens']}"
        )
        if g["cost_usd_n"]:
            line += f" cost_usd={g['cost_usd']:.4f} (reported, n={g['cost_usd_n']})"
        else:
            line += " cost_usd=(none reported)"
        if g["latest_limit"] is not None:
            remaining, kind = g["latest_limit"]
            line += f" latest_limit_remaining={remaining} ({kind})"
        print(line)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    summary = telemetry_export.export_store(args.to.expanduser())
    print(
        f"exported {summary['records']} record(s) for machine {summary['machine']} to {summary['dir']}: "
        f"{summary['files_written']} file(s) written, {summary['files_unchanged']} unchanged"
    )
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    keys = telemetry_export.ProjectKeys.load()
    if args.from_dir is not None:
        root = args.from_dir.expanduser()
        if not root.is_dir():
            raise SystemExit(f"--from is not a directory: {root}")
        machines = telemetry_export.export_machines(root)
        records = telemetry_export.iter_export(root)
        default_machine = "unknown"
    else:
        identity = telemetry_store.machine_identity(create=False) or {}
        default_machine = identity.get("id") or "local"
        machines = {default_machine: identity.get("label")}
        records = telemetry_store.iter_events()

    report = telemetry_baseline.baseline(
        records,
        days=args.days,
        min_turns=args.min_turns,
        by=args.by,
        agent=None if args.agent == "all" else args.agent,
        default_machine=default_machine,
        project_key=keys.key,
        labels=keys.labels,
        machines=machines,
    )
    keys.save()
    if report["overall"]["sessions"] == 0:
        print("no sessions match (run `agent-harness-telemetry collect` first, or lower --min-turns)")
    else:
        sys.stdout.write(telemetry_baseline.render(report))
    if args.json_out is not None:
        out = args.json_out.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
        print(f"wrote {out}")
    return 0


def cmd_machine(args: argparse.Namespace) -> int:
    if args.label is not None:
        telemetry_store.set_machine_label(args.label)
    if args.export_to is not None:
        telemetry_store.set_export_target(args.export_to)
    identity = telemetry_store.machine_identity(create=True) or {}
    label = identity.get("label")
    print(f"machine {identity.get('id')}" + (f" ({label})" if label else " (no label)"))

    target = identity.get("export_to")
    if not target:
        print("daily export: off (set one with --export-to DIR)")
        return 0
    print(f"daily export: {target}")
    status = _load_export_status()
    if status.get("dir") != target:
        print("  not exported there yet; the next daily run will (or run `export --to` now)")
    elif status.get("error"):
        print(f"  last attempt {status.get('ts')} failed: {status['error']}")
    else:
        print(f"  last export {status.get('ts')}: {status.get('records')} record(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "tail":
        return cmd_tail(args)
    if args.command == "aggregate":
        return cmd_aggregate(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "baseline":
        return cmd_baseline(args)
    if args.command == "machine":
        return cmd_machine(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
