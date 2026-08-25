# Worktree Profiles and Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing offline `agent-harness-work` task/worktree command with private and portable profiles, strict handoffs, verification receipts, gated rebase/integration, and safe retirement.

**Architecture:** Keep private task state in the existing machine-local registry under `~/.agent-harness/work/<repository-id>/`. Add small focused modules for layered settings, `.git/info/exclude` updates, strict portable manifests, verification execution, and lifecycle gates; keep `harness/work.py` as the compatibility-preserving CLI/orchestration adapter. Portable state is an explicit, sanitized commit on the task branch and never requires a network call.

**Tech Stack:** Python 3.11+, standard-library `json`/`subprocess`/`pathlib`/`dataclasses`, Git CLI, existing `harness.config_validation`, existing `harness.atomicwrite`, pytest, and the current MCP dispatch layer.

## Global Constraints

- The built-in profile is `private`; the current work device may set that default in machine-local settings.
- Private work creates no committed settings or handoff data.
- A repository-local private artifact is excluded only through `.git/info/exclude`; the root `.gitignore` must remain unchanged.
- The core workflow makes no network call, push, merge, pull request, or hosted-service request.
- Portable handoffs contain only schema version, task ID, objective, repository-relative paths, branch, base SHA, lifecycle status, verification receipts, and next action.
- Portable handoffs reject unknown fields, absolute paths, machine paths, session IDs, plans, logs, credentials, transcripts, and dirty-worktree state.
- Verification commands use an argv list with `shell=False`; repository-supplied commands are never executed automatically.
- Rebase, merge, push, branch deletion, and worktree deletion require explicit commands or flags.
- Existing `agent-harness-work start`, `list`, `diff`, and `finish` behavior and MCP tool names remain compatible unless a new optional argument is being added.
- No new runtime dependency is introduced.
- Every state-changing write is validated first and replaced atomically; failed transitions leave the previous usable state intact.

---

## File map and ownership

Create these focused modules:

- `harness/work_state.py` — shared repository/task models, schema-2 migration, task registry persistence, and the short-lived task lock.
- `harness/work_config.py` — built-in defaults, machine/project settings paths, strict layered settings resolution, and effective-profile reporting.
- `harness/work_exclude.py` — idempotent exact-pattern updates to the common Git directory’s `info/exclude`; never touches root `.gitignore`.
- `harness/work_manifest.py` — portable handoff schema, sanitizer, strict loader, atomic writer, and removal preview.
- `harness/work_verify.py` — trusted local verification profile lookup, argv-only subprocess execution, and verification receipts.
- `harness/work_flow.py` — lifecycle gate state and explicit promote/handoff/resume/rebase/prepare/privatize/retire transitions.

Modify these existing files:

- `harness/work.py` — re-export the shared models/persistence names, retain start/list/diff/finish compatibility, add CLI subcommands, and call the lifecycle module without breaking current callers.
- `harness/mcp_server.py` — expose the lifecycle actions through the existing `dispatch_work` and MCP decorators.
- `README.md` — document private default, portable promotion, lifecycle commands, and the no-root-`.gitignore` rule.

Create or extend these tests:

- `tests/test_work_config.py` — settings precedence, schema rejection, and effective-profile source.
- `tests/test_work_exclude.py` — exact local exclusion behavior and root `.gitignore` preservation.
- `tests/test_work_manifest.py` — portable field allowlist, path/branch/base validation, round-trip, and atomic failure behavior.
- `tests/test_work_verify.py` — argv-only execution, receipt recording, timeout/nonzero result, and untrusted-command refusal.
- `tests/test_work.py` — existing regression tests plus lifecycle, profile, state migration, two-clone resume, rebase recovery, and retirement gates.

## Shared interfaces

The implementation tasks use these exact contracts:

