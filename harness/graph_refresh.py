"""Refresh a graphify code graph, cross-platform.

Default (fast): re-extracts AST via `graphify update`, then re-names
communities via `graphify label`. `graphify update` alone wipes community
names, so the labeling pass is not optional.

Full rebuild: re-runs LLM documentation of every chunk via `graphify extract`
before the update/label steps.

Usage:
    python graph_refresh.py <repo-root> [--full] [--backend BACKEND]

Backend resolution order: --backend flag, then HARNESS_GRAPH_BACKEND env var,
then "auto".

API key resolution: the backend's required env var (see BACKEND_ENV_VARS)
must already be set, OR ~/.agent-harness/config.json may define
{"key_command": "<shell command>", "key_env": "<ENV_VAR_NAME>"} -- the command
is run and its stdout (stripped) is exported as that env var. The key value
is never printed. ~/.agent-harness/config.json must be owned by the current
user and must not be group- or world-writable (enforced on POSIX; see
`_check_config_file_trust`) -- `key_command` runs via a shell, so a
writable-by-others config file is a privilege-escalation vector.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from harness import pathsafe

log = logging.getLogger(__name__)

VIZ_NODE_LIMIT = "10000"
DEFAULT_CONFIG_PATH = Path.home() / ".agent-harness" / "config.json"

# "auto" is graphify's own default backend-selection mode -- it is never a
# real `--backend` value the CLI accepts, so it must never be passed through.
AUTO_BACKEND = "auto"

# Known graphify backends and the API-key env var each one expects. Backends
# not listed here (e.g. "auto") are assumed to manage their own credentials.
BACKEND_ENV_VARS: dict[str, str] = {
    "nvidia-nim": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


@dataclass(frozen=True)
class GraphifyStatus:
    """Result of probing whether graphify is installed and runnable."""

    available: bool
    version: str | None
    error: str | None = None


def detect_graphify() -> GraphifyStatus:
    """Probe for a usable `graphify` installation without running a build.

    Never raises -- an unavailable or broken graphify is reported back as a
    `GraphifyStatus`, so callers can degrade with a clear message instead of
    crashing on the first real subprocess failure.

    ASSUMPTION TO VERIFY: this probe assumes `python -m graphify --version`
    is a safe, fast, side-effect-free command that exits 0 with version text
    on stdout for a working install. That has not been confirmed against a
    real graphify CLI (no paid API calls are made in this codebase's tests)
    -- if the real CLI's `--version` behaves differently (e.g. non-zero exit
    on success, or a slow/side-effectful invocation), this detection needs
    to change to match.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "graphify", "--version"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GraphifyStatus(available=False, version=None, error=str(exc))
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        return GraphifyStatus(available=False, version=None, error=stderr)
    return GraphifyStatus(available=True, version=result.stdout.strip() or None)


def _check_config_file_trust(config_path: Path) -> None:
    """POSIX-only: refuse to read a config file that is group/world-writable
    or not owned by the current user.

    `key_command` is executed via `shell=True` (see `ensure_api_key`), so a
    config file that another local user could edit is a privilege-escalation
    vector -- whoever can write to it can get their shell command executed
    as this user. No-op on Windows: POSIX file-mode bits (`st_mode`,
    `os.getuid()`) don't map cleanly onto Windows ACLs.
    """
    if os.name == "nt":
        return
    try:
        st = config_path.stat()
    except OSError:
        return
    if st.st_uid != os.getuid():
        raise SystemExit(
            f"{config_path} is not owned by the current user; refusing to "
            "read it (key_command would run as you but be controlled by "
            "another user). Fix ownership before retrying."
        )
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SystemExit(
            f"{config_path} is group- or world-writable; refusing to read "
            f"it (key_command execution risk). Run `chmod 600 {config_path}` "
            "before retrying."
        )


def load_user_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if not config_path.exists():
        return {}
    _check_config_file_trust(config_path)
    try:
        with config_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{config_path} must contain a JSON object")
    return data


