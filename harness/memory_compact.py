"""Weekly compaction: keep the inbox bounded so retrieval stays fresh.

Every other memory command adds. `remember` appends, `review` transitions a
status, `index` mirrors whatever is there. Nothing removes, so an inbox in
long-term use grows without bound -- and a growing inbox degrades exactly the
things the store exists to provide: the selector ranks over more candidates,
the context packet spends its budget on settled work, and the semantic index
carries entries nobody will ever need again.

Compaction is the only operation that removes entries from the store of
record, which is why the rules here are narrow and why the ordering matters.

**What may leave the inbox.** Only two things: entries in a closed status
(rejected/superseded/resolved -- work that is genuinely over), and duplicate
*proposals*. Nothing else. In particular an unreviewed proposal is never
discarded, because compaction is automated maintenance and the decision to
throw away an observation belongs to a human. Everything removed is written
to the week's archive first, so a removal is always recoverable.

Near-duplicates -- one summary's wording contained in another's -- are
*reported* and never removed. Containment is not equivalence: the longer
entry states a strictly more specific claim, so collapsing the pair would
destroy a claim rather than a repetition. `find_near_duplicates` feeds the
pressure signal only, and points the operator at `review`.

Within a duplicate group the survivor is the entry carrying the most support
-- evidence first, then tags, unknown fields, and wording -- not the one
recorded first. See `find_duplicates`: because the group key *is* the
normalized summary, twins differ only in their support, and keeping the
earliest kept the barest.

Whatever the dropped twins held that the survivor lacked is then folded into
it, so ranking chooses the base record rather than deciding what is lost.
This is the one place compaction edits an entry that stays, and it is
deliberately narrow: only gaps are filled, never existing values; `id`, `ts`,
`type`, `scope`, `status`, `summary` and `confidence` never move; the splice
stays line-level with the rewritten survivors named explicitly, so every
other line -- including one this schema version cannot parse -- is still
copied across untouched. The count is reported as `CompactResult.merged`.

**Promotion is a copy, not a move.** Accepted knowledge is written into
`durable.md` for human and Git review, but stays in the inbox. `memory_context`
reads durable knowledge *out of the inbox* -- it treats only `scope.md` and
`active.md` as prose -- so moving an accepted decision into `durable.md` would
delete it from retrieval and from the semantic index. That would make the
store smaller by making it worse.

**Write ordering is the crash-safety guarantee**, more than the snapshot is.
The inbox is the store of record and every other file is derived from it, so
the inbox is spliced *last*: archive, then durable, then active, then inbox. A
crash at any earlier point loses only derived work that the next run redoes.
Had the inbox been rewritten first, the same crash would lose entries.

**When to run it** is answered by `compaction_due`, which reads the
``Last compacted:`` stamp `render_active` writes into `active.md` and
compares it with `COMPACT_INTERVAL_DAYS`. `memory_maintain.maintain` is the
unattended caller: it compacts only when due, so running it on every
session start costs one file read the rest of the week.

**Project scope only.** `compact()` operates on `project_memory_dir`. The
machine inbox that `remember --scope machine` feeds has no compaction pass yet,
so the half of the store shared across every project still grows without
bound. It needs different rules rather than the same ones pointed at another
directory -- machine memory has no repository to be reviewed in, no per-project
week to archive against, and promotion there is the cross-machine question
rather than the project-durable one.

Otherwise deliberately **not** implemented here, because each is its own
roadmap item and folding them in would repeat the plan-drift this increment was
meant to end: scope-drift detection (design step 6), cross-project relationship
proposals (step 9), contradiction detection (step 5, which exists nowhere yet),
and the optional AI summarization pass. Design step 4 (reconcile task and blocker
states) needs no work here -- `memory_review` and `memory_tasks` already
enforce those transitions at write time, so there is nothing left to reconcile.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from harness import atomicwrite, memory_store


# Budgets for the rewritten `active.md`. Module constants rather than config:
# `init_project` does not yet write budget settings into config.json, and
# inventing a config system inside this increment would be scope creep.
ACTIVE_MAX_ENTRIES = 20
ACTIVE_MAX_CHARS = 4000

# Types whose accepted entries represent knowledge that outlives the work that
# produced it. Tasks and blockers are transient; relationships are rendered by
# the context packet as their own section and are not project-durable prose.
PROMOTABLE_TYPES: frozenset[str] = frozenset({"decision", "discovery", "lesson"})
WORK_TYPES: frozenset[str] = frozenset({"task", "blocker"})

LOCK_NAME = ".compact.lock"
LOCK_STALE_SECONDS = 900

# When a backlog of unreviewed proposals is worth mentioning. Named constants
# beside the predicates rather than configuration: there is no evidence yet
# about what the right numbers are, and a config surface for two untuned
# integers would be speculative. A proposal recorded this morning is the system
# working; one sitting a fortnight is the failure these thresholds name. Age is
# the sharper trigger; the count catches a sudden pile-up.
UNREVIEWED_COUNT_THRESHOLD = 5
UNREVIEWED_AGE_DAYS = 7

# Shortest summary that may be reported as contained in another. Containment
# over two or three words is coincidence -- "fix it" sits inside a great many
# sentences -- and a signal that fires on coincidence is one the operator
# learns to scroll past.
NEAR_DUPLICATE_MIN_TOKENS = 4

# Fields a dropped duplicate never lends its survivor. Identity and timestamp
# are what make the entry that entry; type, scope and summary are the claim,
# which grouping already proved identical. `confidence` is the load-bearing
# one -- see `merge_duplicate_detail`.
MERGE_PROTECTED_FIELDS: frozenset[str] = frozenset(
    {"schema", "id", "ts", "type", "scope", "status", "summary", "confidence"}
)

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# How often a project is due for compaction. Weekly is the design's cadence
# (the archive is filed by ISO week); the override exists so an operator can
# tighten it on a busy store or set it to 0 to make `maintain` compact on
# every run. Read through `compact_interval_days`, the same shape as
# `memory_index.adaptive_min_entries`: an unset or unparseable value falls
# back to the default rather than failing the command.
COMPACT_INTERVAL_DAYS = 7
COMPACT_INTERVAL_ENV = "AGENT_HARNESS_COMPACT_INTERVAL_DAYS"

_ENTRY_ID_RE = re.compile(r"mem-\d{4}-\d{2}-\d{2}-\d{3,}")
# The stamp `render_active` writes; reading it back is how "when did this
# last run" is answered without a second state file that could disagree.
_LAST_COMPACTED_RE = re.compile(r"^Last compacted: (\S+)", re.MULTILINE)
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


class CompactLockError(RuntimeError):
    """Another compaction holds the project lock."""


@dataclass(frozen=True)
class CompactResult:
    """What one compaction pass did."""

    week: str
    archived: int = 0
    duplicates: int = 0
    # Survivors rewritten to carry a dropped twin's support. Reported because
    # it is the one case where compaction edits an entry that stays.
    merged: int = 0
    promoted: int = 0
    carried: int = 0
    omitted: int = 0
    archive_path: Path = Path()
    index_stale: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.archived or self.promoted)


@dataclass(frozen=True)
class CompactionPressure:
    """How much a store would gain from maintenance, measured on its entries.

    Not elapsed time since the last run. Elapsed time nags a clean store and
    stays silent on a dirty one that happens to have been compacted recently;
    counting what compaction would actually act on speaks the day something
    closes and says nothing the week nothing does. It also dissolves the
    "never compacted" case: a store that has never run a pass is not a special
    state, it simply has whatever pressure it has.
    """

    closed_in_inbox: int = 0
    duplicate_proposals: int = 0
    unreviewed: int = 0
    oldest_unreviewed_days: int | None = None
    # Reported, never removed -- see `find_near_duplicates`. Kept in its own
    # field rather than folded into `duplicate_proposals` because the two are
    # different kinds of number: that one counts what `compact` will remove,
    # this one counts what only a human can decide about.
    near_duplicate_proposals: int = 0


# --- pure helpers ---------------------------------------------------------


def week_stamp(now: datetime) -> str:
    """The ISO week label used for archive filenames, e.g. ``2026-W33``.

    Uses the ISO *year* from `isocalendar()`, not the calendar year: 2027-01-01
    falls in ISO week 53 of 2026, and filing it under 2027 would put the entry
    in a week that does not contain its date.
    """
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def normalize_summary(text: object) -> str:
    """Fold a summary to its comparison form for duplicate detection.

    Case, surrounding whitespace, internal whitespace runs, and punctuation are
    all discarded. Two observations of the same fact recorded minutes apart by
    different agents differ in exactly these ways and in nothing that matters.
    """
    lowered = str(text or "").lower()
    return _WHITESPACE_RE.sub(" ", _PUNCTUATION_RE.sub("", lowered)).strip()


def is_terminal(entry: Mapping) -> bool:
    """Whether `entry` represents work that is over and may be archived."""
    return entry.get("status") in memory_store.CLOSED_STATUSES


def is_promotable(entry: Mapping) -> bool:
    """Whether `entry` is confirmed knowledge worth writing into `durable.md`.

    Status alone is the gate. The trust model already guarantees that accepted
    implies evidence and that an inferred entry can never be accepted, so no
    separate confidence check is needed here.
    """
    return entry.get("status") == "accepted" and entry.get("type") in PROMOTABLE_TYPES


def detail_rank(entry: Mapping) -> tuple[int, ...]:
    """How much `entry` carries beyond the wording its group is keyed on.

    Read as a ladder, most significant first, each rung consulted only when
    the one above ties:

    1. **Evidence**, via `memory_store.has_evidence` -- a commit, a file, or a
       manual attestation. Deliberately that function and not a local copy of
       its rule: it is what `validate_entry` enforces for `accepted`, and a
       dedup that disagreed would keep the twin review then refuses.
    2. **Source keys.** Attribution alone (`project`) is not evidence, but it
       is more than nothing, so it outranks an absent source.
    3. **Tags**, which is what the selector and the index filter on.
    4. **Unrecognised fields.** A key this schema version cannot read is still
       detail -- the store preserves unknown fields for forward compatibility,
       and dedup must not be the one place that quietly prefers the copy
       without them.
    5. **Raw summary length**, last and least. Grouping already folded away
       case, punctuation and spacing, so this separates twins that differ in
       nothing else; the longer text is the one that kept its punctuation.

    Rungs, not weights: each either breaks the tie or defers to the next.
    Summing them would let three tags outvote a commit hash.
    """
    source = entry.get("source")
    source = source if isinstance(source, Mapping) else {}
    tags = entry.get("tags")
    tags = tags if isinstance(tags, (list, tuple)) else ()
    return (
        int(memory_store.has_evidence(source)),
        sum(1 for value in source.values() if value not in (None, "", False)),
        len(tags),
        sum(1 for key in entry if key not in memory_store.SCHEMA_1_FIELDS),
        len(str(entry.get("summary") or "")),
    )


def merge_duplicate_detail(keeper: Mapping, twins: Sequence[Mapping]) -> dict:
    """A copy of `keeper` holding support its dropped `twins` carried.

    Ranking picks a whole entry, so whatever the losers held beyond the winner
    was archived and left the inbox. Usually that is nothing; when it is a
    file reference or a tag the winner lacked, the store came out poorer than
    the sum of what it was told. This fills only the gaps.

    **Gaps, never rewrites.** A key the keeper already has is left exactly as
    it is, including a `source.commit` that differs from the twin's -- two
    different commits are two assertions, and picking one silently would
    invent an attribution nobody made. Tags union in first-seen order; unknown
    fields are adopted only when absent.

    `MERGE_PROTECTED_FIELDS` never moves. Identity and timestamp are what make
    the entry that entry; type, scope and summary are the claim itself, and
    grouping already proved the claim identical. `confidence` is the sharp
    one: folding a `confirmed` twin's confidence into an `inferred` keeper
    would promote an entry no human confirmed, which is precisely the
    escalation the review model exists to prevent.

    Returns a new dict; the inputs are never mutated.
    """
    merged = dict(keeper)
    raw_source = merged.get("source")
    source = dict(raw_source) if isinstance(raw_source, Mapping) else {}
    raw_tags = merged.get("tags")
    tags = list(raw_tags) if isinstance(raw_tags, (list, tuple)) else []

    for twin in twins:
        twin_source = twin.get("source")
        if isinstance(twin_source, Mapping):
            for key, value in twin_source.items():
                if key not in source and value not in (None, "", False):
                    source[key] = value
        twin_tags = twin.get("tags")
        if isinstance(twin_tags, (list, tuple)):
            for tag in twin_tags:
                if tag not in tags:
                    tags.append(tag)
        for key, value in twin.items():
            if key in MERGE_PROTECTED_FIELDS or key in ("source", "tags"):
                continue
            if key not in merged:
                merged[key] = value

    if source:
        merged["source"] = source
    if tags:
        merged["tags"] = tags
    return merged


def resolve_duplicates(entries: Sequence[Mapping]) -> tuple[set[str], dict[str, dict]]:
    """Duplicate ids to drop, plus survivors that gained detail from them.

    Returns `(dropped_ids, {survivor_id: merged_record})`. The second map
    holds only survivors that actually changed, so a group whose members were
    identical in everything but wording produces no rewrite at all.

    Split out from `find_duplicates` so `pressure` can keep asking the cheap
    question -- how many entries would leave -- without computing merges it
    has no use for.
    """
    groups: dict[tuple, list[Mapping]] = {}
    for entry in entries:
        if entry.get("status") != "proposed":
            continue
        if not str(entry.get("id") or ""):
            continue
        key = (entry.get("type"), entry.get("scope"), normalize_summary(entry.get("summary")))
        groups.setdefault(key, []).append(entry)

    dropped: set[str] = set()
    merged_survivors: dict[str, dict] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        # `min` over negated ranks: the richest entry sorts first, and on an
        # exact tie the smallest id -- the earliest -- wins.
        keeper = min(
            members,
            key=lambda e: (tuple(-v for v in detail_rank(e)), str(e.get("id") or "")),
        )
        keeper_id = str(keeper.get("id") or "")
        twins = [e for e in members if str(e.get("id") or "") != keeper_id]
        dropped.update(str(e.get("id") or "") for e in twins)
        merged = merge_duplicate_detail(keeper, twins)
        if merged != dict(keeper):
            merged_survivors[keeper_id] = merged
    return dropped, merged_survivors


def find_duplicates(entries: Sequence[Mapping]) -> set[str]:
    """Ids of redundant *proposals*, keeping the most detailed of each group.

    Only proposals are considered. Two accepted entries that read alike may
    cite different commits, and collapsing them would destroy the audit trail
    the trust model exists to preserve -- so accepted entries are never
    deduplicated, however similar their wording.

    Grouping is by (type, scope, normalized summary): identical wording as a
    task and as a decision are two different claims, not one repeated.

    **Which twin survives is decided by the grouping key.** Because the key
    *is* the normalized summary, twins state the same claim by construction --
    so what differs between them is never the claim, it is the support: the
    commit that proves it, the tags that retrieve it, the fields a later
    schema will read. Keeping the earliest, as this did until now, picked the
    copy written first, which is systematically the barest one: an agent
    records a fact on noticing it and re-records it on proving it. The proof
    was archived and the bare assertion kept -- and since `accepted` requires
    evidence, the survivor could not pass review without a human restoring by
    hand what compaction had just filed away.

    `detail_rank` decides; the earliest id breaks a genuine tie, so ordering
    stays deterministic and a group whose members carry equal detail collapses
    exactly as it always did.

    Whatever a dropped twin held that the survivor lacked is folded into the
    survivor by `merge_duplicate_detail`, so ranking chooses the base record
    rather than deciding what is lost. This function reports only the
    removals; `resolve_duplicates` returns both halves.
    """
    return resolve_duplicates(entries)[0]


def find_near_duplicates(entries: Sequence[Mapping]) -> set[str]:
    """Ids of *proposals* whose wording one another contains -- a report only.

    `find_duplicates` needs the normalized summaries to match exactly, so it
    misses the commoner shape: one agent records "Migrated the users table"
    and another records "Migrated the users table to Postgres with zero
    downtime". Those are almost certainly one piece of work written twice.

    **Almost certainly is why nothing here removes them.** Containment is not
    equivalence -- the longer entry states a strictly more specific claim, and
    the specific one is often the one worth keeping -- and the governing design
    permits reporting stale memory while forbidding removal from the store of
    record without a human. So this feeds `pressure` and nothing else;
    `compact` never consults it.

    Comparison is over the token *set*, so word order and repetition are
    ignored, and a pair whose token sets are equal is skipped: those are
    `find_duplicates`' business, and counting them here would advertise a
    review backlog that running `compact` silently clears. The cost is that
    two entries with the same words in a different order are reported by
    neither -- under-reporting, which is the right direction for a signal
    whose whole value is that it is believed.

    Quadratic in the proposals of one (type, scope) bucket. An inbox is tens
    of entries and this runs on already-loaded data, so the simple form is
    kept until there is a store that makes it matter.
    """
    grouped: dict[tuple, list[tuple[str, frozenset[str]]]] = {}
    for entry in entries:
        if entry.get("status") != "proposed":
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        tokens = frozenset(normalize_summary(entry.get("summary")).split())
        if len(tokens) < NEAR_DUPLICATE_MIN_TOKENS:
            continue
        grouped.setdefault((entry.get("type"), entry.get("scope")), []).append(
            (entry_id, tokens)
        )

    flagged: set[str] = set()
    for members in grouped.values():
        for index, (left_id, left) in enumerate(members):
            for right_id, right in members[index + 1:]:
                if left < right or right < left:  # proper subset either way
                    flagged.update((left_id, right_id))
    return flagged


def _age_days(entry: Mapping, now: datetime) -> int | None:
    """Age of `entry` in whole days, or `None` if its timestamp is unreadable.

    A per-entry guard rather than a wrapper around the whole pass: one
    hand-edited timestamp must cost its own entry an age, not silence the
    count for every other entry in the store.
    """
    try:
        stamped = datetime.strptime(str(entry.get("ts")), _TS_FORMAT)
    except (TypeError, ValueError):
        return None
    # Clamped at zero. Clock skew or a hand-edited stamp can put an entry ahead
    # of `now`, and `oldest -3d` reads as a broken tool rather than as a fact
    # about the store -- which costs the line the only thing it has.
    return max(0, (now - stamped.replace(tzinfo=timezone.utc)).days)


def pressure(entries: Sequence[Mapping], now: datetime) -> CompactionPressure:
    """Measure what maintenance would gain over `entries`.

    Deliberately reuses `is_terminal` and `find_duplicates` rather than
    restating what is compactable. A second definition would drift from the
    removal rules the first time those rules changed, and a nudge that
    overstates its payoff is worse than no nudge: the operator runs the
    command expecting twelve entries to clear, three clear, and the line stops
    being believed.

    Total over malformed input rather than wrapped in a catch, which leaves
    nothing to silently swallow. An unrecognised status counts as neither
    terminal nor proposed. An entry whose `ts` is missing or unparseable still
    counts toward `unreviewed` -- it is a real backlog item -- but cannot
    contribute an age.
    """
    # A naive `now` is read as UTC, the same reading `memory_store.format_ts`
    # gives it. Every caller today passes an aware datetime, but subtracting
    # one from an aware stamp raises, and the one input that must never take
    # the context packet down is the clock.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    closed = {
        str(e.get("id") or "")
        for e in entries
        if is_terminal(e)
    }
    # `compact` discards the empty id before removing, so an entry without one
    # survives the pass. Counting it here would promise a removal that the
    # command cannot deliver.
    closed.discard("")

    proposals = [e for e in entries if e.get("status") == "proposed"]
    ages = [age for age in (_age_days(e, now) for e in proposals) if age is not None]

    return CompactionPressure(
        closed_in_inbox=len(closed),
        # Entries that would be *removed*, not duplicate groups: `find_duplicates`
        # keeps one member of each group, so a group of three contributes two.
        # Counting groups would overstate the payoff. Which member it keeps is
        # `find_duplicates`' business and does not move this number.
        duplicate_proposals=len(find_duplicates(entries)),
        unreviewed=len(proposals),
        oldest_unreviewed_days=max(ages) if ages else None,
        near_duplicate_proposals=len(find_near_duplicates(entries)),
    )


def pressure_notes(
    measured: CompactionPressure, scope: str = "project"
) -> tuple[str, ...]:
    """Render `measured` pressure as at most two session-start lines.

    Two sentences, never merged into one, because they ask for different
    actions and only one of them is mechanical. Compaction cannot remove a
    proposal -- an unreviewed proposal is never discarded automatically,
    because the decision to throw away an observation belongs to a human -- so
    a single line reading "6 entries, run compact" would send the operator to a
    command that clears nothing.

    Both notes are silent at zero. A clean store adds nothing to the packet,
    which is what keeps the line credible when it does appear.

    Scope asymmetry: `review` takes `--scope` and can act on machine memory,
    but `compact` operates on `project_memory_dir` only. Machine pressure
    therefore produces the review note alone, and names the scope explicitly
    because the flag defaults to `project`. Emitting a compact note against
    machine pressure would print a command that silently does nothing -- the
    precise failure that trains an operator to ignore the line.
    """
    machine = scope == "machine"
    notes: list[str] = []

    if not machine and (measured.closed_in_inbox or measured.duplicate_proposals):
        clauses = []
        if measured.closed_in_inbox:
            clauses.append(f"{measured.closed_in_inbox} closed")
        if measured.duplicate_proposals:
            count = measured.duplicate_proposals
            clauses.append(f"{count} duplicate proposal{'' if count == 1 else 's'}")
        notes.append(
            f"[memory] {', '.join(clauses)} -- run `agent-harness-memory compact`"
        )

    oldest = measured.oldest_unreviewed_days
    if measured.unreviewed >= UNREVIEWED_COUNT_THRESHOLD or (
        oldest is not None and oldest >= UNREVIEWED_AGE_DAYS
    ):
        subject = "machine proposal" if machine else "proposal"
        clause = f"{measured.unreviewed} {subject}{'' if measured.unreviewed == 1 else 's'} unreviewed"
        if oldest is not None:
            clause += f", oldest {oldest}d"
        command = "agent-harness-memory review" + (" --scope machine" if machine else "")
        notes.append(f"[memory] {clause} -- run `{command}`")

    # Its own line, and it points at `review`. Compaction cannot clear a
    # near-duplicate -- it deliberately does not remove them at all -- so
    # folding this into the compact note would advertise removals the command
    # will not deliver, which is precisely how a nudge stops being read. Fires
    # at one rather than a threshold: unlike a backlog of unreviewed
    # proposals, which is normal in small numbers, a single contained pair is
    # already the thing worth looking at.
    if measured.near_duplicate_proposals:
        command = "agent-harness-memory review" + (" --scope machine" if machine else "")
        notes.append(
            f"[memory] {measured.near_duplicate_proposals} near-duplicate "
            f"proposals -- run `{command}`"
        )

    return tuple(notes)


def promoted_ids(durable_path: Path) -> set[str]:
    """Entry ids already present in `durable.md`.

    Promotion is idempotent by id rather than by content, so re-running
    compaction never appends the same knowledge twice, and a human who edits
    the wording of a promoted line does not cause it to be re-promoted.
    """
    try:
        text = Path(durable_path).read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(_ENTRY_ID_RE.findall(text))


def _entry_line(entry: Mapping) -> str:
    entry_id = entry.get("id", "?")
    kind = entry.get("type", "?")
    summary = str(entry.get("summary") or "").strip()
    return f"- `{entry_id}` {kind}: {summary}"


def render_archive(week: str, entries: Sequence[Mapping], now: datetime) -> str:
    """One compaction run's removals, as an appendable Markdown section."""
    lines = [f"## Compaction {memory_store.format_ts(now)}", ""]
    for entry in sorted(entries, key=lambda e: str(e.get("id") or "")):
        status = entry.get("status", "?")
        ts = entry.get("ts", "?")
        lines.append(
            f"- `{entry.get('id', '?')}` {entry.get('type', '?')}/{status} ({ts}): "
            f"{str(entry.get('summary') or '').strip()}"
        )
        source = entry.get("source")
        if isinstance(source, Mapping) and source:
            detail = ", ".join(f"{k}: {v}" for k, v in sorted(source.items()))
            lines.append(f"  - source: {detail}")
    lines.append("")
    return "\n".join(lines)


