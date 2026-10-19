"""Family D: decision recall on landfirst.

``setup`` commits a synthetic but realistic design history (``corpus_adr/``,
30 ADRs) as ``docs/adr/``. Eight of the records hold load-bearing decisions
whose specifics (a measured number, a rejected alternative, a flag name) are
not in the Go code or the existing briefs; two of those are supersessions
(0007 -> 0019, 0018 -> 0022), where the right answer needs the later record.
The other 22 records are plausible noise on nearby topics.

Each task asks four questions and is scored from four ``ANSWER n:`` lines:
every required token group must appear in its own answer line
(case-insensitive; numbers matched as whole numbers, with word forms allowed).

Decision map (ADR -> answer tokens):

  D-recall-1
    Q1  0007 -> 0019  staleness threshold: original 15 min, measured median 38 min
    Q2  0023          debounce: max intra-burst gap 90 ms, rejected 500 ms
    Q3  0004          TUI: prototyped on gocui and rejected
    Q4  0017          max-pairs: largest same-fork group 17, rejected -probe-timeout
  D-recall-2
    Q1  0018 -> 0022  watcher: old poll every 5 s, rejected rjeczalik/notify (cgo)
    Q2  0013          palette: tried 8 first, Solarized ruled out 16
    Q3  0006          freshness: rejected ls-remote and reflog
    Q4  0011          concurrency: did not carry over p-limit with 6
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

CORPUS = Path(__file__).parent / "corpus_adr"
ADR_DIR = "docs/adr"


def setup(clone: Path) -> None:
    dest = clone / ADR_DIR
    dest.mkdir(parents=True, exist_ok=True)
    files = sorted(CORPUS.glob("*.md"))
    if len(files) != 30:
        raise RuntimeError(f"expected 30 ADRs in {CORPUS}, found {len(files)}")
    for f in files:
        # Normalise to LF so the committed bytes do not depend on the checkout.
        text = f.read_text(encoding="utf-8").replace("\r\n", "\n")
        (dest / f.name).write_text(text, encoding="utf-8", newline="\n")


# --- answer parsing and matching -------------------------------------------

_ANSWER_RE = re.compile(r"^ANSWER\s*([1-4])[\s*_`]*[:.)\-]\s*(.*)$", re.IGNORECASE)


def answers(text: str) -> dict[int, str]:
    """``{n: text}`` from the last ``ANSWER n:`` line for each n in 1..4.

    Leading markdown decoration (``**``, backticks, ``#``, ``-``, ``>``) is ignored.
    """
    out: dict[int, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("*`#->_ \t")
        m = _ANSWER_RE.match(stripped)
        if m:
            out[int(m.group(1))] = m.group(2).strip().strip("*`").strip()
    return out


def num(n: str, *words: str) -> list[str]:
    """Alternatives (regexes) for a whole number: ``15`` but not ``150``/``1.5``."""
    pats = [rf"(?<![\d.]){re.escape(n)}(?!\.?\d)"]
    pats += [rf"\b{re.escape(w)}\b" for w in words]
    return pats


def lit(*alts: str) -> list[str]:
    """Alternatives (regexes) for case-insensitive substrings."""
    return [re.escape(a) for a in alts]


def _matches(answer: str, alternatives: list[str]) -> bool:
    return any(re.search(p, answer, re.IGNORECASE) for p in alternatives)


def make_check(required: dict[int, list[tuple[str, list[str]]]]):
    """``required`` maps question number -> [(label, alternatives), ...]."""

    def check(clone: Path, result_text: str) -> tuple[bool, str]:
        got = answers(result_text)
        problems = []
        for q, groups in sorted(required.items()):
            ans = got.get(q, "")
            if not ans:
                problems.append(f"Q{q}: no ANSWER {q}: line")
                continue
            missing = [label for label, alts in groups if not _matches(ans, alts)]
            if missing:
                problems.append(f"Q{q}: missing {', '.join(missing)} in {ans[:120]!r}")
        if problems:
            return False, "; ".join(problems)
        return True, "all 4 answers contain their required tokens"

    return check


def make_reference(answer_lines: list[str]):
    def reference(clone: Path) -> str:
        adrs = sorted((clone / ADR_DIR).glob("*.md"))
        if len(adrs) != 30:
            raise RuntimeError(f"setup did not install the ADRs: {len(adrs)} in {ADR_DIR}")
        body = "From the decision records in docs/adr (checking supersessions).\n\n"
        return body + "\n".join(f"ANSWER {i}: {a}" for i, a in enumerate(answer_lines, 1))

    return reference


_FOOTER = """
Do not change any files. Answer from the project's recorded design decisions,
not from guesses. End your reply with exactly four lines, in this format and
with nothing after them:
"""

# --- D-recall-1 ---------------------------------------------------------------

PROMPT_1 = """\
I'm about to revisit a few of landfirst's tuning choices and want the reasoning
that's on record before I touch anything:

1. The per-unit staleness check used to kick in much sooner than it does now.
   What fetch-age threshold did we originally use, and what median interval
   between fetches did we measure that justified raising it?
2. Why is the file-watcher debounce 150ms? Give the largest gap between
   consecutive events inside a single git operation that we measured, and the
   longer debounce window we rejected.
3. Before we settled on Bubble Tea, which TUI library did we actually build a
   prototype panel on and then reject?
4. The -max-pairs default of 200 was sized from a measurement: how many
   branches were in the largest same-fork group we measured, and what was the
   flag name of the alternative kind of limit we rejected?
""" + _FOOTER + """\
ANSWER 1: <original threshold>, <measured median fetch interval>
ANSWER 2: <largest measured gap>, <rejected debounce window>
ANSWER 3: <library>
ANSWER 4: <branch count>, <rejected flag>
"""

REQUIRED_1 = {
    1: [("original threshold 15 min", num("15", "fifteen")),
        ("median fetch interval 38 min", num("38", "thirty-eight"))],
    2: [("measured gap 90 ms", num("90", "ninety")),
        ("rejected 500 ms", num("500") + num("0.5") + lit("half a second"))],
    3: [("gocui", lit("gocui"))],
    4: [("largest same-fork group 17", num("17", "seventeen")),
        ("rejected -probe-timeout", lit("probe-timeout", "probe_timeout",
                                         "probetimeout", "probe timeout"))],
}

ANSWERS_1 = [
    "15 minutes (ADR 0007), raised to 1 hour after measuring a 38-minute median fetch interval (ADR 0019)",
    "90 ms largest intra-burst gap (rebase on Windows); 500 ms (the Node watcher's value) rejected",
    "gocui (jroimartin/gocui, the library lazygit uses)",
    "17 branches (136 pairs); rejected a wall-clock budget flag -probe-timeout",
]

# --- D-recall-2 ---------------------------------------------------------------

PROMPT_2 = """\
A few questions about why landfirst is built the way it is - I need what was
actually decided, including anything that was later replaced:

1. Before the fsnotify watcher, how often did the panel poll refs for live
   updates? And when we moved to fsnotify, which other file-watching library
   was rejected, and for what reason?
2. Why does the graph's lane palette have 12 colours? Name the smaller palette
   size we tried first, and the terminal colour scheme that ruled out going to
   16.
3. Repository freshness comes from FETCH_HEAD's modification time. Which two
   other freshness signals did we consider and reject?
4. Git subprocesses are bounded at runtime.NumCPU(). What fixed concurrency
   limit did we decide not to carry over from the Node version, and which npm
   package provided it there?
""" + _FOOTER + """\
ANSWER 1: <original poll interval>, <rejected library>, <reason>
ANSWER 2: <first palette size>, <colour scheme>
ANSWER 3: <signal one>, <signal two>
ANSWER 4: <fixed limit>, <npm package>
"""

REQUIRED_2 = {
    1: [("original poll every 5 s", num("5", "five")),
        ("rejected rjeczalik/notify", lit("rjeczalik")),
        ("reason: cgo", lit("cgo"))],
    2: [("first tried 8", num("8", "eight")),
        ("Solarized", lit("solarized", "solarised"))],
    3: [("git ls-remote", lit("ls-remote", "ls remote", "lsremote", "ls_remote")),
        ("reflog", lit("reflog"))],
    4: [("fixed limit 6", num("6", "six")),
        ("p-limit", lit("p-limit", "p_limit", "plimit"))],
}

ANSWERS_2 = [
    "every 5 seconds (ADR 0018); rjeczalik/notify rejected because its macOS FSEvents backend needs cgo, breaking CGO_ENABLED=0 (ADR 0022)",
    "8 colours first; Solarized Dark made pairs indistinguishable beyond 12",
    "git ls-remote (network, credential prompts), the reflog of the remote-tracking base ref",
    "6, from the p-limit npm package",
]

TASKS = {
    "D-recall-1": dict(
        family="D", repo="landfirst", setup=setup, prompt=PROMPT_1,
        check=make_check(REQUIRED_1), reference=make_reference(ANSWERS_1),
    ),
    "D-recall-2": dict(
        family="D", repo="landfirst", setup=setup, prompt=PROMPT_2,
        check=make_check(REQUIRED_2), reference=make_reference(ANSWERS_2),
    ),
}
