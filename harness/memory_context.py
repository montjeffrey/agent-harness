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

from harness import memory_compact, memory_store

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


def _parse_markdown_sections(path: Path) -> list[tuple[str, list[str]]]:
    """Return `(heading, body_lines)` pairs from a `##`-structured Markdown file.

    Body lines are kept separate rather than pre-joined so each caller can
    decide what an atomic unit is. `scope.md` is prose that only means
    anything whole; `active.md` is a list whose entries have to be droppable
    one at a time. Joining here would take that choice away from both.

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

    return [(head, body) for head, body in sections if body]


def _prose_units(
    path: Path, skip_headings: frozenset[str] = frozenset()
) -> tuple[list[str], list[dict]]:
    """Markdown sections as packet units, one per bullet where there are any.

    `skip_headings` (matched case-insensitively) drops a whole section. It
    exists for headings the packet already sources from the inbox directly,
    where keeping the file's copy would duplicate the entry at best and
    contradict it at worst.

    `compact` fills `active.md` up to its own `ACTIVE_MAX_CHARS` while this
    packet gives the active section a smaller budget, and the two are allowed
    to disagree. What is not allowed is the disagreement costing the whole
    section: while the bullet list was a single atomic unit, a file over
    budget was dropped entire and the section read empty rather than short.
    Splitting per bullet lets it give way one entry at a time.

    Bullets are not prefixed with their heading. `_entry_line` already writes
    the id, type, and summary into every line, so each one stands alone
    without it, and repeating a heading across twenty units would spend a
    quarter of the section's budget restating what the line already says. The
    heading is kept on the record instead, which goes to `render_json` where
    there is no budget to spend.
    """
    units: list[str] = []
    records: list[dict] = []
    for head, lines in _parse_markdown_sections(path):
        if head.strip().lower() in skip_headings:
            continue
        prose = [line for line in lines if not line.startswith("- ")]
        if prose:
            body = " ".join(prose)
            units.append(f"**{head}:** {body}")
            records.append({"heading": head, "body": body})
        for bullet in (line for line in lines if line.startswith("- ")):
            units.append(bullet)
            records.append({"heading": head, "body": bullet})
    # Appended in lockstep on purpose: `build_packet` trims records with a
    # prefix slice of `len(kept)`, so any drift in order or length would pair
    # a surviving unit with another entry's heading.
    return units, records


def active_objective(project_root: Path) -> str | None:
    """The scope file's active objective, used as the default context query."""
    for heading, lines in _parse_markdown_sections(
        Path(project_root) / ".agent-harness" / "scope.md"
    ):
        if heading.strip().lower() == "active objective":
            # Joined, not split: this is one query string, and an objective
            # wrapped across source lines must not become several.
            return " ".join(lines)
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


# --- filtering ------------------------------------------------------------
#
# A filter narrows *what is ranked*, not what is shown after ranking. That
# ordering matters twice over: a job about one topic never has unrelated
# entries competing for its budget, and the `adaptive` gate in
# `harness.memory_index` counts the entries that survive the filter, so
# narrowing also makes selection cheaper instead of merely hiding its output.


def _split_terms(value: object) -> tuple[str, ...]:
    """Comma-separated string (or list) -> lowercased, de-duplicated terms."""
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else [str(v) for v in value]
    terms: list[str] = []
    for item in items:
        term = item.strip().lower()
        if term and term not in terms:
            terms.append(term)
    return tuple(terms)


@dataclass(frozen=True)
class EntryFilter:
    """What a caller asked to narrow retrieval to, applied before ranking.

    `tags` keeps entries carrying at least one of them; `types` keeps entries
    of those types; `exclude` drops entries carrying any of those tags or
    having one of those ids. Matching is case-insensitive. An empty filter
    keeps everything, so a caller passing nothing gets exactly the packet it
    always got.
    """

    tags: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.tags or self.types or self.exclude)

    def matches(self, entry: dict) -> bool:
        entry_tags = {str(t).lower() for t in entry.get("tags") or []}
        if self.tags and not (entry_tags & set(self.tags)):
            return False
        if self.types and str(entry.get("type") or "").lower() not in self.types:
            return False
        if self.exclude:
            if str(entry.get("id") or "").lower() in self.exclude:
                return False
            if entry_tags & set(self.exclude):
                return False
        return True

    def describe(self) -> str:
        parts = []
        if self.tags:
            parts.append("tags: " + ", ".join(self.tags))
        if self.types:
            parts.append("types: " + ", ".join(self.types))
        if self.exclude:
            parts.append("exclude: " + ", ".join(self.exclude))
        return "; ".join(parts)


