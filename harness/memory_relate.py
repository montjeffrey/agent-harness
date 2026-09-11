"""Cross-project relationship candidates: the half of the feature that proposes.

`relationship` is already a schema-1 entry type and `memory_context` already
gives *accepted* relationship entries their own packet section. What did not
exist was anything that proposes one: a link between two registered projects
had to be written by hand. This module detects candidates deterministically
across every project in the machine registry and writes them as proposals for
a human to judge.

Three signals, each carrying the entry ids on both sides that constitute it:

**Shared tags.** A tag used in both projects, weighted by rarity across the
machine: ``ln(N / df)`` where ``N`` is the number of projects that have any
memory and ``df`` the number using the tag. A tag on every project weighs
nothing. Two projects that share nothing but ``git`` are not related by it.
A tag is a project's vocabulary only when at least `MIN_TAG_SUPPORT` of its
entries use it: on a machine with three projects every two-project tag has
the same rarity, and the real registry showed three one-off tags (`build`,
`security`, `verification`) linking two unrelated projects on rarity alone.
Tags that merely repeat a schema word (`decision`, `blocker`, `proposed`)
describe the entry, not the subject, and are ignored. On a machine where
only two projects hold memory, every shared tag is on every project and the
signal is silent by construction; cross-references and symbols still work.

**Cross-references.** An entry in project A whose summary or tags name
project B's registered id (always) or registered name (only when the name is
at least `MIN_NAME_CHARS` long and differs from A's own). Each referencing
entry is its own piece of evidence, so this is the one signal strong enough
to propose a link alone.

**Shared symbols.** Symbol-shaped identifiers (`memory_graph.extract_symbols`:
backticked, snake_case, camelCase, `call()`) that both projects' entries
mention. Where a project has a code graph the symbol is checked with
`memory_graph.resolve_unique` -- exactly-unique label match, never guessed --
and a symbol resolved on at least one side counts double. Bare textual
overlap is kept as *weaker* evidence and is labelled as such. Filename-shaped
tokens (`README.md`, `package.json`) are dropped entirely: they are generic
across unrelated projects, and a wrong proposed link wastes a human's time.

**Threshold.** A pair qualifies when its score reaches `min_score` AND it has
either a cross-reference or at least `MIN_EVIDENCE_ITEMS` distinct pieces of
evidence. The second clause is what stops one rare word from proposing a
link on its own, whatever the score arithmetic says.

**Trust and placement.** A cross-project relationship belongs to the machine,
not to either project -- a project store must never reference a path outside
itself -- so proposals are appended to `machine_memory_dir()` through
`memory_store.append_entries`, always ``status: proposed`` and
``confidence: inferred``. Nothing here can accept. Re-running is idempotent: a
pair with a live (proposed or accepted) relationship of the same evidence
class is skipped, a hand-written live relationship between the pair covers
every class, and a rejected proposal is not resurrected unless the evidence
digest has changed.

Deterministic and offline: no network, no model, no subprocess, no chromadb.
The same stores always produce the same candidates in the same order. The
function surface (`relate`, `detect`, `render`) is plain data in and out so an
MCP tool can wrap it later without this module knowing.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from harness import graph_location, memory_graph, memory_store

DETECTOR = "memory_relate"

KIND_SHARED_TAG = "shared-tag"
KIND_CROSS_REFERENCE = "cross-reference"
KIND_SHARED_SYMBOL = "shared-symbol"
KINDS: tuple[str, ...] = (KIND_CROSS_REFERENCE, KIND_SHARED_TAG, KIND_SHARED_SYMBOL)

DEFAULT_MIN_SCORE = 1.0
CROSS_REFERENCE_WEIGHT = 1.0
RESOLVED_SYMBOL_WEIGHT = 0.5
TEXTUAL_SYMBOL_WEIGHT = 0.25

# A pair without a cross-reference needs this many distinct pieces of
# evidence, regardless of score. One shared word is never a relationship.
MIN_EVIDENCE_ITEMS = 2

# Registered names shorter than this are never matched as prose: "api" or
# "web" as a project name would cross-reference half the machine.
MIN_NAME_CHARS = 4
MIN_SYMBOL_CHARS = 4
MIN_TAG_CHARS = 2

# A shared tag counts only when this many entries on *each* side use it.
MIN_TAG_SUPPORT = 2

# Tags that restate the entry contract rather than the subject. Drawn from
# the schema, not from a wordlist of English.
_SCHEMA_WORDS: frozenset[str] = frozenset(
    (*memory_store.ENTRY_TYPES, *memory_store.STATUSES, *memory_store.SCOPES)
)

# Bounds on what one proposal records and renders. The full evidence lives in
# the `Candidate`; the entry keeps enough to judge and to re-derive the digest.
MAX_EVIDENCE_RECORDED = 12
MAX_IDS_PER_SIDE = 5
MAX_LISTED = 3
MAX_EVIDENCE_LINES = 8

# Statuses whose entries still count as signal and, for existing
# relationships, still cover a pair. Closed entries are history.
LIVE_STATUSES: frozenset[str] = frozenset({"proposed", "accepted"})

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*(?:\(\))?$")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_CAMEL = re.compile(r"[a-z0-9][A-Z]")


# --- data ------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectMemory:
    """One registered project and its live entries."""

    project_id: str
    name: str
    root: Path
    entries: tuple[dict, ...]

    @property
    def populated(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class Evidence:
    """One piece of evidence for a pair, with the entry ids behind it.

    `left_ids`/`right_ids` follow the candidate's (sorted) project order. For a
    cross-reference only the referencing side carries ids; `detail` is then
    ``<referrer id>-><referred id>``.
    """

    kind: str
    detail: str
    weight: float
    left_ids: tuple[str, ...]
    right_ids: tuple[str, ...]
    note: str = ""

    @property
    def identity(self) -> str:
        """What makes this evidence *this* evidence for digest purposes.

        A tag or symbol is identified by its name: more entries using an
        already-shared tag is not new evidence. A cross-reference is identified
        by the referencing entries, because each one is a separate claim.
        """
        if self.kind == KIND_CROSS_REFERENCE:
            ids = ",".join(sorted(self.left_ids + self.right_ids))
            return f"{self.kind}:{self.detail}:{ids}"
        return f"{self.kind}:{self.detail}"


@dataclass(frozen=True)
class Candidate:
    """A scored pair of projects. `left < right` by project id."""

    left: str
    right: str
    evidence: tuple[Evidence, ...]

    @property
    def pair(self) -> tuple[str, str]:
        return (self.left, self.right)

    @property
    def score(self) -> float:
        return round(sum(e.weight for e in self.evidence), 3)

    @property
    def kinds(self) -> tuple[str, ...]:
        present = {e.kind for e in self.evidence}
        return tuple(k for k in KINDS if k in present)

    @property
    def evidence_class(self) -> str:
        return "+".join(self.kinds)

    @property
    def digest(self) -> str:
        joined = "\n".join(sorted(e.identity for e in self.evidence))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    @property
    def left_ids(self) -> tuple[str, ...]:
        return tuple(sorted({i for e in self.evidence for i in e.left_ids}))

    @property
    def right_ids(self) -> tuple[str, ...]:
        return tuple(sorted({i for e in self.evidence for i in e.right_ids}))

    def qualifies(self, min_score: float = DEFAULT_MIN_SCORE) -> bool:
        if self.score < min_score:
            return False
        if KIND_CROSS_REFERENCE in self.kinds:
            return True
        return len(self.evidence) >= MIN_EVIDENCE_ITEMS


@dataclass(frozen=True)
class ExistingRelationship:
    """A relationship entry already in the machine store, reduced to what
    idempotence needs. `evidence_class`/`digest` are None for a hand-written
    entry, which covers every class."""

    entry_id: str
    status: str
    pair: tuple[str, str]
    evidence_class: str | None
    digest: str | None


@dataclass(frozen=True)
class RelateResult:
    """Observable outcome of one pass."""

    projects: tuple[ProjectMemory, ...]
    candidates: tuple[Candidate, ...]
    proposed: tuple[Candidate, ...]
    proposed_ids: tuple[str, ...]
    skipped: tuple[tuple[Candidate, str], ...]
    below_threshold: tuple[Candidate, ...]
    dry_run: bool
    notes: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.proposed)

    def name_of(self, project_id: str) -> str:
        for project in self.projects:
            if project.project_id == project_id:
                return project.name
        return project_id


# --- loading ---------------------------------------------------------------


def load_projects(registry: Mapping | None = None) -> tuple[list[ProjectMemory], list[str]]:
    """Every registered project with its live entries, plus notes.

    A registered path that no longer exists is reported and skipped rather
    than raised: the registry is bookkeeping, and `prune` is the tool for it.
    Closed entries (rejected/superseded/resolved) are not signal.
    """
    registry = registry if registry is not None else memory_store.load_registry()
    projects: list[ProjectMemory] = []
    notes: list[str] = []
    records = registry.get("projects") if isinstance(registry, Mapping) else None
    if not isinstance(records, Mapping):
        return projects, notes

    for project_id in sorted(records):
        record = records[project_id]
        if not isinstance(record, Mapping):
            continue
        path = str(record.get("path") or "")
        root = Path(path) if path else None
        if root is None or not root.is_dir():
            notes.append(f"{project_id}: registered path is missing; skipped")
            continue
        name = str(record.get("name") or root.name)
        entries = tuple(
            e
            for e in memory_store.iter_entries(memory_store.project_memory_dir(root))
            if e.get("status", "proposed") in LIVE_STATUSES and e.get("id")
        )
        projects.append(ProjectMemory(project_id, name, root, entries))
    return projects, notes


# --- signal extraction -----------------------------------------------------


def _entry_text(entry: Mapping) -> str:
    summary = entry.get("summary")
    tags = entry.get("tags")
    parts = [summary] if isinstance(summary, str) else []
    if isinstance(tags, (list, tuple)):
        parts.extend(t for t in tags if isinstance(t, str))
    return " ".join(parts)


def _tag_index(project: ProjectMemory) -> dict[str, list[str]]:
    """tag -> entry ids using it, tags folded to lower case."""
    index: dict[str, list[str]] = {}
    for entry in project.entries:
        tags = entry.get("tags")
        if not isinstance(tags, (list, tuple)):
            continue
        seen: set[str] = set()
        for tag in tags:
            if not isinstance(tag, str):
                continue
            key = tag.strip().lower()
            if len(key) < MIN_TAG_CHARS or key in seen or key in _SCHEMA_WORDS:
                continue
            seen.add(key)
            index.setdefault(key, []).append(str(entry["id"]))
    return index


def _normalize_symbol(term: str) -> str:
    return term.strip().removesuffix("()").strip().lower()


def _cross_project_symbol(term: str) -> bool:
    """Whether a symbol `memory_graph` extracted is specific enough to compare
    across projects. Stricter than the in-project filter on purpose."""
    token = term.strip()
    if not _IDENTIFIER.match(token) or len(_normalize_symbol(token)) < MIN_SYMBOL_CHARS:
        return False
    if token.endswith("()") or "_" in token.strip("_") or _CAMEL.search(token):
        return True
    # Only a file extension gave it symbol shape: generic across projects.
    return not _EXTENSION.search(token)


def _graph_index(project: ProjectMemory) -> memory_graph.SymbolIndex | None:
    """The project's code graph prepared for lookup, or None. Never raises,
    never shells out: health is not checked here because the *presence* of a
    unique label is the evidence, not its freshness."""
    try:
        path = graph_location.find_graph_in_repo(project.root)
        if path is None:
            return None
        graph = graph_location.load_graph_metadata(path)
        if graph is None:
            return None
        return memory_graph.build_symbol_index(graph)
    except Exception:  # noqa: BLE001 -- enrichment, never a dependency
        return None


def _symbol_index(project: ProjectMemory) -> dict[str, tuple[str, list[str], bool]]:
    """symbol key -> (display form, entry ids mentioning it, resolved in graph)."""
    graph = _graph_index(project)
    index: dict[str, tuple[str, list[str], bool]] = {}
    for entry in project.entries:
        for symbol in memory_graph.extract_symbols(entry):
            if not _cross_project_symbol(symbol):
                continue
            key = _normalize_symbol(symbol)
            display, ids, resolved = index.get(key, (symbol.strip(), [], False))
            if not resolved and graph is not None:
                resolved = memory_graph.resolve_unique(symbol, graph) is not None
            if str(entry["id"]) not in ids:
                ids.append(str(entry["id"]))
            index[key] = (display, ids, resolved)
    return index


def _reference_pattern(referrer: ProjectMemory, referred: ProjectMemory) -> re.Pattern[str]:
    """Match `referred`'s id, or its name when it is safe to match as prose."""
    alternatives = [r"(?<![\w-])" + re.escape(referred.project_id) + r"(?![\w-])"]
    name = referred.name.strip()
    if len(name) >= MIN_NAME_CHARS and name.lower() != referrer.name.strip().lower():
        alternatives.append(r"(?<!\w)" + re.escape(name) + r"(?!\w)")
    return re.compile("|".join(alternatives), re.IGNORECASE)


