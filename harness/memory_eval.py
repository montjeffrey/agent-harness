"""Scoring retrieval quality: did selection put the right entries in reach?

Every later increment of the routing work -- semantic partitions, tiered
expansion, cross-project routing, a model-backed classifier -- is a claim that
retrieval got better. This module is what makes such a claim checkable instead
of plausible. It is built first, deliberately, so no change after it has to be
argued from intuition.

It is a pure observer. It calls `memory_context.deterministic_selector` and
`memory_index.semantic_selector` through the same `Selector` contract the
packet builder uses, and it never writes memory, never mutates an entry, and
never opens a network socket -- `memory_store`'s "nothing here reaches the
network" rule holds unchanged. Its only writes go to the gitignored working
store (`harness.work_store`).

A case is one labelled retrieval question::

    {"name": "auth-rewrite",
     "query": "how did we redo login",
     "relevant": ["project:mem-2026-08-12-001", "machine:mem-2026-08-11-004"]}

`relevant` holds `memory_index.index_key` values -- ``scope:id``, not bare
entry ids. Ids are unique per store, not globally: the project and machine
inboxes routinely mint the same ``mem-YYYY-MM-DD-001`` on the same day, so a
case keyed on the bare id would score a machine hit as a project hit and
quietly inflate precision.

Three metrics, all at the requested `limit` (`@k`):

* **precision** -- of what was returned, how much was wanted. Falls when
  irrelevant entries win budget that relevant ones needed.
* **recall** -- of what was wanted, how much was returned. Falls when the
  right entry exists but ranks below the cut.
* **reciprocal rank** -- 1/(position of the first relevant hit). Falls when
  the right entry is returned but buried, which costs an agent real budget
  even though precision and recall look fine.

Plus **elapsed_ms**, the wall-clock cost of deciding. Selection quality bought
with an unacceptable latency is not an improvement, and the packet's hard
`DEFAULT_MAX_CHARS` ceiling means better ranking is precisely a
precision-and-cost win, never a volume one.

Unlike `memory_store.iter_entries`, which skips malformed lines so one bad
record cannot block a whole store, a malformed *case* raises. A store that
silently drops a line still works; a benchmark that silently drops a case
reports a score for a question it never asked.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness import memory_context, memory_index, memory_store, work_store

EVAL_DIRNAME = "eval"
RESULTS_DIRNAME = "results"
DEFAULT_CASES_NAME = "default.jsonl"

# Matches `memory_context.CANDIDATE_LIMIT`, the number of entries a selector is
# actually asked for per section in production. Scoring at a different depth
# would measure a cut the packet builder never makes.
DEFAULT_LIMIT = memory_context.CANDIDATE_LIMIT

STRATEGIES: tuple[str, ...] = ("deterministic", "semantic")

# A case name is a report heading, not data. Long queries used as default
# names are cut here so one verbose case cannot wreck the Markdown table.
MAX_CASE_NAME_CHARS = 80


class EvalCaseError(ValueError):
    """A case file line that cannot be scored. Raised, never skipped."""


@dataclass(frozen=True)
class EvalCase:
    """One labelled retrieval question."""

    name: str
    query: str
    relevant: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"name": self.name, "query": self.query, "relevant": list(self.relevant)}


@dataclass(frozen=True)
class StrategyResult:
    """How one strategy answered one case."""

    strategy: str
    selected: tuple[str, ...]
    hits: tuple[str, ...]
    precision: float
    recall: float
    reciprocal_rank: float
    elapsed_ms: float

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "selected": list(self.selected),
            "hits": list(self.hits),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "reciprocal_rank": round(self.reciprocal_rank, 4),
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


@dataclass(frozen=True)
class CaseResult:
    """One case, scored by every strategy that could run."""

    case: EvalCase
    results: tuple[StrategyResult, ...]

    def as_dict(self) -> dict:
        return {**self.case.as_dict(), "results": [r.as_dict() for r in self.results]}


@dataclass(frozen=True)
class EvalReport:
    """A whole scored run: what was asked, what ranked, and what it cost."""

    project_id: str
    project_root: str
    generated_at: str
    limit: int
    candidates: int
    cases: tuple[CaseResult, ...] = ()
    notes: tuple[str, ...] = ()

    def aggregate(self) -> dict[str, dict[str, float]]:
        """Mean of each metric per strategy, over the cases that strategy ran.

        A strategy absent from a case (semantic with no index, say) is absent
        from its own mean rather than counted as zero -- averaging in a score
        that was never measured is how an unavailable backend comes to look
        like a bad one.
        """
        buckets: dict[str, list[StrategyResult]] = {}
        for case in self.cases:
            for result in case.results:
                buckets.setdefault(result.strategy, []).append(result)
        return {
            strategy: {
                "cases": float(len(results)),
                "precision": _mean(r.precision for r in results),
                "recall": _mean(r.recall for r in results),
                "reciprocal_rank": _mean(r.reciprocal_rank for r in results),
                "elapsed_ms": _mean(r.elapsed_ms for r in results),
            }
            for strategy, results in buckets.items()
        }


def _mean(values: Iterable[float]) -> float:
    collected = list(values)
    return sum(collected) / len(collected) if collected else 0.0


# --- case files -----------------------------------------------------------


def default_cases_path(project_root: Path) -> Path:
    """Where `eval` looks for cases when none is named."""
    return work_store.work_path(project_root, EVAL_DIRNAME, DEFAULT_CASES_NAME)


def report_path(project_root: Path, report: EvalReport) -> Path:
    """Where a saved report belongs, stamped from the report's own clock.

    Derived from `report.generated_at` rather than a fresh `now()`: reading
    the clock twice would let a report's filename and its contents disagree
    about when it was produced, which is exactly the kind of small
    inconsistency that makes a stored baseline untrustworthy later.

    Colons are stripped from the stamp: `format_ts` emits ISO 8601 with them,
    and they are illegal in Windows filenames.
    """
    stamp = report.generated_at.replace(":", "")
    return work_store.work_path(
        project_root, EVAL_DIRNAME, RESULTS_DIRNAME, f"{stamp}.json"
    )


def parse_case(payload: object, where: str) -> EvalCase:
    """Validate one decoded case object, naming `where` it came from."""
    if not isinstance(payload, dict):
        raise EvalCaseError(
            f"{where}: expected a JSON object, got {type(payload).__name__}"
        )

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise EvalCaseError(f"{where}: 'query' must be a non-empty string")

    relevant = payload.get("relevant")
    if not isinstance(relevant, list) or not relevant:
        raise EvalCaseError(
            f"{where}: 'relevant' must be a non-empty list of index keys"
        )
    for key in relevant:
        if not isinstance(key, str) or ":" not in key:
            raise EvalCaseError(
                f"{where}: relevant entry {key!r} is not a 'scope:id' index key "
                f"(bare ids collide across the project and machine stores)"
            )

    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise EvalCaseError(f"{where}: 'name' must be a string when present")

    return EvalCase(
        name=str(name or query.strip())[:MAX_CASE_NAME_CHARS],
        query=query.strip(),
        relevant=tuple(dict.fromkeys(relevant)),
    )


def load_cases(path: Path) -> list[EvalCase]:
    """Read a JSONL case file. Blank lines and ``#`` comments are allowed."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no eval case file at {path}")

    cases: list[EvalCase] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path.name}:{number}"
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalCaseError(f"{where}: invalid JSON ({exc.msg})") from exc
        cases.append(parse_case(payload, where))

    if not cases:
        raise EvalCaseError(f"{path}: no cases found")
    return cases


