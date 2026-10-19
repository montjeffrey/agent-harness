"""Automatic session hand-off: capture at session end, inject at session start.

The harness-value benchmark (2026-10-15/16) found that agents never call
`memory_remember` on their own -- not as installed, and not with the tools
loaded eagerly and a workflow prompt asking them to. Memory that depends on
the agent's discretion stays empty, so a later session in the same project
starts cold however good retrieval is. This module removes the discretion
from both ends:

- **Capture** (`capture`, run by the Stop / SessionEnd hook): read the
  session's own evidence -- the transcript's tool calls, the files that
  changed in the working tree since the session started, and the final
  assistant message -- and propose a small group of entries describing what
  the session did and what it left open. No model call: every line is
  extracted by fixed rules, so a reviewer can predict what will be written.
- **Retrieval** (`render_block`, called by the SessionStart hook): print the
  most recent hand-offs for the project, newest first, inside a fixed
  character budget, instead of waiting for the agent to ask.

What a hand-off group holds (each entry within the store's 500-char summary
limit, all tagged ``handoff`` and ``session:<id8>``):

1. ``work``      -- files changed since session start with line counts, the
                    build/test commands run, and tool-call counts.
2. ``findings``  -- ``ANSWER:`` lines from the final message and a clipped
                    excerpt of ANSWER.md when this session changed it.
3. ``decisions`` -- sentences of the final message that state a choice
                    ("instead of", "because", "decided", ...).
4. ``open``      -- questions and not-done items from the final message.
5. ``outcome``   -- the final message itself, flattened and clipped.

Rules it lives by:

- Entries land as ``proposed`` / ``inferred`` like every agent-side write;
  nothing here can make itself authoritative.
- The user's prompts are never recorded -- only what the agent did and said.
- Any line that trips the store's secret patterns is dropped before the
  entry is built, so one credential-shaped string cannot sink the group.
- One group per session. ``Stop`` fires after every turn, so a newer capture
  replaces the session's earlier group while it is still unreviewed, and an
  unchanged session (same fingerprint) writes nothing.
- Every git call passes ``stdin=DEVNULL`` (see `harness.work._git`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness import memory_store, work

HANDOFF_TAG = "handoff"
SESSION_TAG_PREFIX = "session:"
PART_TAGS = ("work", "findings", "decisions", "open", "outcome")

HANDOFF_MAX_CHARS = 1500
MAX_CHARS_ENV = "AGENT_HARNESS_HANDOFF_MAX_CHARS"
MAX_SESSIONS_SHOWN = 3

# A session that changed no file and made fewer tool calls than this left
# nothing a later session needs -- a quick question, a greeting.
MIN_TOOL_CALLS = 3

FILES_IN_WORK_ENTRY = 12
ANSWER_FILE = "ANSWER.md"
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})

_GIT_TIMEOUT_S = 30

# Commands worth naming in a hand-off: they say how the work was verified.
_COMMAND_RE = re.compile(
    r"\b(go (?:build|vet|test)|pytest|python -m pytest|npm (?:run )?test|npm run build|"
    r"cargo (?:build|test|check)|tsc|ruff|mypy)\b"
)
_DECISION_RE = re.compile(
    r"\b(decided|decision|chose|choose|went with|opted|instead of|rather than|because|"
    r"so that|trade-?off|deliberately|kept|must not|no longer)\b",
    re.IGNORECASE,
)
_OPEN_RE = re.compile(
    r"\b(todo|not (?:yet )?(?:done|implemented|verified|confirmed|fixed|handled|addressed)|"
    r"haven't|have not|didn't|did not|couldn't|could not|unverified|remaining|remains|"
    r"left for|next session|follow-?ups?|out of scope|another session|still (?:need|fails?|open))\b",
    re.IGNORECASE,
)
_OPEN_HEADING_RE = re.compile(
    r"^(open questions?|follow-?ups?|next steps?|not done|remaining|todo|caveats?|known issues?|"
    r"left to do|still open)\b",
    re.IGNORECASE,
)
_ANSWER_LINE_RE = re.compile(r"^\s*[*_`]*ANSWER\b[^:]{0,6}:", re.IGNORECASE)


# --------------------------------------------------------------- transcript


@dataclass
class Transcript:
    """What the session did, as recorded in its JSONL transcript."""

    final_message: str = ""
    edited_files: list[str] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)

    @property
    def tool_calls(self) -> int:
        return sum(self.tool_counts.values())


def read_transcript(path: Path | None) -> Transcript:
    """Parse a Claude Code transcript; a missing or unreadable file is empty.

    One API message is split over several lines (one content block each)
    sharing ``message.id``, so the final message is every text block of the
    last assistant message id that carried text. Sidechain (subagent) lines
    count toward tool use but never supply the final message.
    """
    out = Transcript()
    if path is None or not Path(path).is_file():
        return out
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    counts: dict[str, int] = {}
    edited: dict[str, None] = {}
    commands: dict[str, None] = {}
    last_id: str | None = None
    last_text: list[str] = []
    for line in lines:
        if '"assistant"' not in line:
            continue  # user turns and tool results: nothing here reads them
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "?")
                counts[name] = counts.get(name, 0) + 1
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name in EDIT_TOOLS:
                    target = args.get("file_path") or args.get("notebook_path")
                    if isinstance(target, str) and target:
                        edited[target] = None
                elif name in ("Bash", "PowerShell"):
                    for match in _COMMAND_RE.finditer(str(args.get("command") or "")):
                        commands[match.group(1)] = None
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if texts and not rec.get("isSidechain"):
            mid = msg.get("id")
            if mid is not None and mid == last_id:
                last_text.extend(texts)
            else:
                last_id, last_text = mid, list(texts)
    out.final_message = "\n".join(t.strip() for t in last_text if t.strip())
    out.edited_files = list(edited)
    out.tool_counts = counts
    out.commands = list(commands)
    return out


# --------------------------------------------------------------- git snapshot


def _git(root: Path, *args: str, env: dict | None = None) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def snapshot_tree(root: Path) -> str | None:
    """A tree object of the whole working tree, untracked files included.

    Built in a throwaway index seeded from the real one (so unchanged files
    cost a stat, not a hash): neither the index nor the working tree is
    touched, and the only write is git objects. Ignored files -- including
    ``.agent-harness/`` -- stay out, as they would from a commit.
    """
    real = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "index")
    fd, tmp = tempfile.mkstemp(prefix="ah-handoff-", suffix=".index")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        if real and Path(real.strip()).is_file():
            tmp_path.write_bytes(Path(real.strip()).read_bytes())
        else:
            tmp_path.unlink()  # git refuses an empty file as an index
        env = dict(os.environ, GIT_INDEX_FILE=str(tmp_path))
        if _git(root, "add", "-A", "--", ".", env=env) is None:
            return None
        tree = _git(root, "write-tree", env=env)
        return tree.strip() if tree else None
    finally:
        tmp_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int | None  # None for a binary file, or when only the transcript knows
    removed: int | None
    status: str  # "A" added, "D" deleted, "M" modified


def tree_changes(root: Path, start_tree: str, end_tree: str) -> list[FileChange] | None:
    """Per-file changes between two snapshot trees, or None if git failed."""
    if start_tree == end_tree:
        return []
    numstat = _git(root, "diff", "--numstat", "--no-renames", start_tree, end_tree)
    names = _git(root, "diff", "--name-status", "--no-renames", start_tree, end_tree)
    if numstat is None or names is None:
        return None
    status = {}
    for line in names.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status[parts[1]] = parts[0][:1]
    out = []
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, path = parts
        out.append(
            FileChange(
                path=path,
                added=int(a) if a.isdigit() else None,
                removed=int(d) if d.isdigit() else None,
                status=status.get(path, "M"),
            )
        )
    return out


# --------------------------------------------------------------- session state


def _state_path(repo: work.Repository, session_id: str) -> Path:
    # Beside the progress hook's marker, in a file of its own: both hooks
    # run in parallel on Stop, and a shared read-modify-write would race.
    marker = work.session_marker_path(repo, session_id)
    return marker.with_name(marker.stem + ".handoff.json")


def load_state(repo: work.Repository, session_id: str) -> dict:
    try:
        data = json.loads(_state_path(repo, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(repo: work.Repository, session_id: str, state: dict) -> Path:
    path = _state_path(repo, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def record_start(project_root: Path, session_id: str, now: datetime | None = None) -> dict | None:
    """Snapshot the working tree at session start (once per session).

    A resumed session keeps its first snapshot, so its hand-off still covers
    everything it changed. Returns the state written, or None.
    """
    if not memory_store.project_memory_dir(project_root).is_dir():
        return None
    repo = work.repository_for(project_root)
    if load_state(repo, session_id).get("start_tree"):
        return None
    tree = snapshot_tree(repo.root)
    if not tree:
        return None
    state = {
        "session_id": session_id,
        "start_tree": tree,
        "start_head": repo.head,
        "started_at": memory_store.format_ts(now or datetime.now(timezone.utc)),
    }
    save_state(repo, session_id, state)
    return state


# --------------------------------------------------------------- extraction


def _flatten(text: str) -> str:
    return " ".join(str(text).split())


def _clip(text: str, limit: int) -> str:
    text = _flatten(text)
    return text if len(text) <= limit else text[: max(limit - 3, 0)].rstrip() + "..."


def _strip_markdown(line: str) -> str:
    line = re.sub(r"^\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", line)
    return line.replace("**", "").replace("__", "").strip()


def _clean(text: str) -> str:
    """Drop code fences and secret-shaped lines; keep the prose."""
    keep, fenced = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or memory_store._find_secret(line):
            continue
        keep.append(line)
    return "\n".join(keep)


def _sentences(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = _strip_markdown(raw)
        if line:
            out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`(])", line) if s.strip())
    return out


def answer_lines(message: str) -> list[str]:
    return [_flatten(l).strip("*_`") for l in message.splitlines() if _ANSWER_LINE_RE.match(l)]


def decisions(message: str, limit: int = 3) -> list[str]:
    found: list[str] = []
    for s in _sentences(message):
        if s.endswith("?") or _ANSWER_LINE_RE.match(s):
            continue
        if _DECISION_RE.search(s) and s not in found:
            found.append(s)
    return found[:limit]


def open_items(message: str, limit: int = 4) -> list[str]:
    """Questions and not-done items, including every line under an
    "Open questions" / "Follow-ups" / "Next steps" style heading."""
    found: list[str] = []
    under_heading = False
    for raw in message.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        plain = _strip_markdown(stripped).rstrip(":")
        if stripped.startswith("#") or (stripped.endswith(":") and len(plain) < 40):
            under_heading = bool(_OPEN_HEADING_RE.match(plain))
            continue
        if _ANSWER_LINE_RE.match(stripped):
            continue  # a result, carried by the findings entry
        for s in _sentences(stripped):
            if (under_heading or s.endswith("?") or _OPEN_RE.search(s)) and s not in found:
                found.append(s)
    return found[:limit]


def _change_label(c: FileChange) -> str:
    if c.status == "A":
        return f"{c.path} (new, +{c.added})" if c.added is not None else f"{c.path} (new)"
    if c.status == "D":
        return f"{c.path} (deleted)"
    if c.added is None:
        return c.path
    return f"{c.path} (+{c.added}/-{c.removed})"


def _relative(path: str, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return Path(path).as_posix()


# --------------------------------------------------------------- entries


@dataclass
class Capture:
    """What one capture found and wrote, for tests and the hook."""

    proposed: list[dict] = field(default_factory=list)
    replaced: int = 0
    skipped: str | None = None


def build_entries(
    session_id: str,
    transcript: Transcript,
    changes: list[FileChange] | None,
    project_root: Path,
    project_id: str,
    branch: str | None,
    when: str,
) -> list[dict]:
    """The hand-off group for one session (possibly empty).

    `changes` None means the working-tree diff was unavailable; the files the
    transcript says were edited stand in for it.
    """
    sid = session_id.strip()[:8]
    if changes is None:
        changes = [
            FileChange(_relative(p, project_root), None, None, "M") for p in transcript.edited_files
        ]
    message = _clean(transcript.final_message)
    if not changes and transcript.tool_calls < MIN_TOOL_CALLS:
        return []

    def entry(part: str, entry_type: str, summary: str, files: list[str] | None = None) -> dict:
        source: dict[str, Any] = {"project": project_id, "session": session_id.strip()}
        if files:
            source["files"] = files[:FILES_IN_WORK_ENTRY]
        return {
            "type": entry_type,
            "scope": "project",
            "status": "proposed",
            "confidence": "inferred",
            "summary": _clip(summary, memory_store.MAX_SUMMARY_CHARS),
            "source": source,
            "tags": [HANDOFF_TAG, f"{SESSION_TAG_PREFIX}{sid}", part],
        }

    entries = []
    if changes:
        shown = ", ".join(_change_label(c) for c in changes[:FILES_IN_WORK_ENTRY])
        if len(changes) > FILES_IN_WORK_ENTRY:
            shown += f", +{len(changes) - FILES_IN_WORK_ENTRY} more"
        did = f"changed {len(changes)} file(s): {shown}"
    else:
        did = "changed no files"
    where = f", branch {branch}" if branch else ""
    work_line = f"Session {sid} ({when}{where}) {did}."
    if transcript.commands:
        work_line += f" Ran: {', '.join(transcript.commands[:6])}."
    top = sorted(transcript.tool_counts.items(), key=lambda kv: -kv[1])[:5]
    if top:
        work_line += " Tool calls: " + ", ".join(f"{n} {c}" for n, c in top) + "."
    entries.append(entry("work", "discovery", work_line, [c.path for c in changes]))

    findings = answer_lines(message)
    answer_md = next((c for c in changes if Path(c.path).name == ANSWER_FILE and c.status != "D"), None)
    if answer_md is not None:
        try:
            text = (project_root / answer_md.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        body = _flatten(" ".join(_strip_markdown(l) for l in _clean(text).splitlines() if l.strip()))
        if body:
            findings.append(f"{answer_md.path}: {body}")
    if findings:
        entries.append(entry("findings", "discovery", f"Findings ({sid}): " + " | ".join(findings)))

    chosen = decisions(message)
    if chosen:
        entries.append(entry("decisions", "decision", f"Decisions ({sid}): " + " | ".join(chosen)))

    pending = open_items(message)
    if pending:
        entries.append(entry("open", "discovery", f"Left open ({sid}): " + " | ".join(pending)))

    # The rest of the final message, minus what the entries above already
    # carry: under a 1,500-char injection budget a repeat is a lost line.
    used = set(chosen) | set(pending)
    rest = [
        s
        for s in _sentences(
            "\n".join(
                l
                for l in message.splitlines()
                if not _ANSWER_LINE_RE.match(l) and not l.lstrip().startswith("#")
            )
        )
        if s not in used and not _OPEN_HEADING_RE.match(s.rstrip(":"))
    ]
    if rest:
        entries.append(entry("outcome", "discovery", f"Outcome ({sid}): " + " ".join(rest)))

    # Belt and braces: an entry the store would refuse is dropped on its own.
    valid = []
    for e in entries:
        try:
            memory_store.validate_entry(e)
        except ValueError:
            continue
        valid.append(e)
    return valid


def _fingerprint(entries: list[dict]) -> str:
    # The work entry carries the capture time; hash everything but that.
    payload = [(e["tags"][-1], re.sub(r"\(\d{4}-\d\d-\d\dT[^,)]*", "(", e["summary"])) for e in entries]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()[:16]


def _session_tag(session_id: str) -> str:
    return f"{SESSION_TAG_PREFIX}{session_id.strip()[:8]}"


def _replace_group(memory_dir: Path, session_id: str, entries: list[dict], now: datetime) -> int:
    """Drop this session's earlier *unreviewed* group, then append `entries`.

    Spliced at the line level so lines the store cannot parse survive, and
    anything a human already reviewed (accepted, rejected) is kept.
    """
    tag = _session_tag(session_id)
    keep, dropped = [], 0
    for line in memory_store.read_lines(memory_dir):
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        if (
            isinstance(rec, dict)
            and HANDOFF_TAG in (rec.get("tags") or [])
            and tag in (rec.get("tags") or [])
            and (rec.get("source") or {}).get("session") == session_id.strip()
            and rec.get("status") == "proposed"
        ):
            dropped += 1
            continue
        keep.append(line)
    if dropped:
        memory_store.write_lines(keep, memory_dir)
    memory_store.append_entries(entries, directory=memory_dir, now=now)
    return dropped


def capture(
    project_root: Path,
    session_id: str,
    transcript_path: Path | None,
    last_message: str | None = None,
    now: datetime | None = None,
) -> Capture:
    """Write (or refresh) this session's hand-off group. Never raises."""
    result = Capture()
    try:
        _capture(Path(project_root), session_id, transcript_path, last_message, now, result)
    except Exception as exc:  # noqa: BLE001 -- a hand-off must never block a session ending
        result.skipped = f"{type(exc).__name__}: {exc}"
    return result