def render_active(entries: Sequence[Mapping], week: str, now: datetime) -> tuple[str, int, int]:
    """Rewrite `active.md` from live entries. Returns (text, carried, omitted).

    Budgeting is unit-atomic, matching the context packet: an entry is included
    whole or not at all, and the reader is told how many were dropped rather
    than being handed a silently truncated list. Sections use `##` headings
    because `memory_context` consumes this file through its Markdown section
    parser -- a rewrite that broke that parse would take the packet down.
    """
    work = sorted(
        (e for e in entries if e.get("type") in WORK_TYPES),
        key=lambda e: str(e.get("id") or ""),
    )
    knowledge = sorted(
        (e for e in entries if is_promotable(e)),
        key=lambda e: str(e.get("id") or ""),
    )

    header = f"# Active memory\n\nLast compacted: {memory_store.format_ts(now)} ({week})\n"
    candidates = [("work", e) for e in work] + [("knowledge", e) for e in knowledge]
    allowed = candidates[:ACTIVE_MAX_ENTRIES]

    def assemble(chosen: list[tuple[str, Mapping]], omitted: int) -> str:
        parts = [header]
        for label, heading in (("work", "## Open work"), ("knowledge", "## Recent decisions")):
            section = [_entry_line(e) for tag, e in chosen if tag == label]
            if section:
                parts.append("\n" + heading + "\n\n" + "\n".join(section) + "\n")
        if omitted:
            parts.append(
                f"\n_{omitted} further entries omitted by the active-memory budget._\n"
            )
        return "".join(parts)

    # Drop from the tail until the whole document fits. Recomputed each round
    # because the omission notice itself costs characters.
    chosen = list(allowed)
    while True:
        omitted = len(candidates) - len(chosen)
        text = assemble(chosen, omitted)
        if len(text) <= ACTIVE_MAX_CHARS or not chosen:
            return text, len(chosen), omitted
        chosen.pop()