```python
# harness/work_config.py
PROFILE_PRIVATE = "private"
PROFILE_PORTABLE = "portable"

@dataclass(frozen=True)
class EffectiveSettings:
    profile: str
    profile_source: str
    portable_allowed: bool
    worktree_root: Path | None
    verification_profiles: Mapping[str, tuple[str, ...]]

def load_settings(
    project_root: Path,
    profile_override: str | None = None,
) -> EffectiveSettings: ...

# harness/work_exclude.py
def ensure_local_exclude(common_git_dir: Path, pattern: str) -> Path: ...
def remove_local_exclude(common_git_dir: Path, pattern: str) -> Path: ...

# harness/work_manifest.py
HANDOFF_SCHEMA = 1

@dataclass(frozen=True)
class PortableHandoff:
    task_id: str
    summary: str
    paths: tuple[str, ...]
    branch: str
    base_sha: str
    status: str
    verification: tuple[dict[str, object], ...]
    next_action: str

def handoff_path(project_root: Path, task_id: str) -> Path: ...
def load_handoff(path: Path) -> PortableHandoff: ...
def write_handoff(path: Path, handoff: PortableHandoff) -> None: ...
def remove_handoff(path: Path) -> None: ...

# harness/work_verify.py
@dataclass(frozen=True)
class VerificationReceipt:
    name: str
    returncode: int
    passed: bool
    commit: str
    completed_at: str

def run_verification(
    worktree: Path,
    commit: str,
    name: str,
    argv: Sequence[str],
    timeout_seconds: float = 300.0,
) -> VerificationReceipt: ...

# harness/work_flow.py
@dataclass(frozen=True)
class GateStatus:
    task_id: str
    profile: str
    phase: str
    blockers: tuple[str, ...]
    next_actions: tuple[str, ...]

def status(directory: Path, task_id: str) -> GateStatus: ...
def resume(directory: Path, task_id: str, session_id: str, agent: str = "codex") -> Task: ...
def promote(directory: Path, task_id: str, confirm: bool) -> Task: ...
def handoff(directory: Path, task_id: str, confirm: bool) -> Task: ...
def prepare_integrate(directory: Path, task_id: str, confirm: bool) -> Task: ...
def privatize(directory: Path, task_id: str, confirm: bool) -> Task: ...
def rebase(directory: Path, task_id: str, onto: str) -> Task: ...
def retire(directory: Path, task_id: str, target: str, delete_branch: bool) -> Task: ...
def advance(directory: Path, task_id: str) -> GateStatus: ...
```

`Task` remains the public model currently imported from `harness.work`; if it
is moved to `harness.work_state`, `harness.work` must re-export it. Add
`profile`, `phase`, and verification receipts with defaults/migration so
existing schema-1 local task files continue to load.

---

### Task 1: Add layered settings and profile validation

**Files:**
- Create: `harness/work_config.py`
- Create: `tests/test_work_config.py`

**Interfaces:**
- Consumes: `config_validation.load_json_object`, `Path.home()`, and the project root passed by `work.py`.
- Produces: `EffectiveSettings`, `load_settings`, `PROFILE_PRIVATE`, and `PROFILE_PORTABLE` for all later tasks.

- [ ] **Step 1: Write failing settings tests.**

  Add tests proving that an absent machine/project config resolves to
  `profile == "private"` with `profile_source == "built-in"`; a machine
  `settings.json` overrides the built-in; an optional project
  `.agent-harness/work-settings.json` overrides the earlier default; and a
  CLI `profile_override` wins over both.

  Add rejection tests for schema `2`, an unknown `work` key, a profile other
  than `private`/`portable`, project-defined verification commands, and a
  malformed JSON file. Use `monkeypatch.setattr(Path, "home", ...)` so tests
  never read the real user profile.

- [ ] **Step 2: Run the focused tests and confirm they fail.**

  Run:

  ```text
  python -m pytest -q tests/test_work_config.py
  ```

  Expected: collection fails because `harness.work_config` and
  `EffectiveSettings` do not exist.

- [ ] **Step 3: Implement strict layered resolution.**

  Define `schema == 1`, read only the machine file if it exists, read the
  project file only if it exists, and validate each object before merging.
  Permit machine `work.profile`, `work.portable_allowed`,
  `work.worktree_root`, and `work.verification_profiles`; permit project
  `work.profile` and `work.portable_allowed` only. Store verification values
  as tuples of argv strings. Resolve a relative `worktree_root` from the
  machine config’s parent, and reject empty argv entries, shell strings, and
  non-string values.

  Apply the override order `built-in → machine → project → CLI`, set
  `profile_source` to the winning layer, and default `portable_allowed` to
  `False`. Return a frozen dataclass; do not write a config file from a read.

- [ ] **Step 4: Run the focused tests and confirm they pass.**

  Run:

  ```text
  python -m pytest -q tests/test_work_config.py
  ```

  Expected: all settings tests pass.

