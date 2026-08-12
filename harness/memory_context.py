"""Token-budgeted context packets: the one output contract every agent reads.

`build_packet` assembles the same logical packet for Claude Code, Codex,
Gemini, or anything else, in the design's fixed section order:

    1. scope              purpose, boundaries, exclusions, acceptance criteria
    2. active             current phase, open work, blockers
    3. project_durable    live project-scope entries
    4. machine_durable    live machine-scope entries
    5. relationships      accepted cross-project links
    6. warnings           freshness and integrity notices

Two knobs are deliberately separate. *Selection* decides which candidate
entries are most relevant and is pluggable -- `deterministic_selector` here
ranks by tag/keyword overlap, status, then recency; `harness.memory_index`
supplies a ChromaDB-backed selector that ranks by embedding distance. Both
satisfy the same `Selector` signature, so swapping one for the other cannot
change the packet's shape, only its contents. *Budgeting* is not pluggable:
every section has an independent character budget, the packet has a hard
maximum, and both are enforced here regardless of selector.

Budgeting is unit-atomic. A section is a sequence of pre-rendered units --
one scope heading, one entry -- and a unit is either included whole or
omitted whole, with the omitted count reported. No record is ever cut in
half, because a half-record reads as a complete one to a model and that is
how a context packet starts lying.

A third seam, `annotator`, enriches entries with facts from outside memory:
`harness.memory_graph` supplies one that resolves the symbols an entry names
against the code graph and appends each one's blast radius. It is injected
rather than imported so this module never depends on a graph existing, and
its output rides *inside* the annotated entry's unit -- never as a unit of
its own, which budgeting could drop while keeping the claim it supports.

Nothing here opens a network socket or calls an AI model. With no selector
and no annotator supplied the module is stdlib-only and fully deterministic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness import memory_store

# Per-section character budgets. They deliberately sum to more than
# DEFAULT_MAX_CHARS: a section may use its full allowance when earlier
# sections are small, but the packet total is the ceiling that actually binds.
DEFAULT_BUDGETS: dict[str, int] = {
    "scope": 2000,
    "active": 2000,
    "priorities": 1200,
    "project_durable": 3000,
    "machine_durable": 1500,
    "relationships": 800,
}

DEFAULT_MAX_CHARS = 8000

# An entry older than this makes the packet's freshness warning fire.
STALE_AFTER_DAYS = 14

SECTION_TITLES: dict[str, str] = {
    "scope": "Scope",
    "active": "Active work",
    "priorities": "Priorities and bottlenecks",
    "project_durable": "Project knowledge",
    "machine_durable": "Machine knowledge",
    "relationships": "Related projects",
}

SECTION_ORDER: tuple[str, ...] = (
    "scope",
    "active",
    "priorities",
    "project_durable",
    "machine_durable",
    "relationships",
)

# A selector ranks candidate entries against a query, most relevant first,
# and returns at most `limit` of them. It may return fewer; it must not
# invent entries. This is the seam Increment 3's ChromaDB backend plugs into.
Selector = Callable[[Sequence[dict], "str | None", int], list[dict]]

# An annotator enriches one entry with facts drawn from outside memory --
# today, `harness.memory_graph` resolves the symbols an entry names against
# the code graph and returns their blast radius. Each returned mapping must
# carry a pre-rendered `line` (appended inside the entry's own unit) and may
# carry any structured fields alongside it (preserved in the packet's
# records, so JSON consumers get more than a string).
#
# Two rules, both load-bearing. An annotator must never raise: it runs inside
# packet assembly, and enrichment failing must never cost an agent its
# memory. And it must be deterministic, because the packet's stability
# guarantee is what makes drift visible in a diff.
#
# The seam is a callable rather than an import so this module stays
# stdlib-only and buildable with no graph anywhere in sight.
Annotator = Callable[[dict], Sequence[dict]]

# How many entries a selector is asked for per section. Generous relative to
# the budget: budgeting trims the tail, so over-fetching costs nothing but
# under-fetching would silently cap the packet below its allowance.
CANDIDATE_LIMIT = 40

_TEMPLATE_PLACEHOLDER = re.compile(r"^_.*_$")
_WORD = re.compile(r"[a-z0-9]+")

# Words that carry no topical signal. Without this, a query and an entry can
# "match" purely on a shared `with`/`from`/`that`, which ranks an unrelated
# entry above a relevant one -- observed in practice on a four-entry store.
# Deliberately short: this is a relevance tiebreaker, not a search engine,
# and every word removed here is one an author can no longer match on.
_STOPWORDS: frozenset[str] = frozenset(
    """
    the and for are but not you all any can had her was one our out day get
    has him his how man new now old see two way who did its let put say she
    too use with from that this have will your what when your they them then
    than were been being into over under about after before while does doing
    """.split()
)


@dataclass(frozen=True)
class Section:
    """One rendered section: the units that fit, and how many did not."""

    key: str
    title: str
    units: tuple[str, ...] = ()
    records: tuple[dict, ...] = ()
    omitted: int = 0

    @property
    def chars(self) -> int:
        return sum(len(u) + 1 for u in self.units)


@dataclass(frozen=True)
class ContextPacket:
    """A complete, budget-bounded context packet."""

    project_id: str
    project_root: str
    generated_at: str
    strategy: str
    sections: tuple[Section, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())
    max_chars: int = DEFAULT_MAX_CHARS

    @property
    def total_chars(self) -> int:
        return sum(s.chars for s in self.sections)

    @property
    def omitted(self) -> int:
        return sum(s.omitted for s in self.sections)


# --- selection ------------------------------------------------------------


def _keywords(text: str | None) -> set[str]:
    """Lowercased content words for overlap scoring; stopwords dropped."""
    if not text:
        return set()
    return {
        w
        for w in _WORD.findall(text.lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


def _entry_keywords(entry: dict) -> set[str]:
    words = _keywords(entry.get("summary"))
    for tag in entry.get("tags") or []:
        words |= _keywords(tag)
    words |= _keywords(entry.get("type"))
    return words


def _recency_key(entry: dict) -> str:
    """Sort key placing newer timestamps first under an ascending sort.

    ISO-8601 `Z` stamps sort lexicographically, so inverting each character's
    ordinal reverses that order without parsing dates -- and, unlike parsing,
    it cannot raise on a hand-edited timestamp.
    """
    ts = str(entry.get("ts") or "")
    return "".join(chr(0x10FFFF - ord(c)) for c in ts)


def deterministic_selector(
    candidates: Sequence[dict],
    query: str | None,
    limit: int,
) -> list[dict]:
    """Rank `candidates` without embeddings, models, or network access.

    Ordering, strongest signal first: keyword/tag overlap with `query`, then
    confirmed over inferred, then accepted over proposed, then recency. Ties
    break on entry id so the same store always produces the same packet -- a
    context command that reshuffles its output between identical runs is
    impossible to diff, and diffing is how drift gets noticed.
    """
    wanted = _keywords(query)

    def rank(entry: dict) -> tuple:
        overlap = len(wanted & _entry_keywords(entry)) if wanted else 0
        confirmed = 1 if entry.get("confidence") == "confirmed" else 0
        accepted = 1 if entry.get("status") == "accepted" else 0
        return (
            -overlap,
            -confirmed,
            -accepted,
            _recency_key(entry),
            str(entry.get("id") or ""),
        )

    return sorted(candidates, key=rank)[:limit]


# --- markdown source files ------------------------------------------------


def _parse_markdown_sections(path: Path) -> list[tuple[str, str]]:
    """Return `(heading, body)` pairs from a `##`-structured Markdown file.

    Blockquote guidance lines and untouched template placeholders (`_like
    this._`) are dropped -- they are instructions to the human, and spending
    an agent's context budget on them would be pure waste.
    """
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    sections: list[tuple[str, list[str]]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
            continue
        if not sections:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(">") or stripped.startswith("#"):
            continue
        if _TEMPLATE_PLACEHOLDER.match(stripped):
            continue
        sections[-1][1].append(stripped)

    return [(head, " ".join(body)) for head, body in sections if body]


def _prose_units(path: Path) -> tuple[list[str], list[dict]]:
    pairs = _parse_markdown_sections(path)
    units = [f"**{head}:** {body}" for head, body in pairs]
    records = [{"heading": head, "body": body} for head, body in pairs]
    return units, records


def active_objective(project_root: Path) -> str | None:
    """The scope file's active objective, used as the default context query."""
    for heading, body in _parse_markdown_sections(
        Path(project_root) / ".agent-harness" / "scope.md"
    ):
        if heading.strip().lower() == "active objective":
            return body
    return None