def parse_filter(
    tags: object = None, types: object = None, exclude: object = None
) -> EntryFilter:
    """Build an `EntryFilter` from the comma-separated strings a tool call carries.

    An unknown type is an error rather than an empty match: a typo there
    would otherwise filter out every entry and read exactly like an empty
    store, which is the silent failure the design forbids.
    """
    parsed_types = _split_terms(types)
    unknown = [t for t in parsed_types if t not in memory_store.ENTRY_TYPES]
    if unknown:
        raise ValueError(
            f"unknown type(s) {', '.join(unknown)} -- choose from "
            f"{', '.join(memory_store.ENTRY_TYPES)}"
        )
    return EntryFilter(_split_terms(tags), parsed_types, _split_terms(exclude))


@dataclass(frozen=True)
class LoadedEntries:
    """Both stores read once, with the filter already applied.

    The unfiltered lists are kept alongside because the packet's warnings
    describe the *store*, not the slice: "7 entries awaiting review" must stay
    true whether or not the caller narrowed to one tag, and `relationships`
    are read from the unfiltered machine store so `projects="related"` does
    not depend on the caller having asked for the `relationship` type.
    """

    project: tuple[dict, ...]
    machine: tuple[dict, ...]
    project_all: tuple[dict, ...]
    machine_all: tuple[dict, ...]
    entry_filter: EntryFilter = field(default_factory=EntryFilter)
    excluded: int = 0

    @property
    def count(self) -> int:
        """What the `adaptive` gate should count: entries that can be ranked."""
        return len(self.project) + len(self.machine)

    def note(self) -> str | None:
        """The packet note stating which filters applied and what they cost."""
        if self.entry_filter.is_empty:
            return None
        plural = "y" if self.excluded == 1 else "ies"
        return (
            f"filters applied ({self.entry_filter.describe()}); "
            f"{self.excluded} entr{plural} excluded before ranking"
        )