- [ ] **Step 5: Commit the settings boundary.**

  ```text
  git add harness/work_config.py tests/test_work_config.py
  git commit -m "feat: add private-first work profiles"
  ```

### Task 2: Route private exclusions through `.git/info/exclude`

**Files:**
- Create: `harness/work_exclude.py`
- Create: `tests/test_work_exclude.py`

**Interfaces:**
- Consumes: `Repository.common_git_dir` and `atomicwrite.atomic_sibling`.
- Produces: idempotent `ensure_local_exclude`/`remove_local_exclude` for private lifecycle code.

- [ ] **Step 1: Write failing exclusion tests.**

  Create a temporary Git repository with a tracked root `.gitignore` and a
  seeded `.git/info/exclude`. Assert that:

  ```python
  ensure_local_exclude(common_git_dir, ".agent-harness/private.json")
  ensure_local_exclude(common_git_dir, ".agent-harness/private.json")
  ```

  adds exactly one line to `info/exclude`, preserves the seed, leaves the
  tracked `.gitignore` byte-for-byte unchanged, and causes `git check-ignore`
  to report `.git/info/exclude` as the source. Test removal deletes only the
  exact managed line and a missing line is a no-op.

- [ ] **Step 2: Run the exclusion tests and confirm they fail.**

  ```text
  python -m pytest -q tests/test_work_exclude.py
  ```

  Expected: import or attribute failures for `harness.work_exclude`.

- [ ] **Step 3: Implement idempotent, atomic local exclusion.**

  Normalize only line endings, require a non-empty repository-relative
  pattern, preserve all existing bytes except the exact line being managed,
  and ensure one trailing newline. Use `atomic_sibling` for replacement.
  Never call an installer helper that edits root `.gitignore`; never remove a
  line that does not exactly equal the requested pattern.

- [ ] **Step 4: Run the exclusion tests and confirm they pass.**

  ```text
  python -m pytest -q tests/test_work_exclude.py
  ```

- [ ] **Step 5: Commit the local-permission boundary.**

  ```text
  git add harness/work_exclude.py tests/test_work_exclude.py
  git commit -m "feat: keep private work exclusions local"
  ```

### Task 3: Version local task state without breaking existing tasks

**Files:**
- Create: `harness/work_state.py`
- Modify: `harness/work.py`
- Modify: `tests/test_work.py`

**Interfaces:**
- Consumes: the existing `Repository`, `Task`, `_load_tasks`, `_save_tasks`, `_state_lock`, and `repository_for` behavior.
- Produces: schema-2 task loading with schema-1 migration, profile/phase/receipt fields, and the unchanged `work.WorkError`, `work.Task`, `work.ACTIVE`, `work.DONE`, `work.start`, `work.list_tasks`, `work.find_task`, `work.diff`, and `work.finish` names.

- [ ] **Step 1: Add migration and compatibility tests.**

  Seed `~/.agent-harness/work/.../tasks.json` with the current schema-1
  payload and assert `_load_tasks` returns a task whose `profile` is
  `private`, `phase` is `active`, and `verification` is empty. Add a schema-2
  round-trip test for a portable task with one receipt. Keep all current
  `tests/test_work.py` assertions unchanged.

- [ ] **Step 2: Run the existing work tests and confirm the new migration test fails.**

  ```text
  python -m pytest -q tests/test_work.py
  ```

  Expected: the new migration/field assertions fail while the current suite
  remains the regression baseline.

- [ ] **Step 3: Implement schema-2 normalization and atomic persistence.**

  Set the local task schema to `2`. Accept schema `1` and normalize missing
  `profile`, `phase`, and `verification` fields before constructing `Task`;
  reject all other schema values. Use `atomic_sibling` in `_save_tasks` so a
  killed process cannot leave a partially replaced registry. Add defaults so
  existing callers constructing `Task` continue to work. Preserve the current
  repository ID, lock, path-claim, and rollback behavior.

- [ ] **Step 4: Run the complete work regression file.**

  ```text
  python -m pytest -q tests/test_work.py
  ```

  Expected: all existing and migration tests pass.

- [ ] **Step 5: Commit the state contract.**

  ```text
  git add harness/work.py harness/work_state.py tests/test_work.py
  git commit -m "refactor: version work task state"
  ```

