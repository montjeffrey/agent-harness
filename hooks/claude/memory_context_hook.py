"""SessionStart hook: print this project's memory table of contents.

Claude Code injects a SessionStart hook's stdout into the session as
context, so an agent starts already knowing the project's scope, its open
work, and where the knowledge is -- without reading any history.

This used to inject the full packet (about 3,500 chars). It is now a table
of contents, target 1,500: scope in one breath, every open task and blocker
by id, the bottleneck line, and the ids of the top-ranked knowledge entries
with a clipped summary each. Knowledge no longer needs to travel here
because it arrives where it is used: `work_start` returns a packet ranked
by the task's own words, and `memory_search` answers one topic mid-task.
Paying for a full packet up front, in every session, whether or not it was
ever read, was the wrong place to spend the context window.

Rules this hook lives by:

1. **Fail open, always.** Any error -- unreadable memory, a bug in the
   harness, an unexpected exception -- exits 0 with no output. A memory
   problem must never stop someone from working. That is why the body is
   wrapped in a bare `except Exception`, which is normally a smell and is
   correct here.

2. **Adaptive selection by default.** Every hook run is a fresh process, so
   an unconditional `auto` would import chromadb and build an ONNX session on
   *every* session start in *every* enrolled project -- seconds of latency
   for a table of contents keyword ranking usually fills well. `adaptive`
   keeps that cost only where it buys something: before touching chromadb it
   asks, in arithmetic alone, whether semantic ranking could change *this*
   packet. It cannot when there is no active objective to rank against, and
   it effectively cannot when the store is small enough that every section
   fits its budget. `AGENT_HARNESS_HOOK_STRATEGY` still forces `auto`,
   `semantic`, or `deterministic`, and `AGENT_HARNESS_ADAPTIVE_MIN_ENTRIES`
   moves the size threshold. The ranking decides which knowledge ids make
   the table of contents, which is why it is still worth having.

3. **Evidence-bounded curation.** Before reading memory, the hook accepts only
   document pointers whose recorded digest still matches the source file and
   rejects exact duplicates. Agent conclusions remain proposed. The pass is
   deterministic, local, and idempotent; it does not compact, index, or call a
   model.

4. **Smaller budget than the CLI default.** This text is prepended to every
   session whether or not it gets used, so it is charged against the
   context window unconditionally. `AGENT_HARNESS_HOOK_MAX_CHARS` overrides.

5. **It names the session.** Claude Code sends its `session_id` on stdin.
   The table of contents prints it, and the PreToolUse claim guard
   (`claim_guard.py`) recognises a task as this session's own only when
   `work_start` was given that same id. The hook also records the session's
   starting HEAD under `~/.agent-harness/work/` so the progress hook can
   later say "new since this session started" as a git range.

Never builds an index, never opens a network socket, never loads the code
graph (a table of contents has no room for blast-radius lines). It writes
only a curation transition when a pointer verifies, and the session marker.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOOK_MAX_CHARS = 1500
STRATEGY_ENV = "AGENT_HARNESS_HOOK_STRATEGY"
MAX_CHARS_ENV = "AGENT_HARNESS_HOOK_MAX_CHARS"

# Clip lengths for one table-of-contents line. Long enough to recognise the
# entry, short enough that a dozen fit; the id is the handle for the rest.
PURPOSE_CHARS = 240
OBJECTIVE_CHARS = 160
OPEN_WORK_CHARS = 110
KNOWLEDGE_CHARS = 100

# Auto-enrollment: create `.agent-harness/` on first sight of a project.
# Off unless a workspace root is configured, because this hook runs in every
# directory Claude Code is opened in -- including a colleague's repo you
# cloned to read for five minutes. Scaffolding committed files into someone
# else's project without being asked is not a good surprise, so the opt-in
# is a path you name.
WORKSPACE_ENV = "AGENT_HARNESS_WORKSPACE"
AUTO_ENROLL_ENV = "AGENT_HARNESS_AUTO_ENROLL"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def should_auto_enroll(project_root: Path) -> bool:
    """Whether to create memory storage for a project that has none.

    True only when `AGENT_HARNESS_AUTO_ENROLL` is truthy AND the project sits
    inside a directory named by `AGENT_HARNESS_WORKSPACE` (which may list
    several roots, `os.pathsep`-separated). Both conditions are required: the
    flag is the intent, the workspace is the blast radius. Without the
    workspace check, opening any cloned repository would silently add
    committed scaffolding to it.
    """
    if os.environ.get(AUTO_ENROLL_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return False

    roots = [r for r in os.environ.get(WORKSPACE_ENV, "").split(os.pathsep) if r.strip()]
    if not roots:
        return False

    try:
        resolved = project_root.resolve()
    except OSError:
        return False

    for root in roots:
        try:
            candidate = Path(root.strip()).expanduser().resolve()
        except OSError:
            continue
        if resolved == candidate or candidate in resolved.parents:
            return True
    return False


def _entry_count(memory_store: Any, project_root: Path) -> int:
    """How many entries both stores hold, for the adaptive size gate.

    This re-reads append-only JSONL that `build_packet` will read again a few
    lines later. The duplication is deliberate: the alternative is threading a
    count out of `build_packet` for one caller's benefit, and a stdlib pass
    over a small file is orders of magnitude cheaper than the ONNX session it
    decides whether to pay for.

    An unreadable store counts as zero, which sends the decision to
    deterministic. That is the correct direction to fail: rule 1 says a memory
    problem must never stop someone working, and the cheap path always
    produces a usable packet.
    """
    try:
        return sum(
            1
            for directory in (
                memory_store.project_memory_dir(project_root),
                memory_store.machine_memory_dir(),
            )
            for _ in memory_store.iter_entries(directory)
        )
    except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
        return 0


def _empty_packet_notice(project_root: Path, memory_store: Any) -> str:
    """One line distinguishing a wired-but-empty store from an absent one.

    An enrolled project whose packet has no units used to emit nothing at all,
    which is exactly what an *unenrolled* project emits. Two states that need
    opposite fixes -- one needs recording, the other needs enrolling -- looked
    identical from the operator's seat, and the absence of an error read as
    "no problem" rather than "no memory".

    The two empty cases are worth telling apart in the line itself. Zero
    entries means nothing was ever recorded. A non-zero count with an empty
    packet means entries exist but none survived selection, which is a budget
    or filter question, not a recording one.
    """
    try:
        count = sum(
            1
            for _ in memory_store.iter_entries(
                memory_store.project_memory_dir(project_root)
            )
        )
    except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
        count = 0

    if count == 0:
        return (
            "_agent-harness: memory is enrolled but empty (0 entries recorded). "
            "Nothing to inject. Record with `agent-harness-memory remember`; "
            "`agent-harness doctor` confirms the wiring._\n"
        )
    return (
        f"_agent-harness: {count} memory entries recorded, none selected into "
        "this packet -- check the budget with `agent-harness-memory status`._\n"
    )


# --- table-of-contents rendering ------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _entry_line(entry: dict, limit: int) -> str:
    status = entry.get("status")
    flag = f" ({status})" if status and status != "accepted" else ""
    return f"- `{entry.get('id', '?')}` [{entry.get('type', '?')}]{flag} {_clip(entry.get('summary', ''), limit)}"


def _toc_sections(packet: Any) -> list[tuple[str, list[str], int]]:
    """`(title, lines, already_omitted)` per section, in reading order.

    Built from the packet's *records* rather than its rendered units, so each
    line can be clipped to table-of-contents length while the ids -- the
    handles for `memory_search` -- stay intact.
    """
    by_key = {section.key: section for section in packet.sections}

    scope: list[str] = []
    other_headings: list[str] = []
    for record in by_key["scope"].records if "scope" in by_key else ():
        heading = str(record.get("heading", "")).strip()
        body = str(record.get("body", ""))
        if heading.lower() == "purpose":
            scope.append(f"**Purpose:** {_clip(body, PURPOSE_CHARS)}")
        elif heading.lower() == "active objective":
            scope.append(f"**Active objective:** {_clip(body, OBJECTIVE_CHARS)}")
        else:
            other_headings.append(heading)
    if other_headings:
        scope.append("_Also in scope.md: " + ", ".join(other_headings) + "._")

    open_work: list[str] = []
    active = by_key.get("active")
    if active is not None:
        entries = [r for r in active.records if r.get("id")]
        entries.sort(key=lambda r: 0 if r.get("type") == "blocker" else 1)
        for record in active.records:
            if not record.get("id"):
                open_work.append(f"**{record.get('heading', '')}:** {_clip(record.get('body', ''), OPEN_WORK_CHARS)}")
        open_work.extend(_entry_line(r, OPEN_WORK_CHARS) for r in entries)

    bottlenecks: list[str] = []
    priorities = by_key.get("priorities")
    if priorities is not None:
        described = []
        for record in priorities.records:
            if record.get("kind") != "bottleneck":
                continue
            parts = [f"{record.get('open', 0)} open"]
            if record.get("blockers"):
                parts.append(f"{record['blockers']} blocking")
            described.append(f"{record.get('tag')} ({', '.join(parts)})")
        if described:
            bottlenecks.append("; ".join(described))

    knowledge: list[str] = []
    knowledge_omitted = 0
    for key in ("project_durable", "machine_durable", "relationships"):
        section = by_key.get(key)
        if section is None:
            continue
        knowledge.extend(_entry_line(r, KNOWLEDGE_CHARS) for r in section.records if r.get("id"))
        knowledge_omitted += section.omitted

    active_omitted = active.omitted if active is not None else 0
    return [
        ("Scope", scope, by_key["scope"].omitted if "scope" in by_key else 0),
        ("Open work", open_work, active_omitted),
        ("Bottlenecks", bottlenecks, 0),
        ("Knowledge", knowledge, knowledge_omitted),
    ]


# Each section's first claim on the budget. They sum to one; whatever a
# section leaves unused is handed to the next section still omitting lines.
# Without shares the greedy fit let seven open tasks starve the bottleneck
# line and every knowledge id -- a table of contents with a chapter missing.
TOC_SHARES: dict[str, float] = {
    "Scope": 0.30,
    "Open work": 0.35,
    "Bottlenecks": 0.10,
    "Knowledge": 0.25,
}


def _fit_lines(lines: list[str], start: int, allowance: int) -> int:
    """How many of `lines[start:]` fit in `allowance` chars (newline included)."""
    spent = 0
    taken = 0
    for line in lines[start:]:
        cost = len(line) + 1
        if spent + cost > allowance:
            break
        spent += cost
        taken += 1
    return taken


def render_toc(packet: Any, max_chars: int, session_id: str | None = None) -> str:
    """The packet as a table of contents that fits `max_chars`.

    The header (title, generation line, notes) sits outside the budget as it
    always did; warnings and the session footer are reserved first so the
    lines an agent must not miss are never the ones the budget drops. The
    body is then shared out per `TOC_SHARES` in two passes: every section
    gets its share, then leftovers go to sections that still omit lines.
    """
    footer: list[str] = []
    if packet.warnings:
        footer += ["## Warnings", ""] + [f"- {w}" for w in packet.warnings] + [""]
    if session_id:
        footer.append(
            f"_Session `{session_id}`: give it to work_start as `session` so the claim "
            "guard recognises this session's own tasks._"
        )
    footer.append("_Full packet: `memory_context`. One topic: `memory_search`._")
    reserved = sum(len(line) + 1 for line in footer)
    budget = max(max_chars - reserved, 0)

    sections = [
        (title, lines, omitted) for title, lines, omitted in _toc_sections(packet) if lines or omitted
    ]
    header_cost = {title: len(title) + 12 for title, _, _ in sections}  # "## ", " (n of m)", blank line
    kept: dict[str, int] = {title: 0 for title, _, _ in sections}

    # Pass 1: each section fits what it can inside its own share.
    spent = 0
    for title, lines, _ in sections:
        allowance = int(budget * TOC_SHARES.get(title, 0)) - header_cost[title]
        kept[title] = _fit_lines(lines, 0, max(allowance, 0))
        spent += header_cost[title] + sum(len(l) + 1 for l in lines[: kept[title]])
    # Pass 2: leftovers, in reading order, to whoever still has lines.
    for title, lines, _ in sections:
        if kept[title] >= len(lines):
            continue
        more = _fit_lines(lines, kept[title], budget - spent)
        spent += sum(len(l) + 1 for l in lines[kept[title] : kept[title] + more])
        kept[title] += more

    body: list[str] = []
    for title, lines, already_omitted in sections:
        shown = lines[: kept[title]]
        omitted = already_omitted + (len(lines) - kept[title])
        heading = f"## {title}"
        if title in ("Open work", "Knowledge"):
            heading += f" ({len(shown)} of {len(shown) + omitted})"
        body += [heading, ""] + shown
        if omitted:
            body.append(f"_... {omitted} more; `memory_context` shows all._")
        body.append("")

    out: list[str] = [
        f"# Project context: {packet.project_id}",
        "",
        f"_Generated {packet.generated_at} - selection: {packet.strategy} - "
        f"table of contents, {spent}/{max_chars} chars_",
    ]
    for note in packet.notes:
        out.append(f"_Note: {note}_")
    out.append("")
    out += body
    out += footer
    return "\n".join(out).rstrip() + "\n"


# --- the hook --------------------------------------------------------------


def _harness():
    try:
        from harness import memory_context, memory_curation, memory_index, memory_store
    except ImportError:
        # Source checkout: this file is <repo>/hooks/claude/, so the package
        # root is two levels up. An installed (pipx/wheel) harness takes the
        # branch above and never reaches here.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from harness import memory_context, memory_curation, memory_index, memory_store
    return memory_context, memory_curation, memory_index, memory_store


def emit(project_root: Path, session_id: str | None = None) -> str:
    """Build the table-of-contents text for `project_root`, or "" if none.

    Returns empty for an unenrolled project so the hook stays silent rather
    than announcing itself in every repository on the machine.
    """
    memory_context, memory_curation, memory_index, memory_store = _harness()

    if not (project_root / ".agent-harness").is_dir():
        if not should_auto_enroll(project_root):
            return ""
        # Memory init only -- stdlib, milliseconds, no network. Building the
        # graph or the semantic index here would put a multi-second job in
        # front of every session start, the same trap that keeps `auto`
        # selection out of this hook. `agent-harness-enroll` does those.
        memory_store.init_project(project_root)

    # Curate before selection so a newly verified document pointer is visible
    # as accepted in this very packet. A curation failure must not suppress a
    # working session, consistent with the hook's fail-open contract.
    try:
        curation_note = memory_curation.curate(project_root).note()
    except Exception:  # noqa: BLE001 -- see the hook's fail-open rule
        curation_note = "memory curation skipped because its local check failed"

    strategy = os.environ.get(STRATEGY_ENV, "adaptive")
    project_id = memory_store.project_id_for(project_root)
    # Resolved once and passed to both the selector decision and the packet, so
    # the strategy is chosen against exactly the query the packet ranks by.
    # Letting `build_packet` default the query again could, if the two ever
    # diverged, pick semantic for a query that is not the one used.
    objective = memory_context.active_objective(project_root)
    try:
        selector, resolved, notes = memory_index.resolve_selector(
            project_id,
            strategy,
            query=objective,
            entry_count=_entry_count(memory_store, project_root),
        )
    except (FileNotFoundError, ValueError):
        selector = memory_context.deterministic_selector
        resolved = "deterministic"
        notes = []

    max_chars = _int_env(MAX_CHARS_ENV, HOOK_MAX_CHARS)
    # The packet is built with room to spare: `render_toc` clips each record
    # to one line and applies the real budget, so a section budget that cut
    # records here would only lose ids the table of contents had room for.
    # Selection still bounds it: a selector returns at most CANDIDATE_LIMIT.
    wide = max(max_chars * 4, memory_context.DEFAULT_MAX_CHARS)
    packet = memory_context.build_packet(
        project_root,
        query=objective,
        budgets={key: wide for key in memory_context.DEFAULT_BUDGETS},
        max_chars=wide,
        selector=selector,
        strategy=resolved,
        notes=list(notes) + ([curation_note] if curation_note else []),
    )
    # An empty packet returned "" here for a while, on the theory that a
    # "nothing recorded yet" line is noise at session start. That traded a
    # little noise for a worse failure: silence is also what an unenrolled
    # project emits, so "not wired" and "wired but idle" became the same
    # observation. One line is cheaper than the debugging session that
    # ambiguity costs. Unenrolled projects still return "" above -- this hook
    # announces itself only where it has actually been installed.
    if not any(section.units for section in packet.sections):
        notice = _empty_packet_notice(project_root, memory_store)
        if session_id:
            # An empty store still has tasks to coordinate; the guard's
            # identity contract needs the id printed regardless.
            notice += f"_Session `{session_id}`: give it to work_start as `session`._\n"
        return notice
    return render_toc(packet, max_chars, session_id)


def record_session_start(project_root: Path, session_id: str, now: datetime | None = None) -> Path | None:
    """Record the session's starting HEAD so progress can be measured later.

    Writes `~/.agent-harness/work/<repo>/sessions/<digest>.json` once per
    session: a resumed session keeps its original marker, otherwise commits
    made before the resume would fall out of every later "new since" range.
    Returns the marker path, or None when there was nothing to record (not
    enrolled, not a git repository, or the marker already exists).
    """
    if not (project_root / ".agent-harness").is_dir():
        return None
    _harness()  # puts a source checkout on sys.path before the import below
    from harness import memory_store, work

    repo = work.repository_for(project_root)
    if work.load_session_marker(repo, session_id) is not None:
        return None
    return work.save_session_marker(
        repo,
        session_id,
        {
            "session_id": session_id,
            "head": repo.head,
            "branch": repo.current_branch,
            "started_at": memory_store.format_ts(now or datetime.now(timezone.utc)),
            "reported_tasks": [],
        },
    )


def _payload() -> dict:
    """The hook event Claude Code sends on stdin, or {} when there is none.

    A terminal (someone running the hook by hand) and pytest's captured
    stdin both yield {} rather than a blocked read or an exception.
    """
    try:
        stream = sys.stdin
        if stream is None or stream.isatty():
            return {}
        payload = json.loads(stream.read() or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
        return {}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        payload = _payload()
        session_id = payload.get("session_id")
        session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        if args:
            root = Path(args[0]).expanduser()
        elif isinstance(payload.get("cwd"), str) and payload["cwd"]:
            root = Path(payload["cwd"])
        else:
            root = Path.cwd()
        if session_id:
            try:
                record_session_start(root, session_id)
            except Exception:  # noqa: BLE001 -- a marker is a nicety; the packet is the job
                pass
        text = emit(root, session_id)
        if text:
            sys.stdout.write(text)
    except Exception:  # noqa: BLE001 -- see rule 1 in the module docstring
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
