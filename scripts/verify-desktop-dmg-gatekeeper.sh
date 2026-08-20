#!/usr/bin/env bash

set -uo pipefail

usage() {
  printf '%s\n' \
    'usage: verify-desktop-dmg-gatekeeper.sh DMG SHA256 BYTES VERSION EVIDENCE_DIR' >&2
}

if [[ "$#" -ne 5 ]]; then
  usage
  exit 64
fi

DMG_INPUT="$1"
EXPECTED_SHA256="$2"
EXPECTED_BYTES="$3"
EXPECTED_VERSION="$4"
EVIDENCE_DIR="$5"
INSTALL_APP="/Applications/Hermes.app"
EXPECTED_BUNDLE_ID="com.nousresearch.hermes"

if [[ ! -f "$DMG_INPUT" ]]; then
  printf 'DMG not found: %s\n' "$DMG_INPUT" >&2
  exit 66
fi

DMG_DIR="$(cd "$(dirname "$DMG_INPUT")" && pwd -P)"
DMG_PATH="$DMG_DIR/$(basename "$DMG_INPUT")"
mkdir -p "$EVIDENCE_DIR"
EVIDENCE_DIR="$(cd "$EVIDENCE_DIR" && pwd -P)"

SUMMARY_FILE="$EVIDENCE_DIR/summary.md"
COMMANDS_FILE="$EVIDENCE_DIR/commands.log"
ENVIRONMENT_FILE="$EVIDENCE_DIR/environment.txt"
CODESIGN_FILE="$EVIDENCE_DIR/codesign.txt"
SPCTL_FILE="$EVIDENCE_DIR/spctl.txt"
LAUNCH_FILE="$EVIDENCE_DIR/launch.txt"

: >"$COMMANDS_FILE"
: >"$CODESIGN_FILE"
: >"$SPCTL_FILE"
: >"$LAUNCH_FILE"

cat >"$SUMMARY_FILE" <<'EOF'
# Hermes Desktop fresh macOS DMG acceptance

| Check | Result | Detail |
|---|---|---|
EOF

FAILURES=0
MOUNTED=0
INSTALLED=0
MOUNT_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/hermes-dmg-mount.XXXXXX")"

sanitize_detail() {
  printf '%s' "$1" | tr '\n|' '  '
}

record() {
  local name="$1"
  local result="$2"
  local detail
  detail="$(sanitize_detail "$3")"
  printf '| %s | %s | %s |\n' "$name" "$result" "$detail" >>"$SUMMARY_FILE"
  printf '%s\t%s\t%s\n' "$result" "$name" "$detail"
  if [[ "$result" == "FAIL" ]]; then
    FAILURES=$((FAILURES + 1))
  fi
}

log_command() {
  local label="$1"
  shift
  {
    printf '[%s] ' "$label"
    printf '%q ' "$@"
    printf '\n'
  } >>"$COMMANDS_FILE"
}

cleanup() {
  local cleanup_bundle=""
  pkill -f "^${INSTALL_APP}/Contents/MacOS/Hermes" >/dev/null 2>&1 || true
  if [[ "$INSTALLED" -eq 1 && -d "$INSTALL_APP" ]]; then
    cleanup_bundle="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' \
      "$INSTALL_APP/Contents/Info.plist" 2>/dev/null || true)"
    if [[ "$cleanup_bundle" == "$EXPECTED_BUNDLE_ID" ]]; then
      sudo /bin/rm -rf -- "$INSTALL_APP"
    else
      printf 'cleanup refused unexpected bundle at %s\n' "$INSTALL_APP" >>"$COMMANDS_FILE"
    fi
  fi
  if [[ "$MOUNTED" -eq 1 ]]; then
    hdiutil detach "$MOUNT_DIR" >>"$COMMANDS_FILE" 2>&1 || true
  fi
  /bin/rm -rf -- "$MOUNT_DIR"
}
trap cleanup EXIT INT TERM

RUNNER_ARCH="$(uname -m)"
ACTUAL_SHA256="$(shasum -a 256 "$DMG_PATH" | awk '{print $1}')"
ACTUAL_BYTES="$(stat -f '%z' "$DMG_PATH")"
{
  printf 'timestamp_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf 'runner_image=%s\n' "${ImageOS:-unknown}-${ImageVersion:-unknown}"
  printf 'macos_version=%s\n' "$(sw_vers -productVersion)"
  printf 'architecture=%s\n' "$RUNNER_ARCH"
  printf 'dmg_name=%s\n' "$(basename "$DMG_PATH")"
  printf 'dmg_bytes=%s\n' "$ACTUAL_BYTES"
  printf 'dmg_sha256=%s\n' "$ACTUAL_SHA256"
  printf 'expected_version=%s\n' "$EXPECTED_VERSION"
} >"$ENVIRONMENT_FILE"

