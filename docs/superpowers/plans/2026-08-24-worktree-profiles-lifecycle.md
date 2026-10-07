# Worktree Profiles and Lifecycle — Implementation Plan

Spec: `docs/superpowers/specs/2026-08-24-worktree-profiles-lifecycle-design.md` (approved)

Plan revision: 2 (2026-08-25). Revision 1 restated the spec as prose and left six
design questions for the executor to answer mid-implementation. This revision
answers them. Nothing below requires the implementer to design anything.

**Goal:** extend `agent-harness-work` with a private/portable profile, verification
receipts, and gated rebase / integration-prep / retirement — offline, with no new
runtime dependency and no change to the four existing commands or four existing
MCP tools.

**How to execute:** work the numbered tasks in order. Each task is independently
committable and leaves the suite green. Steps are `- [ ]` for tracking. Run
`python -m pytest -q tests/test_work.py` after every task; it is the regression
baseline and must never go red.

---

## Part 1 — Decisions

These were open in revision 1. They are now settled; implement them as written.

### D1. No `harness/work_state.py`. Extend `Task` in place.

Revision 1 extracted the task model into a new module and re-exported it. The
spec does not ask for this, `work.py` is 446 lines (limit 800), and the split
creates a compatibility surface for two real importers — `mcp_server.dispatch_work`
(`from harness import work`) and `tests/test_work.py`.

Add two fields to the existing `Task` dataclass in `harness/work.py`:

```python
profile: str = PROFILE_PRIVATE          # "private" | "portable"
verification: tuple[dict[str, object], ...] = ()
```

Both have defaults, so every existing `Task(...)` construction and the
`Task(**{**asdict(task), ...})` idiom in `finish()` keep working untouched.

### D2. The stage is derived, never stored.

Revision 1 contradicted itself — Task 3 stored `phase="active"`, Task 6 declared
five different phase values. Storing a stage creates a second source of truth for
a fact Git already knows.

One vocabulary throughout: these are **stages**, and they are what D3 gates on. The
word "phase" from revision 1 is retired — do not reintroduce it in code or tests.

`Task` gets **no** `stage` field. `work_flow.derive_stage()` computes it from
stored facts plus live Git. Exactly six derived stages (plus `retired`, reached only
through the `retire` transition, which sets `status = DONE`):

Let `head` = `git rev-parse HEAD` in the task worktree, and define:

- `has_own_work` = `git rev-list --count <task.base_sha>..HEAD` is **> 0**
- `contained_in_onto` = `git rev-list --count <onto>..HEAD` is **0**
- `contains_onto` = `git merge-base --is-ancestor <onto> HEAD` exits 0
- `verified` = `any(r["passed"] and r["commit"] == head for r in task.verification)`

| Stage | Condition (evaluated in this order) |
|---|---|
| `integrated` | `onto` given and `has_own_work` and `contained_in_onto` |
| `rebased-ready` | `onto` given and `verified` and `contains_onto` |
| `portable-verified` | `profile == portable` and `verified` |
| `portable-active` | `profile == portable` |
| `private-verified` | `profile == private` and `verified` |
| `private-active` | otherwise |

**`has_own_work` is load-bearing, not decoration.** Immediately after `work start`
the task branch has zero commits of its own, so `HEAD` *is* the tip of `onto` and a
bare `merge-base --is-ancestor HEAD <onto>` succeeds vacuously — a freshly started
task would report `integrated` and be told to `retire --delete-branch`. Requiring at
least one commit past `base_sha` closes that. Cover it with
`test_a_freshly_started_task_with_onto_is_not_integrated` (Task 5). The same fact
surfaces as the `no-own-work` blocker on `promote`, so an empty branch cannot be
published either.

Receipts pinned to a commit are what make `verified` derivable — a receipt for an
older commit is simply not counted, which is also why rebase needs no
receipt-clearing bookkeeping.

`onto` is optional. Without it, `integrated` and `rebased-ready` are unreachable and
`status` reports the `needs-onto-ref` blocker rather than guessing a ref.

### D3. Ship a built-in verification profile; gate the stage, not the command.

Revision 1 made verification profiles machine-settings-only, never seeded
`~/.agent-harness/settings.json`, and then gated `handoff`, `rebase`,
`prepare-integrate` and `retire` on a passing receipt. On a fresh machine that is
zero runnable profiles → no receipt → every gate shut permanently, including
`retire` on a private task, whose worktree the tool could then never remove.

Two changes:

1. `work_config.BUILTIN_VERIFICATION_PROFILES = {"tests": ("python", "-m", "pytest", "-q")}`.
   Machine settings may add or override entries. This is harness-shipped, not
   repository-supplied, so it does not violate "never auto-execute repository
   configuration" — and it never runs on its own; the operator must type
   `work verify <id> --profile tests`.

