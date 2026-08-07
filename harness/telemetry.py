"""Local-only telemetry CLI: collect, tail, and aggregate agent session
events (see `harness.telemetry_store` for the on-disk schema).

Collection is manual/CLI-only in this phase -- nothing here is wired to a
hook or runs automatically. Nothing in this module opens a network socket;
`collect` only reads local agent log files and appends to the local JSONL
store.

Usage:
    python harness/telemetry.py collect [--agent claude-code|codex|gemini]
    python harness/telemetry.py tail [-n N] [--agent AGENT] [--project P]
    python harness/telemetry.py aggregate [--by agent|project] [--since ISO8601]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent

try:
    from harness import telemetry_store
    from harness.collectors import run_collector
except ImportError:  # running as `python harness/telemetry.py`, not `-m harness.telemetry`
    sys.path.insert(0, str(HARNESS_ROOT))
    from harness import telemetry_store
    from harness.collectors import run_collector


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

    tail_p = sub.add_parser("tail", help="print the most recent events")
    tail_p.add_argument("-n", type=int, default=20, help="number of records to print (default: 20)")
    tail_p.add_argument("--agent", choices=telemetry_store.AGENTS, default=None)
    tail_p.add_argument("--project", default=None)

    agg_p = sub.add_parser("aggregate", help="print per-group event/token/cost summaries")
    agg_p.add_argument("--by", choices=("agent", "project"), default="agent")
    agg_p.add_argument("--since", default=None, help="ISO 8601 timestamp; only include events at/after it")

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
    path = _collect_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _record_ts(rec: dict) -> datetime | None:
    ts = rec.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return telemetry_store.parse_ts(ts)
    except ValueError:
        return None


def cmd_collect(args: argparse.Namespace) -> int:
    agents = [args.agent] if args.agent else list(telemetry_store.AGENTS)
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

        written = telemetry_store.append_events(new_records) if new_records else 0
        total_written += written

        latest = since
        for r in new_records:
            rts = _record_ts(r)
            if rts is not None and (latest is None or rts > latest):
                latest = rts
        if latest is not None:
            state[agent] = telemetry_store.format_ts(latest)

        print(f"{agent}: collected {len(records)} record(s), {errors} error(s), wrote {written}")
    _save_collect_state(state)
    print(f"total written: {total_written}")
    return 0


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "collect":
        return cmd_collect(args)
    if args.command == "tail":
        return cmd_tail(args)
    if args.command == "aggregate":
        return cmd_aggregate(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