if [[ "$RUNNER_ARCH" == "arm64" ]]; then
  record 'Runner architecture' 'PASS' 'arm64 Apple Silicon'
else
  record 'Runner architecture' 'FAIL' "expected arm64, got $RUNNER_ARCH"
fi

if [[ "$ACTUAL_SHA256" == "$EXPECTED_SHA256" ]]; then
  record 'DMG SHA-256' 'PASS' "$ACTUAL_SHA256"
else
  record 'DMG SHA-256' 'FAIL' "expected $EXPECTED_SHA256, got $ACTUAL_SHA256"
fi

if [[ "$ACTUAL_BYTES" == "$EXPECTED_BYTES" ]]; then
  record 'DMG byte size' 'PASS' "$ACTUAL_BYTES"
else
  record 'DMG byte size' 'FAIL' "expected $EXPECTED_BYTES, got $ACTUAL_BYTES"
fi

if [[ "$FAILURES" -ne 0 ]]; then
  record 'DMG inspection' 'NOT_RUN' 'immutable input preflight failed'
  printf '\n**Overall: FAIL** — immutable input or runner preflight failed.\n' >>"$SUMMARY_FILE"
  exit 1
fi

QUARANTINE_VALUE="0083;$(printf '%x' "$(date +%s)");GitHubActions;"
log_command 'quarantine-dmg' xattr -w com.apple.quarantine '[redacted-provenance]' "$DMG_PATH"
if xattr -w com.apple.quarantine "$QUARANTINE_VALUE" "$DMG_PATH" && \
  xattr -p com.apple.quarantine "$DMG_PATH" >/dev/null; then
  record 'Downloaded DMG quarantine' 'PASS' 'com.apple.quarantine present (provenance value redacted)'
else
  record 'Downloaded DMG quarantine' 'FAIL' 'could not attach or read quarantine metadata'
fi

log_command 'verify-dmg' hdiutil verify "$DMG_PATH"
if hdiutil verify "$DMG_PATH" >>"$COMMANDS_FILE" 2>&1; then
  record 'DMG filesystem verification' 'PASS' 'hdiutil verify accepted the image'
else
  record 'DMG filesystem verification' 'FAIL' 'hdiutil verify rejected the image'
fi

if [[ -e "$INSTALL_APP" ]]; then
  record 'Real Applications target' 'FAIL' '/Applications/Hermes.app unexpectedly exists on fresh runner'
  record 'DMG mount and install' 'NOT_RUN' 'existing target was not overwritten'
  printf '\n**Overall: FAIL** — the fresh runner install target was not empty.\n' >>"$SUMMARY_FILE"
  exit 1
fi

log_command 'mount-dmg' hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT_DIR" "$DMG_PATH"
if hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT_DIR" "$DMG_PATH" \
  >>"$COMMANDS_FILE" 2>&1; then
  MOUNTED=1
  record 'Read-only DMG mount' 'PASS' 'mounted without browsing or write access'
else
  record 'Read-only DMG mount' 'FAIL' 'hdiutil attach failed'
  record 'Installed app inspection' 'NOT_RUN' 'DMG did not mount'
  printf '\n**Overall: FAIL** — the DMG could not be mounted.\n' >>"$SUMMARY_FILE"
  exit 1
fi

APP_COUNT="$(find "$MOUNT_DIR" -maxdepth 1 -type d -name '*.app' | wc -l | tr -d ' ')"
SOURCE_APP="$MOUNT_DIR/Hermes.app"
if [[ "$APP_COUNT" == "1" && -d "$SOURCE_APP" ]]; then
  record 'DMG app payload' 'PASS' 'exactly one Hermes.app is present'
else
  record 'DMG app payload' 'FAIL' "expected one Hermes.app, found $APP_COUNT app bundles"
fi

if [[ -L "$MOUNT_DIR/Applications" && "$(readlink "$MOUNT_DIR/Applications")" == "/Applications" ]]; then
  record 'DMG Applications link' 'PASS' 'Applications -> /Applications'
else
  record 'DMG Applications link' 'FAIL' 'required Applications link is absent or points elsewhere'
fi

if [[ ! -d "$SOURCE_APP" ]]; then
  record 'Real Applications install' 'NOT_RUN' 'Hermes.app payload is unavailable'
  printf '\n**Overall: FAIL** — required app payload is absent.\n' >>"$SUMMARY_FILE"
  exit 1
fi