def load_entries(
    project_root: Path,
    entry_filter: EntryFilter | None = None,
    include_machine: bool = True,
) -> LoadedEntries:
    """Read a project's store (and, by default, the machine store) filtered.

    `include_machine=False` is for reading *another* project's inbox during a
    cross-project search: machine memory is shared, so it is read once for
    the current project and must not be counted again per project.
    """
    root = Path(project_root)
    chosen = entry_filter or EntryFilter()
    project_all = tuple(
        memory_store.iter_entries(memory_store.project_memory_dir(root))
    )
    machine_all: tuple[dict, ...] = ()
    if include_machine:
        machine_all = tuple(
            memory_store.iter_entries(memory_store.machine_memory_dir())
        )
    project = tuple(e for e in project_all if chosen.matches(e))
    machine = tuple(e for e in machine_all if chosen.matches(e))
    excluded = (len(project_all) - len(project)) + (len(machine_all) - len(machine))
    return LoadedEntries(project, machine, project_all, machine_all, chosen, excluded)


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
    entry_filter: EntryFilter | None = None,
    loaded: LoadedEntries | None = None,
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

    `entry_filter` narrows the entries *before* they are ranked (see
    `EntryFilter`); the packet then states the filter and how many entries
    it excluded, in a note. `loaded` lets a caller that has already read the
    stores -- to count entries for the adaptive gate, say -- hand them over
    instead of reading twice; it already carries its filter.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    budgets = {**DEFAULT_BUDGETS, **(budgets or {})}
    choose = selector or deterministic_selector
    pinned = set(include_ids)

    if loaded is None:
        loaded = load_entries(root, entry_filter)
    project_entries = list(loaded.project)
    machine_entries = list(loaded.machine)

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
    # `## Open work` is skipped because open work is derived from the entries
    # below. Keeping both duplicated every task and blocker, and the file's
    # copy is only as current as the last compaction: work resolved since then
    # still sits in the snapshot, which would report finished work as
    # outstanding. The inbox is the store of record, so it wins.
    active_units, active_records = _prose_units(
        memory_store.project_memory_dir(root) / "active.md",
        skip_headings=frozenset({"open work"}),
    )

    # Unresolved work leads the rest of the active section: it is the design's
    # second priority tier and outranks anything a selector would surface.
    # Leads it literally -- trimming keeps a prefix, so whatever sits at the
    # tail is evicted first, and a blocker must not lose its place to a
    # month-old decision.
    open_work = _open_work(project_entries)
    open_units, open_records = _entry_units(open_work, annotator)
    active_units = open_units + active_units
    active_records = open_records + active_records

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

    # Warnings describe the store, not the filtered slice (see LoadedEntries).
    warnings = _freshness_warnings(list(loaded.project_all), now)
    scope_path = root / ".agent-harness" / "scope.md"
    if scope_path.is_file() and not scope_units:
        warnings.append(
            "scope.md is still the untouched template; scope-drift detection "
            "and objective-focused selection cannot work until it is filled in"
        )
    if not loaded.project_all:
        warnings.append("no project memory recorded yet")
    elif not loaded.entry_filter.is_empty and loaded.count == 0:
        warnings.append(
            "filters matched no entries; loosen tags/types or drop exclude"
        )

    all_notes = list(notes)
    filter_note = loaded.note()
    if filter_note:
        all_notes.append(filter_note)

    # Compaction pressure, measured once per pool and never over the two
    # combined: a merged count could not tell the operator which store to act
    # on, and only project memory has a working `compact`. Computed over the
    # raw pools rather than the selected records because `_is_live` strips
    # exactly the terminal entries `closed_in_inbox` exists to count -- reading
    # it off the sections would report zero on the store needing it most.
    all_notes = [
        *all_notes,
        *memory_compact.pressure_notes(
            memory_compact.pressure(project_entries, now), scope="project"
        ),
        *memory_compact.pressure_notes(
            memory_compact.pressure(machine_entries, now), scope="machine"
        ),
    ]

    return ContextPacket(
        project_id=memory_store.project_id_for(root),
        project_root=str(root),
        generated_at=memory_store.format_ts(now),
        strategy=strategy,
        sections=tuple(sections),
        warnings=tuple(warnings),
        notes=tuple(all_notes),
        max_chars=max_chars,
    )


# --- focused search -------------------------------------------------------
#
# `memory_context` answers "what should I know before I start?" and pays for
# a whole packet to do it. Mid-task an agent has a narrower question -- "what
# was decided about the retry policy?" -- and the right answer is a handful
# of ranked entries, not a second packet. `search` is that: no scope prose, no
# warnings, no graph join, a hard result cap, and every hit labelled with the
# project it came from so cross-project hits cannot be mistaken for local ones.

SEARCH_DEFAULT_LIMIT = 8
SEARCH_MAX_LIMIT = 25
SEARCH_DEFAULT_MAX_CHARS = 4000

# The source label for machine-scope entries. Machine memory is shared by
# every project, so it is searched once, as its own source, rather than being
# folded into whichever project happened to be current.
MACHINE_SOURCE = "machine"

# Resolves the selector for one search source: `(source_id, entry_count) ->
# (selector, strategy, notes)`. Injected so this module never imports the
# semantic index; `harness.mcp_server` supplies one that routes each source
# to its own index.
SelectorResolver = Callable[[str, int], tuple[Selector, str, Sequence[str]]]


@dataclass(frozen=True)
class SearchHit:
    source: str
    entry: dict


@dataclass(frozen=True)
class SearchResult:
    query: str
    limit: int
    max_chars: int
    hits: tuple[SearchHit, ...] = ()
    omitted: int = 0
    strategies: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchScope:
    """Which projects a search will read, resolved before any store is opened.

    `projects` is `(project_id, root)` pairs, current project first; `notes`
    say what was asked for and could not be searched, per project, so a
    skipped project is visible rather than silently absent from the results.
    """

    projects: tuple[tuple[str, Path], ...]
    notes: tuple[str, ...] = ()