### Task 4: Add strict portable handoffs

**Files:**
- Create: `harness/work_manifest.py`
- Create: `tests/test_work_manifest.py`

**Interfaces:**
- Consumes: schema-2 `Task`, `VerificationReceipt`, `config_validation.parse_json_object`, and `atomicwrite.atomic_sibling`.
- Produces: `PortableHandoff`, `handoff_path`, strict load/write/remove functions for lifecycle transitions.

- [ ] **Step 1: Write failing manifest tests.**

  Test a valid handoff round-trip with these exact safe fields:

  ```python
  {
      "schema": 1,
      "task_id": "work-20260824-a1b2c3d4",
      "summary": "Update the parser",
      "paths": ["harness/parser.py", "tests/test_parser.py"],
      "branch": "codex/work-20260824-a1b2c3d4",
      "base_sha": "a" * 40,
      "status": "portable-active",
      "verification": [],
      "next_action": "run local verification",
  }
  ```

  Add rejection tests for an unknown key, absolute path, `..` path,
  `session_id`, machine plan path, malformed JSON, wrong schema, empty task
  ID, and a verification item containing a command argv or output. Assert
  that `write_handoff` uses an atomic sibling and leaves no temp file when
  replacement fails.

- [ ] **Step 2: Run manifest tests and confirm they fail.**

  ```text
  python -m pytest -q tests/test_work_manifest.py
  ```

- [ ] **Step 3: Implement the allowlisted manifest codec.**

  Use a frozen dataclass and an explicit key set. Reuse
  `work.normalize_paths` for repository-relative path rules, enforce a
  40-character lowercase/hex base SHA, require a non-empty next action, and
  retain verification receipts only as `{name, returncode, passed, commit,
  completed_at}`. `load_handoff` must reject unknown keys before constructing
  the dataclass. `write_handoff` must create only the parent handoff
  directory and atomically replace the JSON file. `remove_handoff` must be
  explicit and idempotent.

- [ ] **Step 4: Run manifest tests and confirm they pass.**

  ```text
  python -m pytest -q tests/test_work_manifest.py
  ```

- [ ] **Step 5: Commit the portable data contract.**

  ```text
  git add harness/work_manifest.py tests/test_work_manifest.py
  git commit -m "feat: add strict portable work handoffs"
  ```

### Task 5: Add local verification receipts

**Files:**
- Create: `harness/work_verify.py`
- Create: `tests/test_work_verify.py`

**Interfaces:**
- Consumes: `EffectiveSettings.verification_profiles`, a task worktree, and the Git commit resolved immediately before execution.
- Produces: `VerificationReceipt` and `run_verification` for `work verify` and portable handoff gates.

- [ ] **Step 1: Write failing verification tests.**

  Monkeypatch `subprocess.run` and assert the command receives a list argv,
  `cwd=worktree`, `shell=False`, `check=False`, captured text output, and the
  configured timeout. Assert a zero exit creates `passed is True`; a nonzero
  exit creates `passed is False` without raising; and a timeout creates a
  failed receipt with a deterministic return code. Assert the receipt stores
  only the profile name, result, commit, and completion timestamp—not stdout,
  stderr, session ID, or absolute paths.

- [ ] **Step 2: Run verification tests and confirm they fail.**

  ```text
  python -m pytest -q tests/test_work_verify.py
  ```

- [ ] **Step 3: Implement argv-only verification.**

  Resolve the named profile from machine settings, reject a missing profile,
  empty argv, or a project-defined command, and call
  `subprocess.run(list(argv), cwd=worktree, shell=False, capture_output=True,
  text=True, check=False, timeout=timeout_seconds)`. Convert timeout to a
  failed receipt, resolve the current commit before invocation, and return a
  receipt without persisting command output. Add a state helper that appends
  the receipt under the repository task lock and writes the registry
  atomically.

- [ ] **Step 4: Run verification tests and confirm they pass.**

  ```text
  python -m pytest -q tests/test_work_verify.py
  ```

- [ ] **Step 5: Commit verification evidence.**

  ```text
  git add harness/work_verify.py tests/test_work_verify.py
  git commit -m "feat: record offline work verification"
  ```

### Task 6: Implement lifecycle gates and explicit transitions