log_command 'install-app' sudo ditto "$SOURCE_APP" "$INSTALL_APP"
if sudo ditto "$SOURCE_APP" "$INSTALL_APP" >>"$COMMANDS_FILE" 2>&1; then
  INSTALLED=1
  record 'Real Applications install' 'PASS' 'copied to /Applications/Hermes.app'
else
  record 'Real Applications install' 'FAIL' 'ditto failed without overwriting an existing app'
  printf '\n**Overall: FAIL** — installation to /Applications failed.\n' >>"$SUMMARY_FILE"
  exit 1
fi

log_command 'quarantine-app' sudo xattr -r -w com.apple.quarantine '[redacted-provenance]' "$INSTALL_APP"
if sudo xattr -r -w com.apple.quarantine "$QUARANTINE_VALUE" "$INSTALL_APP" && \
  xattr -p com.apple.quarantine "$INSTALL_APP" >/dev/null; then
  record 'Installed app quarantine' 'PASS' 'com.apple.quarantine present (provenance value redacted)'
else
  record 'Installed app quarantine' 'FAIL' 'could not attach or read quarantine metadata'
fi

INFO_PLIST="$INSTALL_APP/Contents/Info.plist"
MAIN_EXECUTABLE="$INSTALL_APP/Contents/MacOS/Hermes"
RESOURCES="$INSTALL_APP/Contents/Resources"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$INFO_PLIST" 2>/dev/null || true)"
APP_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$INFO_PLIST" 2>/dev/null || true)"

if [[ "$BUNDLE_ID" == "$EXPECTED_BUNDLE_ID" ]]; then
  record 'Bundle identifier' 'PASS' "$BUNDLE_ID"
else
  record 'Bundle identifier' 'FAIL' "expected $EXPECTED_BUNDLE_ID, got ${BUNDLE_ID:-missing}"
fi

if [[ "$APP_VERSION" == "$EXPECTED_VERSION" ]]; then
  record 'App version' 'PASS' "$APP_VERSION"
else
  record 'App version' 'FAIL' "expected $EXPECTED_VERSION, got ${APP_VERSION:-missing}"
fi

EXECUTABLE_DESCRIPTION="$(file "$MAIN_EXECUTABLE" 2>/dev/null || true)"
if [[ "$EXECUTABLE_DESCRIPTION" == *'Mach-O 64-bit executable arm64'* ]]; then
  record 'Main executable architecture' 'PASS' 'Mach-O arm64'
else
  record 'Main executable architecture' 'FAIL' "unexpected executable: ${EXECUTABLE_DESCRIPTION:-missing}"
fi

PAYLOAD_FAILURES=0
for payload in \
  "$RESOURCES/bootstrap/hermes-backend.tar.gz" \
  "$RESOURCES/bootstrap/install.sh" \
  "$RESOURCES/bootstrap/payload-manifest.json" \
  "$RESOURCES/install-stamp.json"; do
  if [[ ! -s "$payload" ]]; then
    PAYLOAD_FAILURES=$((PAYLOAD_FAILURES + 1))
  fi
done
if [[ "$PAYLOAD_FAILURES" -eq 0 && -x "$RESOURCES/bootstrap/install.sh" ]]; then
  record 'Bundled bootstrap payload' 'PASS' 'backend archive, installer, manifest, and install stamp are present'
else
  record 'Bundled bootstrap payload' 'FAIL' "$PAYLOAD_FAILURES payload files missing/empty or installer not executable"
fi

if tar -tzf "$RESOURCES/bootstrap/hermes-backend.tar.gz" >/dev/null 2>&1 && \
  plutil -lint "$RESOURCES/bootstrap/payload-manifest.json" >/dev/null 2>&1 && \
  plutil -lint "$RESOURCES/install-stamp.json" >/dev/null 2>&1; then
  record 'Bundled payload readability' 'PASS' 'archive and JSON metadata parse successfully'
else
  record 'Bundled payload readability' 'FAIL' 'archive or JSON metadata is unreadable'
fi

log_command 'verify-signature' codesign --verify --deep --strict "$INSTALL_APP"
{
  printf '%s\n' '--- codesign display ---'
  codesign -dv --verbose=4 "$INSTALL_APP"
} >"$CODESIGN_FILE" 2>&1
if codesign --verify --deep --strict --verbose=2 "$INSTALL_APP" \
  >>"$CODESIGN_FILE" 2>&1; then
  record 'Code signature integrity' 'PASS' 'codesign deep strict verification succeeded'
else
  record 'Code signature integrity' 'FAIL' 'codesign deep strict verification failed'
fi

printf '%s\n' '--- quarantined DMG open assessment ---' >"$SPCTL_FILE"
log_command 'assess-dmg' spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG_PATH"
spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG_PATH" \
  >>"$SPCTL_FILE" 2>&1