def write_cases(path: Path, cases: Sequence[EvalCase]) -> Path:
    """Write cases back as JSONL, one object per line."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(c.as_dict(), ensure_ascii=False) + "\n" for c in cases)
    path.write_text(body, encoding="utf-8")
    return path


# --- scoring --------------------------------------------------------------


def candidate_pool(project_root: Path) -> list[dict]:
    """The entries selection ranks for the two knowledge sections.

    Drawn from `memory_context.durable_pools` rather than rebuilt here, so the
    pool being scored is the pool a packet is built from. Relationships are
    left out: that section is accepted-only and tiny, and folding it in would
    let a change in link count move a score meant to track knowledge
    retrieval.
    """
    root = Path(project_root)
    pools = memory_context.durable_pools(
        list(memory_store.iter_entries(memory_store.project_memory_dir(root))),
        list(memory_store.iter_entries(memory_store.machine_memory_dir())),
    )
    return pools["project_durable"] + pools["machine_durable"]


def score(
    selected_keys: Sequence[str], relevant: Sequence[str]
) -> tuple[float, float, float]:
    """Return `(precision, recall, reciprocal_rank)` for one ranked answer.

    Precision divides by how many entries were actually returned, not by the
    requested `limit`: a store holding fewer entries than the limit would
    otherwise be scored as if selection had failed to fill a quota it was
    never able to fill.
    """
    wanted = set(relevant)
    hits = [i for i, key in enumerate(selected_keys) if key in wanted]
    precision = len(hits) / len(selected_keys) if selected_keys else 0.0
    recall = len(hits) / len(wanted) if wanted else 0.0
    reciprocal = 1.0 / (hits[0] + 1) if hits else 0.0
    return precision, recall, reciprocal


def run_case(
    case: EvalCase,
    candidates: Sequence[dict],
    strategy: str,
    selector: memory_context.Selector,
    limit: int,
) -> StrategyResult:
    """Time one selector on one case and score what it returned."""
    started = time.perf_counter()
    selected = selector(candidates, case.query, limit)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    keys = tuple(memory_index.index_key(entry) for entry in selected)
    precision, recall, reciprocal = score(keys, case.relevant)
    wanted = set(case.relevant)
    return StrategyResult(
        strategy=strategy,
        selected=keys,
        hits=tuple(k for k in keys if k in wanted),
        precision=precision,
        recall=recall,
        reciprocal_rank=reciprocal,
        elapsed_ms=elapsed_ms,
    )


def resolve_strategies(
    project_id: str, names: Sequence[str]
) -> tuple[list[tuple[str, memory_context.Selector]], list[str]]:
    """Resolve each named strategy, reporting the ones that cannot run.

    Each name is requested *explicitly* -- never through ``auto`` -- because
    `resolve_selector`'s own contract is that an explicit strategy raises
    rather than substituting another. A benchmark that silently ran
    deterministic selection under a `semantic` heading would be worse than no
    benchmark at all.
    """
    resolved: list[tuple[str, memory_context.Selector]] = []
    notes: list[str] = []
    for name in names:
        try:
            selector, actual, selector_notes = memory_index.resolve_selector(
                project_id, name
            )
        except FileNotFoundError as exc:
            notes.append(f"{name}: unavailable ({exc})")
            continue
        resolved.append((actual, selector))
        notes.extend(f"{actual}: {note}" for note in selector_notes)
    return resolved, notes


def evaluate(
    project_root: Path,
    cases: Sequence[EvalCase],
    limit: int = DEFAULT_LIMIT,
    strategies: Sequence[str] = STRATEGIES,
    now: datetime | None = None,
) -> EvalReport:
    """Score every case against every available strategy.

    A non-positive `limit` is refused rather than passed through: selectors
    slice with ``[:limit]``, so ``-1`` would silently drop the *last* ranked
    entry and report a plausible-looking score for a cut nobody asked for.
    """
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")

    now = now or datetime.now(timezone.utc)
    root = Path(project_root)
    project_id = memory_store.project_id_for(root)

    candidates = candidate_pool(root)
    resolved, notes = resolve_strategies(project_id, strategies)
    if not resolved:
        notes.append("no strategy could be resolved; nothing was scored")
    if not candidates:
        notes.append("no durable entries to rank; every score is zero by construction")

    results = tuple(
        CaseResult(
            case=case,
            results=tuple(
                run_case(case, candidates, name, selector, limit)
                for name, selector in resolved
            ),
        )
        for case in cases
    )

    return EvalReport(
        project_id=project_id,
        project_root=str(root),
        generated_at=memory_store.format_ts(now),
        limit=limit,
        candidates=len(candidates),
        cases=results,
        notes=tuple(notes),
    )


# --- rendering ------------------------------------------------------------


def render_markdown(report: EvalReport) -> str:
    """The report as Markdown: aggregate first, then per-case detail."""
    out: list[str] = [
        f"# Retrieval eval: {report.project_id}",
        "",
        f"_Generated {report.generated_at} - {len(report.cases)} cases - "
        f"{report.candidates} candidates - limit {report.limit}_",
    ]
    for note in report.notes:
        out.append(f"_Note: {note}_")
    out.append("")

    aggregate = report.aggregate()
    if aggregate:
        out.append("## Aggregate")
        out.append("")
        out.append("| strategy | cases | precision@k | recall@k | MRR | ms |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for strategy, metrics in aggregate.items():
            out.append(
                f"| {strategy} | {int(metrics['cases'])} | "
                f"{metrics['precision']:.3f} | {metrics['recall']:.3f} | "
                f"{metrics['reciprocal_rank']:.3f} | {metrics['elapsed_ms']:.1f} |"
            )
        out.append("")

    out.append("## Cases")
    out.append("")
    for case in report.cases:
        out.append(f"### {case.case.name}")
        out.append("")
        out.append(f"query: `{case.case.query}`")
        out.append(f"wanted: {', '.join(f'`{k}`' for k in case.case.relevant)}")
        if not case.results:
            out.append("_no strategy available_")
        for result in case.results:
            found = ", ".join(f"`{k}`" for k in result.hits) or "none"
            out.append(
                f"- **{result.strategy}** precision {result.precision:.3f} "
                f"recall {result.recall:.3f} MRR {result.reciprocal_rank:.3f} "
                f"({result.elapsed_ms:.1f} ms) -- found: {found}"
            )
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_json(report: EvalReport) -> str:
    """The report as JSON -- the machine-readable half of the same contract."""
    payload = {
        "schema": memory_store.SCHEMA_VERSION,
        "project_id": report.project_id,
        "project_root": report.project_root,
        "generated_at": report.generated_at,
        "limit": report.limit,
        "candidates": report.candidates,
        "notes": list(report.notes),
        "aggregate": report.aggregate(),
        "cases": [c.as_dict() for c in report.cases],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
