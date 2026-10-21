#!/usr/bin/env bash
# run_ruff.sh - lint and format check for every Python file in the workspace.
set -euo pipefail

RUFF="$1"
CONFIG="$2"

ROOT="${TEST_SRCDIR}/${TEST_WORKSPACE}"
if [ ! -d "$ROOT/foundry" ]; then
    ROOT="."
fi

cd "$ROOT"

status=0
"$RUFF" check --config "$CONFIG" . || status=$?
"$RUFF" format --check --config "$CONFIG" . || status=$?
if [ "$status" -ne 0 ]; then
    echo "hint: run 'bazel run //tools:ruff_format' to format and fix Python files." >&2
    exit "$status"
fi
