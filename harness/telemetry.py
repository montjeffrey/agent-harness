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
import sys
from datetime import datetime, timezone

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


def cmd_collect(args: argparse.Namespace) -> int:
    agents = [args.agent] if args.agent else list(telemetry_store.AGENTS)
    total_written = 0
    for agent in agents:
        records = run_collector(agent)
        errors = sum(1 for r in records if r.get("event") == "collector_error")
        written = telemetry_store.append_events(records) if records else 0
        total_written += written
        print(f"{agent}: collected {len(records)} record(s), {errors} error(s), wrote {written}")
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