def _capture(project_root, session_id, transcript_path, last_message, now, result: Capture) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        result.skipped = "no session id"
        return
    memory_dir = memory_store.project_memory_dir(project_root)
    if not memory_dir.is_dir():
        result.skipped = "project is not enrolled"
        return
    now = now or datetime.now(timezone.utc)
    repo = work.repository_for(project_root)
    state = load_state(repo, session_id)

    transcript = read_transcript(Path(transcript_path) if transcript_path else None)
    if isinstance(last_message, str) and last_message.strip():
        transcript.final_message = last_message

    changes: list[FileChange] | None = None
    start_tree = state.get("start_tree")
    if start_tree:
        end_tree = snapshot_tree(repo.root)
        if end_tree:
            changes = tree_changes(repo.root, start_tree, end_tree)

    entries = build_entries(
        session_id,
        transcript,
        changes,
        repo.root,
        memory_store.project_id_for(project_root),
        repo.current_branch,
        memory_store.format_ts(now),
    )
    if not entries:
        result.skipped = "nothing worth handing off"
        return
    fingerprint = _fingerprint(entries)
    if state.get("fingerprint") == fingerprint:
        result.skipped = "unchanged since the last capture"
        return
    result.replaced = _replace_group(memory_dir, session_id, entries, now)
    result.proposed = entries
    save_state(repo, session_id, {**state, "fingerprint": fingerprint,
                                  "captured_at": memory_store.format_ts(now)})