def _references(referrer: ProjectMemory, referred: ProjectMemory) -> tuple[str, ...]:
    """Ids of `referrer` entries that name `referred`."""
    pattern = _reference_pattern(referrer, referred)
    return tuple(
        str(e["id"]) for e in referrer.entries if pattern.search(_entry_text(e))
    )


# --- detection -------------------------------------------------------------


def detect(projects: Sequence[ProjectMemory], only: Sequence[str] = ()) -> list[Candidate]:
    """Every pair of populated projects with any evidence, best first.

    `only` restricts the pairs to those involving at least one named project;
    rarity weights are still computed over the whole machine, because that is
    what "rare" means.
    """
    populated = sorted((p for p in projects if p.populated), key=lambda p: p.project_id)
    if len(populated) < 2:
        return []

    wanted = set(only)
    tags = {p.project_id: _tag_index(p) for p in populated}
    document_frequency: dict[str, int] = {}
    for index in tags.values():
        for tag in index:
            document_frequency[tag] = document_frequency.get(tag, 0) + 1
    total = len(populated)

    symbols: dict[str, dict[str, tuple[str, list[str], bool]]] = {}

    def symbols_for(project: ProjectMemory) -> dict[str, tuple[str, list[str], bool]]:
        if project.project_id not in symbols:
            symbols[project.project_id] = _symbol_index(project)
        return symbols[project.project_id]

    candidates: list[Candidate] = []
    for left, right in combinations(populated, 2):
        if wanted and left.project_id not in wanted and right.project_id not in wanted:
            continue
        evidence: list[Evidence] = []

        left_to_right = _references(left, right)
        if left_to_right:
            evidence.append(Evidence(
                KIND_CROSS_REFERENCE,
                f"{left.project_id}->{right.project_id}",
                round(CROSS_REFERENCE_WEIGHT * len(left_to_right), 3),
                left_to_right, (),
            ))
        right_to_left = _references(right, left)
        if right_to_left:
            evidence.append(Evidence(
                KIND_CROSS_REFERENCE,
                f"{right.project_id}->{left.project_id}",
                round(CROSS_REFERENCE_WEIGHT * len(right_to_left), 3),
                (), right_to_left,
            ))

        left_tags, right_tags = tags[left.project_id], tags[right.project_id]
        for tag in sorted(set(left_tags) & set(right_tags)):
            if min(len(left_tags[tag]), len(right_tags[tag])) < MIN_TAG_SUPPORT:
                continue
            weight = math.log(total / document_frequency[tag])
            if weight <= 0:
                continue
            evidence.append(Evidence(
                KIND_SHARED_TAG, tag, round(weight, 3),
                tuple(left_tags[tag]), tuple(right_tags[tag]),
                note=f"on {document_frequency[tag]} of {total} projects",
            ))

        left_symbols, right_symbols = symbols_for(left), symbols_for(right)
        for key in sorted(set(left_symbols) & set(right_symbols)):
            display, left_ids, left_resolved = left_symbols[key]
            _, right_ids, right_resolved = right_symbols[key]
            resolved = left_resolved or right_resolved
            evidence.append(Evidence(
                KIND_SHARED_SYMBOL, display,
                RESOLVED_SYMBOL_WEIGHT if resolved else TEXTUAL_SYMBOL_WEIGHT,
                tuple(left_ids), tuple(right_ids),
                note="resolved in a code graph" if resolved else "textual match only, weaker",
            ))

        if evidence:
            candidates.append(Candidate(left.project_id, right.project_id, tuple(evidence)))

    candidates.sort(key=lambda c: (-c.score, c.left, c.right))
    return candidates


