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
export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://npmmirror.com/mirrors/electron-builder-binaries/}"
export CSC_IDENTITY_AUTO_DISCOVERY=false

if [[ "${1:-}" == "--check" ]]; then
  printf 'Node=%s\n' "$ACTUAL_NODE_VERSION"
  printf 'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=%s\n' "$PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"
  printf 'ELECTRON_MIRROR=%s\n' "$ELECTRON_MIRROR"
  printf 'ELECTRON_BUILDER_BINARIES_MIRROR=%s\n' "$ELECTRON_BUILDER_BINARIES_MIRROR"
  exit 0
fi

[[ $# -eq 0 ]] || fail "unknown argument: $1"

RELEASE_DIR="$REPO_ROOT/apps/desktop/release"
LOG_DIR="$REPO_ROOT/apps/desktop/build/logs"
BUILD_LOG="$LOG_DIR/phase1-desktop-dmg-build.log"
CONTRACT_SCRIPT="$REPO_ROOT/scripts/desktop-dmg-contract.mjs"

mkdir -p "$RELEASE_DIR" "$LOG_DIR"
: > "$BUILD_LOG"
BUILD_STARTED_AT_MS="$(node -e 'process.stdout.write(String(Date.now()))')"

run_logged() {
  printf '\n>>> %s\n' "$*" | tee -a "$BUILD_LOG"
  "$@" 2>&1 | tee -a "$BUILD_LOG"
}

cd "$REPO_ROOT"
run_logged npm ci
run_logged npm run --workspace apps/desktop dist:mac:dmg

node "$CONTRACT_SCRIPT" validate-log "$BUILD_LOG"
DMG_PATH="$(node "$CONTRACT_SCRIPT" find-dmg "$RELEASE_DIR" "$BUILD_STARTED_AT_MS")"

printf 'Hermes DMG ready: %s\n' "$DMG_PATH"