def ensure_api_key(backend: str, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Ensure the backend's API key env var is set, sourcing it if configured."""
    env_var = BACKEND_ENV_VARS.get(backend)
    if env_var is None:
        log.info("backend '%s' has no known key requirement; skipping key check", backend)
        return
    if os.environ.get(env_var):
        return

    config = load_user_config(config_path)
    key_command = config.get("key_command")
    key_env = config.get("key_env")
    if not key_command or key_env != env_var:
        raise SystemExit(
            f"{env_var} is not set and no matching key_command found in {config_path}. "
            f'Set {env_var} directly, or add {{"key_command": "...", "key_env": "{env_var}"}} '
            "to that file."
        )

    result = subprocess.run(
        key_command, shell=True, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"key_command for {env_var} failed (exit {result.returncode})")
    value = result.stdout.strip()
    if not value:
        raise SystemExit(f"key_command for {env_var} produced no output")
    os.environ[env_var] = value
    log.info("sourced %s from key_command", env_var)


@contextlib.contextmanager
def scoped_api_key(backend: str, config_path: Path = DEFAULT_CONFIG_PATH) -> Iterator[None]:
    """Ensure the backend's API key env var is set for the `with` block only.

    If the env var was already set by the caller/operator, it is left
    untouched on exit. If this function sources it (via `key_command`), it is
    removed again once the block exits -- the key lives in the environment
    for the refresh duration only, never longer, and its value is never
    printed or logged (see `ensure_api_key`).
    """
    env_var = BACKEND_ENV_VARS.get(backend)
    if env_var is None:
        yield
        return
    already_set = env_var in os.environ
    if not already_set:
        ensure_api_key(backend, config_path)
    try:
        yield
    finally:
        if not already_set:
            os.environ.pop(env_var, None)


def run_step(cmd: list[str], step_name: str) -> None:
    log.info("running: %s", step_name)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{step_name} failed (exit {result.returncode})")


def resolve_backend(cli_backend: str | None) -> str:
    return cli_backend or os.environ.get("HARNESS_GRAPH_BACKEND") or AUTO_BACKEND


def has_configured_label_backend(backend: str) -> bool:
    """Whether it is safe to request LLM-based community labels.

    Graphify's AST ``update`` command is local and useful without a model.
    Community naming is not: calling it with no configured credential can
    either fail or prompt a client to select a paid provider.  Keep the
    default lifecycle local-first and run the label pass only after an
    explicit backend was selected or a harness-supported key is present.
    """
    if backend != AUTO_BACKEND:
        return True
    return any(os.environ.get(env_var) for env_var in BACKEND_ENV_VARS.values())


def build_graphify_command(subcommand: str, repo_root: Path, backend: str) -> list[str]:
    """Assemble a `python -m graphify <subcommand> <repo_root> [--backend X]`
    argv list (list form -- never shell=True -- so paths with spaces/Unicode
    are inherently safe).

    `--backend` is omitted entirely when `backend` is `"auto"` (graphify's
    own auto-selection default); `--backend auto` is never emitted, per the
    supported CLI contract.
    """
    cmd = [sys.executable, "-m", "graphify", subcommand, str(repo_root)]
    if backend != AUTO_BACKEND:
        cmd.extend(["--backend", backend])
    return cmd


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo_root", type=Path, help="path to the repo/directory to graph")
    parser.add_argument("--full", action="store_true", help="run `graphify extract` before update/label")
    parser.add_argument("--backend", default=None, help="graphify backend name (default: auto)")
    return parser.parse_args(argv)


def update_semantic_index(repo_root: Path) -> None:
    """Best-effort semantic-index refresh, run after every graph rebuild.

    This is intentionally decoupled from the rest of `refresh()`: the
    optional `[semantic]` extra (ChromaDB) may not be installed, and even
    when it is, an index update failure must never fail an otherwise-
    successful graph refresh. Two independent safety nets enforce that:

    1. When chromadb isn't importable, this no-ops with a log.info -- the
       core harness (and graphify) must keep working without the extra.
    2. Once chromadb IS available, the actual update is wrapped in a broad
       `except Exception` that logs `log.warning(...)` and returns --
       nothing here is allowed to raise out of `refresh()`.
    """
    try:
        from harness import graph_index
    except ImportError as exc:  # pragma: no cover -- harness.graph_index always ships
        log.warning("could not import harness.graph_index; skipping semantic index update: %s", exc)
        return

    if not graph_index.chromadb_available():
        log.info(
            'chromadb not installed; skipping semantic index update '
            '(from this checkout run `python -m pip install -e ".[semantic]"` '
            'to enable `graph_query.py search`)'
        )
        return

    try:
        from harness.graph_location import GRAPH_RELATIVE_PATH

        graph_path = repo_root / GRAPH_RELATIVE_PATH
        if not graph_path.exists():
            log.info("no graph.json at %s; skipping semantic index update", graph_path)
            return
        with graph_path.open(encoding="utf-8") as fh:
            graph = json.load(fh)
        index_dir = graph_index.index_path_for(graph_path)
        stats = graph_index.update_index(graph, index_dir)
        log.info(
            "semantic index updated: %d added, %d updated, %d deleted, %d unchanged",
            stats.added, stats.updated, stats.deleted, stats.unchanged,
        )
    except Exception as exc:  # noqa: BLE001 -- must never break refresh()
        log.warning("semantic index update failed (non-fatal): %s", exc)


def refresh(repo_root: Path, backend: str, full: bool) -> None:
    status = detect_graphify()
    if not status.available:
        raise SystemExit(
            # The distribution is `graphifyy` (two y's); the module it
            # provides is `graphify` (one). `pip install graphify` is a 404 --
            # no such package exists -- so the hint must name the
            # distribution, not the import.
            "graphify is not installed or not runnable "
            f"({status.error or 'unknown error'}). Install it with "
            "`pip install graphifyy` before refreshing the code graph. "
            "If the harness is installed via pipx, inject it into that "
            "environment instead: `pipx inject agent-harness-kit graphifyy`."
        )

    if full and backend == AUTO_BACKEND and not has_configured_label_backend(backend):
        raise SystemExit(
            "full semantic extraction requires an explicit configured backend. "
            "Set HARNESS_GRAPH_BACKEND and its API key, then retry; for a free "
            "local AST graph, rerun without --full."
        )

    with scoped_api_key(backend):
        os.environ["GRAPHIFY_VIZ_NODE_LIMIT"] = VIZ_NODE_LIMIT

        if full:
            run_step(build_graphify_command("extract", repo_root, backend), "graphify extract")

        run_step(build_graphify_command("update", repo_root, backend), "graphify update")
        if has_configured_label_backend(backend):
            run_step(build_graphify_command("label", repo_root, backend), "graphify label")
        else:
            log.info(
                "no LLM graph backend configured; leaving community labels as local placeholders "
                "(set HARNESS_GRAPH_BACKEND to enable naming)"
            )

    update_semantic_index(repo_root)


def _quote_for_display(value: str) -> str:
    """Render `value` as a copy-pasteable shell argument.

    Reuses the shared safe-rendering module (`harness.pathsafe.shell_quote`,
    also used by `install.py`) instead of hand-rolling quoting here.
    """
    return pathsafe.shell_quote(value)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    backend = resolve_backend(args.backend)
    if not args.repo_root.exists():
        raise SystemExit(f"repo root not found: {args.repo_root}")
    refresh(args.repo_root, backend, args.full)
    graph_path = _quote_for_display(str(args.repo_root / "graphify-out" / "graph.json"))
    print(
        "Graph refreshed. Query it: python harness/graph_query.py find "
        f"<term> --graph {graph_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