# --- idempotence -----------------------------------------------------------


def _mentioned_projects(entry: Mapping, projects: Sequence[ProjectMemory]) -> tuple[str, ...]:
    """Registered ids a hand-written relationship names, by id or by name."""
    text = _entry_text(entry)
    found: list[str] = []
    for project in projects:
        alternatives = [r"(?<![\w-])" + re.escape(project.project_id) + r"(?![\w-])"]
        if len(project.name.strip()) >= MIN_NAME_CHARS:
            alternatives.append(r"(?<!\w)" + re.escape(project.name.strip()) + r"(?!\w)")
        if re.search("|".join(alternatives), text, re.IGNORECASE):
            found.append(project.project_id)
    return tuple(found)


def existing_relationships(
    machine_dir: Path, projects: Sequence[ProjectMemory]
) -> list[ExistingRelationship]:
    """Relationship entries already in the machine store, every status.

    Detector-written entries carry their pair, class, and digest in `source`.
    A hand-written one is attributed to every pair it names; with no class it
    covers all of them, because a human already judged that pair.
    """
    found: list[ExistingRelationship] = []
    for entry in memory_store.iter_entries(machine_dir, type="relationship"):
        entry_id = str(entry.get("id") or "")
        status = str(entry.get("status") or "proposed")
        source = entry.get("source") if isinstance(entry.get("source"), Mapping) else {}
        pair_ids = source.get("projects")
        if (
            source.get("detector") == DETECTOR
            and isinstance(pair_ids, list)
            and len(pair_ids) == 2
            and all(isinstance(p, str) for p in pair_ids)
        ):
            left, right = sorted(pair_ids)
            found.append(ExistingRelationship(
                entry_id, status, (left, right),
                str(source.get("evidence_class") or "") or None,
                str(source.get("evidence_digest") or "") or None,
            ))
            continue
        mentioned = _mentioned_projects(entry, projects)
        if isinstance(pair_ids, list):
            mentioned = tuple(dict.fromkeys(
                [*mentioned, *(p for p in pair_ids if isinstance(p, str))]
            ))
        for left, right in combinations(sorted(mentioned), 2):
            found.append(ExistingRelationship(entry_id, status, (left, right), None, None))
    return found


