#!/usr/bin/env bash
set -euo pipefail

RUFF="$(readlink -f "$1")"
CONFIG="$(readlink -f "$2")"

cd "${BUILD_WORKSPACE_DIRECTORY:-.}"
"$RUFF" format --config "$CONFIG" foundry/ reference/ tools/