# --------------------------------------------------------------- injection


def _groups(memory_dir: Path, exclude_session: str | None) -> list[list[dict]]:
    """Live hand-off groups, newest first, each in part order."""
    skip = _session_tag(exclude_session) if exclude_session else None
    by_session: dict[str, list[dict]] = {}
    for rec in memory_store.iter_entries(memory_dir, scope="project"):
        tags = rec.get("tags") or []
        if HANDOFF_TAG not in tags or rec.get("status") in memory_store.CLOSED_STATUSES:
            continue
        stag = next((t for t in tags if t.startswith(SESSION_TAG_PREFIX)), None)
        if not stag or stag == skip:
            continue
        # Re-inserting moves the session to the end: the latest write wins recency.
        by_session[stag] = by_session.pop(stag, []) + [rec]
    rank = {p: i for i, p in enumerate(PART_TAGS)}
    part = lambda r: min((rank[t] for t in r.get("tags", []) if t in rank), default=len(rank))
    return [sorted(recs, key=part) for recs in reversed(list(by_session.values()))]


def handoff_ids(memory_dir: Path) -> set[str]:
    """Ids of every hand-off entry; the table of contents leaves them to `render_block`."""
    return {
        str(r["id"])
        for r in memory_store.iter_entries(memory_dir)
        if HANDOFF_TAG in (r.get("tags") or []) and r.get("id")
    }


