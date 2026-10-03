#!/usr/bin/env bash
# run_ty.sh - type check every Python file in the workspace.
set -euo pipefail

TY="$1"

ROOT="${TEST_SRCDIR:-}/${TEST_WORKSPACE:-}"
if [ ! -d "$ROOT/foundry" ]; then
    if [ -n "${TEST_SRCDIR:-}" ] && [ -d "${TEST_SRCDIR}/_main/foundry" ]; then
        ROOT="${TEST_SRCDIR}/_main"
    else
        ROOT="."
    fi
fi

cd "$ROOT"

"$TY" check foundry tools