**Files:**
- Create: `harness/work_flow.py`
- Modify: `harness/work.py`
- Modify: `tests/test_work.py`

**Interfaces:**
- Consumes: `work_config`, `work_exclude`, `work_manifest`, `work_verify`, the existing Git/worktree helpers, and schema-2 task state.
- Produces: `GateStatus`, `resume`, `status`, `advance`, `promote`, `handoff`, `rebase`, `prepare_integrate`, `privatize`, and `retire`.

- [ ] **Step 1: Write failing gate tests.**

  Add tests for these exact transitions:

  - `status` on a new private task reports `private-active` and says to
    commit/discard changes before promotion.
  - `promote(..., confirm=False)` prints/returns a preview without changing
    Git, and `confirm=True` refuses a dirty worktree.
  - A clean committed task can be promoted only when portable is allowed;
    the resulting commit contains only `.agent-harness/handoffs/<id>.json`.
  - `resume` from a fetched portable branch validates current branch and base
    SHA, creates local state with the new session ID, and refuses a mismatch.
  - A failed verification blocks `handoff`, `rebase`, and
    `prepare_integrate`; a successful receipt permits them.
  - `rebase` creates `refs/agent-harness/recovery/<id>/<timestamp>` before
    running Git, never fetches, clears old receipts after success, and leaves
    the recovery ref/task state intact on conflict.
  - `prepare_integrate` removes only the handoff manifest in an explicit
    commit; it refuses private tasks and unverified/dirty tasks.
  - `privatize` removes future portable updates but cannot erase prior Git
    history.
  - `retire` refuses dirty state, an unmerged target, or a missing successful
    verification; with `delete_branch=False` it removes only the external
    worktree, and with `delete_branch=True` it deletes the branch only after
    `merge-base --is-ancestor` succeeds.

- [ ] **Step 2: Run the focused gate tests and confirm they fail.**

  ```text
  python -m pytest -q tests/test_work.py -k "status or resume or promote or handoff or rebase or integrate or privatize or retire"
  ```

- [ ] **Step 3: Implement the gate state machine.**

  Use explicit phases `private-active`, `portable-active`,
  `portable-verified`, `rebased-ready`, and `integrated`. Compute blockers
  from current branch, worktree cleanliness, receipts, manifest validity,
  target ancestry, and task profile. `advance` may perform only local state
  updates and recovery-ref creation; it must return the next action instead of
  invoking push, merge, rebase, or deletion implicitly.

  Implement promotion and handoff as preview-then-confirm operations. Stage
  only the manifest path for metadata commits. Implement rebase with the
  explicit `--onto` ref and a recovery ref first. On a successful rebase,
  resolve the new base SHA and clear receipts because the tested commit has
  changed. Implement retire using `git worktree remove` only for a recorded
  external worktree and use `git branch -d` only after the named target proves
  the branch is an ancestor.

- [ ] **Step 4: Add CLI parsing and output in `harness/work.py`.**

  Preserve the existing commands and add:

  ```text
  resume TASK_ID --session SESSION [--agent AGENT]
  status TASK_ID
  verify TASK_ID --profile PROFILE
  promote TASK_ID --portable --confirm
  handoff TASK_ID --confirm
  rebase TASK_ID --onto REF
  prepare-integrate TASK_ID --confirm
  privatize TASK_ID --confirm
  advance TASK_ID
  retire TASK_ID --target REF [--delete-branch]
  ```

  Render every result with the existing `status`, `summary`, `next_actions`,
  and `artifacts` keys. A missing confirmation, invalid transition, Git
  conflict, or failed gate returns exit code `2` with a concrete recovery
  action; a verification command returns its own pass/fail result without
  hiding the exit code.

- [ ] **Step 5: Run the complete work test file and confirm it passes.**

  ```text
  python -m pytest -q tests/test_work.py
  ```

- [ ] **Step 6: Commit the lifecycle engine.**

  ```text
  git add harness/work.py harness/work_flow.py tests/test_work.py
  git commit -m "feat: gate worktree lifecycle transitions"
  ```

### Task 7: Expose lifecycle actions through MCP and documentation

**Files:**
- Modify: `harness/mcp_server.py`
- Modify: `tests/test_work.py`, which already owns `dispatch_work` coverage for work actions.
- Modify: `README.md` around the existing “Parallel task workspaces” section at lines 537–559.

