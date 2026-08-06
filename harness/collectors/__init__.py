"""Best-effort telemetry collectors for agent CLIs.

Collector contract
-------------------
Each collector is a module ``harness.collectors.<name>`` (name uses
underscores, e.g. ``claude_code``) exposing:

    def collect(since: datetime | None) -> list[dict]:
        ...

`collect` returns a list of schema-1 records (see `harness.telemetry_store`)
for events observed after `since` (or all history if `since` is None). It
reads only local files on this machine, never opens a network socket, and
should try hard not to raise -- but `run_collector` below treats it as
best-effort regardless: any exception it raises is caught and turned into a
single `collector_error` record instead of propagating.

`run_collector` is the only supported entry point for driving a collector --
it is what makes "best-effort" an enforced property rather than a
convention each collector has to individually honor.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

from harness import telemetry_store

__all__ = ["run_collector"]


def _collector_error(agent: str, message: str) -> dict:
    return {
        "schema": telemetry_store.SCHEMA_VERSION,
        "ts": telemetry_store.format_ts(datetime.now(timezone.utc)),
        "agent": agent,
        "event": "collector_error",
        "source": message,
    }


def run_collector(name: str, since: datetime | None = None) -> list[dict]:
    """Run collector `name` (a value from `telemetry_store.AGENTS`) and
    return schema-valid records. Never raises.

    - If the collector module can't be imported, or `collect()` raises any
      exception, returns a single-element list containing one
      `collector_error` record (never an empty list AND never a raise).
    - Any individual record returned by the collector that fails
      `telemetry_store.validate_record` is dropped and replaced with its own
      `collector_error` record, describing what was dropped and why -- valid
      records from the same batch are still returned.
    """
    module_name = name.replace("-", "_")
    try:
        module = importlib.import_module(f"harness.collectors.{module_name}")
        raw_records = list(module.collect(since))
    except Exception as exc:  # noqa: BLE001 -- best-effort by design, see docstring
        return [_collector_error(name, f"collector '{name}' raised: {exc!r}")]

    now = datetime.now(timezone.utc)
    results: list[dict] = []
    for raw in raw_records:
        try:
            results.append(telemetry_store.validate_record(raw, now=now))
        except Exception as exc:  # noqa: BLE001 -- see docstring
            results.append(_collector_error(name, f"dropped invalid record from '{name}': {exc!r}"))
    return results