# --- entry rendering ------------------------------------------------------


def render_entry(entry: dict, impacts: Sequence[dict] = ()) -> str:
    """One entry as a single atomic Markdown unit.

    Carries id, type, and status because an agent that wants to supersede or
    resolve something needs the id, and one weighing a claim needs to know
    whether it was accepted or is merely proposed.

    `impacts` (from an `Annotator`) append their `line` as continuation lines
    *within the returned string*, not as separate units. That is deliberate:
    budgeting is unit-atomic, so a blast radius emitted as its own unit could
    be dropped while the entry claiming it survived -- an entry whose
    evidence silently vanished reads exactly like one that never had any.
    """
    parts = [f"- `{entry.get('id', '?')}` [{entry.get('type', '?')}]"]
    status = entry.get("status")
    if status and status != "accepted":
        parts.append(f"({status})")
    parts.append(str(entry.get("summary", "")).strip())
    tags = entry.get("tags")
    if tags:
        parts.append("-- tags: " + ", ".join(tags))
    line = " ".join(p for p in parts if p)

    extra = [str(i["line"]) for i in impacts if i.get("line")]
    return "\n".join([line, *extra])


def _entry_units(
    entries: Sequence[dict], annotator: Annotator | None
) -> tuple[list[str], list[dict]]:
    """Render `entries` to units, annotated when an annotator is supplied.

    Returns annotated *copies* -- the entries handed in came from the store
    and are never mutated, so a packet can never write its own enrichment
    back into project memory.
    """
    if annotator is None:
        return [render_entry(e) for e in entries], list(entries)

    units: list[str] = []
    records: list[dict] = []
    for entry in entries:
        impacts = list(annotator(entry))
        units.append(render_entry(entry, impacts))
        records.append({**entry, "graph_impact": impacts} if impacts else entry)
    return units, records