def plan(
    candidates: Sequence[Candidate], existing: Sequence[ExistingRelationship]
) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    """Split qualified candidates into those to propose and those covered.

    Skip reasons are the whole contract: a live entry whose evidence class
    already carries every kind this candidate has (or a hand-written live
    one, any class) covers the pair; a rejection with the same evidence
    digest, or a hand-written rejection, keeps it closed. A candidate with a
    *new* kind of evidence is proposed beside the older one, because that is
    new information for the reviewer.
    """
    to_propose: list[Candidate] = []
    skipped: list[tuple[Candidate, str]] = []
    for candidate in candidates:
        reason = None
        for prior in existing:
            if prior.pair != candidate.pair:
                continue
            covered = prior.evidence_class is None or set(candidate.kinds) <= set(
                prior.evidence_class.split("+")
            )
            if prior.status in LIVE_STATUSES and covered:
                reason = f"already {prior.status} as {prior.entry_id}"
                break
            if prior.status == "rejected" and prior.digest in (None, candidate.digest):
                reason = f"rejected as {prior.entry_id} and the evidence has not changed"
                break
        if reason:
            skipped.append((candidate, reason))
        else:
            to_propose.append(candidate)
    return to_propose, skipped


# --- proposal shape --------------------------------------------------------


def _listed(items: Sequence[str]) -> str:
    shown = list(items[:MAX_LISTED])
    hidden = len(items) - len(shown)
    return ", ".join(shown) + (f", +{hidden} more" if hidden > 0 else "")


