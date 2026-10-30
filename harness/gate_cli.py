"""Command line for the on-demand MCP gate: `agent-harness-gate`.

Subcommands: `serve` (the default; the stdio MCP server itself), `status`
(configured servers and their cached manifests), `warm` (capture manifests),
and `report` (per-server usage and keep/remove advice from the gate's
accounting records, see `harness.gate_report`).

`status` and `report` work without the optional `mcp` SDK; `serve` and
`warm` explain a missing extra instead of crashing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harness.gate_config import (
    GateConfig,
    GateConfigError,
    ManifestCache,
    default_config_path,
    load_config,
)


def _status_lines(config: GateConfig) -> list[str]:
    if not config.servers:
        return [f"no gated servers configured (config: {default_config_path()})"]
    cache = ManifestCache(config.cache_dir)
    lines = []
    for name, spec in sorted(config.servers.items()):
        tools = cache.load(spec)
        cached = "not cached (run warm)" if tools is None else f"{len(tools)} tools cached"
        lines.append(f"{name}: {cached}; idle timeout {spec.idle_timeout_seconds:g}s")
    return lines


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of days >= 1, got {text!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-harness-gate",
        description="Serve heavy MCP servers on demand: asleep until used, stopped when idle.",
    )
    # Accepted before or after the subcommand: agent MCP configs naturally
    # write `serve --config ...`. SUPPRESS keeps a subcommand from resetting a
    # value given before it.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    for target in (parser, common):
        target.add_argument(
            "--config", type=Path, help="gate config (default: ~/.agent-harness/gate.json)"
        )
        target.add_argument("--cache-dir", type=Path, help="tool manifest cache directory")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", parents=[common], help="run the gate as a stdio MCP server (default)")
    sub.add_parser(
        "status", parents=[common], help="list gated servers and their cached tool manifests"
    )
    warm_parser = sub.add_parser(
        "warm", parents=[common], help="capture tool manifests (wakes each server once)"
    )
    warm_parser.add_argument("servers", nargs="*", help="servers to warm (default: all)")
    report_parser = sub.add_parser(
        "report", parents=[common], help="per-server usage and advice from the gate's records"
    )
    report_parser.add_argument(
        "--days", type=_positive_int, default=7, help="window in days (default: 7)"
    )
    report_parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        config = load_config(args.config, args.cache_dir)
    except GateConfigError as exc:
        print(f"agent-harness-gate: {exc}", file=sys.stderr)
        return 2

    if args.command == "status":
        print("\n".join(_status_lines(config)))
        return 0
    if args.command == "report":
        from harness.gate_report import run_report

        print(run_report(days=args.days, as_json=args.json, configured=config.servers), end="")
        return 0

    try:
        import anyio
        import mcp  # noqa: F401
    except ImportError:
        print(
            "agent-harness-gate requires the optional 'mcp' Python SDK.\n"
            'from this checkout install with: python -m pip install -e ".[mcp]"',
            file=sys.stderr,
        )
        return 1

    from harness import mcp_gate

    if args.command == "warm":
        names = args.servers or sorted(config.servers)
        unknown = [name for name in names if name not in config.servers]
        if unknown:
            print(f"agent-harness-gate: unknown servers: {', '.join(unknown)}", file=sys.stderr)
            return 2
        outcomes = anyio.run(mcp_gate.warm, config, names)
        for ok, message in outcomes:
            print(message if ok else f"FAILED: {message}")
        return 0 if all(ok for ok, _ in outcomes) else 1

    anyio.run(mcp_gate.serve, config)
    return 0