# --- budgeting ------------------------------------------------------------


def _fit(units: Sequence[str], budget: int) -> tuple[list[str], int]:
    """Take units in order while they fit in `budget`; report how many did not.

    Stops at the first unit that does not fit rather than skipping ahead to a
    smaller later one: units arrive in relevance order, and reordering them to
    pack the budget tighter would quietly promote a less relevant entry over a
    more relevant one.
    """
    kept: list[str] = []
    used = 0
    for index, unit in enumerate(units):
        cost = len(unit) + 1
        if used + cost > budget:
            return kept, len(units) - index
        kept.append(unit)
        used += cost
    return kept, 0


# --- packet assembly ------------------------------------------------------


def _open_work(entries: Sequence[dict]) -> list[dict]:
    """Unresolved tasks and blockers -- the highest-priority live records."""
    return [
        e
        for e in entries
        if e.get("type") in ("task", "blocker")
        and e.get("status") in ("proposed", "accepted")
    ]


def _is_live(entry: dict) -> bool:
    return entry.get("status") not in memory_store.CLOSED_STATUSES


def durable_pools(
    project_entries: Sequence[dict], machine_entries: Sequence[dict]
) -> dict[str, list[dict]]:
    """The candidate pools a `Selector` actually ranks, split by section.

    Open work is excluded from `project_durable` because it is rendered
    unranked in the `active` section; relationships are split out because they
    are their own section and are accepted-only.

    Public and factored out of `build_packet` so that anything *measuring*
    selection quality ranks the same pool production ranks. Scoring a
    plausible-looking substitute -- "every live entry", say -- yields numbers
    that read as retrieval quality while describing a pool no packet is ever
    built from.
    """
    open_ids = {e.get("id") for e in _open_work(project_entries)}
    return {
        "project_durable": [
            e
            for e in project_entries
            if _is_live(e)
            and e.get("type") != "relationship"
            and e.get("id") not in open_ids
        ],
        "machine_durable": [
            e
            for e in machine_entries
            if _is_live(e) and e.get("type") != "relationship"
        ],
        "relationships": [
            e
            for e in list(project_entries) + list(machine_entries)
            if e.get("type") == "relationship" and e.get("status") == "accepted"
        ],
    }


