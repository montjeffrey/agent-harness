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
- This changelog.

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
