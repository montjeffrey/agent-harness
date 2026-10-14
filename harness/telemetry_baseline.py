"""Usage baseline: per-session rollups and the K1-K6 efficiency KPIs.

Reads telemetry records (the local store, or a merged export tree from
several machines), rolls usage records up per session, and reports the
KPIs defined in ``docs/benchmarks/2026-10-14-usage-baseline.md``:

    K1  context per turn, p50/p90   session tokens / (main + subagent turns)
    K2  token share of the top-20 sessions (plus top 1/5/10)
    K3  harness retrieval calls per session
    K4  harness retrieval calls : memory_remember calls
    K5  context at the first main-loop turn, p50/p90
    K6  cache-hit ratio             cache read / all input

Method, matching the baseline document so numbers stay comparable:

- A session is ``(machine, agent, session_id)``. A Claude Code session
  includes its subagent turns; usage is already one record per API
  message (the collector dedupes by ``message.id``).
- "Interactive" sessions have at least ``min_turns`` main-loop turns
  (subagent turns do not count towards it). The default of 3 drops the
  one- and two-turn automated runs that would otherwise swamp every median.
- ``days`` keeps records from the last N days; a session is whatever of it
  falls inside the window.
- Percentiles interpolate linearly between closest ranks.
- Tokens, never dollars: nothing here prices usage.

Records written before schema 2 have no cache split; their input counts as
uncached and the report says how many there were.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from harness import telemetry_store

GROUPINGS: tuple[str, ...] = ("machine", "project", "month")

RETRIEVAL_TOOLS: frozenset[str] = frozenset({"memory_context", "memory_search"})
REMEMBER_TOOL = "memory_remember"

# Targets from the baseline document. K5's real target is the harness's own
# share of the first turn (<= 1.6 k), which telemetry cannot isolate, so it
# is reported without a pass/fail.
TARGETS: dict[str, tuple[str, float]] = {
    "K1": ("<=", 150_000),
    "K2": ("<=", 0.60),
    "K3": (">=", 1.0),
    "K4": (">=", 1.0),
    "K6": (">=", 0.95),
}


def is_retrieval(tool: str) -> bool:
    return tool in RETRIEVAL_TOOLS or tool.startswith("graph_")


@dataclass
class Session:
    machine: str
    agent: str
    session_id: str
    project: str | None = None
    start: str | None = None
    uncached: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    turns: int = 0
    sub_turns: int = 0
    first_ctx: int | None = None
    first_ctx_ts: str | None = None
    tool_calls: int = 0
    harness: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)
    unsplit_records: int = 0

    @property
    def total(self) -> int:
        return self.uncached + self.cache_write + self.cache_read + self.output

    @property
    def all_turns(self) -> int:
        return self.turns + self.sub_turns

    @property
    def context_per_turn(self) -> float | None:
        return self.total / self.all_turns if self.all_turns else None

    @property
    def month(self) -> str | None:
        return self.start[:7] if self.start else None


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def rollup(records: Iterable[dict], default_machine: str = "local") -> list[Session]:
    """One `Session` per ``(machine, agent, session_id)`` from usage records.

    Usage records without a session id cannot be attributed and are skipped,
    as are `<synthetic>` placeholders a schema-1 collector may have stored.
    """
    sessions: dict[tuple[str, str, str], Session] = {}
    for rec in records:
        if rec.get("event") != "usage":
            continue
        sid = rec.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        if rec.get("model") == "<synthetic>":
            continue
        machine = rec.get("machine") or default_machine
        agent = rec.get("agent") or "?"
        key = (machine, agent, sid)
        s = sessions.get(key)
        if s is None:
            s = sessions[key] = Session(machine=machine, agent=agent, session_id=sid)

        ts = rec.get("ts") if isinstance(rec.get("ts"), str) else None
        if ts and (s.start is None or ts < s.start):
            s.start = ts
        if s.project is None and isinstance(rec.get("project"), str):
            s.project = rec["project"]

        total_in = _int(rec.get("input_tokens"))
        cw = _int(rec.get("cache_write_tokens"))
        cr = _int(rec.get("cache_read_tokens"))
        if "cache_write_tokens" not in rec and "cache_read_tokens" not in rec and total_in:
            s.unsplit_records += 1
        s.cache_write += cw
        s.cache_read += cr
        s.uncached += max(0, total_in - cw - cr)
        s.output += _int(rec.get("output_tokens"))
        s.tool_calls += _int(rec.get("tool_calls"))
        tools = rec.get("harness_tools")
        if isinstance(tools, dict):
            for name, count in tools.items():
                if isinstance(name, str):
                    s.harness[name] += _int(count)
        if isinstance(rec.get("model"), str):
            s.models[rec["model"]] += 1

        if rec.get("subagent") is True:
            s.sub_turns += 1
        else:
            s.turns += 1
            if ts is not None and (s.first_ctx_ts is None or ts < s.first_ctx_ts):
                s.first_ctx_ts = ts
                s.first_ctx = total_in
    return [s for s in sessions.values() if s.total]


def percentile(values: Iterable[float], q: float) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _dist(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": percentile(values, 0.5),
        "p90": percentile(values, 0.9),
        "mean": sum(values) / len(values) if values else None,
    }


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _status(kpi: str, value: float | None) -> str | None:
    if value is None or kpi not in TARGETS:
        return None
    op, target = TARGETS[kpi]
    met = value <= target if op == "<=" else value >= target
    return "met" if met else "not met"


def kpis(sessions: list[Session], labels: dict[str, str] | None = None, top: int = 5) -> dict:
    """The KPI block for one set of sessions."""
    labels = labels or {}
    uncached = sum(s.uncached for s in sessions)
    cw = sum(s.cache_write for s in sessions)
    cr = sum(s.cache_read for s in sessions)
    out = sum(s.output for s in sessions)
    total = uncached + cw + cr + out

    totals = sorted((s.total for s in sessions), reverse=True)
    concentration = {f"top{n}": _ratio(sum(totals[:n]), total) for n in (1, 5, 10, 20)}

    harness: Counter = Counter()
    models: Counter = Counter()
    for s in sessions:
        harness.update(s.harness)
        models.update(s.models)
    retrieval = sum(c for name, c in harness.items() if is_retrieval(name))
    remember = harness.get(REMEMBER_TOOL, 0)

    ctx = _dist([s.context_per_turn for s in sessions if s.context_per_turn is not None])
    first = _dist([s.first_ctx for s in sessions if s.first_ctx])
    cache_hit = _ratio(cr, cr + cw + uncached)
    k3 = _ratio(retrieval, len(sessions))
    k4 = _ratio(retrieval, remember)

    heaviest = sorted(sessions, key=lambda s: s.total, reverse=True)[:top]
    return {
        "sessions": len(sessions),
        "first": min((s.start for s in sessions if s.start), default=None),
        "last": max((s.start for s in sessions if s.start), default=None),
        "tokens": {"input": uncached, "cache_write": cw, "cache_read": cr, "output": out, "total": total},
        "share": {
            "input": _ratio(uncached, total),
            "cache_write": _ratio(cw, total),
            "cache_read": _ratio(cr, total),
            "output": _ratio(out, total),
        },
        "turns_main": sum(s.turns for s in sessions),
        "turns_subagent": sum(s.sub_turns for s in sessions),
        "per_session_tokens": _dist([s.total for s in sessions]),
        "per_session_turns": _dist([s.turns for s in sessions]),
        "concentration": concentration,
        "harness": {
            "sessions_using": sum(1 for s in sessions if s.harness),
            "calls": dict(harness.most_common()),
            "retrieval_calls": retrieval,
            "remember_calls": remember,
        },
        "tool_calls": sum(s.tool_calls for s in sessions),
        "models": dict(models.most_common(8)),
        "unsplit_records": sum(s.unsplit_records for s in sessions),
        "kpi": {
            "K1": {"p50": ctx["p50"], "p90": ctx["p90"], "status": _status("K1", ctx["p90"])},
            "K2": {"value": concentration["top20"], "status": _status("K2", concentration["top20"])},
            "K3": {"value": k3, "status": _status("K3", k3)},
            "K4": {"retrieval": retrieval, "remember": remember, "value": k4, "status": _status("K4", k4)},
            "K5": {"p50": first["p50"], "p90": first["p90"], "status": None},
            "K6": {"value": cache_hit, "status": _status("K6", cache_hit)},
        },
        "heaviest": [
            {
                "start": s.start,
                "machine": s.machine,
                "agent": s.agent,
                "project": labels.get(s.project or "", s.project),
                "turns_main": s.turns,
                "turns_subagent": s.sub_turns,
                "tokens": s.total,
                "context_per_turn": s.context_per_turn,
                "harness_calls": sum(s.harness.values()),
            }
            for s in heaviest
        ],
    }


def _group_key(s: Session, by: str) -> str:
    if by == "machine":
        return s.machine
    if by == "project":
        return s.project or "(unknown)"
    return s.month or "(unknown)"


def baseline(
    records: Iterable[dict],
    *,
    days: int | None = None,
    min_turns: int = 3,
    by: str | None = None,
    agent: str | None = None,
    now: datetime | None = None,
    default_machine: str = "local",
    project_key: Callable[[str | None], str | None] | None = None,
    labels: dict[str, str] | None = None,
    machines: dict[str, str | None] | None = None,
) -> dict:
    """The full report: window, filters, overall KPIs, and optional groups.

    `project_key` maps a record's `project` to the key sessions group by (the
    export's cross-machine key); `labels` maps keys back to readable names
    for this machine's own projects.
    """
    if by is not None and by not in GROUPINGS:
        raise ValueError(f"unknown grouping {by!r} (expected one of {GROUPINGS})")
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None

    def selected() -> Iterable[dict]:
        for rec in records:
            if rec.get("event") != "usage":
                continue
            if agent is not None and rec.get("agent") != agent:
                continue
            if since is not None:
                try:
                    if telemetry_store.parse_ts(str(rec.get("ts"))) < since:
                        continue
                except ValueError:
                    continue
            if project_key is not None and "project" in rec:
                rec = dict(rec, project=project_key(rec.get("project")))
            yield rec

    all_sessions = rollup(selected(), default_machine=default_machine)
    sessions = [s for s in all_sessions if s.turns >= min_turns]
    report = {
        "generated": telemetry_store.format_ts(now),
        "window_days": days,
        "min_turns": min_turns,
        "agent": agent,
        "sessions_in_window": len(all_sessions),
        "tokens_in_window": sum(s.total for s in all_sessions),
        "machines": machines or {},
        "targets": {k: {"op": op, "value": v} for k, (op, v) in TARGETS.items()},
        "overall": kpis(sessions, labels),
    }
    if by:
        groups: dict[str, list[Session]] = {}
        for s in sessions:
            groups.setdefault(_group_key(s, by), []).append(s)
        report["by"] = by
        if by == "project" and labels:
            report["project_labels"] = {k: labels[k] for k in groups if k in labels}
        report["groups"] = {
            key: kpis(members, labels, top=3)
            for key, members in sorted(groups.items(), key=lambda kv: -sum(s.total for s in kv[1]))
        }
    return report


# --- text rendering -----------------------------------------------------------


def human(n: float | None) -> str:
    """``4.94 B``, ``110 M``, ``30.2 k`` -- three significant figures."""
    if n is None:
        return "-"
    for size, suffix in ((1e9, " B"), (1e6, " M"), (1e3, " k")):
        if abs(n) >= size:
            value = n / size
            if value >= 100:
                return f"{value:.0f}{suffix}"
            return f"{value:.1f}{suffix}" if value >= 10 else f"{value:.2f}{suffix}"
    return f"{n:.0f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _num(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def _tag(block: dict) -> str:
    return f"  [{block['status']}]" if block.get("status") else ""


def render_block(k: dict, indent: str = "") -> list[str]:
    kpi, tok, share, conc = k["kpi"], k["tokens"], k["share"], k["concentration"]
    lines = [
        f"sessions {k['sessions']}  ({(k['first'] or '-')[:10]} .. {(k['last'] or '-')[:10]})",
        f"tokens {human(tok['total'])}: cache read {_pct(share['cache_read'])}, "
        f"cache write {_pct(share['cache_write'])}, output {_pct(share['output'])}, "
        f"uncached input {_pct(share['input'])}",
        f"turns main {k['turns_main']}, subagent {k['turns_subagent']}; per session "
        f"p50 {human(k['per_session_turns']['p50'])}, p90 {human(k['per_session_turns']['p90'])}",
        f"tokens per session p50 {human(k['per_session_tokens']['p50'])}, "
        f"p90 {human(k['per_session_tokens']['p90'])}",
        f"K1 context/turn        p50 {human(kpi['K1']['p50'])}, p90 {human(kpi['K1']['p90'])}{_tag(kpi['K1'])}",
        f"K2 top-20 share        {_pct(conc['top20'])} (top 1 {_pct(conc['top1'])}, "
        f"top 5 {_pct(conc['top5'])}, top 10 {_pct(conc['top10'])}){_tag(kpi['K2'])}",
        f"K3 retrieval/session   {_num(kpi['K3']['value'])}{_tag(kpi['K3'])}",
        f"K4 retrieval:remember  {kpi['K4']['retrieval']} : {kpi['K4']['remember']}"
        f" (ratio {_num(kpi['K4']['value'])}){_tag(kpi['K4'])}",
        f"K5 first-turn context  p50 {human(kpi['K5']['p50'])}, p90 {human(kpi['K5']['p90'])}",
        f"K6 cache-hit ratio     {_pct(kpi['K6']['value'])}{_tag(kpi['K6'])}",
        f"harness use            {k['harness']['sessions_using']} session(s), "
        f"{sum(k['harness']['calls'].values())} call(s)",
    ]
    if k["unsplit_records"]:
        lines.append(f"note: {k['unsplit_records']} pre-schema-2 record(s) carry no cache split")
    return [indent + line for line in lines]


def render(report: dict) -> str:
    window = f"last {report['window_days']} days" if report["window_days"] else "all time"
    overall = report["overall"]
    lines = [
        f"usage baseline ({report['agent'] or 'all agents'}), {window}, "
        f"sessions with >= {report['min_turns']} main-loop turns: "
        f"{overall['sessions']} of {report['sessions_in_window']} sessions, "
        f"{human(report['tokens_in_window'])} tokens in the window",
        "",
        *render_block(overall, "  "),
    ]
    if overall["heaviest"]:
        lines += ["", "  heaviest sessions:"]
        for h in overall["heaviest"]:
            lines.append(
                f"    {(h['start'] or '-')[:10]}  {h['project'] or '(unknown)'}  "
                f"turns {h['turns_main']}+{h['turns_subagent']}  {human(h['tokens'])}  "
                f"ctx/turn {human(h['context_per_turn'])}  harness calls {h['harness_calls']}"
            )
    names = {**(report.get("project_labels") or {}), **{k: v for k, v in (report.get("machines") or {}).items() if v}}
    for key, block in (report.get("groups") or {}).items():
        name = f"{key} ({names[key]})" if key in names else key
        lines += ["", f"{report['by']} {name}:", *render_block(block, "  ")]
    return "\n".join(lines) + "\n"
