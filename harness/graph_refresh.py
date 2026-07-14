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
is never printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

VIZ_NODE_LIMIT = "10000"
DEFAULT_CONFIG_PATH = Path.home() / ".agent-harness" / "config.json"

# Known graphify backends and the API-key env var each one expects. Backends
# not listed here (e.g. "auto") are assumed to manage their own credentials.
BACKEND_ENV_VARS: dict[str, str] = {
    "nvidia-nim": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def load_user_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if not config_path.exists():
        return {}
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


def run_step(cmd: list[str], step_name: str) -> None:
    log.info("running: %s", step_name)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{step_name} failed (exit {result.returncode})")


def resolve_backend(cli_backend: str | None) -> str:
    return cli_backend or os.environ.get("HARNESS_GRAPH_BACKEND") or "auto"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo_root", type=Path, help="path to the repo/directory to graph")
    parser.add_argument("--full", action="store_true", help="run `graphify extract` before update/label")
    parser.add_argument("--backend", default=None, help="graphify backend name (default: auto)")
    return parser.parse_args(argv)


def refresh(repo_root: Path, backend: str, full: bool) -> None:
    ensure_api_key(backend)
    os.environ["GRAPHIFY_VIZ_NODE_LIMIT"] = VIZ_NODE_LIMIT

    if full:
        run_step(
            [sys.executable, "-m", "graphify", "extract", str(repo_root), "--backend", backend],
            "graphify extract",
        )

    run_step([sys.executable, "-m", "graphify", "update", str(repo_root)], "graphify update")
    run_step(
        [sys.executable, "-m", "graphify", "label", str(repo_root), "--backend", backend],
        "graphify label",
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    backend = resolve_backend(args.backend)
    if not args.repo_root.exists():
        raise SystemExit(f"repo root not found: {args.repo_root}")
    refresh(args.repo_root, backend, args.full)
    print(f"Graph refreshed. Query it: python harness/graph_query.py find <term> --graph {args.repo_root / 'graphify-out' / 'graph.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