2. **The gate lives on the stage transition. Commands are instruments.**

   Revision 1 attached a receipt requirement to each of `handoff`, `rebase`,
   `prepare-integrate` and `retire` individually. That scatters one policy across six
   call sites, so the CLI and the MCP surface can drift apart, and every command added
   later re-litigates the rules. It also mis-frames the tools: `verify`, `diff` and
   `status` exist to help a task *satisfy* a stage — they are how you earn the
   transition, not things that should each carry a veto.

   Exactly one function decides whether state may change:

   ```python
   def transition(directory: Path, task_id: str, to_stage: str, *,
                  confirm: bool = False, onto: str | None = None,
                  delete_branch: bool = False) -> GateStatus: ...
   ```

   Every state-changing command is a one-line wrapper naming its target stage. The
   strict preconditions live here and nowhere else:

| Transition | Command | Strict preconditions (**all** must hold) |
|---|---|---|
| `private-active → portable-active` | `promote --portable --confirm` | clean worktree; `has_own_work`; `portable_allowed`; explicit `--confirm` |
| `portable-active → private-active` | `privatize --confirm` | not published (D7); explicit `--confirm` |
| `* → rebased-ready` | `rebase --onto REF` | clean worktree; no *failing* receipt at HEAD; `onto` resolves as a local ref |
| `rebased-ready → integrated` | `prepare-integrate --confirm` | clean; **passing** receipt at HEAD; portable tasks only (private tasks have no manifest to remove) |
| `* → retired` | `retire --target REF` | clean worktree. Removes the external worktree only; the branch is left intact |
| `integrated → retired` | `retire --target REF --delete-branch` | clean; passing receipt at HEAD; `git merge-base --is-ancestor <branch> <target>` succeeds |

   **`verify` is not in this table, by design.** It is an evidence tool: it writes a
   receipt and returns the command's own exit code. Because D2 derives the stage from
   receipts, `*-active → *-verified` happens as a *consequence* of evidence rather than
   as a gated act. Nothing to gate — there is no way to reach `-verified` except by
   actually passing.

   **So `portable-verified` and `private-verified` have no row, and that is correct.**
   You do not transition into them; you run `work verify <id> --profile P` until a
   receipt passes at HEAD, and the stage re-derives on the next `status`. The table
   covers only acts that mutate Git or the registry.

   `rebase` is deliberately `* → rebased-ready`, not `*-verified → rebased-ready`.
   Rebasing changes HEAD, which invalidates any receipt anyway, so requiring a passing
   one first would re-open the D3 deadlock through the stage column. A *failing* receipt
   at HEAD still blocks, because that is a known-broken tree.

   The `retire` rows preserve the deadlock fix: an abandoned task with no receipt can
   always have its worktree reclaimed, because only `--delete-branch` requires
   `integrated`.

### D4. Drop `harness/work_exclude.py`. Replace it with a guard test.

Revision 1 built and tested `ensure_local_exclude` / `remove_local_exclude`, then
never called them from tasks 3–8. Nothing in this feature writes a repo-local
private artifact: private mode writes only under `~/.agent-harness/`, and the
portable manifest at `.agent-harness/handoffs/<id>.json` is *deliberately tracked*.

The spec's wording is conditional — "*if* a repository-local private artifact is
unavoidable". Here it is avoidable, so building the module now is speculative code.

What the spec actually protects is the invariant, so **test the invariant instead**:
`test_private_lifecycle_leaves_root_gitignore_and_tracked_files_untouched` (Task 1).
If a future feature genuinely needs a repo-local private file, `.git/info/exclude`
is the mechanism, and commit `790beff` is the precedent for doing it by hand.

Note `.agent-harness/work/` carries its own `*` gitignore (see `work_store.py`), and
`.agent-harness/handoffs/` is a *sibling* of that directory, so the manifest is
trackable with no gitignore change anywhere.

### D5. `advance` creates a recovery ref and nothing else.

The spec allows `advance` to make "safe local transitions", listing manifest updates —
but a manifest update is a commit, and committing is a history change, not a safe
automatic transition. Narrow it: `advance` returns the next action and may perform
exactly one mutation — creating the pre-rebase recovery ref when a rebase is next.
Refs under `refs/agent-harness/` are invisible to normal Git workflows and purely
additive. `advance` never commits, rebases, merges, pushes, or deletes.