def summarize(candidate: Candidate, left: ProjectMemory, right: ProjectMemory) -> str:
    """One line a human can judge without opening either store."""
    clauses: list[str] = []
    names = {left.project_id: left.name, right.project_id: right.name}
    for evidence in candidate.evidence:
        if evidence.kind != KIND_CROSS_REFERENCE:
            continue
        referrer, referred = evidence.detail.split("->", 1)
        count = len(evidence.left_ids + evidence.right_ids)
        noun = "entry" if count == 1 else "entries"
        clauses.append(f"{count} {names[referrer]} {noun} name {names[referred]}")
    shared_tags = [e.detail for e in candidate.evidence if e.kind == KIND_SHARED_TAG]
    if shared_tags:
        noun = "tag" if len(shared_tags) == 1 else "tags"
        clauses.append(f"{len(shared_tags)} distinctive {noun} in common ({_listed(shared_tags)})")
    shared_symbols = [e.detail for e in candidate.evidence if e.kind == KIND_SHARED_SYMBOL]
    if shared_symbols:
        noun = "symbol" if len(shared_symbols) == 1 else "symbols"
        clauses.append(f"{len(shared_symbols)} {noun} in common ({_listed(shared_symbols)})")
    return (
        f"{left.name} and {right.name} look related (score {candidate.score:g}): "
        + "; ".join(clauses)
        + "."
    )