DMG_SPCTL_RC=$?
if [[ "$DMG_SPCTL_RC" -eq 0 ]]; then
  record 'Gatekeeper DMG open assessment' 'PASS' 'spctl accepted the quarantined DMG'
else
  record 'Gatekeeper DMG open assessment' 'FAIL' "spctl rejected the quarantined DMG (exit $DMG_SPCTL_RC)"
fi

printf '%s\n' '--- quarantined installed app execute assessment ---' >>"$SPCTL_FILE"
log_command 'assess-app' spctl --assess --type execute --verbose=4 "$INSTALL_APP"
spctl --assess --type execute --verbose=4 "$INSTALL_APP" >>"$SPCTL_FILE" 2>&1
APP_SPCTL_RC=$?
if [[ "$APP_SPCTL_RC" -eq 0 ]]; then
  record 'Gatekeeper app execution assessment' 'PASS' 'spctl accepted the quarantined installed app'
else
  record 'Gatekeeper app execution assessment' 'FAIL' "spctl rejected the quarantined installed app (exit $APP_SPCTL_RC)"
fi

if [[ "$APP_SPCTL_RC" -ne 0 ]]; then
  record 'Normal installed-app launch' 'NOT_RUN' 'Gatekeeper rejection blocked launch; quarantine was preserved'
  record 'Signed-out protected backend check' 'NOT_RUN' 'launch was blocked at the distribution gate'
  record 'First-screen visual inspection' 'NOT_RUN' 'GitHub runner has no reliable human-visible dialog inspection'
else
  log_command 'launch-app' open -n "$INSTALL_APP"
  open -n "$INSTALL_APP" >>"$LAUNCH_FILE" 2>&1
  OPEN_RC=$?
  if [[ "$OPEN_RC" -ne 0 ]]; then
    record 'Normal installed-app launch' 'FAIL' "open rejected the installed app (exit $OPEN_RC)"
    record 'Signed-out protected backend check' 'NOT_RUN' 'application process did not start'
  else
    APP_PID=""
    for _ in {1..15}; do
      APP_PID="$(pgrep -f "^${INSTALL_APP}/Contents/MacOS/Hermes" | head -n 1 || true)"
      [[ -n "$APP_PID" ]] && break
      sleep 1
    done
    if [[ -n "$APP_PID" ]]; then
      record 'Normal installed-app launch' 'PASS' 'installed executable started with quarantine intact'
      printf 'installed_app_process_count=1\n' >>"$LAUNCH_FILE"
    else
      record 'Normal installed-app launch' 'FAIL' 'open returned success but the installed executable did not remain running'
      printf 'installed_app_process_count=0\n' >>"$LAUNCH_FILE"
    fi

    screencapture -x "$EVIDENCE_DIR/first-launch.png" >>"$LAUNCH_FILE" 2>&1 || true

    PROTECTED_PATTERN='[h]ermes serve|[h]ermes gateway|[h]ermes_cli\.web_server|[r]un_agent\.py'
    PROTECTED_COUNT="$(pgrep -fal "$PROTECTED_PATTERN" | wc -l | tr -d ' ')"
    LISTENER_COUNT="$(lsof -nP -iTCP -sTCP:LISTEN -a \
      \( -c Hermes -o -c Python -o -c Python3 \) 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')"
    printf 'protected_backend_process_count=%s\n' "$PROTECTED_COUNT" >>"$LAUNCH_FILE"
    printf 'hermes_or_python_listener_count=%s\n' "$LISTENER_COUNT" >>"$LAUNCH_FILE"
    if [[ "$PROTECTED_COUNT" == "0" && "$LISTENER_COUNT" == "0" ]]; then
      record 'Signed-out protected backend check' 'PASS' 'no protected backend process or Hermes/Python listener appeared before login'
    else
      record 'Signed-out protected backend check' 'FAIL' "protected_processes=$PROTECTED_COUNT listeners=$LISTENER_COUNT"
    fi
  fi
  record 'First-screen visual inspection' 'NOT_RUN' 'screenshot retained for manual inspection; no credentials were supplied'
fi

if [[ "$FAILURES" -eq 0 ]]; then
  printf '\n**Overall: PASS** — the exact quarantined DMG passed automated distribution checks.\n' \
    >>"$SUMMARY_FILE"
else
  printf '\n**Overall: FAIL** — %s release-blocking check(s) failed.\n' "$FAILURES" \
    >>"$SUMMARY_FILE"
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  cat "$SUMMARY_FILE" >>"$GITHUB_STEP_SUMMARY"
fi

if [[ "$FAILURES" -ne 0 ]]; then
  exit 1
fi