**Ref ownership:** `rebase` is the sole creator. `advance` calls the same
`ensure_recovery_ref(repo, task, head)` helper, which is idempotent — if a ref under
`refs/agent-harness/recovery/<task-id>/` already points at the current `head`, it
returns that ref instead of minting a second timestamped one. Without this,
`advance` followed by `rebase` leaves two refs for one rebase.

### D6. Recovery ref name uses a colon-free timestamp.

`refs/agent-harness/recovery/<task-id>/<YYYYMMDDTHHMMSSZ>`

`memory_store.format_ts` emits ISO-8601 with colons, which `git check-ref-format`
rejects. Use `now.strftime("%Y%m%dT%H%M%SZ")` for the ref component only; receipts
and task timestamps keep the existing `format_ts` shape.

### D7. `privatize` changes the source of truth, or it does nothing at all.

Revision 1 had `privatize` "remove the manifest in an explicit commit" while noting it
"cannot erase a manifest already committed or pushed." That describes a command that
half-works: it reports success, tidies the working tree, and leaves the data public.
An operator reading `status: success` would reasonably believe the handoff is private.

Make it binary. There is no partial privatize.

- **Not published** — the manifest-introducing commit is not reachable from any
  remote-tracking ref. Then privatize really changes the source of truth: set
  `profile = private` in `tasks.json` **and** remove the manifest in one explicit
  commit. The task stops producing portable updates. This is a genuine state change,
  not a cosmetic one.
- **Published** — **drop the operation entirely.** No commit, no registry write, no
  partial cleanup. Return the `already-published` blocker. History that has left the
  machine cannot be made private by a local toggle, and a local tidy-up would
  misrepresent what actually happened.

The manifest-introducing commit is **derived, never stored** — same rule as D2. Do not
add a field to `Task` for it; ask Git:

```text
git log --diff-filter=A --format=%H -1 -- .agent-harness/handoffs/<id>.json
```

Empty output means no manifest was ever committed, so the task is trivially unpublished.

The publication check is offline and uses only local refs — never a fetch:

```text
git for-each-ref --format=%(refname) refs/remotes
→ for each ref: git merge-base --is-ancestor <manifest-commit> <ref>
```

This reflects *last-known* remote state, which is the most an offline tool can honestly
know. The refusal text must say so, so the operator understands a stale remote-tracking
ref is possible and that "not published" is a best-effort answer, not a guarantee.

### Verified helper contracts

Checked against the working tree — use these exact names:

| Symbol | Location | Signature |
|---|---|---|
| `atomic_sibling` | `harness/atomicwrite.py:81` | `@contextmanager (dst: Path) -> Iterator[Path]` — yields temp path, replaces on clean exit |
| `replace_with_retry` | `harness/atomicwrite.py:50` | `(src, dst, *, attempts=5, delay=0.01) -> None` |
| `load_json_object` | `harness/config_validation.py:42` | `(path: Path) -> dict` — reads a file |
| `parse_json_object` | `harness/config_validation.py:24` | `(text: str, *, source: str) -> dict` — parses a string |
| `ConfigValidationError` | `harness/config_validation.py:20` | subclass of `ValueError` |

Both `config_validation` names are real and do different jobs; use `load_json_object`
for settings files and `parse_json_object` for manifest text already in hand.

`tests/conftest.py` has an **autouse** `isolated_home` fixture that patches
`Path.home` and `HOME`/`USERPROFILE`. Revision 1 told every test to
`monkeypatch.setattr(Path, "home", ...)` — that is redundant. Do not repeat it.

---

## Part 2 — File map

Create:

| File | Owns |
|---|---|
| `harness/work_config.py` | built-in defaults, layered settings resolution, `EffectiveSettings` |
| `harness/work_manifest.py` | portable handoff schema, strict codec, sanitizer |
| `harness/work_verify.py` | argv-only verification execution, receipts |
| `harness/work_flow.py` | stage derivation, blockers, and the single `transition()` chokepoint |

Modify:

| File | Change |
|---|---|
| `harness/work.py` | `Task.profile` + `Task.verification`, schema-2 migration, `atomic_sibling` in `_save_tasks`, new CLI subcommands |
| `harness/mcp_server.py` | nine new actions in `dispatch_work` + decorated wrappers |
| `README.md` | "Parallel task workspaces" section, lines 537–559 |

Tests — create `tests/test_work_config.py`, `tests/test_work_manifest.py`,
`tests/test_work_verify.py`, `tests/test_work_flow.py`; extend the existing
150-line `tests/test_work.py`, whose `repo` fixture is the base for the two-clone
portable test.

## Part 3 — Contracts