# --- due-ness -------------------------------------------------------------


def compact_interval_days() -> int:
    """Days between compactions before a project counts as due."""
    try:
        return max(0, int(os.environ[COMPACT_INTERVAL_ENV]))
    except (KeyError, ValueError):
        return COMPACT_INTERVAL_DAYS


def last_compacted(directory: Path) -> datetime | None:
    """When `directory` was last compacted, from `active.md`'s own stamp.

    None when the project has never been compacted (the file is still the
    `init_project` template, or was hand-rewritten without the line) or the
    stamp cannot be parsed. Both read as "never", which errs toward running
    a pass that is safe to repeat rather than skipping one that is owed.
    """
    try:
        text = (Path(directory) / "active.md").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _LAST_COMPACTED_RE.search(text)
    if not match:
        return None
    try:
        stamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class DueStatus:
    """Whether a project is owed a compaction pass, and why."""

    due: bool
    reason: str
    last: datetime | None
    interval_days: int


def compaction_due(directory: Path, now: datetime | None = None) -> DueStatus:
    """Decide whether `directory` is due, from the stamp and the interval.

    Pure file arithmetic: no lock is taken and nothing is written, so this
    is safe to call from `status` on every invocation. A never-compacted
    store is due immediately -- the pass is idempotent and stamps
    `active.md`, so the next call answers "not due" until the interval has
    elapsed again.
    """
    now = now or datetime.now(timezone.utc)
    interval = compact_interval_days()
    last = last_compacted(directory)
    if last is None:
        return DueStatus(True, "never compacted", None, interval)
    age = now - last
    days = age.days
    if age >= timedelta(days=interval):
        return DueStatus(
            True, f"last compacted {days} day(s) ago, interval is {interval}", last, interval
        )
    return DueStatus(
        False, f"compacted {days} day(s) ago, next due in {interval - days} day(s)", last, interval
    )


