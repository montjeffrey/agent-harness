#!/usr/bin/env sh
# agent-harness quick setup (bash/sh, macOS/Linux/Git-Bash)
#
#   ./setup.sh <project-dir> [--no-hooks] [--check] [--agents claude,codex,gemini]
#
# Runs the preflight doctor first and reports anything missing; installs only
# if no check FAILs. --check reports and stops. --no-hooks skips the Claude
# Code PreToolUse hook merge (hooks are ON by default in quick setup).

set -eu

HARNESS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$#" -lt 1 ]; then
    echo "usage: ./setup.sh <project-dir> [--no-hooks] [--check] [--agents list]" >&2
    exit 2
fi

PROJECT_DIR=$1
shift

# Find a Python 3 interpreter.
PYTHON=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,) else 1)' 2>/dev/null; then
            PYTHON=$candidate
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[FAIL] python: no Python 3 interpreter found on PATH (need >= 3.11)" >&2
    echo "Install Python from https://www.python.org/downloads/ and rerun." >&2
    exit 1
fi

HOOKS_FLAG="--hooks"
EXTRA_ARGS=""
CHECK_ONLY=0
BUILD_GRAPH=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-hooks) HOOKS_FLAG="" ;;
        --check) CHECK_ONLY=1 ;;
        --build-graph) BUILD_GRAPH=1 ;;
        --agents) EXTRA_ARGS="$EXTRA_ARGS --agents $2"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$CHECK_ONLY" -eq 1 ]; then
    # shellcheck disable=SC2086
    exec "$PYTHON" "$HARNESS_ROOT/install.py" "$PROJECT_DIR" --check $EXTRA_ARGS
fi

# install.py runs the doctor preflight itself and aborts on any FAIL.
# shellcheck disable=SC2086
"$PYTHON" "$HARNESS_ROOT/install.py" "$PROJECT_DIR" $HOOKS_FLAG $EXTRA_ARGS

if [ "$BUILD_GRAPH" -eq 1 ] && [ ! -f "$PROJECT_DIR/graphify-out/graph.json" ]; then
    echo
    echo "No code graph yet -- building one (--build-graph). This can take a while..."
    "$PYTHON" "$HARNESS_ROOT/harness/graph_refresh.py" "$PROJECT_DIR" --full
fi

echo
echo "Next steps:"
if [ ! -f "$PROJECT_DIR/graphify-out/graph.json" ]; then
    echo "  1. Build the code graph:  $PYTHON \"$HARNESS_ROOT/harness/graph_refresh.py\" \"$PROJECT_DIR\" --full"
    echo "     (or rerun setup with --build-graph)"
fi
echo "  - Query the graph:  $PYTHON \"$HARNESS_ROOT/harness/graph_query.py\" find <term>"
echo "  - Re-run anytime -- the install is idempotent."