def _freshness_warnings(entries: Sequence[dict], now: datetime) -> list[str]:
    warnings: list[str] = []
    stamps = [str(e.get("ts")) for e in entries if e.get("ts")]
    if stamps:
        try:
            newest = datetime.strptime(max(stamps), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            newest = None
        if newest is not None and now - newest > timedelta(days=STALE_AFTER_DAYS):
            age = (now - newest).days
            warnings.append(
                f"memory is stale: newest entry is {age} days old "
                f"(threshold {STALE_AFTER_DAYS})"
            )
    pending = [e for e in entries if e.get("status") == "proposed"]
    if pending:
        plural = "y" if len(pending) == 1 else "ies"
        warnings.append(
            f"{len(pending)} entr{plural} awaiting review; proposed knowledge "
            f"is not authoritative"
        )
    return warnings


def build_packet(
    project_root: Path,
    query: str | None = None,
    budgets: dict[str, int] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_ids: Sequence[str] = (),
    selector: Selector | None = None,
    strategy: str = "deterministic",
    notes: Sequence[str] = (),
    annotator: Annotator | None = None,
    now: datetime | None = None,
) -> ContextPacket:
    """Assemble a budget-bounded context packet for `project_root`.

    `query` defaults to the scope file's active objective, so a bare
    `context` call is already focused on what the project is currently doing.
    `include_ids` pins specific entries to the front of their section --
    the design's "explicitly referenced entry IDs" priority tier -- and they
    are still budgeted, never exempt.

    `selector` defaults to `deterministic_selector`; `strategy` is recorded
    on the packet purely so a reader (or a benchmark) can tell which
    selection produced it.

    `annotator` is the optional join to the code graph (see `Annotator` and
    `harness.memory_graph`). With none supplied the packet is exactly what it
    was before that join existed -- memory has never needed a graph to work,
    and adding one must not change that.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
    choose = selector or deterministic_selector
    pinned = set(include_ids)

    project_entries = list(
        memory_store.iter_entries(memory_store.project_memory_dir(root))
    )
    machine_entries = list(
        memory_store.iter_entries(memory_store.machine_memory_dir())
    )

    if query is None:
        query = active_objective(root)

    def select(candidates: Sequence[dict]) -> list[dict]:
        """Rank a section's candidates, with pinned ids held above the ranking.

        Pinned entries are pulled out *before* the selector runs, not
        reordered afterwards: an explicitly requested id that happened to
        rank below `CANDIDATE_LIMIT` would otherwise vanish from the packet
        entirely, which is the opposite of what asking for it by id means.
        """
        if not pinned:
            return choose(candidates, query, CANDIDATE_LIMIT)
        head = [c for c in candidates if c.get("id") in pinned]
        rest = [c for c in candidates if c.get("id") not in pinned]
        return head + choose(rest, query, CANDIDATE_LIMIT)

    # 1. scope, 2. active -- prose, always first, never selector-ranked.
    scope_units, scope_records = _prose_units(root / ".agent-harness" / "scope.md")
    active_units, active_records = _prose_units(
        memory_store.project_memory_dir(root) / "active.md"
    )

    # Unresolved work leads the rest of the active section: it is the design's
    # second priority tier and outranks anything a selector would surface.
    open_work = _open_work(project_entries)
    open_units, open_records = _entry_units(open_work, annotator)
    active_units = active_units + open_units
    active_records = active_records + open_records

    pools = durable_pools(project_entries, machine_entries)
    selected = {key: select(pool) for key, pool in pools.items()}

    # Priorities: which topics keep stalling, then what to pick up because of
    # them. Placed after `active` and before knowledge because an agent needs
    # to know what is stuck before it needs to know what is true.
    from harness import memory_tasks

    stuck = memory_tasks.bottlenecks(project_entries, now=now)
    priority_units = [f"**Bottleneck:** {b.describe()}" for b in stuck]
    priority_records: list[dict] = [
        {
            "kind": "bottleneck",
            "tag": b.tag,
            "open": b.open_count,
            "blockers": b.blocker_count,
            "oldest_days": b.oldest_days,
            "score": b.score,
        }
        for b in stuck
    ]
    if stuck:
        ranked = memory_tasks.prioritized(project_entries, now=now)
        ranked_units, ranked_records = _entry_units(ranked, annotator)
        priority_units += ranked_units
        priority_records += ranked_records

    raw: dict[str, tuple[list[str], list[dict]]] = {
        "scope": (scope_units, scope_records),
        "active": (active_units, active_records),
        "priorities": (priority_units, priority_records),
    }
    for key, records in selected.items():
        raw[key] = _entry_units(records, annotator)

    sections: list[Section] = []
    spent = 0
    for key in SECTION_ORDER:
        units, records = raw[key]
        # The section budget and whatever remains of the packet ceiling both
        # apply; the tighter one wins.
        allowance = min(budgets.get(key, 0), max(max_chars - spent, 0))
        kept, omitted = _fit(units, allowance)
        section = Section(
            key=key,
            title=SECTION_TITLES[key],
            units=tuple(kept),
            records=tuple(records[: len(kept)]),
            omitted=omitted,
        )
        spent += section.chars
        sections.append(section)

    warnings = _freshness_warnings(project_entries, now)
    scope_path = root / ".agent-harness" / "scope.md"
    if scope_path.is_file() and not scope_units:
        warnings.append(
            "scope.md is still the untouched template; scope-drift detection "
            "and objective-focused selection cannot work until it is filled in"
        )
    if not project_entries:
        warnings.append("no project memory recorded yet")

    return ContextPacket(
        project_id=memory_store.project_id_for(root),
        project_root=str(root),
        generated_at=memory_store.format_ts(now),
        strategy=strategy,
        sections=tuple(sections),
        warnings=tuple(warnings),
        notes=tuple(notes),
        max_chars=max_chars,
    )


# --- rendering ------------------------------------------------------------


def render_markdown(packet: ContextPacket) -> str:
    """The packet as Markdown -- what an agent reads at session start."""
    out: list[str] = [
        f"# Project context: {packet.project_id}",
        "",
        f"_Generated {packet.generated_at} - selection: {packet.strategy} - "
        f"{packet.total_chars}/{packet.max_chars} chars_",
    ]
    for note in packet.notes:
        out.append(f"_Note: {note}_")
    out.append("")

    for section in packet.sections:
        if not section.units and not section.omitted:
            continue
        out.append(f"## {section.title}")
        out.append("")
        out.extend(section.units)
        if section.omitted:
            out.append(f"_... {section.omitted} more omitted for budget._")
        out.append("")

    if packet.warnings:
        out.append("## Warnings")
        out.append("")
        out.extend(f"- {w}" for w in packet.warnings)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_json(packet: ContextPacket) -> str:
    """The packet as JSON -- the machine-readable half of the same contract."""
    payload = {
        "schema": memory_store.SCHEMA_VERSION,
        "project_id": packet.project_id,
        "project_root": packet.project_root,
        "generated_at": packet.generated_at,
        "strategy": packet.strategy,
        "notes": list(packet.notes),
        "max_chars": packet.max_chars,
        "total_chars": packet.total_chars,
        "omitted": packet.omitted,
        "sections": [
            {
                "key": s.key,
                "title": s.title,
                "units": list(s.units),
                "records": list(s.records),
                "omitted": s.omitted,
                "chars": s.chars,
            }
            for s in packet.sections
        ],
        "warnings": list(packet.warnings),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