```python
# harness/work_config.py
PROFILE_PRIVATE = "private"
PROFILE_PORTABLE = "portable"
SETTINGS_SCHEMA = 1
BUILTIN_VERIFICATION_PROFILES = {"tests": ("python", "-m", "pytest", "-q")}

@dataclass(frozen=True)
class EffectiveSettings:
    profile: str
    profile_source: str          # "built-in" | "machine" | "project" | "cli"
    portable_allowed: bool       # default False
    worktree_root: Path | None
    verification_profiles: Mapping[str, tuple[str, ...]]

def load_settings(project_root: Path, profile_override: str | None = None) -> EffectiveSettings: ...

# harness/work_manifest.py
HANDOFF_SCHEMA = 1
HANDOFF_KEYS = frozenset({"schema", "task_id", "summary", "paths",
                          "branch", "base_sha", "status", "verification", "next_action"})
RECEIPT_KEYS = frozenset({"name", "returncode", "passed", "commit", "completed_at"})

@dataclass(frozen=True)
class PortableHandoff:
    task_id: str; summary: str; paths: tuple[str, ...]; branch: str
    base_sha: str; status: str; verification: tuple[dict[str, object], ...]; next_action: str

def handoff_path(project_root: Path, task_id: str) -> Path: ...   # <root>/.agent-harness/handoffs/<id>.json
def load_handoff(path: Path) -> PortableHandoff: ...
def write_handoff(path: Path, handoff: PortableHandoff) -> None: ...
def remove_handoff(path: Path) -> None: ...
def sanitize(task: Task, next_action: str) -> PortableHandoff: ...

# harness/work_verify.py
@dataclass(frozen=True)
class VerificationReceipt:
    name: str; returncode: int; passed: bool; commit: str; completed_at: str

TIMEOUT_RETURNCODE = 124   # matches coreutils `timeout`

def run_verification(worktree: Path, commit: str, name: str,
                     argv: Sequence[str], timeout_seconds: float = 300.0) -> VerificationReceipt: ...
def record_receipt(repo: Repository, task_id: str, receipt: VerificationReceipt) -> Task: ...

# harness/work_flow.py
STAGES = ("private-active", "private-verified", "portable-active",
          "portable-verified", "rebased-ready", "integrated", "retired")

@dataclass(frozen=True)
class GateStatus:
    task_id: str; profile: str; stage: str
    blockers: tuple[str, ...]; next_actions: tuple[str, ...]

# --- derivation and read-only queries -------------------------------------
def derive_stage(task: Task, worktree: Path, onto: str | None = None) -> str: ...
def blockers_for(task: Task, worktree: Path, to_stage: str, **opts) -> tuple[str, ...]: ...
def status(directory: Path, task_id: str, onto: str | None = None) -> GateStatus: ...
def advance(directory: Path, task_id: str, onto: str | None = None) -> GateStatus: ...

# --- THE chokepoint: the only function that may change state --------------
def transition(directory: Path, task_id: str, to_stage: str, *,
               confirm: bool = False, onto: str | None = None,
               delete_branch: bool = False) -> GateStatus: ...

# --- thin wrappers; each names a target stage and adds no gate logic -------
def promote(directory, task_id, confirm):            # -> "portable-active"
def privatize(directory, task_id, confirm):          # -> "private-active"
def rebase(directory, task_id, onto):                # -> "rebased-ready"
def prepare_integrate(directory, task_id, confirm):  # -> "integrated"
def retire(directory, task_id, target, delete_branch=False):  # -> "retired"

# --- evidence and session tools; not transitions --------------------------
def resume(directory: Path, task_id: str, session_id: str, agent: str = "codex") -> Task: ...
def handoff(directory: Path, task_id: str, confirm: bool) -> Task: ...   # refreshes the manifest in place
def is_published(worktree: Path, manifest_commit: str) -> bool: ...      # D7, offline
```

`transition()` is the single place preconditions are evaluated and the single place
state is written. Wrappers must contain no `if` that could refuse — a wrapper that
grows its own gate is the bug this decision exists to prevent.

`handoff` is no longer a gated transition. It refreshes an existing portable manifest
to match the registry and does not change stage; it simply refuses a private task
(`wrong-profile`), which is a type error, not a gate.

Blocker vocabulary (exact strings, used by both tests and CLI output):
`dirty-worktree`, `failing-verification`, `no-verification`, `portable-not-allowed`,
`wrong-profile`, `manifest-mismatch`, `worktree-missing`, `not-merged`,
`needs-onto-ref`, `no-own-work`, `already-published`, `needs-confirm`.

---

## Part 4 — Action tasks

### Task 1 — Task record: profile, receipts, schema-2 migration

Files: `harness/work.py`, `tests/test_work.py`

