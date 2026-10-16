# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Release policy**: the version is single-sourced in `pyproject.toml`'s
`[project].version` -- there is no second place a version number lives. A
release is exactly one commit that (a) bumps that version and (b) finalizes
this file's `[Unreleased]` section into a new dated `[X.Y.Z]` section,
followed by an annotated git tag `vX.Y.Z` on that commit. The tag is the
human's release act, never an agent's. The `release.yml` GitHub Actions
workflow enforces tag/version equality at build time (it reads
`[project].version` from `pyproject.toml` via `tomllib` and exits 1 if the
pushed tag `vX.Y.Z` doesn't match) before anything is published.

## [Unreleased]

### Added

- Automatic session hand-off (`harness/handoff.py`). A new Stop/SessionEnd
  hook, `hooks/claude/handoff_capture_hook.py`, writes one proposed entry
  group per session from evidence alone: the files changed since session
  start (a working-tree snapshot taken by the SessionStart hook into a
  throwaway index), the build/test commands and tool calls from the
  transcript, `ANSWER:` lines and ANSWER.md, and decisions and open items
  extracted by fixed rules from the final message. No model call, never the
  user's prompt, secret-shaped lines dropped, and a later turn replaces the
  session's still-unreviewed group instead of piling up. The SessionStart hook
  now pushes the newest hand-offs (up to three sessions, 1,500 chars,
  `AGENT_HARNESS_HANDOFF_MAX_CHARS`, 0 disables) after the table of contents,
  which no longer lists those ids. Registered at user scope and in the
  project settings fragment.

- Cross-machine usage benchmarking (see
  `docs/benchmarks/2026-10-14-usage-baseline.md`). Telemetry schema 2 adds
  `machine` (a random id in `~/.agent-harness/machine.json`, never the
  hostname), the cache write/read split of `input_tokens`, and tool-call and
  `mcp__agent-harness__*` call counts; schema-1 records are still read.
  `agent-harness-telemetry export --to DIR` writes
  `DIR/<machine>/events-YYYYMM.jsonl` with local paths dropped and projects
  keyed by a hash of their git remote. `agent-harness-telemetry baseline`
  reports per-session rollups and KPIs K1-K6 from the local store or a merged
  export (`--from`), by machine, project or month. `machine --label` names a
  machine. The SessionStart hook runs `collect` in the background at most once
  a day (`AGENT_HARNESS_TELEMETRY_AUTO=0` turns it off).
  `machine --export-to DIR` sets an opt-in export target (kept in
  `machine.json`, never exported); with one set, that daily run is
  `collect --then-export`, which exports under the collect lock and records
  its outcome for `machine` to show, so synced folders fill hands-off.

- Evidence-bounded automatic memory curation: SessionStart and
  `memory_context` now verify and accept unchanged ingested document pointers,
  reject exact duplicate proposals, and expose only the remaining exception
  queue through `agent-harness-memory curate` / `memory_curate`.
- `agent-harness-work` and four MCP work tools (`work_start`, `work_status`,
  `work_diff`, `work_finish`): local-only task records with session-level path
  claims, private plans, immutable diff bases, branches, and conditional
  external worktrees for parallel implementation.
- `harness/install_cli.py`: the installer's implementation, now a proper
  package module (previously only reachable as the repo-root `install.py`
  script, which was excluded from both the wheel and the sdist). Reads its
  packaged resources (`AGENTS.harness.md`, `feature-blueprint.md`,
  `settings-fragment.json`) via `importlib.resources` so it resolves
  correctly both from a source checkout and once installed into
  site-packages.
- `agent-harness-install` console script (`harness.install_cli:main`),
  the pip-installed equivalent of `python install.py`.
- `.github/workflows/release.yml`: tag-triggered release workflow
  (`v*` tags) that builds the wheel/sdist, verifies the tag matches
  `[project].version`, runs the full test suite, and publishes to TestPyPI
  then PyPI via OIDC trusted publishing -- no API-token secrets anywhere in
  the workflow.
- `harness/doctor.py`: the installed-resources check now also verifies
  `harness.install_cli` is importable, FAILing with a "broken wheel build?"
  hint if not.
- `harness/bootstrap_register.py` + tests: user-scope registration of the
  SessionStart/PreToolUse hooks (`~/.claude/settings.json`) and the
  `agent-harness` MCP server (`~/.claude.json`), extracted from
  `bootstrap.ps1`. Idempotent, validates before writing, writes atomically,
  and rewrites a stale harness entry in place instead of skipping it so a
  reinstall cannot leave a hook pointing at a deleted venv. Also usable
  directly on macOS/Linux, where the manual install path previously had no
  registration step at all.
- `graph` optional-dependency group (`graphifyy`), so graph refresh is
  installable in the same step as everything else. Previously the only route
  was a follow-up `pipx inject`, which does not survive the
  `pipx install --force` that re-running bootstrap performs -- a machine set
  up that way silently lost graph refresh on its next bootstrap.
- `harness/memory_ingest.py` + `agent-harness-memory ingest` (also wired into
  `agent-harness-enroll` as step `[2/4] docs`): seeds project memory with
  pointer entries -- never copies -- from the README, ROADMAP, CHANGELOG, and
  ADRs, and seeds `scope.md`'s Purpose from the README's opening paragraph so
  an untouched template doesn't permanently pin the project to deterministic
  (non-semantic) selection. Idempotent via a content digest stored on each
  entry, so a rerun only proposes what actually changed.
- `harness/memory_claims.py` + `agent-harness-memory claim` + MCP
  `memory_claim`/`memory_release` tools: lets an agent program (`claude`,
  `codex`, or `agy`) claim an open task or blocker so parallel sessions don't
  pick up the same work. `memory_tasks` now shows each item's claim status.
  A claim held more than `CLAIM_STALE_HOURS` (4h) is presumed abandoned and
  can be taken over by another owner.
- This changelog.

### Fixed

- The Claude Code telemetry collector counted each API response once per
  transcript line (a response is often written as several lines with one
  `message.id`). It also skipped subagent transcripts and failed to open
  paths past 260 characters on Windows. Usage is now counted once per
  message id, subagent turns are included and marked, and history that a
  resumed session copies from its parent is no longer counted twice.
- `hooks/claude/memory_context_hook.py` returned silently (an empty string)
  when a project's memory store was enrolled but had zero entries, making
  "not wired" and "wired but idle" indistinguishable from the operator's
  seat. It now reports which case applies, and points at `ingest`/`remember`.
  `harness/doctor.py`'s memory-store check gained the same distinction --
  WARN with entry counts instead of a bare INFO for an empty-but-enrolled
  store.

- `bootstrap.ps1` aborted on stock Windows. The step-5 settings merge used
  `ConvertFrom-Json -AsHashtable`, which is PowerShell 6+ only, so under
  Windows PowerShell 5.1 it threw and -- with `$ErrorActionPreference =
  'Stop'` -- killed the run *after* the pipx install had succeeded. The
  machine was left with working `agent-harness-*` commands and no hooks
  registered, with nothing reporting a failure. The merge now runs through
  `harness.bootstrap_register`, so one implementation covers 5.1, 7, and
  POSIX.
- `bootstrap.ps1` never registered the MCP server despite its step heading
  and closing banner both saying it did; `mcpServers` appeared nowhere in
  the script.
- `bootstrap.ps1` installed only the `semantic` extra, so the MCP server it
  advertised could not start -- `agent-harness-mcp` exited immediately on a
  missing `mcp` SDK. The install spec now always includes `mcp` and `graph`,
  leaving `-NoSemantic` as the one opt-out that matters (it is the extra that
  costs hundreds of megabytes).
- `tests/test_semantic_install_guidance.py` pinned one exact extras list in
  the README's pipx line, so adding an extra failed a test whose actual
  subject is "install from the local path, never the unpublished PyPI name".
  It now matches the local path with `semantic` present among the extras.

### Changed

- `install.py` (repo root) is now a thin shim that re-exports
  `harness.install_cli`'s public names, kept for `./setup.sh` / `.\setup.ps1`
  and any existing `python install.py ...` muscle memory in a source
  checkout.
- `README.md` Quickstart: leads with `pipx install agent-harness` +
  `agent-harness-install`; the git-clone + `setup.sh`/`setup.ps1` flow moves
  to an "Install from source" subsection.
- `MANIFEST.in`: `install.py` is no longer excluded from the sdist (it's the
  shim described above, and the sdist should contain full source).

## [0.1.0] - 2026-07-15

Initial tagged release: portable code-relations query, graph refresh, and
advisory blueprint-gate installer for coding agents.
