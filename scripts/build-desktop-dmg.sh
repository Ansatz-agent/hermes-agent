#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
EXPECTED_NODE_VERSION="v$(tr -d '[:space:]' < "$REPO_ROOT/.node-version")"

fail() {
  printf 'Hermes DMG build: %s\n' "$1" >&2
  exit 1
}

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS is required"
[[ "$(uname -m)" == "arm64" ]] || fail "arm64 is required"
command -v node >/dev/null 2>&1 || fail "Node.js $EXPECTED_NODE_VERSION is required"

ACTUAL_NODE_VERSION="$(node --version)"
[[ "$ACTUAL_NODE_VERSION" == "$EXPECTED_NODE_VERSION" ]] || \
  fail "expected $EXPECTED_NODE_VERSION, got $ACTUAL_NODE_VERSION"

export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://npmmirror.com/mirrors/electron/}"

if [[ "${1:-}" == "--check" ]]; then
  printf 'Node=%s\n' "$ACTUAL_NODE_VERSION"
  printf 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=%s\n' "$PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"
  printf 'ELECTRON_MIRROR=%s\n' "$ELECTRON_MIRROR"
  exit 0
fi

fail "the DMG pipeline has not been implemented yet"