- [ ] Add to `tests/test_work.py`:
      `test_schema_1_task_state_migrates_to_private_profile_with_no_receipts`
      (seed `tasks.json` with `{"schema": 1, "tasks": [...]}` using today's field set,
      assert `_load_tasks` returns `profile == "private"` and `verification == ()`);
      `test_schema_2_round_trips_a_portable_task_with_one_receipt`;
      `test_unknown_schema_version_is_refused`;
      `test_private_lifecycle_leaves_root_gitignore_and_tracked_files_untouched`
      (per D4). **The existing `repo` fixture creates only `src/app.py` and `README.md`
      — there is no `.gitignore` to snapshot.** Have this test write and commit a
      `.gitignore` first, then compare its bytes and `git ls-files` output before and
      after a full private `start` → `diff` → `finish`, asserting both identical.
- [ ] Run `python -m pytest -q tests/test_work.py` — the four new tests fail, the eight existing pass.
- [ ] Set `SCHEMA_VERSION = 2`. In `_load_tasks`, accept `1` or `2`; for `1`, inject
      `profile=PROFILE_PRIVATE` before constructing `Task`. Reject any other value with the
      existing "unsupported schema" `WorkError`.
- [ ] **Coerce `verification` to a tuple in the same comprehension that coerces `paths`.**
      JSON has no tuple type, so a schema-2 round-trip returns a `list` and the
      round-trip assertion fails unless the loader normalizes it. One edit covers both the
      schema-1 default and the schema-2 round-trip:

      ```python
      Task(**{**item,
              "paths": tuple(item["paths"]),
              "verification": tuple(item.get("verification", ()))})
      ```
- [ ] Add the two fields from D1 to `Task`, both with defaults.
- [ ] Replace the hand-rolled temp-and-replace in `_save_tasks` with
      `with atomic_sibling(path) as tmp: tmp.write_text(...)`. Keep the `uuid` import —
      `start()` still uses it for task IDs.
- [ ] Run `python -m pytest -q tests/test_work.py` — all green.
- [ ] `git add harness/work.py tests/test_work.py && git commit -m "refactor: version work task state"`

### Task 2 — Layered settings

Files: `harness/work_config.py`, `tests/test_work_config.py`

- [ ] Write `tests/test_work_config.py`. Precedence: absent files → `profile == "private"`,
      `profile_source == "built-in"`; machine `~/.agent-harness/settings.json` beats
      built-in; project `.agent-harness/work-settings.json` beats machine;
      `profile_override` beats all with `profile_source == "cli"`. Rejection: `schema: 2`,
      unknown key under `work`, a profile other than private/portable, a
      `verification_profiles` key in the **project** file, an argv given as a bare string
      rather than a list, an empty argv, malformed JSON. Also
      `test_builtin_tests_profile_is_available_with_no_settings_files` (D3) and
      `test_machine_settings_override_a_builtin_profile_of_the_same_name`.
      Do **not** patch `Path.home` — `conftest.isolated_home` already does.
- [ ] Run `python -m pytest -q tests/test_work_config.py` — collection fails, module absent.
- [ ] Implement. Read machine then project, each only if present, via `load_json_object`;
      wrap `ConfigValidationError` as `WorkError`. Machine layer may set `work.profile`,
      `work.portable_allowed`, `work.worktree_root`, `work.verification_profiles`. Project
      layer may set `work.profile` and `work.portable_allowed` **only**. Merge
      `BUILTIN_VERIFICATION_PROFILES` *under* the machine map so a same-named machine entry
      wins. Store argv as `tuple[str, ...]`; reject non-string or empty members. Resolve a
      relative `worktree_root` against the machine file's parent. `portable_allowed`
      defaults to `False`. Return a frozen dataclass; never write a file from a read.
- [ ] Run `python -m pytest -q tests/test_work_config.py` — green.
- [ ] `git commit -m "feat: add private-first work profiles"`

### Task 3 — Portable handoff manifest

Files: `harness/work_manifest.py`, `tests/test_work_manifest.py`

- [ ] Write `tests/test_work_manifest.py`. Round-trip this exact payload:

      ```json
      {"schema": 1, "task_id": "work-20260824-a1b2c3d4", "summary": "Update the parser",
       "paths": ["harness/parser.py", "tests/test_parser.py"],
       "branch": "codex/work-20260824-a1b2c3d4", "base_sha": "aaaa...(40 hex)",
       "status": "portable-active", "verification": [], "next_action": "run local verification"}
      ```

      Reject: unknown key, absolute path, `..` path, a `session_id` key, a `plan_path` key,
      malformed JSON, `schema: 2`, empty `task_id`, an uppercase or short `base_sha`, and a
      receipt carrying `argv`/`stdout`/`stderr`. Add
      `test_sanitize_drops_session_and_plan_path_from_a_task` and
      `test_write_handoff_leaves_no_temp_file_when_replacement_fails`.