# --- locking --------------------------------------------------------------


def _lock_is_stale(path: Path, now: datetime) -> bool:
    """Whether an existing lock may be broken.

    An unreadable or malformed lock counts as stale: a corrupt file must not be
    able to wedge compaction permanently, and the failure mode of breaking one
    is a duplicate run that dedupe absorbs.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        held = datetime.fromisoformat(str(data["ts"]).replace("Z", "+00:00"))
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if held.tzinfo is None:
        return True
    return (now - held.astimezone(timezone.utc)).total_seconds() > LOCK_STALE_SECONDS


@contextmanager
def compact_lock(directory: Path, now: datetime | None = None) -> Iterator[Path]:
    """Hold an advisory project-scoped compaction lock.

    This excludes compaction from compaction, which is the case that can
    corrupt state: two passes archiving and splicing the same inbox. It does
    *not* make appenders honour it -- writer-side locking is a separate
    concern tracked on its own, and `append_entries` remains an atomic append
    that a compaction in flight tolerates.
    """
    now = now or datetime.now(timezone.utc)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOCK_NAME

    if path.exists():
        if not _lock_is_stale(path, now):
            raise CompactLockError(
                f"another compaction holds {path}; wait for it to finish or "
                f"remove the file if no compaction is running"
            )
        path.unlink(missing_ok=True)

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:  # lost a race between the check and here
        raise CompactLockError(f"another compaction holds {path}") from exc

    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": memory_store.format_ts(now)}).encode("utf-8"))
    finally:
        os.close(fd)

    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# --- file writes ----------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` via a temp sibling, the shape `write_lines` already uses.

    A plain `write_text` truncates before it writes. For the archive that is a
    real data-loss window rather than a tidiness point: the file accumulates
    *earlier weeks'* sections whose source entries left the inbox in previous
    runs, so they exist nowhere else. A process killed mid-write would take
    them with it, which would contradict both "appended, never replaced" and
    the promise that a crash costs only work the next run redoes.

    `atomic_sibling` supplies the scratch path. A name derived from the
    destination gave every writer of a given archive the same one, so two
    compactions racing the same file would write into each other's temp and
    land a blend of the two -- or fail the replace outright, on a source the
    other had already moved away.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomicwrite.atomic_sibling(path) as tmp:
        tmp.write_text(text, encoding="utf-8")


def _write_archive(path: Path, week: str, entries: Sequence[Mapping], now: datetime) -> None:
    """Append this run's removals to the week's archive.

    Appends rather than replaces: `archive/YYYY-Www.md` is immutable history,
    and a second run in the same ISO week must not erase the first run's record.
    """
    existing = path.read_text(encoding="utf-8") if path.is_file() else f"# Archive {week}\n"
    body = render_archive(week, entries, now)
    _atomic_write(path, existing.rstrip("\n") + "\n\n" + body)


def _write_durable(path: Path, entries: Sequence[Mapping], now: datetime) -> None:
    """Append newly promoted knowledge, preserving everything already there."""
    existing = path.read_text(encoding="utf-8") if path.is_file() else "# Durable project memory\n"
    lines = [f"## Promoted {memory_store.format_ts(now)}", ""]
    lines.extend(_entry_line(e) for e in sorted(entries, key=lambda e: str(e.get("id") or "")))
    lines.append("")
    _atomic_write(path, existing.rstrip("\n") + "\n\n" + "\n".join(lines))


def _write_active(directory: Path, text: str) -> None:
    _atomic_write(Path(directory) / "active.md", text)


def _splice_inbox(
    directory: Path,
    raw_lines: Sequence[str],
    removed_ids: set[str],
    replacements: Mapping[str, Mapping] | None = None,
) -> None:
    """Drop `removed_ids` from the inbox at the *line* level, rewriting the
    few lines named in `replacements`.

    Line-level rather than `rewrite_entries` for two reasons that both still
    hold: a line this schema version cannot parse may be a future schema
    rather than garbage and must be carried across untouched, and
    re-serializing every surviving record would rewrite entries this pass has
    no business modifying.

    `replacements` is the narrow exception -- survivors of a duplicate merge,
    and nothing else. It is a named map rather than a blanket re-serialize
    precisely so the second reason keeps its force: an entry not in the map is
    still copied byte for byte. `memory_review` already rewrites a line in
    place when it transitions a status, so rewriting is not foreign to the
    store; what was foreign is *compaction* doing it, which is why the set is
    explicit and the count is reported.
    """
    replacements = replacements or {}
    kept: list[str] = []
    for line in raw_lines:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            kept.append(line)
            continue
        if isinstance(record, dict):
            record_id = str(record.get("id") or "")
            if record_id in removed_ids:
                continue
            if record_id in replacements:
                kept.append(json.dumps(replacements[record_id], ensure_ascii=False))
                continue
        kept.append(line)
    memory_store.write_lines(kept, directory)


def _snapshot(paths: Iterable[Path]) -> dict[Path, bytes | None]:
    """Capture file contents for rollback; `None` means the file did not exist."""
    snapshot: dict[Path, bytes | None] = {}
    for path in paths:
        try:
            snapshot[path] = path.read_bytes() if path.is_file() else None
        except OSError:
            snapshot[path] = None
    return snapshot


def _restore(snapshot: Mapping[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        except OSError:
            # Best effort: one unrestorable file must not mask the original
            # error, which is what the caller actually needs to see.
            continue


# --- the pass ------------------------------------------------------------


def compact(project_root: Path, now: datetime | None = None) -> CompactResult:
    """Run one deterministic compaction pass over `project_root`.

    Raises `CompactLockError` if another compaction holds the lock, and
    re-raises any write failure after restoring the derived files.
    """
    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    directory = memory_store.project_memory_dir(root)
    week = week_stamp(now)
    archive_path = directory / "archive" / f"{week}.md"
    durable_path = directory / "durable.md"
    active_path = directory / "active.md"

    with compact_lock(directory, now=now):
        raw_lines = memory_store.read_lines(directory)
        entries = list(memory_store.iter_entries(directory))

        duplicates, merged_survivors = resolve_duplicates(entries)
        removed_ids = {str(e.get("id") or "") for e in entries if is_terminal(e)} | duplicates
        removed_ids.discard("")
        removed = [e for e in entries if str(e.get("id") or "") in removed_ids]
        # Survivors carry their merged form onward, so `active.md` and the
        # promotion pass describe the record the inbox will actually hold.
        kept = [
            merged_survivors.get(str(e.get("id") or ""), e)
            for e in entries
            if str(e.get("id") or "") not in removed_ids
        ]

        already = promoted_ids(durable_path)
        to_promote = [e for e in kept if is_promotable(e) and str(e.get("id") or "") not in already]

        snapshot = _snapshot((archive_path, durable_path, active_path, directory / "inbox.jsonl"))
        try:
            if removed:
                _write_archive(archive_path, week, removed, now)
            if to_promote:
                _write_durable(durable_path, to_promote, now)
            text, carried, omitted = render_active(kept, week, now)
            _write_active(directory, text)
            # Last, deliberately: see the module docstring on write ordering.
            if removed_ids:
                _splice_inbox(directory, raw_lines, removed_ids, merged_survivors)
        except BaseException:
            _restore(snapshot)
            raise

    return CompactResult(
        week=week,
        archived=len(removed),
        duplicates=len(duplicates),
        merged=len(merged_survivors),
        promoted=len(to_promote),
        carried=carried,
        omitted=omitted,
        archive_path=archive_path,
        # `update_index` deletes ids that are gone, but only when it is re-run.
        # Until then the index keeps serving archived entries, so a removal has
        # to be reported as making the index stale.
        index_stale=bool(removed_ids),
    )
