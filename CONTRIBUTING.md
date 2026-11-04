# Contributing to agent-harness

Thanks for looking at agent-harness. This guide covers how to run the checks
locally and what a pull request should look like. Please read the
[Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## Supported versions

CI (`.github/workflows/ci.yml`) runs the test job on Python 3.11, 3.12, 3.13 and
3.14, on Ubuntu, Windows and macOS. `pyproject.toml` requires Python 3.11 or
newer. The `harness/` package uses the standard library only; keep it that way
unless an optional extra in `pyproject.toml` already covers the dependency.

## Build and test

These are the commands the CI `test` job runs:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip install build setuptools wheel
python -m pytest tests/ -q
python -m build
```

CI then installs the built wheel into a clean virtual environment and runs
`python -m harness.doctor` and `agent-harness-install ... --skip-checks` from
outside the checkout. If you change packaging, the installer or the console
scripts, run the same smoke test (the steps are in `.github/workflows/ci.yml`).

## Pull requests

- Branch from `main` and open a pull request against `main`.
- Keep each pull request small and focused on one change. Unrelated cleanups
  belong in their own pull request.
- Add or update tests in `tests/` for any behavior change or bug fix.
- Update the README, `docs/` or `CHANGELOG.md` when user-visible behavior
  changes.
- CI must pass on all matrix entries before a pull request is merged.
- Use clear commit messages. Conventional prefixes such as `feat:`, `fix:`,
  `docs:` and `ci:` match the existing history.
- Explain what changed and why in the pull request description.

## Reporting bugs and requesting features

Use the issue forms under the Issues tab. Include the Python version, your
operating system, and the exact command you ran. Do not paste secrets or
private file contents.

## Security

Do not report security problems in a public issue or pull request. This
repository has no SECURITY.md, so use GitHub's private vulnerability reporting:
open the Security tab and choose "Report a vulnerability".