- [ ] Run `python -m pytest -q tests/test_work_manifest.py` — fails.
- [ ] Implement. `load_handoff` checks `set(payload) == HANDOFF_KEYS` **before** constructing
      the dataclass. Reuse `work.normalize_paths` for path rules. `base_sha` must match
      `^[0-9a-f]{40}$`. Filter each receipt to `RECEIPT_KEYS` and reject extras.
      `next_action` must be non-empty. `write_handoff` creates only the parent directory and
      uses `atomic_sibling`. `remove_handoff` is idempotent.
- [ ] Run `python -m pytest -q tests/test_work_manifest.py` — green.
- [ ] `git commit -m "feat: add strict portable work handoffs"`

### Task 4 — Verification receipts

Files: `harness/work_verify.py`, `tests/test_work_verify.py`

- [ ] Write `tests/test_work_verify.py`. Monkeypatch `subprocess.run` and assert it is called
      with a **list** argv, `cwd=worktree`, `shell=False`, `check=False`,
      `capture_output=True`, `text=True`, `timeout=timeout_seconds`. Assert exit 0 →
      `passed is True`; nonzero → `passed is False` without raising; `TimeoutExpired` → a
      failed receipt with `returncode == TIMEOUT_RETURNCODE`. Assert
      `test_receipt_stores_no_stdout_stderr_argv_or_absolute_path` — the receipt's field set
      is exactly `RECEIPT_KEYS`. Assert an unknown profile name raises `WorkError`, and
      `test_record_receipt_appends_under_the_task_lock_and_persists_atomically`.
- [ ] Run `python -m pytest -q tests/test_work_verify.py` — fails.
- [ ] Implement. Resolve the profile from `EffectiveSettings.verification_profiles`; reject a
      missing profile or empty argv. Resolve the commit with `git rev-parse HEAD` **before**
      invoking. Never persist output. `record_receipt` takes `work._state_lock`, appends to
      `task.verification`, and saves atomically.
- [ ] Run `python -m pytest -q tests/test_work_verify.py` — green.
- [ ] `git commit -m "feat: record offline work verification"`

### Task 5 — Stage derivation and read-only queries

Files: `harness/work_flow.py`, `tests/test_work_flow.py`

- [ ] Write `tests/test_work_flow.py` covering the D2 table: one test per stage, named
      `test_derive_stage_returns_<stage_with_underscores>`. Plus
      `test_dirty_worktree_is_a_blocker_not_a_stage`,
      `test_receipt_for_an_older_commit_does_not_count_as_verified`,
      `test_status_without_onto_reports_needs_onto_ref_instead_of_guessing`,
      `test_a_freshly_started_task_with_onto_is_not_integrated` (the `has_own_work`
      guard in D2 — a zero-commit branch must report `private-active`, not `integrated`), and
      `test_advance_creates_only_a_recovery_ref_and_never_commits` (assert `git log` on the
      branch is unchanged and `git status --porcelain` is empty afterwards).
- [ ] Run `python -m pytest -q tests/test_work_flow.py` — fails.
- [ ] Implement `derive_stage`, `blockers_for`, `status`, `advance` exactly per D2 and D5.
      Ancestry via `git merge-base --is-ancestor` — exit 0/1 *is* the answer, so call
      `subprocess.run` directly rather than `work._git`, which raises on any nonzero exit.
      Recovery ref name per D6.
- [ ] `blockers_for` is pure: `(task, worktree, to_stage, **opts) -> tuple[str, ...]`, no
      writes, no Git mutation. It is the whole D3 precondition table and the only place
      those rules exist. Tasks 6 and 7 add rows to it; they add no gate logic anywhere else.
- [ ] Run `python -m pytest -q tests/test_work_flow.py` — green.
- [ ] `git commit -m "feat: derive work lifecycle stage from git and receipts"`

### Task 6 — The `transition()` chokepoint, plus promote / privatize / resume / handoff

Files: `harness/work_flow.py`, `tests/test_work_flow.py`

- [ ] Add chokepoint tests first, because they constrain everything after:
      `test_every_state_changing_wrapper_delegates_to_transition` (monkeypatch
      `work_flow.transition` to raise, then assert each of `promote`, `privatize`,
      `rebase`, `prepare_integrate`, `retire` raises — proving none has its own path to
      state), and `test_transition_refuses_without_writing_when_any_blocker_is_present`
      (assert `tasks.json` mtime and `git log` are both unchanged after a refusal).
