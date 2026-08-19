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

RELEASE_DIR="${HERMES_DMG_RELEASE_DIR:-$REPO_ROOT/apps/desktop/release}"
PACKAGED_APP="$RELEASE_DIR/mac-arm64/Hermes.app"
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
if run_logged npm run --workspace apps/desktop dist:mac:dmg -- --config.mac.identity=-; then
  BUILDER_STATUS=0
else
  BUILDER_STATUS=$?
fi

if [[ "$BUILDER_STATUS" -ne 0 ]]; then
  VOLUME_DENIED='ditto: /Volumes/Install Hermes/Hermes.app: Operation not permitted'
  [[ -d "$PACKAGED_APP" ]] || exit "$BUILDER_STATUS"
  grep -Fq "$VOLUME_DENIED" "$BUILD_LOG" || exit "$BUILDER_STATUS"

  APP_VERSION="$(node -p "require('./apps/desktop/package.json').version")"
  FALLBACK_DMG="$RELEASE_DIR/Hermes-$APP_VERSION-mac-arm64.dmg"
  FALLBACK_WORK="$(mktemp -d "${TMPDIR:-/tmp}/hermes-dmg-fallback.XXXXXX")"
  FALLBACK_RW="$FALLBACK_WORK/Hermes-rw.dmg"
  FALLBACK_MOUNT="$FALLBACK_WORK/mount"
  FALLBACK_ATTACHED=0
  cleanup_fallback_work() {
    if [[ "$FALLBACK_ATTACHED" -eq 1 ]]; then
      hdiutil detach "$FALLBACK_MOUNT" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$FALLBACK_WORK"
  }
  trap cleanup_fallback_work EXIT

  printf '\n>>> electron-builder could not write its mounted DMG volume; using restricted-volume fallback\n' | tee -a "$BUILD_LOG"
  APP_KIB="$(du -sk "$PACKAGED_APP" | awk '{print $1}')"
  IMAGE_MIB="$(( (APP_KIB + 1023) / 1024 + 128 ))"
  mkdir "$FALLBACK_MOUNT"
  run_logged hdiutil create \
    -size "${IMAGE_MIB}m" \
    -fs 'HFS+' \
    -volname 'Install Hermes' \
    -type UDIF \
    "$FALLBACK_RW"
  run_logged hdiutil attach -nobrowse -mountpoint "$FALLBACK_MOUNT" "$FALLBACK_RW"
  FALLBACK_ATTACHED=1
  run_logged /usr/bin/ditto "$PACKAGED_APP" "$FALLBACK_MOUNT/Hermes.app"
  run_logged ln -s /Applications "$FALLBACK_MOUNT/Applications"
  run_logged hdiutil detach "$FALLBACK_MOUNT"
  FALLBACK_ATTACHED=0
  run_logged hdiutil convert "$FALLBACK_RW" -format UDZO -ov -o "$FALLBACK_DMG"
  run_logged hdiutil verify "$FALLBACK_DMG"
fi

run_logged codesign --verify --deep --strict "$PACKAGED_APP"

node "$CONTRACT_SCRIPT" validate-log "$BUILD_LOG"
DMG_PATH="$(node "$CONTRACT_SCRIPT" find-dmg "$RELEASE_DIR" "$BUILD_STARTED_AT_MS")"

printf 'Hermes DMG ready: %s\n' "$DMG_PATH"