def related_project_ids(
    entries: Sequence[dict], registered_ids: Sequence[str], current_id: str
) -> list[str]:
    """Counterpart project ids named by accepted relationship entries.

    A relationship entry has no single fixed counterpart field yet -- the
    proposal side of cross-project relationships is still being built -- so
    this reads every place a project id plausibly lives: list fields such as
    `projects` or `related`, the `source` mapping, tags, and any registered id
    spelled out in the summary. Only ids actually in the registry count: a
    relationship to a project this machine does not know cannot be searched
    anyway, and the current project is never its own counterpart.
    """
    registered = set(registered_ids)
    found: list[str] = []

    def consider(value: object) -> None:
        if isinstance(value, str):
            candidate = value.strip()
            if candidate in registered and candidate != current_id and candidate not in found:
                found.append(candidate)
        elif isinstance(value, (list, tuple)):
            for item in value:
                consider(item)

    for entry in entries:
        if entry.get("type") != "relationship" or entry.get("status") != "accepted":
            continue
        for key in ("projects", "related", "related_projects", "project_ids", "counterparts", "target", "target_project"):
            consider(entry.get(key))
        source = entry.get("source")
        if isinstance(source, dict):
            for key in ("project", "projects", "related", "related_project", "counterpart", "target", "target_project", "from", "to"):
                consider(source.get(key))
        consider(entry.get("tags"))
        summary = str(entry.get("summary") or "")
        for project_id in sorted(registered):
            if project_id in summary:
                consider(project_id)
    return found


def resolve_search_projects(
    spec: str | None,
    current_root: Path,
    registry: dict,
    relationships: Sequence[dict] = (),
) -> SearchScope:
    """Turn a `projects` argument into the projects a search will read.

    `"current"` (the default) is the project alone. `"all"` is every
    registered project whose path still exists. `"related"` is every
    counterpart named by an accepted relationship (see `related_project_ids`).
    Anything else is a comma-separated list of registered project ids.

    A missing, unreadable, or empty registry -- `memory_store.load_registry`
    returns an empty one for all three -- narrows to the current project with
    a note; so does a registered project whose path has since vanished. Never
    an error: another project's absence must not stop work on this one.
    """
    root = Path(current_root)
    current_id = memory_store.project_id_for(root)
    wanted_spec = (spec or "current").strip()
    chosen: list[tuple[str, Path]] = [(current_id, root)]
    notes: list[str] = []

    if wanted_spec.lower() == "current":
        return SearchScope(tuple(chosen))

    registered = registry.get("projects") if isinstance(registry, dict) else None
    if not isinstance(registered, dict) or not registered:
        return SearchScope(
            tuple(chosen),
            (
                "no other projects registered on this machine (registry "
                "missing, unreadable, or empty); searched the current project only",
            ),
        )

    wanted: list[str]
    if wanted_spec.lower() == "all":
        wanted = [pid for pid in registered if pid != current_id]
        if not wanted:
            notes.append(
                "no other projects registered on this machine; searched the "
                "current project only"
            )
    elif wanted_spec.lower() == "related":
        wanted = related_project_ids(relationships, list(registered), current_id)
        if not wanted:
            notes.append(
                "no accepted relationship names another registered project; "
                "searched the current project only"
            )
    else:
        wanted = []
        for pid in (p.strip() for p in wanted_spec.split(",")):
            if not pid or pid == current_id:
                continue
            if pid not in registered:
                notes.append(f"project {pid!r} is not registered on this machine; skipped")
                continue
            if pid not in wanted:
                wanted.append(pid)

    seen_paths = {root.resolve()}
    for pid in wanted:
        record = registered.get(pid)
        raw_path = record.get("path") if isinstance(record, dict) else None
        path = Path(str(raw_path)) if raw_path else None
        if path is None or not path.is_dir():
            notes.append(
                f"project {pid}: registered path {raw_path or '?'} no longer "
                "exists; skipped"
            )
            continue
        resolved = path.resolve()
        if resolved in seen_paths:
            # The current project, registered under its own id.
            continue
        seen_paths.add(resolved)
        chosen.append((pid, path))
    return SearchScope(tuple(chosen), tuple(notes))