**Interfaces:**
- Consumes: the public functions from `harness.work`/`harness.work_flow` and the existing `dispatch_work` response shape.
- Produces: MCP actions `work_resume`, `work_verify`, `work_promote`, `work_handoff`, `work_rebase`, `work_prepare_integrate`, `work_privatize`, `work_advance`, and `work_retire`; existing four work tools remain available.

- [ ] **Step 1: Write failing MCP tests in `tests/test_work.py`.**

  Exercise `dispatch_work` for a private `work_status` response, a portable
  promotion preview, a verification receipt, a rebase refusal without an
  explicit target, and retire refusal before merge. Assert every response
  contains the current status/summary/next_actions/artifacts fields and never
  contains a machine plan path in a portable response.

- [ ] **Step 2: Run the MCP tests and confirm the new actions fail.**

  ```text
  python -m pytest -q tests/test_work.py -k mcp
  ```

- [ ] **Step 3: Register MCP descriptions and dispatch branches.**

  Add one `WORK_TOOL_DESCRIPTIONS` entry and one typed decorated function per
  new action. Keep `work_status` backward-compatible when no `task_id` is
  provided; with a task ID it returns the gate state and next actions. Parse
  paths and booleans exactly as the existing dispatcher does, and convert
  `WorkError` to the existing recoverable `status: error` shape.

- [ ] **Step 4: Update README usage and security guidance.**

  Document the machine/project settings locations, private default, local
  `.git/info/exclude` rule, promotion requirements, two-device resume flow,
  command sequence, and explicit push/merge/delete boundaries. Keep examples
  offline and use the concrete task ID `work-20260824-a1b2c3d4` in examples.

- [ ] **Step 5: Run MCP and documentation checks.**

  ```text
  python -m pytest -q tests/test_work.py -k mcp
  python -m pytest -q tests/test_work.py
  git diff --check
  ```

- [ ] **Step 6: Commit the public surface.**

  ```text
  git add harness/mcp_server.py tests/test_work.py README.md
  git commit -m "feat: expose gated work lifecycle actions"
  ```

### Task 8: Run the full verification matrix and record the handoff

**Files:**
- Modify: none; fixes exposed by this matrix belong in the task that owns the failing behavior.
- Test: `tests/test_work_config.py`, `tests/test_work_exclude.py`, `tests/test_work_manifest.py`, `tests/test_work_verify.py`, and `tests/test_work.py`.

**Interfaces:**
- Consumes: all completed lifecycle modules and the existing test fixtures.
- Produces: repeatable local evidence that private mode does not alter root `.gitignore`, portable mode round-trips across clones, and existing work behavior remains green.

- [ ] **Step 1: Run the focused lifecycle matrix.**

  ```text
  python -m pytest -q tests/test_work_config.py tests/test_work_exclude.py tests/test_work_manifest.py tests/test_work_verify.py tests/test_work.py -k work
  ```

  Expected: all selected tests pass.

- [ ] **Step 2: Run the complete repository suite with the isolated temp root when needed.**

  ```text
  $env:TEMP='C:\tmp'; $env:TMP='C:\tmp'; python -m pytest -q
  ```

  Expected: no new failures; report existing platform-specific skips and any
  command-runner timeout separately from pytest failures.

- [ ] **Step 3: Run static/packaging checks.**

  ```text
  python -m compileall -q harness
  python -m build
  git diff --check
  git status --short --branch
  ```

  Expected: compilation/build succeed, whitespace is clean, and only the
  intended implementation commits are ahead of the rebased main branch.

- [ ] **Step 4: Record the verification result in the portable handoff only when the task is explicitly portable.**

  Private mode records the result only in the machine-local registry. Portable
  mode may update its sanitized handoff after the user reviews the receipt;
  no command in this step pushes or merges anything.

## Plan self-review checklist

- Settings, local exclusion, state migration, manifest, verification, gates,
  MCP, documentation, and regression testing each have an owning task.
- Private mode never creates a project settings file or root `.gitignore`
  change; portable mode is explicit and branch-local.
- No task requires network access or executes repository-supplied commands.
- Existing `work.py` public names and four MCP actions remain compatible.
- Rebase recovery, conflict pause, prepare-integrate cleanup, and retire
  merged-proof gates are covered by tests.
- No unresolved marker or “implement later” step remains in the plan.