def to_entry(candidate: Candidate, left: ProjectMemory, right: ProjectMemory) -> dict:
    """The machine-scope proposal for `candidate`. Never accepted, never a path."""
    return {
        "type": "relationship",
        "scope": "machine",
        "status": "proposed",
        "confidence": "inferred",
        "summary": summarize(candidate, left, right),
        "tags": ["cross-project", *candidate.kinds],
        "source": {
            "detector": DETECTOR,
            "projects": [candidate.left, candidate.right],
            "evidence_class": candidate.evidence_class,
            "evidence_digest": candidate.digest,
            "score": candidate.score,
            "evidence": [
                {
                    "kind": e.kind,
                    "detail": e.detail,
                    "weight": e.weight,
                    "left_ids": list(e.left_ids[:MAX_IDS_PER_SIDE]),
                    "right_ids": list(e.right_ids[:MAX_IDS_PER_SIDE]),
                }
                for e in candidate.evidence[:MAX_EVIDENCE_RECORDED]
            ],
        },
    }


# --- the pass --------------------------------------------------------------


def relate(
    *,
    only: Sequence[str] = (),
    min_score: float = DEFAULT_MIN_SCORE,
    dry_run: bool = False,
    now: datetime | None = None,
    registry: Mapping | None = None,
    machine_dir: Path | None = None,
) -> RelateResult:
    """Detect, threshold, de-duplicate, and (unless `dry_run`) propose.

    Raises LookupError for an `only` id that is not registered and ValueError
    for a negative `min_score` or an entry the store contract refuses; both
    are the caller's input, not a detector failure.
    """
    if min_score < 0:
        raise ValueError(f"--min-score must be zero or more, got {min_score}")
    registry = registry if registry is not None else memory_store.load_registry()
    machine_dir = Path(machine_dir) if machine_dir is not None else memory_store.machine_memory_dir()
    now = now or datetime.now(timezone.utc)

    projects, notes = load_projects(registry)
    known = {p.project_id for p in projects}
    registered = registry.get("projects") if isinstance(registry, Mapping) else {}
    unknown = [pid for pid in only if pid not in (registered or {}) and pid not in known]
    if unknown:
        raise LookupError("not in the registry: " + ", ".join(sorted(set(unknown))))

    populated = [p for p in projects if p.populated]
    if len(populated) < 2:
        notes.append(
            f"{len(populated)} registered project(s) have memory; "
            f"relationships need at least two"
        )

    candidates = detect(projects, only=only)
    qualified = [c for c in candidates if c.qualifies(min_score)]
    below = [c for c in candidates if not c.qualifies(min_score)]
    to_propose, skipped = plan(qualified, existing_relationships(machine_dir, projects))

    by_id = {p.project_id: p for p in projects}
    entries = [to_entry(c, by_id[c.left], by_id[c.right]) for c in to_propose]
    proposed_ids: tuple[str, ...] = ()
    if entries and not dry_run:
        written = memory_store.append_entries(entries, machine_dir, now=now)
        proposed_ids = tuple(
            str(e["id"]) for e in list(memory_store.iter_entries(machine_dir))[-written:]
        )

    return RelateResult(
        projects=tuple(projects),
        candidates=tuple(candidates),
        proposed=tuple(to_propose),
        proposed_ids=proposed_ids,
        skipped=tuple(skipped),
        below_threshold=tuple(below),
        dry_run=dry_run,
        notes=tuple(notes),
    )