def _max_chars() -> int:
    try:
        return int(os.environ[MAX_CHARS_ENV])
    except (KeyError, ValueError):
        return HANDOFF_MAX_CHARS


MIN_LINE_CHARS = 60


def render_block(project_root: Path, session_id: str | None = None, max_chars: int | None = None) -> str:
    """The hand-off block for session start, or "" when there is none.

    Newest session first, at most `MAX_SESSIONS_SHOWN`. Each entry is one
    line; a line that does not fit is clipped to the room left (never below
    `MIN_LINE_CHARS`, else dropped), so under a tight budget it is the newest
    session's work and findings that survive. ``max_chars`` (default 1,500,
    `AGENT_HARNESS_HANDOFF_MAX_CHARS`; 0 turns injection off) bounds the
    whole block, heading included.
    """
    budget = _max_chars() if max_chars is None else max_chars
    if budget <= 0:
        return ""
    groups = _groups(memory_store.project_memory_dir(project_root), session_id)
    if not groups:
        return ""
    head = [
        "## Hand-off from earlier sessions",
        "",
        "_Captured automatically when each session ended (proposed, unreviewed); "
        "verify against the code before relying on it._",
        "",
    ]
    spent = sum(len(l) + 1 for l in head) + 1  # + the trailing blank line
    body: list[str] = []
    for recs in groups[:MAX_SESSIONS_SHOWN]:
        for rec in recs:
            room = budget - spent - 1
            if room < MIN_LINE_CHARS:
                break
            line = _clip(f"- {rec.get('summary', '')}", room)
            body.append(line)
            spent += len(line) + 1
    if not body:
        return ""
    return "\n".join(head + body) + "\n\n"