def _interleave_by_rank(
    ranked: Sequence[tuple[str, Sequence[dict]]], limit: int
) -> list[SearchHit]:
    """Merge per-source rankings: every source's rank 1, then rank 2, and so on.

    Sources are visited in the order given -- current project first -- so a
    tie on rank favours the project the agent is actually working in. Rank is
    the only signal the `Selector` contract exposes, and it is comparable
    across sources in a way raw scores from different strategies are not.
    """
    hits: list[SearchHit] = []
    depth = 0
    while len(hits) < limit:
        added = False
        for source, entries in ranked:
            if depth < len(entries):
                hits.append(SearchHit(source, entries[depth]))
                added = True
                if len(hits) >= limit:
                    break
        if not added:
            break
        depth += 1
    return hits


def search(
    query: str,
    sources: Sequence[tuple[str, Sequence[dict]]],
    limit: int = SEARCH_DEFAULT_LIMIT,
    max_chars: int = SEARCH_DEFAULT_MAX_CHARS,
    resolver: SelectorResolver | None = None,
    notes: Sequence[str] = (),
) -> SearchResult:
    """Rank live entries from each source against `query`; keep the top `limit`.

    Each source is ranked by its own selector (from `resolver`, or
    `deterministic_selector` when none is given) and asked for at most
    `limit` entries, so the work stays bounded by the cap however many
    projects are searched. The per-source rankings are then merged by rank.
    `limit` is clamped to `SEARCH_MAX_LIMIT`, and the rendered hits are then
    budgeted like a packet section -- whole or omitted, with the count stated.
    """
    requested = int(limit)
    limit = max(1, min(requested, SEARCH_MAX_LIMIT))
    all_notes = list(notes)
    if requested > SEARCH_MAX_LIMIT:
        all_notes.append(f"limit {requested} capped at {SEARCH_MAX_LIMIT}")

    ranked: list[tuple[str, list[dict]]] = []
    strategies: list[tuple[str, str]] = []
    # Identical notes from several sources ("semantic selection skipped ...")
    # are stated once, prefixed by every source they apply to, so a search
    # across many projects does not spend its output repeating one sentence.
    source_notes: dict[str, list[str]] = {}
    for source, candidates in sources:
        live = [e for e in candidates if _is_live(e)]
        if not live:
            # Nothing to rank, so no selector is resolved and no index opened.
            strategies.append((source, "no live entries"))
            ranked.append((source, []))
            continue
        if resolver is None:
            selector, strategy, extra = deterministic_selector, "deterministic", ()
        else:
            selector, strategy, extra = resolver(source, len(live))
        for note in extra:
            source_notes.setdefault(note, []).append(source)
        strategies.append((source, strategy))
        ranked.append((source, selector(live, query, limit)))
    all_notes.extend(
        f"{', '.join(names)}: {note}" for note, names in source_notes.items()
    )

    hits = _interleave_by_rank(ranked, limit)
    kept, omitted = _fit([render_search_hit(h) for h in hits], max(int(max_chars), 0))
    return SearchResult(
        query=query,
        limit=limit,
        max_chars=int(max_chars),
        hits=tuple(hits[: len(kept)]),
        omitted=omitted,
        strategies=tuple(strategies),
        notes=tuple(all_notes),
    )


def render_search_hit(hit: SearchHit) -> str:
    """One hit as a single line: the entry's own line, prefixed by its source."""
    line = render_entry(hit.entry)
    return f"- [{hit.source}] {line[2:]}" if line.startswith("- ") else f"- [{hit.source}] {line}"


def render_search_markdown(result: SearchResult) -> str:
    """The search result as Markdown: a header, notes, and one line per hit."""
    shown = len(result.hits)
    ranked_by = ", ".join(f"{source}={strategy}" for source, strategy in result.strategies)
    out = [
        f"# Memory search: {result.query}",
        "",
        f"_{shown} result{'' if shown == 1 else 's'} (limit {result.limit}) - "
        f"ranked: {ranked_by or 'nothing to rank'}_",
    ]
    out.extend(f"_Note: {note}_" for note in result.notes)
    out.append("")
    if result.hits:
        out.extend(render_search_hit(hit) for hit in result.hits)
    elif not result.omitted:
        out.append("_No live entries matched; try broader terms, another `projects` value, or fewer filters._")
    if result.omitted:
        out.append(f"_... {result.omitted} more omitted for budget._")
    return "\n".join(out).rstrip() + "\n"


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