- [ ] Add `test_retire_does_not_touch_the_worktree_before_the_gate_runs`. Delegation alone
      is not enough for `retire`: it also removes a worktree, and a test that only checks
      "the wrapper raised" passes whether removal happens before or after the gate. Patch
      `transition` to raise, call `retire`, and assert the worktree directory **still
      exists** — a refused retire must not have already deleted it.
- [ ] Add transition tests: `promote(confirm=False)` returns a preview and mutates no Git
      state; `promote(confirm=True)` refuses `dirty-worktree`, `no-own-work`, and
      `portable-not-allowed`; on success the new commit's changed-file list is exactly
      `[".agent-harness/handoffs/<id>.json"]`.
- [ ] Add D7 privatize tests: `test_privatize_unpublished_sets_profile_private_in_the_registry`
      (assert `tasks.json` really flips **and** the manifest commit lands),
      `test_privatize_published_makes_no_commit_and_no_registry_write`
      (seed a `refs/remotes/origin/...` ref containing the manifest commit; assert
      `already-published`, unchanged `tasks.json`, unchanged `git log`), and
      `test_is_published_uses_only_local_refs_and_never_fetches`.
- [ ] Add tool tests: `resume` from a fetched branch validates task ID, branch, and
      `base_sha`, writes local state with the new session ID, and raises
      `manifest-mismatch` on disagreement. `handoff` refreshes the manifest without
      changing stage and refuses a private task with `wrong-profile`.
- [ ] Run the focused tests — fail.
- [ ] Implement `transition()` as the sole writer: resolve stage → collect
      `blockers_for(...)` → refuse with `GateStatus` if any → act → persist atomically. On
      failure leave the prior task record usable. Stage **only** the manifest path for
      metadata commits (`git add <manifest>`, never `git add -A`).
- [ ] Implement `promote` and `privatize` as wrappers naming their target stage, and
      `is_published` per D7. No wrapper may contain a refusal branch.
- [ ] Run `python -m pytest -q tests/test_work_flow.py tests/test_work.py` — green.
- [ ] `git commit -m "feat: route work state changes through one gated transition"`

### Task 7 — History transitions: rebase, prepare-integrate, retire

Files: `harness/work_flow.py`, `tests/test_work_flow.py`

- [ ] Add tests: `rebase` creates the recovery ref **before** running Git, never fetches
      (monkeypatch the git runner and assert no `fetch` argv), updates `base_sha` on success,
      and on conflict leaves Git in its normal conflict state with the recovery ref and task
      record intact. `prepare_integrate` requires a passing receipt (`no-verification`
      otherwise), refuses a private task, and removes only the manifest. `retire` without
      `--delete-branch` removes only a recorded **external** worktree and works with no
      receipt (D3); `retire --delete-branch` requires a passing receipt and refuses with
      `not-merged` until `git merge-base --is-ancestor <branch> <target>` succeeds, then uses
      `git branch -d` (never `-D`).
- [ ] Run the focused tests — fail.
- [ ] Add the remaining rows to `blockers_for` per the D3 table — `rebased-ready`,
      `integrated`, `retired`. Implement `rebase`, `prepare_integrate`, `retire` as
      wrappers over `transition()`; like Task 6's, none may contain a refusal branch.
      `rebase` uses an explicit `--onto <ref>`.
- [ ] Run `python -m pytest -q tests/test_work_flow.py tests/test_work.py` — green.
- [ ] `git commit -m "feat: gate rebase, integration prep, and retirement"`

### Task 8 — CLI surface

Files: `harness/work.py`, `tests/test_work.py`

- [ ] Add tests asserting each new subcommand parses, and that a blocked gate exits `2` with
      the existing `status:` / `summary:` / `next_actions:` / `artifacts:` shape.
- [ ] Add subparsers, leaving `start` / `list` / `diff` / `finish` byte-identical:
      `resume TASK_ID --session S [--agent A]`, `status TASK_ID [--onto REF]`,
      `verify TASK_ID --profile P`, `promote TASK_ID --portable [--confirm]`,
      `handoff TASK_ID [--confirm]`, `rebase TASK_ID --onto REF`,
      `prepare-integrate TASK_ID [--confirm]`, `privatize TASK_ID [--confirm]`,
      `advance TASK_ID [--onto REF]`, `retire TASK_ID --target REF [--delete-branch]`.
      Every one takes the existing `--project` argument defaulting to `"."`.
- [ ] `verify` returns the command's own pass/fail as exit `0`/`1`, kept distinct from the
      gate-refusal exit `2`.
- [ ] **The CLI adds no gate logic.** Each subcommand parses arguments and calls one
      `work_flow` function. Add `test_cli_never_refuses_a_transition_on_its_own`:
      monkeypatch `work_flow.transition` to return a clean `GateStatus` and assert every
      state-changing subcommand exits `0` — proving no refusal branch lives in `work.py`.