# --- rendering -------------------------------------------------------------


def _render_evidence(evidence: Evidence, result: RelateResult) -> str:
    if evidence.kind == KIND_CROSS_REFERENCE:
        referrer, referred = evidence.detail.split("->", 1)
        ids = evidence.left_ids + evidence.right_ids
        return (
            f"    {evidence.kind}: {_listed(ids)} ({result.name_of(referrer)}) "
            f"name {result.name_of(referred)}  +{evidence.weight:g}"
        )
    sides = []
    if evidence.left_ids:
        sides.append(f"{_listed(evidence.left_ids)}")
    if evidence.right_ids:
        sides.append(f"{_listed(evidence.right_ids)}")
    note = f" [{evidence.note}]" if evidence.note else ""
    return f"    {evidence.kind}: {evidence.detail}  +{evidence.weight:g}{note}  {' | '.join(sides)}"


def _render_candidate(candidate: Candidate, result: RelateResult) -> list[str]:
    lines = [
        f"  {result.name_of(candidate.left)} <-> {result.name_of(candidate.right)}"
        f"  score {candidate.score:g}  [{', '.join(candidate.kinds)}]"
        f"  ({candidate.left}, {candidate.right})"
    ]
    for evidence in candidate.evidence[:MAX_EVIDENCE_LINES]:
        lines.append(_render_evidence(evidence, result))
    hidden = len(candidate.evidence) - MAX_EVIDENCE_LINES
    if hidden > 0:
        lines.append(f"    +{hidden} more piece(s) of evidence")
    return lines


def render(result: RelateResult) -> str:
    """Deterministic, bounded CLI output: what was (or would be) proposed,
    what was skipped and why, and what fell short of the threshold."""
    populated = sum(1 for p in result.projects if p.populated)
    verb = "would propose" if result.dry_run else "proposed"
    lines = [
        f"{verb} {len(result.proposed)} relationship(s) across "
        f"{len(result.projects)} registered project(s), {populated} with memory"
    ]
    for note in result.notes:
        lines.append(f"  note: {note}")
    for index, candidate in enumerate(result.proposed):
        lines.extend(_render_candidate(candidate, result))
        if result.proposed_ids and index < len(result.proposed_ids):
            lines.append(f"    -> {result.proposed_ids[index]} [proposed/inferred] relationship")
    if result.skipped:
        lines.append(f"skipped {len(result.skipped)} pair(s) already covered:")
        for candidate, reason in result.skipped:
            lines.append(
                f"  {result.name_of(candidate.left)} <-> {result.name_of(candidate.right)}: {reason}"
            )
    if result.below_threshold:
        lines.append(f"below threshold, not proposed: {len(result.below_threshold)} pair(s)")
        for candidate in result.below_threshold:
            lines.append(
                f"  {result.name_of(candidate.left)} <-> {result.name_of(candidate.right)}"
                f"  score {candidate.score:g}  [{', '.join(candidate.kinds)}]"
                f"  {len(candidate.evidence)} piece(s) of evidence"
            )
    if result.dry_run:
        lines.append("dry run -- nothing was written.")
    elif result.proposed:
        lines.append("Accept with `agent-harness-memory review accept <id> --scope machine`.")
    return "\n".join(lines)
