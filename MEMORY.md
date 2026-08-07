# Project Memory

## 2026-07-20 — Phase 2 telemetry integration

- Added non-blocking `doctor` diagnostics for telemetry storage and supported
  Claude Code/Codex log sources. The checks are side-effect-free and return
  only `INFO` or `WARN`.
- Added Phase 2 operator documentation covering manual local collection,
  JSONL storage, filters, environment overrides, supported collectors, and
  source-dependent cost/token/rate-limit availability.
- Reconciled `ROADMAP.md` with the approved Phase 2 scope: Claude Code and
  Codex are supported; Gemini/Antigravity is excluded because no sufficiently
  stable, privacy-safe local source was approved.
- Extended the existing multi-OS GitHub Actions job to smoke-test the
  installed `agent-harness-telemetry tail` and `summary` entry points outside
  the checkout.
- Preserved the existing `agent-harness-telemetry` console-script declaration
  and added no dependencies, installer flags, or AGENTS template changes.

### Local verification

- Focused Phase 2 suite before final edge-case additions: `98 passed`.
- Final doctor suite: `22 passed, 2 skipped`; the skips exercise POSIX-only
  directory permission semantics and are expected on Windows.
- Complete suite with `TEMP`/`TMP` set to the isolated `C:\tmp` root:
  `219 passed, 4 skipped`. Two skips are the doctor checks above and two are
  Windows-only skips for POSIX graph-refresh file permission checks.
- `python -m build`: wheel and source distribution built successfully.
- `python -m compileall -q harness`: passed.
- Telemetry CLI help matched the documented `collect`, `tail`, `summary`,
  `-n`, `--agent`, and `--project` interface.
- Installed-wheel telemetry `tail` and `summary` smoke checks exited 0 with an
  empty store.

The default Windows pytest temp directory is beneath an unrelated
home-directory Git repository on this machine, which makes three
graph-location isolation tests inherit the wrong repository boundary. Those
tests pass `24/24` under the isolated `C:\tmp` root; no Phase 1 code was
changed.

### Pending external verification

- The configured GitHub Actions matrix covers Ubuntu, Windows, and macOS on
  Python 3.11 through 3.14. A new remote run is pending until these changes
  are committed and pushed; do not describe the remote matrix as green yet.