- [ ] Run `python -m pytest -q tests/test_work.py` — green.
- [ ] `git commit -m "feat: add work lifecycle subcommands"`

### Task 9 — MCP surface

Files: `harness/mcp_server.py`, `tests/test_work.py`

- [ ] Add tests to `tests/test_work.py` exercising `dispatch_work` for `work_status` with a
      task ID, a promotion preview, a verification receipt, a rebase refused without `onto`,
      and retire refused before merge. Assert every response carries
      status/summary/next_actions/artifacts, plus
      `test_portable_mcp_response_never_contains_a_machine_plan_path`. Assert the existing
      four tools still respond unchanged, including `work_status` with no `task_id`.
- [ ] **Add the cross-surface consistency test** — this is what keeps the two call
      surfaces from drifting: `test_cli_and_mcp_report_identical_blockers_for_the_same_task`.
      Build one task in each blocked condition (`dirty-worktree`, `no-verification`,
      `not-merged`, `already-published`), then assert the CLI result and the
      `dispatch_work` result carry the same `blockers` tuple and the same stage. Both
      surfaces reach state only through `work_flow.transition`, so any divergence here
      means gate logic leaked into a wrapper.
- [ ] Run `python -m pytest -q tests/test_work.py -k mcp` — new cases fail.
- [ ] Add one `WORK_TOOL_DESCRIPTIONS` entry and one typed decorated wrapper per action:
      `work_resume`, `work_verify`, `work_promote`, `work_handoff`, `work_rebase`,
      `work_prepare_integrate`, `work_privatize`, `work_advance`, `work_retire`. Extend the
      `for func in (...)` registration tuple at line 808. Convert `WorkError` to the existing
      recoverable `status: error` shape. Update the tool count in the module docstring
      (line 2, currently "20 typed MCP tools") and the `work_*` list at lines 36–39.
      `tests/test_mcp_server.py:141` hard-codes a tool-name set, but it asserts on
      `TOOL_TO_COMMAND`, which is graph-only — adding `work_*` entries to the separate
      `WORK_TOOL_DESCRIPTIONS` map does not touch it, so that file needs no edit.
- [ ] Run `python -m pytest -q tests/test_work.py tests/test_mcp_server.py` — green.
- [ ] `git commit -m "feat: expose gated work lifecycle actions"`

### Task 10 — Docs and full verification matrix

Files: `README.md`

- [ ] Rewrite README lines 537–559. Document: machine vs project settings paths, the
      `private` default, `portable_allowed` defaulting to `False`, the built-in `tests`
      profile, the two-device fetch-then-`resume` flow, the full command sequence, and the
      explicit push/merge/delete boundary. Keep every example offline and use the task ID
      `work-20260824-a1b2c3d4`. Correct the existing sentence "the project `.gitignore` is
      never modified" to also state that no `.git/info/exclude` entry is written either (D4).
- [ ] `python -m pytest -q tests/test_work.py tests/test_work_config.py tests/test_work_manifest.py tests/test_work_verify.py tests/test_work_flow.py`
- [ ] `$env:TEMP='C:\tmp'; $env:TMP='C:\tmp'; python -m pytest -q` — full suite; report
      pre-existing platform skips separately from failures.
- [ ] `python -m compileall -q harness && python -m build && git diff --check && git status --short --branch`
- [ ] `git commit -m "docs: document the gated work lifecycle"`

---

## Acceptance

- `python -m pytest -q` shows no new failures.
- The eight original tests in `tests/test_work.py` are unmodified and passing.
- `work_start` / `work_status` / `work_diff` / `work_finish` respond exactly as before.
- A private task start→finish changes neither root `.gitignore` nor `git ls-files` output.
- A fresh machine with no `~/.agent-harness/settings.json` can run
  `work verify <id> --profile tests` and reach `retire`.
- No module in `harness/` exceeds 800 lines; `pyproject.toml` gains no runtime dependency.
- No test reaches the network.
- **`work_flow.transition` is the only function that writes task state or mutates Git.**
  `grep -n "_save_tasks\|git commit\|git rebase\|git branch -d\|worktree remove" harness/work_flow.py`
  shows every hit inside `transition()` or a helper it alone calls.
- **The gate exists once.** Every blocker string is produced by `blockers_for` and by
  nothing else; no wrapper, CLI subcommand, or MCP action contains a refusal branch.
- CLI and MCP return identical blockers and stage for the same task in every blocked
  condition.
- `privatize` is all-or-nothing: on a published task it writes nothing at all (D7).
