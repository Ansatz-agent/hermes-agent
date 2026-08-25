#!/bin/bash
# ============================================================================
# Hermes Agent Installer
# ============================================================================
# Installation script for Linux, macOS, and Android/Termux.
# Uses uv for desktop/server installs and Python's stdlib venv + pip on Termux.
#
# Usage (administrator-supplied reviewed package):
#   ./install.sh --bundled-source --no-venv --skip-setup
#
# ============================================================================

set -e

# Guard against environment leakage when the installer is launched from another
# Python-driven tool session (e.g. Hermes terminal tool). A pre-set PYTHONPATH
# can force pip/entrypoints to import a different checkout than the one being
# installed, which makes fresh installs appear broken or stale.
if [ -n "${PYTHONPATH:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONPATH during install to avoid module shadowing"
    unset PYTHONPATH
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONHOME during install"
    unset PYTHONHOME
fi

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.  See #21269.
export UV_NO_CONFIG=1

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Configuration
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# INSTALL_DIR is resolved AFTER arg parsing and OS detection so we can pick an
# FHS-style layout for root installs.  Track whether the user gave us an
# explicit directory — if so we never override it.
if [ -n "${HERMES_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$HERMES_INSTALL_DIR"
    INSTALL_DIR_EXPLICIT=true
else
    INSTALL_DIR=""
    INSTALL_DIR_EXPLICIT=false
fi
PYTHON_VERSION="3.11"
NODE_VERSION="26"

# FHS-style root install layout (set by resolve_install_layout when applicable):
#   code at /usr/local/lib/hermes-agent, command at /usr/local/bin/hermes,
#   data still at /root/.hermes (HERMES_HOME).  Matches Claude Code / Codex CLI
#   and keeps Docker bind-mounted /root/ volumes lean.
ROOT_FHS_LAYOUT=false
DETECTED_BROWSER_EXECUTABLE=""

# Options
USE_VENV=true
RUN_SETUP=true
SKIP_BROWSER=false
SKIP_COMPUTER_USE=false
NO_SKILLS=false
BRANCH="main"
INSTALL_COMMIT=""
FORCE_COMMIT=false
ENSURE_DEPS=""

MANIFEST_MODE=false
STAGE_NAME=""
JSON_OUTPUT=false
NON_INTERACTIVE=false
INCLUDE_DESKTOP=false
BUNDLED_SOURCE=false
BUNDLED_TOOLCHAIN_ROOT=""
BOOTSTRAP_SCOPE="runtime"
DESKTOP_PRODUCT=""

DESKTOP_PYTHON_PRIMARY_MIRROR="https://mirrors.ustc.edu.cn/pypi/simple"
DESKTOP_PYTHON_FALLBACK_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
DESKTOP_NPM_REGISTRY="https://registry.npmmirror.com"
DESKTOP_NPM_OFFICIAL_REGISTRY="https://registry.npmjs.org"
DESKTOP_NODE_MIRROR="https://registry.npmmirror.com/-/binary/node/"
DESKTOP_NODE_FALLBACK_MIRROR="https://npmmirror.com/mirrors/node/"
DESKTOP_NODE_OFFICIAL_MIRROR="https://nodejs.org/dist/"
DESKTOP_PLAYWRIGHT_MIRROR="https://registry.npmmirror.com/-/binary/playwright"
DESKTOP_PLAYWRIGHT_FALLBACK_MIRROR="https://npmmirror.com/mirrors/playwright/"
DESKTOP_ELECTRON_PRIMARY_MIRROR="https://npmmirror.com/mirrors/electron/"
DESKTOP_ELECTRON_SECONDARY_MIRROR="https://registry.npmmirror.com/-/binary/electron/"

# Product-managed child processes never inherit package/model redirect knobs.
# Installers set one reviewed domestic-first policy for every transitive child.
unset UV_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL UV_CONFIG_FILE UV_PYTHON \
    PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_CONFIG_FILE \
    NPM_CONFIG_REGISTRY npm_config_registry NPM_CONFIG_USERCONFIG \
    NODEJS_ORG_MIRROR ELECTRON_MIRROR ELECTRON_BUILDER_BINARIES_MIRROR \
    PLAYWRIGHT_DOWNLOAD_HOST HF_ENDPOINT HF_TOKEN HUGGING_FACE_HUB_TOKEN
export UV_NO_CONFIG=1
export UV_DEFAULT_INDEX="$DESKTOP_PYTHON_PRIMARY_MIRROR"
export UV_INDEX="$DESKTOP_PYTHON_PRIMARY_MIRROR"
export PIP_CONFIG_FILE=/dev/null
export PIP_INDEX_URL="$DESKTOP_PYTHON_PRIMARY_MIRROR"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export HERMES_UV_FALLBACK_INDEX="$DESKTOP_PYTHON_FALLBACK_MIRROR"
export NPM_CONFIG_USERCONFIG=/dev/null
export NPM_CONFIG_REGISTRY="$DESKTOP_NPM_REGISTRY"
export npm_config_registry="$DESKTOP_NPM_REGISTRY"
export NODEJS_ORG_MIRROR="$DESKTOP_NODE_MIRROR"
export HERMES_NODE_MIRROR="$DESKTOP_NODE_MIRROR"
export PLAYWRIGHT_DOWNLOAD_HOST="$DESKTOP_PLAYWRIGHT_MIRROR"
export ELECTRON_MIRROR="$DESKTOP_ELECTRON_PRIMARY_MIRROR"
export ELECTRON_BUILDER_BINARIES_MIRROR="https://npmmirror.com/mirrors/electron-builder-binaries/"
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
MANAGED_DOWNLOAD_ENVIRONMENT=1

# Detect non-interactive mode (e.g. curl | bash)
# When stdin is not a terminal, read -p will fail with EOF,
# causing set -e to silently abort the entire script.
if [ -t 0 ]; then
    IS_INTERACTIVE=true
else
    IS_INTERACTIVE=false
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-venv)
            USE_VENV=false
            shift
            ;;
        --skip-setup)
            RUN_SETUP=false
            shift
            ;;
        --skip-browser|--no-playwright)
            SKIP_BROWSER=true
            shift
            ;;
        --skip-computer-use)
            SKIP_COMPUTER_USE=true
            shift
            ;;
        --no-skills)
            NO_SKILLS=true
            shift
            ;;
        --branch|-Branch)
            BRANCH="$2"
            shift 2
            ;;
        --commit|-Commit)
            INSTALL_COMMIT="$2"
            shift 2
            ;;
        --force-commit|-ForceCommit)
            FORCE_COMMIT=true
            shift
            ;;
        --manifest|-Manifest)
            MANIFEST_MODE=true
            shift
            ;;
        --stage|-Stage)
            STAGE_NAME="$2"
            shift 2
            ;;
        --json|-Json)
            JSON_OUTPUT=true
            shift
            ;;
        --non-interactive|-NonInteractive)
            NON_INTERACTIVE=true
            shift
            ;;
        --include-desktop|-IncludeDesktop)
            INCLUDE_DESKTOP=true
            shift
            ;;
        --bundled-source)
            BUNDLED_SOURCE=true
            shift
            ;;
        --bundled-toolchain)
            BUNDLED_TOOLCHAIN_ROOT="${2:-}"
            if [ -z "$BUNDLED_TOOLCHAIN_ROOT" ]; then
                echo "Missing bundled authentication toolchain path" >&2
                exit 1
            fi
            shift 2
            ;;
        --bootstrap-scope)
            BOOTSTRAP_SCOPE="${2:-}"
            case "$BOOTSTRAP_SCOPE" in
                auth|runtime) ;;
                *) echo "Unknown bootstrap scope: ${BOOTSTRAP_SCOPE:-<empty>}" >&2; exit 1 ;;
            esac
            shift 2
            ;;
        --desktop-product)
            DESKTOP_PRODUCT="${2:-}"
            case "$DESKTOP_PRODUCT" in
                ansatz-voice-trace) ;;
                *) echo "Unknown desktop product: ${DESKTOP_PRODUCT:-<empty>}" >&2; exit 1 ;;
            esac
            shift 2
            ;;
        --dir)
            INSTALL_DIR="$2"
            INSTALL_DIR_EXPLICIT=true
            shift 2
            ;;
        --hermes-home)
            HERMES_HOME="$2"
            shift 2
            ;;
        --ensure)
            ENSURE_DEPS="$2"
            shift 2
            ;;

        -h|--help)
            echo "Hermes Agent Installer"
            echo ""
            echo "Usage: install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-venv      Don't create virtual environment"
            echo "  --skip-setup   Skip interactive setup wizard"
            echo "  --skip-browser Skip Playwright/Chromium install (browser tools won't work)"
            echo "  --skip-computer-use  Skip the cua-driver (Computer Use) install"
            echo "  --no-skills    Start with a blank slate — seed no bundled skills, and"
            echo "                   write \$HERMES_HOME/.no-bundled-skills so future"
            echo "                   'ansatz update' runs never inject bundled skills either"
            echo "  --branch NAME  Git branch to install (default: main)"
            echo "  --commit SHA   Pin checkout to a specific commit after clone/update"
            echo "                   (ignored when it would roll an existing install back)"
            echo "  --force-commit Apply --commit even if it rolls the install backwards"
            echo "  --manifest     Print desktop bootstrap stage manifest as JSON"
            echo "  --stage NAME   Run one desktop bootstrap stage"
            echo "  --json         Print a JSON result frame for --stage"
            echo "  --non-interactive  Skip stages that require user input"
            echo "  --include-desktop  Also build the desktop app (apps/desktop -> Hermes.app)"
            echo "  --bundled-source  Use verified Desktop source already present at --dir;"
            echo "                    never clone, fetch, pull, or check out repository data"
            echo "  --bundled-toolchain PATH  Verified Desktop authentication toolchain"
            echo "  --bootstrap-scope SCOPE  Desktop bootstrap scope: auth or runtime"
            echo "  --desktop-product PRODUCT  Desktop launcher profile (ansatz-voice-trace)"
            echo "  --dir PATH     Installation directory"
            echo "                   default (non-root):  ~/.hermes/hermes-agent"
            echo "                   default (root, Linux): /usr/local/lib/hermes-agent"
            echo "  --hermes-home PATH  Data directory (default: ~/.hermes, or \$HERMES_HOME)"
            echo "  -h, --help     Show this help"
            echo ""
            echo "Notes:"
            echo "  When running as root on Linux, Hermes installs the code under"
            echo "  /usr/local/lib/hermes-agent and links the command into"
            echo "  /usr/local/bin/hermes (FHS layout — matches Claude Code / Codex CLI)."
            echo "  Data, config, sessions, and logs still live in \$HERMES_HOME"
            echo "  (default /root/.hermes).  This keeps Docker bind-mounted volumes"
            echo "  small and ensures the command is on PATH for all shells."
            echo "  Existing installs at \$HERMES_HOME/hermes-agent are preserved in-place."
            echo "  --ensure DEPS  Install only specified deps (comma-separated)"
            echo "                   Supported: node, browser, ripgrep, ffmpeg"
            echo "                   Does NOT clone repo or create venv"

            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================================
# Helper functions
# ============================================================================

print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│             ⚕ Hermes Agent Installer                    │"
    echo "├─────────────────────────────────────────────────────────┤"
    echo "│  An open source AI agent by Nous Research.              │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
}

log_info() {
    echo -e "${CYAN}→${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

json_escape() {
    # Enough for short installer status strings; avoids requiring jq during
    # pre-install bootstrap.
    printf '%s' "$1" | tr '\n' ' ' | sed \
        -e 's/\\/\\\\/g' \
        -e 's/"/\\"/g'
}

# npm rewrites tracked package-lock.json files non-deterministically during
# `npm install` / `npm run pack`. On a managed install those diffs are never
# intentional, but they leave the checkout dirty — which forces `ansatz update`
# to autostash on every run and makes branch switches fragile. Restore them so
# a fresh install ends with a clean tree. Best-effort; only touches lockfiles.
restore_dirty_lockfiles() {
    local repo="${1:-$INSTALL_DIR}"
    [ -n "$repo" ] && [ -d "$repo/.git" ] || return 0
    command -v git >/dev/null 2>&1 || return 0
    local dirty
    dirty=$(git -C "$repo" diff --name-only 2>/dev/null | grep 'package-lock\.json$' || true)
    [ -z "$dirty" ] && return 0
    echo "$dirty" | while IFS= read -r f; do
        [ -n "$f" ] && git -C "$repo" checkout -- "$f" 2>/dev/null || true
    done
}

# npm rewrites tracked package-lock.json files non-deterministically during
# local builds. On a managed install those diffs are usually runtime churn, not
# intentional user edits, so discard them before the repository-stage stash.
# If package.json in the same directory is also dirty we keep both changes.
discard_update_lockfile_churn() {
    local repo="${1:-$INSTALL_DIR}"
    [ -n "$repo" ] && [ -d "$repo/.git" ] || return 0
    command -v git >/dev/null 2>&1 || return 0

    local dirty_diff
    dirty_diff=$(git -C "$repo" diff --name-only 2>/dev/null) || return 0
    [ -n "$dirty_diff" ] || return 0

    local dirty_package_dirs=""
    while IFS= read -r path; do
        case "$path" in
            *package.json)
                dirty_package_dirs="${dirty_package_dirs}$(dirname "$path")"$'\n'
                ;;
        esac
    done <<EOF
$dirty_diff
EOF

    local dirty_locks=""
    local dirty_count=0
    while IFS= read -r path; do
        case "$path" in
            *package-lock.json)
                local lock_dir
                lock_dir=$(dirname "$path")
                case $'\n'"$dirty_package_dirs" in
                    *$'\n'"$lock_dir"$'\n'*) continue ;;
                esac
                dirty_locks="${dirty_locks}${path}"$'\n'
                dirty_count=$((dirty_count + 1))
                ;;
        esac
    done <<EOF
$dirty_diff
EOF

    [ "$dirty_count" -gt 0 ] || return 0
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        git -C "$repo" checkout -- "$path" 2>/dev/null || true
    done <<EOF
$dirty_locks
EOF
    log_info "Discarded npm lockfile churn (${dirty_count} file(s))"
}

emit_manifest() {
    # Stage-Desktop is included only with --include-desktop, mirroring
    # install.ps1: the signed bootstrap installer (Hermes-Setup) passes it so
    # a GUI install ends up with a launchable app; the Electron app's own
    # first-launch bootstrap and the CLI one-liner omit it (building the
    # desktop from inside the already-running app would clobber it).
    if [ "$BOOTSTRAP_SCOPE" = "auth" ]; then
        printf '%s\n' '{"protocol_version":1,"bootstrap_scope":"auth","stages":[{"name":"auth-prerequisites","title":"Prepare authentication runtime","category":"auth","needs_user_input":false},{"name":"repository","title":"Prepare signed Hermes source","category":"auth","needs_user_input":false},{"name":"venv","title":"Create authentication environment","category":"auth","needs_user_input":false},{"name":"python-auth-deps","title":"Install authentication dependencies","category":"auth","needs_user_input":false},{"name":"auth-complete","title":"Finish authentication runtime","category":"auth","needs_user_input":false}]}'
        return
    fi

    local desktop_stage=""
    if [ "$INCLUDE_DESKTOP" = true ]; then
        desktop_stage='{"name":"desktop","title":"Build desktop app","category":"runtime","needs_user_input":false},'
    fi
    printf '%s' '{"protocol_version":1,"bootstrap_scope":"runtime","stages":[{"name":"prerequisites","title":"System prerequisites","category":"runtime","needs_user_input":false},{"name":"repository","title":"Download Hermes Agent","category":"runtime","needs_user_input":false},{"name":"venv","title":"Create Python virtual environment","category":"runtime","needs_user_input":false},{"name":"python-deps","title":"Install Python dependencies","category":"runtime","needs_user_input":false},{"name":"node-deps","title":"Install browser-tool dependencies","category":"runtime","needs_user_input":false},{"name":"path","title":"Install hermes command","category":"runtime","needs_user_input":false},{"name":"config","title":"Prepare config and skills","category":"configuration","needs_user_input":false},{"name":"setup","title":"Configure API keys and settings","category":"configuration","needs_user_input":true},{"name":"gateway","title":"Configure gateway service","category":"configuration","needs_user_input":true},'"$desktop_stage"'{"name":"complete","title":"Finish install","category":"runtime","needs_user_input":false}]}'
    printf '\n'
}

stage_needs_user_input() {
    case "$1" in
        setup|gateway) return 0 ;;
        *) return 1 ;;
    esac
}

emit_stage_json() {
    local stage="$1"
    local ok="$2"
    local skipped="${3:-false}"
    local reason="${4:-}"
    local escaped_reason
    escaped_reason="$(json_escape "$reason")"
    if [ -n "$escaped_reason" ]; then
        printf '{"ok":%s,"stage":"%s","skipped":%s,"reason":"%s"}\n' "$ok" "$stage" "$skipped" "$escaped_reason"
    else
        printf '{"ok":%s,"stage":"%s","skipped":%s}\n' "$ok" "$stage" "$skipped"
    fi
}

prompt_yes_no() {
    local question="$1"
    local default="${2:-yes}"
    local prompt_suffix
    local answer=""

    # Use case patterns (not ${var,,}) so this works on bash 3.2 (macOS /bin/bash).
    case "$default" in
        [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) prompt_suffix="[Y/n]" ;;
        *) prompt_suffix="[y/N]" ;;
    esac

    if [ "$NON_INTERACTIVE" = true ]; then
        answer=""
    elif [ "$IS_INTERACTIVE" = true ]; then
        read -r -p "$question $prompt_suffix " answer || answer=""
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s %s " "$question" "$prompt_suffix" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=""
    else
        answer=""
    fi

    answer="${answer#"${answer%%[![:space:]]*}"}"
    answer="${answer%"${answer##*[![:space:]]}"}"

    if [ -z "$answer" ]; then
        case "$default" in
            [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) return 0 ;;
            *) return 1 ;;
        esac
    fi

    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

is_termux() {
    [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *"com.termux/files/usr"* ]]
}

# Decide where the repo checkout + venv live, and where the `hermes` command
# symlink goes.  Called after detect_os so $OS/$DISTRO are known.
#
# Defaults:
#   - Non-root, any OS:       INSTALL_DIR = $HERMES_HOME/hermes-agent
#                             command link in $HOME/.local/bin
#   - Termux (any uid):       INSTALL_DIR = $HERMES_HOME/hermes-agent
#                             command link in $PREFIX/bin (already on PATH)
#   - Root on Linux (new):    INSTALL_DIR = /usr/local/lib/hermes-agent
#                             command link in /usr/local/bin
#                             (unless a legacy install already exists at
#                              $HERMES_HOME/hermes-agent — then preserve it)
#
# Always no-op when the user set --dir or $HERMES_INSTALL_DIR.
resolve_install_layout() {
    if [ "$INSTALL_DIR_EXPLICIT" = true ]; then
        log_info "Install directory: $INSTALL_DIR (explicit)"
        return 0
    fi

    # Termux: package manager manages /data/data/..., keep code in HERMES_HOME.
    if is_termux; then
        INSTALL_DIR="$HERMES_HOME/hermes-agent"
        return 0
    fi

    # Root on Linux: prefer FHS layout unless a legacy install already exists.
    # macOS root installs keep the legacy layout because /usr/local/ on macOS
    # is Homebrew territory and we don't want to fight that.
    if [ "$OS" = "linux" ] && [ "$(id -u)" -eq 0 ]; then
        if [ -d "$HERMES_HOME/hermes-agent/.git" ]; then
            INSTALL_DIR="$HERMES_HOME/hermes-agent"
            log_info "Existing install detected at $INSTALL_DIR — keeping legacy layout"
            log_info "  (new root installs use /usr/local/lib/hermes-agent)"
            return 0
        fi
        INSTALL_DIR="/usr/local/lib/hermes-agent"
        ROOT_FHS_LAYOUT=true
        # Place uv-managed Python under /usr/local/share so the venv interpreter
        # is world-readable.  Default uv paths land in /root/.local/share/uv,
        # which non-root users can't traverse — leaving the shared
        # /usr/local/bin/hermes wrapper unable to exec the bad-interpreter venv
        # python.  See #21457.
        export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/usr/local/share/uv/python}"
        export UV_PYTHON_BIN_DIR="${UV_PYTHON_BIN_DIR:-/usr/local/share/uv/bin}"
        log_info "Root install on Linux — using FHS layout"
        log_info "  Code:    $INSTALL_DIR"
        log_info "  Command: /usr/local/bin/hermes"
        log_info "  Data:    $HERMES_HOME (unchanged)"
        log_info "  uv Python: $UV_PYTHON_INSTALL_DIR (world-readable)"
        return 0
    fi

    # Default: non-root, non-Termux → legacy user-scoped layout.
    INSTALL_DIR="$HERMES_HOME/hermes-agent"
}

get_command_link_dir() {
    if is_termux && [ -n "${PREFIX:-}" ]; then
        echo "$PREFIX/bin"
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo "/usr/local/bin"
    else
        echo "$HOME/.local/bin"
    fi
}

get_command_link_display_dir() {
    if is_termux && [ -n "${PREFIX:-}" ]; then
        echo '$PREFIX/bin'
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo '/usr/local/bin'
    else
        echo '~/.local/bin'
    fi
}

# Point a Hermes-managed Node's `npm install -g` at a directory that is on
# PATH. npm's default global prefix for a bundled Node is the Node dir itself,
# so global package binaries land in $HERMES_HOME/node/bin — which is NOT on
# PATH (only the command link dir is) and is wiped on every Node upgrade.
# Redirecting the prefix to the link dir's parent makes global bins resolve to
# the command link dir (node/npm/npx live there too, already on PATH) and
# survive upgrades. Scoped to the managed Node via its prefix-local global
# npmrc, so the user's other Node installs and their ~/.npmrc are untouched.
# Hermes's own global installs pass an explicit --prefix and are unaffected.
# Idempotent and a no-op when there is no Hermes-managed npm, so calling it on
# every install run repairs pre-existing installs, not just fresh ones.
configure_managed_node_npm_prefix() {
    [ -x "$HERMES_HOME/node/bin/npm" ] || return 0
    local link_dir
    link_dir="$(get_command_link_dir)"
    mkdir -p "$HERMES_HOME/node/etc"
    printf 'prefix=%s\n' "$(dirname "$link_dir")" > "$HERMES_HOME/node/etc/npmrc"
}

get_hermes_command_path() {
    local link_dir
    link_dir="$(get_command_link_dir)"
    if [ -x "$link_dir/hermes" ]; then
        echo "$link_dir/hermes"
    else
        echo "hermes"
    fi
}

# ============================================================================
# System detection
# ============================================================================

detect_os() {
    case "$(uname -s)" in
        Linux*)
            if is_termux; then
                OS="android"
                DISTRO="termux"
            else
                OS="linux"
                if [ -f /etc/os-release ]; then
                    . /etc/os-release
                    DISTRO="$ID"
                    # VERSION_ID (e.g. "26.04", "14") lets us tell whether the
                    # apt release is newer than the newest one Playwright's
                    # platform resolver recognizes — the #35166 hang condition.
                    DISTRO_VERSION="${VERSION_ID:-}"
                else
                    DISTRO="unknown"
                    DISTRO_VERSION=""
                fi
            fi
            ;;
        Darwin*)
            OS="macos"
            DISTRO="macos"
            ;;
        CYGWIN*|MINGW*|MSYS*)
            OS="windows"
            DISTRO="windows"
            log_error "Windows detected. Use the reviewed PowerShell installer supplied by your administrator."
            exit 1
            ;;
        *)
            OS="unknown"
            DISTRO="unknown"
            log_warn "Unknown operating system"
            ;;
    esac

    log_success "Detected: $OS ($DISTRO)"
}

# ============================================================================
# Dependency checks
# ============================================================================

validate_bundled_toolchain_root() {
    [ -n "$BUNDLED_TOOLCHAIN_ROOT" ] || return 1

    case "$BUNDLED_TOOLCHAIN_ROOT" in
        /*) ;;
        *) log_error "Bundled authentication toolchain path must be absolute"; return 1 ;;
    esac

    if [ ! -d "$BUNDLED_TOOLCHAIN_ROOT" ] || [ -L "$BUNDLED_TOOLCHAIN_ROOT" ]; then
        log_error "Bundled authentication toolchain root is unavailable"
        return 1
    fi

    local required
    for required in manifest.json uv.gz python.tar.gz auth-requirements.txt; do
        if [ ! -f "$BUNDLED_TOOLCHAIN_ROOT/$required" ] || [ -L "$BUNDLED_TOOLCHAIN_ROOT/$required" ]; then
            log_error "Bundled authentication toolchain asset is unavailable: $required"
            return 1
        fi
    done

    if [ ! -d "$BUNDLED_TOOLCHAIN_ROOT/wheelhouse" ] || [ -L "$BUNDLED_TOOLCHAIN_ROOT/wheelhouse" ]; then
        log_error "Bundled authentication wheelhouse is unavailable"
        return 1
    fi

    return 0
}

validate_bundled_runtime_mirrors() {
    if [ "$BUNDLED_SOURCE" != true ] || [ "$BOOTSTRAP_SCOPE" != "runtime" ]; then
        return 0
    fi

    if [ "${UV_DEFAULT_INDEX:-}" != "$DESKTOP_PYTHON_PRIMARY_MIRROR" ] || \
       [ "${HERMES_UV_FALLBACK_INDEX:-}" != "$DESKTOP_PYTHON_FALLBACK_MIRROR" ] || \
       [ "${NPM_CONFIG_REGISTRY:-}" != "$DESKTOP_NPM_REGISTRY" ] || \
       [ "${HERMES_NODE_MIRROR:-}" != "$DESKTOP_NODE_MIRROR" ] || \
       [ "${PLAYWRIGHT_DOWNLOAD_HOST:-}" != "$DESKTOP_PLAYWRIGHT_MIRROR" ]; then
        log_error "Bundled Desktop runtime mirror policy is unavailable"
        return 1
    fi

    return 0
}

install_bundled_uv() {
    validate_bundled_toolchain_root || return 1
    mkdir -p "$HERMES_HOME/bin"

    local managed_uv="$HERMES_HOME/bin/uv"
    local temporary_uv="$managed_uv.tmp.$$"
    rm -f "$temporary_uv"
    if ! gzip -dc "$BUNDLED_TOOLCHAIN_ROOT/uv.gz" > "$temporary_uv"; then
        rm -f "$temporary_uv"
        log_error "Bundled uv archive extraction failed"
        return 1
    fi
    chmod 0755 "$temporary_uv"

    if ! "$temporary_uv" --version 2>/dev/null | grep -Eq '^uv [0-9]+\.[0-9]+\.[0-9]+'; then
        rm -f "$temporary_uv"
        log_error "Bundled uv executable validation failed"
        return 1
    fi

    mv -f "$temporary_uv" "$managed_uv"
    UV_CMD="$managed_uv"
    UV_VERSION=$($UV_CMD --version 2>/dev/null)
    log_success "Managed uv installed from the signed Desktop payload ($UV_VERSION)"
}

install_bundled_python() {
    validate_bundled_toolchain_root || return 1
    export UV_PYTHON_INSTALL_DIR="$HERMES_HOME/python"
    export UV_PYTHON_BIN_DIR="$HERMES_HOME/python-bin"

    local archive="$BUNDLED_TOOLCHAIN_ROOT/python.tar.gz"
    local entry root_name temporary_root target_root python_bin
    root_name=""

    while IFS= read -r entry; do
        [ -n "$entry" ] || continue
        case "$entry" in
            /*|../*|*/../*|*'/..')
                log_error "Bundled Python archive contains an unsafe path"
                return 1
                ;;
        esac
        local candidate="${entry%%/*}"
        [ -n "$candidate" ] || continue
        if [ -z "$root_name" ]; then
            root_name="$candidate"
        elif [ "$candidate" != "$root_name" ]; then
            log_error "Bundled Python archive must contain one runtime root"
            return 1
        fi
    done < <(tar -tzf "$archive")

    case "$root_name" in
        cpython-3.11.*-macos-aarch64-none) ;;
        *) log_error "Bundled Python runtime target is invalid"; return 1 ;;
    esac

    target_root="$UV_PYTHON_INSTALL_DIR/$root_name"
    python_bin="$target_root/bin/python3.11"
    if [ -x "$python_bin" ] && "$python_bin" --version 2>&1 | grep -Eq '^Python 3\.11\.'; then
        return 0
    fi

    temporary_root="$HERMES_HOME/.python-install.$$"
    rm -rf "$temporary_root"
    mkdir -p "$temporary_root"
    if ! tar -xzf "$archive" -C "$temporary_root"; then
        rm -rf "$temporary_root"
        log_error "Bundled Python runtime extraction failed"
        return 1
    fi

    if [ ! -x "$temporary_root/$root_name/bin/python3.11" ] || \
       ! "$temporary_root/$root_name/bin/python3.11" --version 2>&1 | grep -Eq '^Python 3\.11\.'; then
        rm -rf "$temporary_root"
        log_error "Bundled Python runtime validation failed"
        return 1
    fi

    mkdir -p "$UV_PYTHON_INSTALL_DIR"
    rm -rf "$target_root"
    mv "$temporary_root/$root_name" "$target_root"
    rm -rf "$temporary_root"
    log_success "Python 3.11 installed from the signed Desktop payload"
}

install_uv() {
    if [ "$DISTRO" = "termux" ]; then
        log_info "Termux detected — using Python's stdlib venv + pip instead of uv"
        UV_CMD=""
        return 0
    fi

    if [ -n "$BUNDLED_TOOLCHAIN_ROOT" ]; then
        install_bundled_uv
        return $?
    fi

    # Hermes owns its own uv at $HERMES_HOME/bin/uv.  Always install there —
    # no PATH probing, no conda guards, no multi-location resolution chains.
    # The runtime update path (hermes_cli/managed_uv.py) looks in the same
    # place, so install.sh and `ansatz update` stay in sync.
    local _managed_uv="$HERMES_HOME/bin/uv"

    if [ -x "$_managed_uv" ]; then
        UV_CMD="$_managed_uv"
        UV_VERSION=$($UV_CMD --version 2>/dev/null)
        log_success "Managed uv found ($UV_VERSION)"
        return 0
    fi

    log_info "Preparing managed uv in $HERMES_HOME/bin ..."
    mkdir -p "$HERMES_HOME/bin"

    # Packaged Desktop always supplies a hash-verified uv payload. A source
    # install may adopt an already installed executable, but never downloads
    # and executes a remote installer script.
    local _existing_uv
    _existing_uv="$(command -v uv 2>/dev/null || true)"
    if [ -n "$_existing_uv" ] && [ -x "$_existing_uv" ]; then
        cp "$_existing_uv" "$_managed_uv.tmp.$$"
        chmod 0755 "$_managed_uv.tmp.$$"
        if "$_managed_uv.tmp.$$" --version 2>/dev/null | grep -Eq '^uv [0-9]+\.[0-9]+\.[0-9]+'; then
            mv -f "$_managed_uv.tmp.$$" "$_managed_uv"
            UV_CMD="$_managed_uv"
            UV_VERSION=$($UV_CMD --version 2>/dev/null)
            log_success "Managed uv adopted from the local installation ($UV_VERSION)"
            return 0
        fi
        rm -f "$_managed_uv.tmp.$$"
    fi

    log_error "A verified uv payload is unavailable."
    log_info "Use the packaged Desktop installer or ask an administrator to place a reviewed uv binary at $_managed_uv."
    return 1
}

check_python() {
    if [ "$DISTRO" = "termux" ]; then
        log_info "Checking Termux Python..."
        if command -v python >/dev/null 2>&1; then
            PYTHON_PATH="$(command -v python)"
            if "$PYTHON_PATH" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
                log_success "Python found: $PYTHON_FOUND_VERSION"
                return 0
            fi
        fi

        log_info "Installing Python via pkg..."
        pkg install -y python >/dev/null
        PYTHON_PATH="$(command -v python)"
        PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
        log_success "Python installed: $PYTHON_FOUND_VERSION"
        return 0
    fi

    log_info "Checking Python $PYTHON_VERSION..."

    if [ -n "$BUNDLED_TOOLCHAIN_ROOT" ]; then
        install_bundled_python || exit 1
    fi

    # Let uv handle Python — it can download and manage Python versions
    # First check if a suitable Python is already available
    if PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION" 2>/dev/null)"; then
        PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
        log_success "Python found: $PYTHON_FOUND_VERSION"
        return 0
    fi

    # Python not found — use uv to install it (no sudo needed!)
    log_info "Python $PYTHON_VERSION not found, installing via uv..."
    if "$UV_CMD" python install "$PYTHON_VERSION"; then
        PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION")"
        PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
        log_success "Python installed: $PYTHON_FOUND_VERSION"
    else
        log_error "Failed to install Python $PYTHON_VERSION"
        log_info "Install Python $PYTHON_VERSION manually, then re-run this script"
        exit 1
    fi
}

# Best-effort automatic git provisioning, mirroring install.ps1's Install-Git
# (which downloads PortableGit on Windows). git is required to clone the repo,
# and a fresh "normie" machine with no developer tools won't have it. Returns 0
# if git is available afterwards, non-zero otherwise (caller prints manual
# instructions and aborts).
attempt_install_git() {
    case "$OS" in
        macos)
            # Prefer Homebrew — fully headless when present.
            if command -v brew >/dev/null 2>&1; then
                log_info "Installing Git via Homebrew..."
                brew install git >/dev/null 2>&1 || true
                command -v git >/dev/null 2>&1 && return 0
            fi
            # Fall back to Apple Command Line Tools, which provide git AND the
            # compiler some Python wheels need. `xcode-select --install` pops a
            # system dialog (Apple gates CLT behind it — it cannot be fully
            # silent without MDM), so we trigger it and poll for git to appear.
            if command -v xcode-select >/dev/null 2>&1; then
                log_info "Requesting Apple Command Line Tools (provides git + compiler)..."
                log_info "If a macOS dialog appears, click \"Install\" and accept the license."
                xcode-select --install >/dev/null 2>&1 || true
                local waited=0
                local timeout=900
                while [ "$waited" -lt "$timeout" ]; do
                    if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
                        return 0
                    fi
                    sleep 5
                    waited=$((waited + 5))
                    if [ $((waited % 60)) -eq 0 ]; then
                        log_info "Still waiting for Command Line Tools install ($((waited / 60))m)..."
                    fi
                done
            fi
            return 1
            ;;
        linux)
            local sudo_cmd=""
            if [ "$(id -u 2>/dev/null || echo 1000)" -ne 0 ]; then
                command -v sudo >/dev/null 2>&1 && sudo_cmd="sudo"
            fi
            case "$DISTRO" in
                ubuntu|debian)
                    log_info "Installing Git via apt..."
                    $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
                    $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git >/dev/null 2>&1 || true
                    ;;
                fedora)
                    log_info "Installing Git via dnf..."
                    $sudo_cmd dnf install -y git >/dev/null 2>&1 || true
                    ;;
                arch)
                    log_info "Installing Git via pacman..."
                    $sudo_cmd pacman -S --noconfirm git >/dev/null 2>&1 || true
                    ;;
                *)
                    return 1
                    ;;
            esac
            command -v git >/dev/null 2>&1 && return 0
            return 1
            ;;
    esac
    return 1
}

check_git() {
    log_info "Checking Git..."

    # On fresh macOS /usr/bin/git is a stub that exits non-zero until CLT is installed.
    if command -v git &> /dev/null && git --version &> /dev/null; then
        GIT_VERSION=$(git --version | awk '{print $3}')
        log_success "Git $GIT_VERSION found"
        return 0
    fi

    log_error "Git not found"

    if [ "$DISTRO" = "termux" ]; then
        log_info "Installing Git via pkg..."
        pkg install -y git >/dev/null
        if command -v git >/dev/null 2>&1; then
            GIT_VERSION=$(git --version | awk '{print $3}')
            log_success "Git $GIT_VERSION installed"
            return 0
        fi
    fi

    # Try to install it automatically before giving up (parity with install.ps1).
    log_info "Attempting to install Git automatically..."
    if attempt_install_git; then
        GIT_VERSION=$(git --version | awk '{print $3}')
        log_success "Git $GIT_VERSION installed"
        return 0
    fi

    log_warn "Could not install Git automatically. Please install it manually:"

    case "$OS" in
        linux)
            case "$DISTRO" in
                ubuntu|debian)
                    log_info "  sudo apt update && sudo apt install git"
                    ;;
                fedora)
                    log_info "  sudo dnf install git"
                    ;;
                arch)
                    log_info "  sudo pacman -S git"
                    ;;
                *)
                    log_info "  Use your package manager to install git"
                    ;;
            esac
            ;;
        android)
            log_info "  pkg install git"
            ;;
        macos)
            log_info "  xcode-select --install"
            log_info "  Or: brew install git"
            ;;
    esac

    exit 1
}

# The dependency tree's real Node floor is >=22.22.0, set by react-router 8.3.0
# (`engines.node`), with Vite ^8 next at `^20.19 || >=22.12`. Keep this in sync
# with the root package.json — a gate looser than the manifest lets an install
# proceed to a `npm ci` that then dies with EBADENGINE, and a gate stricter than
# the manifest replaces a working user toolchain for nothing. Returns 0 when the
# given `node --version` string clears the floor; anything below it is replaced
# with the Hermes-managed Node $NODE_VERSION.
node_satisfies_build() {
    local ver="${1#v}"
    local major="${ver%%.*}"
    local minor="${ver#*.}"; minor="${minor%%.*}"
    case "$major" in ''|*[!0-9]*) return 1 ;; esac
    case "$minor" in ''|*[!0-9]*) minor=0 ;; esac
    if [ "$major" -ge 22 ] && { [ "$major" -gt 22 ] || [ "$minor" -ge 22 ]; }; then return 0; fi
    return 1
}

# npm 11.10.0–11.16.x honor `min-release-age` but ignore
# `min-release-age-exclude`, both of which `.npmrc` sets. That combination
# applies the 14-day age gate to packages we deliberately exempted, so every
# install fails ETARGET on a freshly published dependency. The root
# package.json excludes that band via `engines.npm`, and `engine-strict=true`
# makes it fatal — so a system npm in the band cannot install this repo, no
# matter how new its Node is. Returns 0 when the npm is usable.
npm_supports_npmrc() {
    local ver="${1#v}"
    local major="${ver%%.*}"
    local minor="${ver#*.}"; minor="${minor%%.*}"
    case "$major" in ''|*[!0-9]*) return 1 ;; esac
    case "$minor" in ''|*[!0-9]*) minor=0 ;; esac
    # The bad band is 11.10.0 through 11.16.x.
    if [ "$major" -eq 11 ] && [ "$minor" -ge 10 ] && [ "$minor" -le 16 ]; then
        return 1
    fi
    return 0
}

# The npm range the install manifest demands. Read from engines.npm rather
# than duplicated here so the two can never drift; falls back to the current
# range when the manifest is unreadable.
managed_npm_engines_range() {
    local manifest="$INSTALL_DIR/package.json" range
    if [ -r "$manifest" ]; then
        # sed, not node: this runs before a usable node is guaranteed.
        range=$(sed -n '/"engines"/,/}/p' "$manifest" \
            | sed -n 's/.*"npm"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            | head -1)
        if [ -n "$range" ]; then
            printf '%s\n' "$range"
            return 0
        fi
    fi
    printf '<11.10.0 || >=11.17.0\n'
}

# Upgrade the managed tree's bundled npm out of the .npmrc-incompatible band.
#
# The reviewed latest-v26.x mirrors can serve a Node whose bundled npm sits in
# the 11.10-11.16 band that engines.npm excludes (v26.0.0 bundles 11.12.1), so
# the toolchain the installer just provisioned failed its very first
# `npm install` with EBADENGINE. Provision a compatible npm here instead.
#
# Three details are load-bearing, mirroring _nb_ensure_bundled_npm_range in
# scripts/lib/node-bootstrap.sh:
#   - a temp cwd, so the checkout's own .npmrc (engine-strict, min-release-age)
#     does not gate the very upgrade meant to satisfy it;
#   - npm_config_min_release_age=0, which also neutralises a user ~/.npmrc;
#   - an explicit --prefix at the managed tree, because
#     configure_managed_node_npm_prefix pointed global installs at the command
#     link dir, and without the override this installs a second npm elsewhere
#     while the managed tree stays stale.
#
# Best-effort: a failure here leaves a working Node with an old npm, and the
# node-deps `npm install` that follows fails loudly rather than silently.
ensure_managed_npm_compatible() {
    local npm_bin="$HERMES_HOME/node/bin/npm"
    [ -x "$npm_bin" ] || return 0

    local bundled_version
    bundled_version="$(PATH="$HERMES_HOME/node/bin:$PATH" "$npm_bin" --version 2>/dev/null)"
    if npm_supports_npmrc "$bundled_version"; then
        return 0
    fi

    local range
    range="$(managed_npm_engines_range)"
    log_info "Bundled npm ${bundled_version:-unavailable} cannot honor this repo's .npmrc — upgrading to npm@$range..."
    local tmp_cwd
    tmp_cwd=$(mktemp -d)
    if (
        cd "$tmp_cwd" || exit 1
        CI=1 npm_config_min_release_age=0 \
            PATH="$HERMES_HOME/node/bin:$PATH" \
            run_npm_with_mirror_fallback "$NODE_DEPS_TIMEOUT" \
            "$npm_bin" install --global \
                --prefix "$HERMES_HOME/node" \
                "npm@$range" \
                --no-fund --no-audit --progress=false >/dev/null 2>&1
    ); then
        rm -rf "$tmp_cwd"
        local upgraded_version
        upgraded_version="$(PATH="$HERMES_HOME/node/bin:$PATH" "$npm_bin" --version 2>/dev/null)"
        if npm_supports_npmrc "$upgraded_version"; then
            log_success "npm $upgraded_version installed (Hermes-managed)"
            return 0
        fi
        log_warn "npm upgrade command completed but managed npm is still ${upgraded_version:-unavailable}."
        return 1
    fi

    rm -rf "$tmp_cwd"
    log_warn "Could not upgrade bundled npm to $range — npm install may fail with EBADENGINE."
    log_warn "Fix manually: npm install -g --prefix \"$HERMES_HOME/node\" npm@\"$range\""
    return 1
}

check_node() {
    log_info "Checking Node.js (for browser tools)..."

    # Repair pre-existing Hermes-managed installs where `npm install -g` lands
    # off PATH. No-op when there's no managed Node, so this is safe to run on
    # every install — including re-runs that skip the Node (re)install below.
    configure_managed_node_npm_prefix

    # The system toolchain is only usable when BOTH halves work: a Node new
    # enough for the desktop build AND an npm that can read our .npmrc. A
    # bad-band npm (see npm_supports_npmrc) fails `npm ci` outright, and the
    # managed Node we install instead bundles one that works.
    #
    # npm must actually be reachable, not just node: a stray `node` symlink
    # without a sibling npm (leftover from a node version manager) makes
    # `command -v node` succeed while every later `npm install` silently
    # fails and the desktop build dies with an opaque "Node.js / npm
    # unavailable" (#77003). Node only counts as found when npm resolves on
    # the same PATH.
    if command -v node &> /dev/null && command -v npm &> /dev/null \
        && node_satisfies_build "$(node --version)"; then
        if npm_supports_npmrc "$(npm --version 2>/dev/null)"; then
            log_success "Node.js $(node --version) found"
            HAS_NODE=true
            return 0
        fi
        log_warn "npm $(npm --version) cannot honor this repo's .npmrc (npm 11.10-11.16 ignore"
        log_warn "min-release-age-exclude) — installing Hermes-managed Node $NODE_VERSION instead..."
        install_node
        return
    fi

    # Prefer a Hermes-managed Node from a previous run over a too-old system one.
    if [ -x "$HERMES_HOME/node/bin/node" ] && [ -x "$HERMES_HOME/node/bin/npm" ] \
        && node_satisfies_build "$("$HERMES_HOME/node/bin/node" --version)"; then
        local managed_npm_version
        # Probe with the managed bin dir already on PATH: managed npm is a
        # `#!/usr/bin/env node` script, and a packaged GUI bootstrap has no
        # other node on PATH yet. Probing bare yields an empty version
        # (`env: node: No such file or directory`), which read as
        # incompatible and wiped a perfectly compatible managed tree.
        managed_npm_version="$(PATH="$HERMES_HOME/node/bin:$PATH" "$HERMES_HOME/node/bin/npm" --version 2>/dev/null)"
        if npm_supports_npmrc "$managed_npm_version"; then
            export PATH="$HERMES_HOME/node/bin:$PATH"
            log_success "Node.js $("$HERMES_HOME/node/bin/node" --version) found (Hermes-managed)"
            HAS_NODE=true
            return 0
        fi
        log_warn "npm $managed_npm_version cannot honor this repo's .npmrc (npm 11.10-11.16 ignore"
        log_warn "min-release-age-exclude) — repairing the Hermes-managed npm in place..."
        if ensure_managed_npm_compatible; then
            export PATH="$HERMES_HOME/node/bin:$PATH"
            log_success "Node.js $("$HERMES_HOME/node/bin/node" --version) found (Hermes-managed; npm repaired)"
            HAS_NODE=true
            return 0
        fi
        log_warn "Managed npm repair failed — replacing Hermes-managed Node $NODE_VERSION..."
    fi

    if command -v node &> /dev/null && ! command -v npm &> /dev/null; then
        log_warn "node found but npm is not on PATH (stray node symlink?) — installing Hermes-managed Node $NODE_VERSION LTS..."
    elif command -v node &> /dev/null; then
        log_warn "Node.js $(node --version) is too old (Hermes requires Node >=26) — installing Hermes-managed Node $NODE_VERSION..."
    elif [ "$DISTRO" = "termux" ]; then
        log_info "Node.js not found — installing Node.js via pkg..."
    else
        log_info "Node.js not found — installing Node.js $NODE_VERSION LTS..."
    fi
    install_node
}

install_node() {
    if [ "$DISTRO" = "termux" ]; then
        log_info "Installing Node.js via pkg..."
        if pkg install -y nodejs >/dev/null; then
            local installed_ver
            installed_ver=$(node --version 2>/dev/null)
            log_success "Node.js $installed_ver installed via pkg"
            HAS_NODE=true
        else
            log_warn "Failed to install Node.js via pkg"
            HAS_NODE=false
        fi
        return 0
    fi

    local arch=$(uname -m)
    local node_arch
    case "$arch" in
        x86_64)        node_arch="x64"    ;;
        aarch64|arm64) node_arch="arm64"  ;;
        armv7l)        node_arch="armv7l" ;;
        *)
            log_warn "Unsupported architecture ($arch) for Node.js auto-install"
            log_info "Install manually: https://nodejs.org/en/download/"
            HAS_NODE=false
            return 0
            ;;
    esac

    local node_os
    case "$OS" in
        linux) node_os="linux"  ;;
        macos) node_os="darwin" ;;
        *)
            log_warn "Unsupported OS for Node.js auto-install"
            HAS_NODE=false
            return 0
            ;;
    esac

    local tmp_dir
    tmp_dir=$(mktemp -d)
    local tarball_name=""
    local index_url=""
    local index_page=""
    local node_mirror
    local node_mirrors=(
        "${HERMES_NODE_MIRROR:-$DESKTOP_NODE_MIRROR}"
        "$DESKTOP_NODE_FALLBACK_MIRROR"
        "$DESKTOP_NODE_OFFICIAL_MIRROR"
    )

    for node_mirror in "${node_mirrors[@]}"; do
        index_url="${node_mirror%/}/latest-v${NODE_VERSION}.x/"
        if ! index_page=$(curl --connect-timeout 15 --max-time 60 -fsSL "$index_url"); then
            log_warn "Managed Node.js mirror failed; trying the next reviewed source."
            continue
        fi
        tarball_name=$(printf '%s' "$index_page" \
            | grep -oE "node-v${NODE_VERSION}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.xz" \
            | head -1)
        if [ -z "$tarball_name" ]; then
            tarball_name=$(printf '%s' "$index_page" \
                | grep -oE "node-v${NODE_VERSION}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.gz" \
                | head -1)
        fi
        if [ -z "$tarball_name" ]; then
            log_warn "Node.js archive was not listed by the current managed mirror; trying the next source."
            continue
        fi
        log_info "Downloading $tarball_name..."
        if curl --connect-timeout 15 --max-time 300 -fsSL "${index_url}${tarball_name}" -o "$tmp_dir/$tarball_name"; then
            break
        fi
        tarball_name=""
        log_warn "Managed Node.js download failed; trying the next reviewed source."
    done

    if [ -z "$tarball_name" ] || [ ! -f "$tmp_dir/$tarball_name" ]; then
        log_warn "Could not obtain Node.js $NODE_VERSION for $node_os-$node_arch from the managed sources"
        rm -rf "$tmp_dir"
        HAS_NODE=false
        return 0
    fi

    log_info "Extracting to ~/.hermes/node/..."
    if [[ "$tarball_name" == *.tar.xz ]]; then
        tar xf "$tmp_dir/$tarball_name" -C "$tmp_dir"
    else
        tar xzf "$tmp_dir/$tarball_name" -C "$tmp_dir"
    fi

    local extracted_dir
    extracted_dir=$(ls -d "$tmp_dir"/node-v* 2>/dev/null | head -1)

    if [ ! -d "$extracted_dir" ]; then
        log_warn "Extraction failed"
        rm -rf "$tmp_dir"
        HAS_NODE=false
        return 0
    fi

    # Place into ~/.hermes/node/ and symlink binaries into the same bin dir
    # the hermes command uses (get_command_link_dir): /usr/local/bin for root
    # FHS installs, $PREFIX/bin on Termux, ~/.local/bin otherwise.
    rm -rf "$HERMES_HOME/node"
    mkdir -p "$HERMES_HOME"
    mv "$extracted_dir" "$HERMES_HOME/node"
    rm -rf "$tmp_dir"

    local node_link_dir
    node_link_dir="$(get_command_link_dir)"
    mkdir -p "$node_link_dir"
    ln -sf "$HERMES_HOME/node/bin/node" "$node_link_dir/node"
    ln -sf "$HERMES_HOME/node/bin/npm"  "$node_link_dir/npm"
    ln -sf "$HERMES_HOME/node/bin/npx"  "$node_link_dir/npx"

    configure_managed_node_npm_prefix

    export PATH="$HERMES_HOME/node/bin:$PATH"

    local installed_ver
    installed_ver=$("$HERMES_HOME/node/bin/node" --version 2>/dev/null)
    log_success "Node.js $installed_ver installed to ~/.hermes/node/"

    # The archive may bundle an npm the repo's .npmrc excludes — heal it now,
    # before the first `npm install` can die with EBADENGINE.
    ensure_managed_npm_compatible || true
    HAS_NODE=true
}

check_network_prerequisites() {
    log_info "Checking internet connectivity for package install and web tools..."

    local url
    local failed=false
    local checks=("https://pypi.org/simple/" "https://duckduckgo.com/")
    if [ "$BUNDLED_SOURCE" = true ] && [ "$BOOTSTRAP_SCOPE" = "runtime" ]; then
        checks=("$UV_DEFAULT_INDEX" "$NPM_CONFIG_REGISTRY")
    fi

    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl not found; skipping connectivity probes"
        return 0
    fi

    # Run the probes in parallel — serially, two blocked probes cost
    # 2 × --max-time (16 s) before the user sees any useful error; in
    # parallel the worst case is one --max-time (8 s).
    local pids=()
    local tmpdir
    tmpdir=$(mktemp -d)
    local i=0
    for url in "${checks[@]}"; do
        (
            if curl -fsSI --max-time 8 "$url" >/dev/null 2>&1; then
                : > "$tmpdir/ok_$i"
            fi
        ) &
        pids+=($!)
        i=$((i + 1))
    done
    wait "${pids[@]}" 2>/dev/null

    i=0
    for url in "${checks[@]}"; do
        if [ ! -e "$tmpdir/ok_$i" ]; then
            failed=true
            log_warn "Could not reach $url"
        fi
        i=$((i + 1))
    done
    rm -rf "$tmpdir"

    if [ "$failed" = false ]; then
        log_success "Internet connectivity looks good"
        return 0
    fi

    if [ "$DISTRO" = "termux" ]; then
        log_warn "Termux network prerequisites may be incomplete."
        log_info "Try: pkg install -y ca-certificates curl && pkg update"
        log_info "If mirrors are stale: termux-change-repo"
        log_info "Then test: curl -I https://pypi.org/simple/ && curl -I https://duckduckgo.com/"
    else
        log_warn "Network checks failed. Hermes install may complete, but web search and dependency downloads can fail."
        log_info "Verify internet/DNS and retry if pip install fails."
    fi
}

install_system_packages() {
    # Detect what's missing
    HAS_RIPGREP=false
    HAS_FFMPEG=false
    local need_ripgrep=false
    local need_ffmpeg=false

    log_info "Checking ripgrep (fast file search)..."
    if command -v rg &> /dev/null; then
        log_success "$(rg --version | head -1) found"
        HAS_RIPGREP=true
    else
        need_ripgrep=true
    fi

    log_info "Checking ffmpeg (TTS voice messages)..."
    if command -v ffmpeg &> /dev/null; then
        local ffmpeg_ver=$(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')
        log_success "ffmpeg $ffmpeg_ver found"
        HAS_FFMPEG=true
    else
        need_ffmpeg=true
    fi

    # Termux always needs the Android build toolchain for the tested pip path,
    # even when ripgrep/ffmpeg are already present.
    if [ "$DISTRO" = "termux" ]; then
        local termux_pkgs=(clang rust make pkg-config libffi openssl ca-certificates curl)
        if [ "$need_ripgrep" = true ]; then
            termux_pkgs+=("ripgrep")
        fi
        if [ "$need_ffmpeg" = true ]; then
            termux_pkgs+=("ffmpeg")
        fi

        log_info "Installing Termux packages: ${termux_pkgs[*]}"
        if pkg install -y "${termux_pkgs[@]}" >/dev/null; then
            [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
            [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
            log_success "Termux build dependencies installed"
            return 0
        fi

        log_warn "Could not auto-install all Termux packages"
        log_info "Install manually: pkg install ${termux_pkgs[*]}"
        return 0
    fi

    # Nothing to install — done
    if [ "$need_ripgrep" = false ] && [ "$need_ffmpeg" = false ]; then
        return 0
    fi

    # Build a human-readable description + package list
    local desc_parts=()
    local pkgs=()
    if [ "$need_ripgrep" = true ]; then
        desc_parts+=("ripgrep for faster file search")
        pkgs+=("ripgrep")
    fi
    if [ "$need_ffmpeg" = true ]; then
        desc_parts+=("ffmpeg for TTS voice messages")
        pkgs+=("ffmpeg")
    fi
    local description
    description=$(IFS=" and "; echo "${desc_parts[*]}")

    # ── macOS: brew ──
    if [ "$OS" = "macos" ]; then
        if command -v brew &> /dev/null; then
            log_info "Installing ${pkgs[*]} via Homebrew..."
            if brew install "${pkgs[@]}"; then
                [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                return 0
            fi
        fi
        log_warn "Could not auto-install (brew not found or install failed)"
        log_info "Install manually: brew install ${pkgs[*]}"
        return 0
    fi

    # ── Linux: resolve package manager command ──
    local pkg_install=""
    case "$DISTRO" in
        ubuntu|debian) pkg_install="apt install -y"   ;;
        fedora)        pkg_install="dnf install -y"   ;;
        arch)          pkg_install="pacman -S --noconfirm" ;;
    esac

    if [ -n "$pkg_install" ]; then
        local install_cmd="$pkg_install ${pkgs[*]}"

        # Prevent needrestart/whiptail dialogs from blocking non-interactive installs
        case "$DISTRO" in
            ubuntu|debian) export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a ;;
        esac

        # Already root — just install
        if [ "$(id -u)" -eq 0 ]; then
            log_info "Installing ${pkgs[*]}..."
            if $install_cmd; then
                [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                return 0
            fi
        # Passwordless sudo — just install
        elif command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
            log_info "Installing ${pkgs[*]}..."
            if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd; then
                [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                return 0
            fi
        # sudo needs password — ask once for everything
        elif command -v sudo &> /dev/null; then
            if [ "$IS_INTERACTIVE" = true ]; then
                echo ""
                log_info "sudo is needed ONLY to install optional system packages (${pkgs[*]}) via your package manager."
                log_info "Hermes Agent itself does not require or retain root access."
                if prompt_yes_no "Install ${description}? (requires sudo)" "no"; then
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd; then
                        [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                        [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                        return 0
                    fi
                fi
            elif (: </dev/tty) 2>/dev/null; then
                # Non-interactive (e.g. curl | bash) but a terminal is available.
                # Read the prompt from /dev/tty (same approach the setup wizard uses).
                # Probe by actually opening /dev/tty: a bare existence test passes
                # in Docker builds where the device node is in the mount namespace
                # but opening fails with ENXIO. See #16746.
                echo ""
                log_info "sudo is needed ONLY to install optional system packages (${pkgs[*]}) via your package manager."
                log_info "Hermes Agent itself does not require or retain root access."
                if prompt_yes_no "Install ${description}?" "yes"; then
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd < /dev/tty; then
                        [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                        [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                        return 0
                    fi
                fi
            else
                log_warn "Non-interactive mode and no terminal available — cannot install system packages"
                log_info "Install manually after setup completes: sudo $install_cmd"
            fi
        fi
    fi

    # ── Fallback for ripgrep: cargo ──
    if [ "$need_ripgrep" = true ] && [ "$HAS_RIPGREP" = false ]; then
        if command -v cargo &> /dev/null; then
            log_info "Trying cargo install ripgrep (no sudo needed)..."
            if cargo install ripgrep; then
                log_success "ripgrep installed via cargo"
                HAS_RIPGREP=true
            fi
        fi
    fi

    # ── Show manual instructions for anything still missing ──
    if [ "$HAS_RIPGREP" = false ] && [ "$need_ripgrep" = true ]; then
        log_warn "ripgrep not installed (file search will use grep fallback)"
        show_manual_install_hint "ripgrep"
    fi
    if [ "$HAS_FFMPEG" = false ] && [ "$need_ffmpeg" = true ]; then
        log_warn "ffmpeg not installed (TTS voice messages will be limited)"
        show_manual_install_hint "ffmpeg"
    fi
}

show_manual_install_hint() {
    local pkg="$1"
    log_info "To install $pkg manually:"
    case "$OS" in
        linux)
            case "$DISTRO" in
                ubuntu|debian) log_info "  sudo apt install $pkg" ;;
                fedora)        log_info "  sudo dnf install $pkg" ;;
                arch)          log_info "  sudo pacman -S $pkg"   ;;
                *)             log_info "  Use your package manager or visit the project homepage" ;;
            esac
            ;;
        android)
            log_info "  pkg install $pkg"
            ;;
        macos) log_info "  brew install $pkg" ;;
    esac
}

# ============================================================================
# Installation
# ============================================================================

validate_bundled_source() {
    local marker_path="$INSTALL_DIR/.hermes-bundled-source.json"

    if [ ! -f "$marker_path" ]; then
        log_error "Bundled source marker not found: $marker_path"
        return 1
    fi

    local required_path
    for required_path in \
        "pyproject.toml" \
        "hermes_cli/main.py" \
        "tools/sensevoice_stt.py" \
        "scripts/install.sh"; do
        if [ ! -f "$INSTALL_DIR/$required_path" ]; then
            log_error "Bundled source is missing required file: $required_path"
            return 1
        fi
    done

    if [ -n "$INSTALL_COMMIT" ]; then
        if [ "${#INSTALL_COMMIT}" -ne 40 ]; then
            log_error "Bundled source commit must be exactly 40 hexadecimal characters"
            return 1
        fi
        case "$INSTALL_COMMIT" in
            *[!0-9a-fA-F]*)
                log_error "Bundled source commit must be exactly 40 hexadecimal characters"
                return 1
                ;;
        esac
        if ! grep -Eq '"commit"[[:space:]]*:[[:space:]]*"'"$INSTALL_COMMIT"'"' "$marker_path"; then
            log_error "Bundled source marker does not match requested commit $INSTALL_COMMIT"
            return 1
        fi
    fi

    return 0
}

write_install_method_stamp() {
    local method="git"
    if [ "$BUNDLED_SOURCE" = true ]; then
        method="desktop-bundle"
    fi
    printf '%s\n' "$method" > "$INSTALL_DIR/.install_method"
}

clone_repo() {
    log_info "Installing to $INSTALL_DIR..."

    if [ "$BUNDLED_SOURCE" = true ]; then
        validate_bundled_source || return 1
        cd "$INSTALL_DIR"
        log_success "Using verified backend source bundled with Ansatz"
        return 0
    fi

    log_error "Automatic source download is unavailable without a reviewed bundled payload."
    log_info "Use the packaged Desktop installer supplied by your administrator."
    return 1
}
setup_venv() {
    if [ "$USE_VENV" = false ]; then
        log_info "Skipping virtual environment (--no-venv)"
        return 0
    fi

    if [ "$DISTRO" = "termux" ]; then
        log_info "Creating virtual environment with Termux Python..."

        if [ -d "venv" ]; then
            log_info "Virtual environment already exists, recreating..."
            rm -rf venv
        fi

        "$PYTHON_PATH" -m venv venv
        log_success "Virtual environment ready ($(./venv/bin/python --version 2>/dev/null))"
        return 0
    fi

    log_info "Creating virtual environment with Python $PYTHON_VERSION..."

    if [ -d "venv" ]; then
        log_info "Virtual environment already exists, recreating..."
        rm -rf venv
    fi

    # uv creates the venv and pins the Python version in one step
    $UV_CMD venv venv --python "$PYTHON_VERSION"

    # Neutralize any inherited UV_PYTHON (e.g. UV_PYTHON=3.14 left in the
    # user's shell env). uv honours UV_PYTHON over an existing venv for the
    # later `uv sync` / `uv pip install` tiers, so without this it would
    # silently delete this 3.11 venv and recreate it at the inherited
    # version — building Rust transitives that have no wheel for that
    # version from source via maturin, which fails. Pinning UV_PYTHON to the
    # interpreter we just created forces every subsequent uv command onto it.
    if [ -x "$INSTALL_DIR/venv/bin/python" ]; then
        export UV_PYTHON="$INSTALL_DIR/venv/bin/python"
    fi

    log_success "Virtual environment ready (Python $PYTHON_VERSION)"
}

install_deps() {
    log_info "Installing dependencies..."
    local release_uv_config="$INSTALL_DIR/uv.toml"

    # Re-pin UV_PYTHON to the venv interpreter. setup_venv already does this,
    # but the bootstrap runs install stages (`venv`, `python-deps`) as separate
    # processes, so an export from setup_venv does NOT survive into a separate
    # python-deps invocation. Re-deriving it here covers that path. Without it,
    # an inherited UV_PYTHON=3.14 makes the uv sync/pip tiers below recreate the
    # venv at 3.14 and fail the maturin source build (no cp314 wheels yet).
    if [ "$DISTRO" != "termux" ] && [ -x "$INSTALL_DIR/venv/bin/python" ]; then
        export UV_PYTHON="$INSTALL_DIR/venv/bin/python"
    fi

    if [ "$DISTRO" = "termux" ]; then
        if [ "$USE_VENV" = true ]; then
            export VIRTUAL_ENV="$INSTALL_DIR/venv"
            PIP_PYTHON="$INSTALL_DIR/venv/bin/python"
        else
            PIP_PYTHON="$PYTHON_PATH"
        fi

        if [ -z "${ANDROID_API_LEVEL:-}" ]; then
            ANDROID_API_LEVEL="$(getprop ro.build.version.sdk 2>/dev/null || true)"
            if [ -z "$ANDROID_API_LEVEL" ]; then
                ANDROID_API_LEVEL=24
            fi
            export ANDROID_API_LEVEL
            log_info "Using ANDROID_API_LEVEL=$ANDROID_API_LEVEL for Android wheel builds"
        fi

        "$PIP_PYTHON" -m pip install --upgrade pip setuptools wheel >/dev/null

        # On Android, psutil's setup.py rejects sys.platform == 'android' before
        # it ever invokes the C build, so the next pip install would fail at
        # "platform android is not supported".  Prebuild psutil from the official
        # sdist with a one-line marker patch (Linux source path is fine on
        # Android).  Stopgap until psutil#2762 ships upstream.
        if "$PIP_PYTHON" -c 'import sys; raise SystemExit(0 if sys.platform == "android" else 1)' 2>/dev/null; then
            log_info "Android Python detected: prebuilding psutil compatibility shim..."
            if ! "$PIP_PYTHON" "$INSTALL_DIR/scripts/install_psutil_android.py" --pip "$PIP_PYTHON -m pip"; then
                log_warn "psutil Android prebuild failed — package install will likely fail next."
                log_info "Workaround: manually rerun 'python scripts/install_psutil_android.py' once your toolchain is set up."
            fi
        fi

        # Try the broad Termux profile first (best-effort "install all" for Android),
        # then fall back to the conservative Termux baseline, then base package.
        if ! "$PIP_PYTHON" -m pip install -e '.[termux-all]' -c constraints-termux.txt; then
            log_warn "Termux broad profile (.[termux-all]) failed, trying baseline Termux profile..."
            if ! "$PIP_PYTHON" -m pip install -e '.[termux]' -c constraints-termux.txt; then
                log_warn "Termux baseline profile (.[termux]) failed, trying base install..."
                if ! "$PIP_PYTHON" -m pip install -e '.' -c constraints-termux.txt; then
                    log_error "Package installation failed on Termux."
                    log_info "Ensure these packages are installed: pkg install clang rust make pkg-config libffi openssl ca-certificates curl"
                    log_info "Then re-run: cd $INSTALL_DIR && python -m pip install -e '.[termux-all]' -c constraints-termux.txt"
                    exit 1
                fi
            fi
        fi

        log_success "Main package installed"
        log_info "Termux note: matrix e2ee and local faster-whisper extras are excluded from .[termux-all] due to upstream Android wheel/toolchain blockers."
        log_info "Termux note: browser/WhatsApp tooling is not installed by default; see the Termux guide for optional follow-up steps."

        log_success "All dependencies installed"
        return 0
    fi

    if [ "$USE_VENV" = true ]; then
        # Tell uv to install into our venv (no need to activate)
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi

    # On Debian/Ubuntu (including WSL), some Python packages need build tools.
    # Check and offer to install them if missing.
    if [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
        local need_build_tools=false
        for pkg in gcc python3-dev libffi-dev; do
            if ! dpkg -s "$pkg" &>/dev/null; then
                need_build_tools=true
                break
            fi
        done
        if [ "$need_build_tools" = true ]; then
            log_info "Some build tools may be needed for Python packages..."
            if command -v sudo &> /dev/null; then
                if sudo -n true 2>/dev/null; then
                    sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq build-essential python3-dev libffi-dev >/dev/null 2>&1 || true
                    log_success "Build tools installed"
                else
                    log_info "sudo is needed ONLY to install build tools (build-essential, python3-dev, libffi-dev) via apt."
                    log_info "Hermes Agent itself does not require or retain root access."
                    if prompt_yes_no "Install build tools?" "yes"; then
                        sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq build-essential python3-dev libffi-dev >/dev/null 2>&1 || true
                        log_success "Build tools installed"
                    fi
                fi
            fi
        fi
    fi

    # Install the main package in editable mode with all extras.
    #
    # Hash-verified install (Tier 0) — when uv.lock is present, prefer
    # a lock-backed install. The lockfile records SHA256 hashes for every
    # transitive, so a compromised transitive (different hash than what
    # we shipped) is REJECTED by the resolver. This is the *only* path
    # that protects against the "direct dep is fine, but the dep's dep
    # got worm-poisoned overnight" failure mode. All `uv pip install`
    # tiers below re-resolve transitives fresh from PyPI without any
    # hash verification — they exist to keep installs working when the
    # lockfile is stale, missing, or out-of-sync with the current
    # extras spec, NOT because they're equivalent in posture.
    if [ -f "uv.lock" ]; then
        if [ ! -f "$release_uv_config" ]; then
            if [ "$BUNDLED_SOURCE" = true ]; then
                log_error "The signed Desktop uv configuration is missing."
                return 1
            fi
            release_uv_config=""
        fi
        log_info "Trying tier: hash-verified (uv.lock) ..."
        log_info "(this resolves + downloads the curated [all] set — first run on a"
        log_info " fresh venv can take 1-5 minutes; uv prints progress below)"
        # Stream uv's progress directly to the user instead of swallowing
        # it with `2>"$(mktemp)"`.  Two reasons:
        #   1. `--extra all --locked` against a fresh venv has to pull
        #      every transitive — silencing stderr makes the install
        #      look frozen for minutes on slow networks. Users see
        #      "Trying tier: hash-verified ..." and assume it's hung.
        #   2. The previous `2>"$(mktemp)"` substituted the path at
        #      command-build time but never saved it, so on failure the
        #      uv error message was unreachable — the user just got the
        #      generic "lockfile may be stale" warning.
        #
        # Critical flag choice: `--extra all`, NOT `--all-extras`.
        #   --all-extras = every [project.optional-dependencies] key.
        #                  This bypasses the curated `[all]` extra
        #                  entirely and pulls e.g. [matrix] (which
        #                  needs python-olm + make on Windows) and
        #                  [rl] (git+https deps that fail offline).
        #   --extra all  = install just the `[all]` extra's contents.
        #                  This respects the curation in pyproject.toml.
        # uv's own progress UI handles TTY detection and downgrades
        # gracefully when stdout/stderr aren't terminals.
        local -a release_uv_args=()
        if [ -n "$release_uv_config" ]; then
            release_uv_args=(--config-file "$release_uv_config")
        fi

        # A packaged Desktop install uses fixed domestic mirrors, but uv treats
        # the default index URL as part of lock identity. Running `uv sync
        # --locked` with a mirror override therefore rejects an otherwise
        # current, generic PyPI lock before downloading anything. Export the
        # already-signed lock without network/index overrides, then make both
        # approved mirrors install that exact hash set. This keeps the lock
        # mirror-independent and preserves fail-closed hash verification.
        if [ "$BUNDLED_SOURCE" = true ]; then
            local runtime_requirements_dir="$HERMES_HOME/tmp"
            local runtime_requirements=""
            local bundled_python="${UV_PYTHON:-$INSTALL_DIR/venv/bin/python}"
            mkdir -p "$runtime_requirements_dir"
            runtime_requirements="$(mktemp "$runtime_requirements_dir/runtime-requirements.XXXXXX")" || {
                log_error "Could not create the signed Desktop dependency export."
                return 1
            }

            if ! env -u UV_DEFAULT_INDEX UV_OFFLINE=1 "$UV_CMD" export \
                --frozen \
                --extra all \
                --no-dev \
                --no-emit-project \
                --format requirements.txt \
                --output-file "$runtime_requirements" \
                "${release_uv_args[@]}"; then
                rm -f "$runtime_requirements"
                log_error "The signed Desktop dependency lock could not be exported."
                return 1
            fi

            if [ ! -s "$runtime_requirements" ] || \
               ! grep -q -- '--hash=sha256:' "$runtime_requirements" || \
               grep -Eiq '(https?://|git\+)' "$runtime_requirements"; then
                rm -f "$runtime_requirements"
                log_error "The signed Desktop dependency export failed its safety checks."
                return 1
            fi

            if ! UV_DEFAULT_INDEX="$DESKTOP_PYTHON_PRIMARY_MIRROR" \
                "$UV_CMD" pip sync --require-hashes \
                --python "$bundled_python" "$runtime_requirements"; then
                log_info "Primary Python mirror failed; retrying the same signed hashes with the approved fallback mirror..."
                if ! UV_DEFAULT_INDEX="$HERMES_UV_FALLBACK_INDEX" \
                    "$UV_CMD" pip sync --require-hashes \
                    --python "$bundled_python" "$runtime_requirements"; then
                    rm -f "$runtime_requirements"
                    log_error "The signed Desktop dependency lock could not be installed."
                    log_info "Packaged Hermes will not fall back to an unlocked package resolve."
                    return 1
                fi
            fi

            rm -f "$runtime_requirements"
            if ! env -u UV_DEFAULT_INDEX UV_OFFLINE=1 "$UV_CMD" pip install \
                --no-deps \
                --no-build-isolation \
                --editable "$INSTALL_DIR" \
                --python "$bundled_python"; then
                log_error "The signed Desktop project could not be installed offline."
                return 1
            fi

            log_success "Main package installed (hash-verified via uv.lock)"
            log_success "All dependencies installed"
            return 0
        fi

        if UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/venv" "$UV_CMD" sync --extra all --locked "${release_uv_args[@]}"; then
            log_success "Main package installed (hash-verified via uv.lock)"
            log_success "All dependencies installed"
            return 0
        fi

        log_warn "uv.lock sync failed (see uv output above), falling back to PyPI resolve..."
    else
        if [ "$BUNDLED_SOURCE" = true ]; then
            log_error "The signed Desktop dependency lock is missing."
            return 1
        fi

        log_info "uv.lock not found — falling back to PyPI resolve (no hash verification)"
    fi

    # Multi-tier fallback. The point of the tiers is that ONE compromised
    # PyPI package (a worm-poisoned release that gets quarantined, like
    # mistralai 2.4.6 in May 2026) shouldn't be able to silently demote a
    # fresh install all the way down to "core only" — the user should keep
    # everything else they signed up for.
    #
    # Tier 1: [all] — the curated extra in pyproject.toml.
    # Tier 2: [all] minus the currently-broken extras list (_BROKEN_EXTRAS).
    #         Edit _BROKEN_EXTRAS below when something on PyPI breaks; this
    #         lets users keep the rest of [all] when one transitive is
    #         unavailable. The list of [all]'s contents is parsed from
    #         pyproject.toml at runtime — there is NO hand-mirrored copy
    #         to drift out of sync. If you want to change what [all]
    #         contains, edit pyproject.toml only.
    # Tier 3: bare `.` — last-resort so at least the core CLI launches.
    #         Skipped tiers like "PyPI-only extras (no git deps)" used to
    #         exist to dodge [rl] / [matrix] git+sdist deps; those are no
    #         longer in [all] post-2026-05-12 lazy-install migration, so
    #         a separate PyPI-only tier had no remaining content.
    local _BROKEN_EXTRAS=()  # populate when an extra becomes unresolvable

    # Parse [project.optional-dependencies].all from pyproject.toml.
    # tomllib is stdlib on Python 3.11+ which uv's bootstrap guarantees.
    # Falls back to a hand list if parse fails — defensive only.
    local _ALL_EXTRAS_CSV
    _ALL_EXTRAS_CSV="$(
        "$PYTHON_PATH" - <<'PY' 2>/dev/null
import re, sys, tomllib
try:
    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    specs = data["project"]["optional-dependencies"]["all"]
    extras = []
    for s in specs:
        m = re.search(r"hermes-agent\[([\w-]+)\]", s)
        if m:
            extras.append(m.group(1))
    print(",".join(extras))
except Exception as e:
    print("", file=sys.stderr)
    sys.exit(1)
PY
    )"
    if [ -z "$_ALL_EXTRAS_CSV" ]; then
        log_warn "Could not parse [all] from pyproject.toml; falling back to .[all] only."
        _ALL_EXTRAS_CSV=""
    fi

    # Build "[all] minus broken" spec by filtering the parsed list.
    local _SAFE_SPEC=".[all]"
    if [ -n "$_ALL_EXTRAS_CSV" ] && [ "${#_BROKEN_EXTRAS[@]}" -gt 0 ]; then
        local _SAFE_EXTRAS=()
        local _e _b _skip
        IFS=',' read -ra _ALL_EXTRAS_ARR <<< "$_ALL_EXTRAS_CSV"
        for _e in "${_ALL_EXTRAS_ARR[@]}"; do
            _skip=false
            for _b in "${_BROKEN_EXTRAS[@]}"; do
                if [ "$_e" = "$_b" ]; then _skip=true; break; fi
            done
            if [ "$_skip" = false ]; then _SAFE_EXTRAS+=("$_e"); fi
        done
        _SAFE_SPEC=".[$(IFS=,; echo "${_SAFE_EXTRAS[*]}")]"
    fi

    ALL_INSTALL_LOG=$(mktemp)
    local _installed=false
    local _tier_name=""

    install_tier() {
        local name="$1"; local spec="$2"
        log_info "Trying tier: $name ..."
        if $UV_CMD pip install -e "$spec" 2>"$ALL_INSTALL_LOG"; then
            log_success "Main package installed ($name)"
            _installed=true
            _tier_name="$name"
            return 0
        fi
        log_warn "Tier '$name' failed. Top of pip output:"
        head -5 "$ALL_INSTALL_LOG" | sed 's/^/    /' >&2
        return 1
    }

    install_tier "all" ".[all]" \
        || install_tier "all minus known-broken (${_BROKEN_EXTRAS[*]:-none})" "$_SAFE_SPEC" \
        || install_tier "core only (no extras)" "."

    rm -f "$ALL_INSTALL_LOG"

    if [ "$_installed" = false ]; then
        log_error "Package installation failed even with no extras."
        log_info "Check that build tools are installed: sudo apt install build-essential python3-dev"
        log_info "Then re-run: cd $INSTALL_DIR && uv pip install -e '.[all]'"
        exit 1
    fi

    if [ "$_tier_name" != "all" ]; then
        log_warn "Note: installed via fallback tier ($_tier_name)."
        log_info "Some optional features may be missing. After resolving any"
        log_info "PyPI/network issue, re-run: $UV_CMD pip install -e '.[all]'"
    fi

    log_success "Main package installed"

    log_success "All dependencies installed"
}

install_auth_deps() {
    local auth_project="$INSTALL_DIR/desktop_auth_runtime"
    local auth_uv_config="$auth_project/uv.toml"
    local auth_python="$INSTALL_DIR/venv/bin/python"

    if [ "$DISTRO" = "windows" ]; then
        auth_python="$INSTALL_DIR/venv/Scripts/python.exe"
    fi

    if [ ! -f "$auth_project/pyproject.toml" ] || [ ! -f "$auth_project/uv.lock" ] || [ ! -f "$auth_uv_config" ]; then
        log_error "Signed authentication dependency lock is missing"
        return 1
    fi
    if [ ! -x "$auth_python" ]; then
        log_error "Authentication Python environment is unavailable at $auth_python"
        return 1
    fi

    export VIRTUAL_ENV="$INSTALL_DIR/venv"
    export UV_PYTHON="$auth_python"
    log_info "Installing the locked authentication dependency set..."

    if [ -n "$BUNDLED_TOOLCHAIN_ROOT" ]; then
        validate_bundled_toolchain_root || return 1
        if ! "$UV_CMD" pip sync "$BUNDLED_TOOLCHAIN_ROOT/auth-requirements.txt" \
            --python "$auth_python" \
            --require-hashes \
            --no-index \
            --find-links "$BUNDLED_TOOLCHAIN_ROOT/wheelhouse" \
            --offline \
            --only-binary :all:; then
            log_error "Bundled authentication dependency installation failed"
            return 1
        fi
    # This is deliberately fail-closed. The non-bundled Desktop path never
    # falls back to an unlocked `uv pip install` or another package index when
    # the release lock is missing, stale, or unavailable.
    elif ! UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/venv" \
        "$UV_CMD" sync --project "$auth_project" --locked --no-install-project \
        --config-file "$auth_uv_config"; then
        log_error "Locked authentication dependency installation failed"
        return 1
    fi

    if ! "$auth_python" -c 'import httpx, keyring, hermes_cli.client_auth.bridge'; then
        log_error "Authentication runtime import validation failed"
        return 1
    fi

    log_success "Authentication dependencies installed"
}

write_auth_bootstrap_marker() {
    local marker_path="$INSTALL_DIR/.hermes-auth-bootstrap-complete"
    local marker_tmp="${marker_path}.tmp.$$"

    printf '%s\n' '{"schemaVersion":1,"scope":"auth"}' > "$marker_tmp"
    chmod 600 "$marker_tmp" 2>/dev/null || true
    mv -f "$marker_tmp" "$marker_path"
    log_success "Authentication runtime ready"
}

normalize_absolute_path_lexically() {
    local remaining=""
    local component=""
    local normalized=""

    case "$1" in
        /*) remaining="${1#/}" ;;
        *) return 1 ;;
    esac

    while :; do
        case "$remaining" in
            */*)
                component="${remaining%%/*}"
                remaining="${remaining#*/}"
                ;;
            *)
                component="$remaining"
                remaining=""
                ;;
        esac

        case "$component" in
            ""|.) ;;
            ..)
                case "$normalized" in
                    */*) normalized="${normalized%/*}" ;;
                    *) normalized="" ;;
                esac
                ;;
            *)
                if [ -n "$normalized" ]; then
                    normalized="$normalized/$component"
                else
                    normalized="$component"
                fi
                ;;
        esac

        [ -n "$remaining" ] || break
    done

    if [ -n "$normalized" ]; then
        printf '/%s\n' "$normalized"
    else
        printf '/\n'
    fi
}

canonicalize_path_allow_missing_suffix() {
    local target_path="$1"
    local target_parent=""
    local target_suffix=""
    local parent_component=""
    local physical_parent=""

    case "$target_path" in
        /*) ;;
        *) return 1 ;;
    esac

    target_parent="$(dirname "$target_path")"
    target_suffix="$(basename "$target_path")"
    while [ ! -d "$target_parent" ]; do
        # A dangling symlink, symlink loop, or non-directory prefix cannot be
        # resolved safely. Preserve the launcher instead of guessing ownership.
        if [ -e "$target_parent" ] || [ -L "$target_parent" ]; then
            return 1
        fi
        [ "$target_parent" != "/" ] || return 1
        parent_component="$(basename "$target_parent")"
        target_suffix="$parent_component/$target_suffix"
        target_parent="$(dirname "$target_parent")"
    done

    physical_parent="$(cd -P "$target_parent" 2>/dev/null && pwd)" || return 1
    normalize_absolute_path_lexically "$physical_parent/$target_suffix"
}

remove_legacy_desktop_launcher_if_owned() {
    local launcher_path="$1"
    local install_root="$2"
    local link_target=""
    local launcher_dir=""
    local target_path=""
    local canonical_target=""

    if [ -L "$launcher_path" ]; then
        link_target="$(readlink "$launcher_path" 2>/dev/null || true)"
        [ -n "$link_target" ] || return 0
        launcher_dir="$(cd -P "$(dirname "$launcher_path")" 2>/dev/null && pwd)" || return 0
        case "$link_target" in
            /*) target_path="$link_target" ;;
            *) target_path="$launcher_dir/$link_target" ;;
        esac
        canonical_target="$(canonicalize_path_allow_missing_suffix "$target_path")" || return 0
        install_root="$(cd -P "$install_root" 2>/dev/null && pwd)" || return 0
        case "$canonical_target" in
            "$install_root"|"$install_root"/*)
                rm -f "$launcher_path"
                log_info "Removed legacy launcher owned by Ansatz: $launcher_path"
                ;;
        esac
        return 0
    fi

    if [ -f "$launcher_path" ] && grep -F -q -- "$install_root" "$launcher_path" 2>/dev/null; then
        rm -f "$launcher_path"
        log_info "Removed legacy launcher owned by Ansatz: $launcher_path"
    fi
}

setup_path() {
    local primary_launcher="ansatz"

    if [ "$DESKTOP_PRODUCT" = "ansatz-voice-trace" ]; then
        log_info "Setting up Ansatz commands..."
    else
        log_info "Setting up ansatz command..."
    fi

    if [ "$USE_VENV" = true ]; then
        HERMES_BIN="$INSTALL_DIR/venv/bin/python"
        ANSATZ_ENTRYPOINT="$INSTALL_DIR/ansatz"
        HERMES_ENTRYPOINT="$INSTALL_DIR/hermes"
    else
        HERMES_BIN="$(command -v ansatz 2>/dev/null || echo "")"
        if [ -z "$HERMES_BIN" ]; then
            log_warn "ansatz not found on PATH after install"
            return 0
        fi
    fi

    # Verify the interpreter and the checked-in entrypoint needed by the launcher.
    if [ ! -x "$HERMES_BIN" ] || { [ "$USE_VENV" = true ] && [ ! -f "$ANSATZ_ENTRYPOINT" ]; }; then
        log_warn "Ansatz launcher prerequisites not found"
        log_info "This usually means the Python package install didn't complete successfully."
        if [ "$DISTRO" = "termux" ]; then
            log_info "Try: cd $INSTALL_DIR && python -m pip install -e '.[termux-all]' -c constraints-termux.txt"
        else
            log_info "Try: cd $INSTALL_DIR && uv pip install -e '.[all]'"
        fi
        return 0
    fi

    local command_link_dir
    local command_link_display_dir
    command_link_dir="$(get_command_link_dir)"
    command_link_display_dir="$(get_command_link_display_dir)"

    # Publish the canonical command family first. We intentionally clear
    # PYTHONPATH/PYTHONHOME so another checkout cannot shadow this install.
    mkdir -p "$command_link_dir"
    local quoted_ansatz_bin
    local quoted_ansatz_entrypoint
    local quoted_agent_entrypoint
    local quoted_command_link_dir
    local launcher_home_export=""
    printf -v quoted_ansatz_bin '%q' "$HERMES_BIN"
    printf -v quoted_ansatz_entrypoint '%q' "${ANSATZ_ENTRYPOINT:-}"
    printf -v quoted_agent_entrypoint '%q' "$INSTALL_DIR/run_agent.py"
    printf -v quoted_command_link_dir '%q' "$command_link_dir"
    if [ "$DESKTOP_PRODUCT" = "ansatz-voice-trace" ]; then
        local quoted_ansatz_home
        printf -v quoted_ansatz_home '%q' "$HERMES_HOME"
        launcher_home_export="export HERMES_HOME=$quoted_ansatz_home"
    fi

    rm -f "$command_link_dir/ansatz"
    if [ "$USE_VENV" = true ]; then
        cat > "$command_link_dir/ansatz" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
$launcher_home_export
exec $quoted_ansatz_bin $quoted_ansatz_entrypoint "\$@"
EOF
    else
        cat > "$command_link_dir/ansatz" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
$launcher_home_export
exec $quoted_ansatz_bin "\$@"
EOF
    fi
    chmod +x "$command_link_dir/ansatz"
    log_success "Installed ansatz launcher → $command_link_display_dir/ansatz"

    rm -f "$command_link_dir/ansatz-agent"
    if [ "$USE_VENV" = true ]; then
        cat > "$command_link_dir/ansatz-agent" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
$launcher_home_export
exec $quoted_ansatz_bin $quoted_agent_entrypoint "\$@"
EOF
    else
        cat > "$command_link_dir/ansatz-agent" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
$launcher_home_export
exec $quoted_ansatz_bin run_agent.py "\$@"
EOF
    fi
    chmod +x "$command_link_dir/ansatz-agent"
    log_success "Installed ansatz-agent launcher → $command_link_display_dir/ansatz-agent"

    rm -f "$command_link_dir/ansatz-acp"
    if [ "$USE_VENV" = true ]; then
        cat > "$command_link_dir/ansatz-acp" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
$launcher_home_export
exec $quoted_ansatz_bin $quoted_ansatz_entrypoint acp "\$@"
EOF
    else
        cat > "$command_link_dir/ansatz-acp" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
$launcher_home_export
exec $quoted_ansatz_bin acp "\$@"
EOF
    fi
    chmod +x "$command_link_dir/ansatz-acp"
    log_success "Installed ansatz-acp launcher → $command_link_display_dir/ansatz-acp"

    if [ "$DESKTOP_PRODUCT" = "ansatz-voice-trace" ]; then
        for product_launcher in ansatz-voice-trace ansatz-voice-trace-agent ansatz-voice-trace-acp; do
            rm -f "$command_link_dir/$product_launcher"
        done
        cp "$command_link_dir/ansatz" "$command_link_dir/ansatz-voice-trace"
        cp "$command_link_dir/ansatz-agent" "$command_link_dir/ansatz-voice-trace-agent"
        cp "$command_link_dir/ansatz-acp" "$command_link_dir/ansatz-voice-trace-acp"
        log_success "Installed ansatz-voice-trace launcher → $command_link_display_dir/ansatz-voice-trace"
        log_success "Installed ansatz-voice-trace-agent launcher → $command_link_display_dir/ansatz-voice-trace-agent"
        log_success "Installed ansatz-voice-trace-acp launcher → $command_link_display_dir/ansatz-voice-trace-acp"

        local install_root
        install_root="$(cd "$INSTALL_DIR" && pwd -P)"
        remove_legacy_desktop_launcher_if_owned "$command_link_dir/hermes" "$install_root"
        remove_legacy_desktop_launcher_if_owned "$command_link_dir/hermes-agent" "$install_root"
        remove_legacy_desktop_launcher_if_owned "$command_link_dir/hermes-acp" "$install_root"
    else
        # Temporary compatibility boundary. These aliases can be deleted as
        # one block after downstream callers have migrated to Ansatz.
        rm -f "$command_link_dir/hermes"
        cat > "$command_link_dir/hermes" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
if [ -t 0 ] && [ -t 2 ]; then
    printf '%s\n' 'Deprecated command `hermes`; use `ansatz` instead.' >&2
fi
exec $quoted_command_link_dir/ansatz "\$@"
EOF
        chmod +x "$command_link_dir/hermes"
        log_success "Installed hermes compatibility launcher → $command_link_display_dir/hermes"

        rm -f "$command_link_dir/hermes-agent"
        cat > "$command_link_dir/hermes-agent" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
if [ -t 0 ] && [ -t 2 ]; then
    printf '%s\n' 'Deprecated command `hermes-agent`; use `ansatz-agent` instead.' >&2
fi
exec $quoted_command_link_dir/ansatz-agent "\$@"
EOF
        chmod +x "$command_link_dir/hermes-agent"
        log_success "Installed hermes-agent compatibility launcher → $command_link_display_dir/hermes-agent"

        rm -f "$command_link_dir/hermes-acp"
        cat > "$command_link_dir/hermes-acp" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
if [ -t 0 ] && [ -t 2 ]; then
    printf '%s\n' 'Deprecated command `hermes-acp`; use `ansatz-acp` instead.' >&2
fi
exec $quoted_command_link_dir/ansatz-acp "\$@"
EOF
        chmod +x "$command_link_dir/hermes-acp"
        log_success "Installed hermes-acp compatibility launcher → $command_link_display_dir/hermes-acp"
    fi

    if [ "$DISTRO" = "termux" ]; then
        export PATH="$command_link_dir:$PATH"
        log_info "$command_link_display_dir is the native Termux command path"
        log_success "$primary_launcher command ready"
        return 0
    fi

    # FHS layout: /usr/local/bin is normally on PATH for login shells (via
    # /etc/profile pathmunge), but on RHEL/CentOS/Rocky/Alma 8+ non-login
    # interactive root shells (su, sudo -s, tmux panes, some web terminals)
    # only source /etc/bashrc, which does NOT add /usr/local/bin — and
    # /root/.bash_profile doesn't either.  So verify with `command -v` and
    # fall back to writing a PATH guard into /root/.bashrc when needed.
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        export PATH="$command_link_dir:$PATH"
        # Probe a fresh non-login interactive bash the way the user will use it.
        # `bash -i -c` sources ~/.bashrc but NOT ~/.bash_profile or /etc/profile,
        # which is the exact scenario where RHEL root loses /usr/local/bin.
        if env -i HOME="$HOME" TERM="${TERM:-dumb}" bash -i -c "command -v '$primary_launcher'" \
                >/dev/null 2>&1; then
            log_info "/usr/local/bin is already on PATH for all shells"
            log_success "$primary_launcher command ready"
            return 0
        fi

        log_info "$primary_launcher not on PATH in non-login shells (common on RHEL-family)"
        PATH_LINE='export PATH="/usr/local/bin:$PATH"'
        PATH_COMMENT='# Hermes Agent — ensure /usr/local/bin is on PATH (RHEL non-login shells)'
        for SHELL_CONFIG in "$HOME/.bashrc" "$HOME/.bash_profile"; do
            [ -f "$SHELL_CONFIG" ] || continue
            if ! grep -v '^[[:space:]]*#' "$SHELL_CONFIG" 2>/dev/null \
                    | grep -qE 'PATH=.*(/usr/local/bin|\$command_link_dir)'; then
                echo "" >> "$SHELL_CONFIG"
                echo "$PATH_COMMENT" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                log_success "Added /usr/local/bin to PATH in $SHELL_CONFIG"
            fi
        done
        log_success "$primary_launcher command ready"
        return 0
    fi

    # Check if ~/.local/bin is on PATH; if not, add it to shell config.
    # Detect the user's actual login shell (not the shell running this script,
    # which is always bash when piped from curl).
    if ! echo "$PATH" | tr ':' '\n' | grep -q "^$command_link_dir$"; then
        SHELL_CONFIGS=()
        IS_FISH=false
        LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        case "$LOGIN_SHELL" in
            zsh)
                [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
                [ -f "$HOME/.zprofile" ] && SHELL_CONFIGS+=("$HOME/.zprofile")
                # If neither exists, create ~/.zshrc (common on fresh macOS installs)
                if [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
                    touch "$HOME/.zshrc"
                    SHELL_CONFIGS+=("$HOME/.zshrc")
                fi
                ;;
            bash)
                [ -f "$HOME/.bashrc" ] && SHELL_CONFIGS+=("$HOME/.bashrc")
                [ -f "$HOME/.bash_profile" ] && SHELL_CONFIGS+=("$HOME/.bash_profile")
                ;;
            fish)
                # fish uses ~/.config/fish/config.fish and fish_add_path — not export PATH=
                IS_FISH=true
                FISH_CONFIG="$HOME/.config/fish/config.fish"
                mkdir -p "$(dirname "$FISH_CONFIG")"
                touch "$FISH_CONFIG"
                ;;
            *)
                [ -f "$HOME/.bashrc" ] && SHELL_CONFIGS+=("$HOME/.bashrc")
                [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
                ;;
        esac
        # Also ensure ~/.profile has it (sourced by login shells on
        # Ubuntu/Debian/WSL even when ~/.bashrc is skipped)
        [ "$IS_FISH" = "false" ] && [ -f "$HOME/.profile" ] && SHELL_CONFIGS+=("$HOME/.profile")

        PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

        for SHELL_CONFIG in "${SHELL_CONFIGS[@]}"; do
            if ! grep -v '^[[:space:]]*#' "$SHELL_CONFIG" 2>/dev/null | grep -qE 'PATH=.*\.local/bin'; then
                echo "" >> "$SHELL_CONFIG"
                echo "# Hermes Agent — ensure ~/.local/bin is on PATH" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                log_success "Added ~/.local/bin to PATH in $SHELL_CONFIG"
            fi
        done

        # fish uses fish_add_path instead of export PATH=...
        if [ "$IS_FISH" = "true" ]; then
            if ! grep -q 'fish_add_path.*\.local/bin' "$FISH_CONFIG" 2>/dev/null; then
                echo "" >> "$FISH_CONFIG"
                echo "# Hermes Agent — ensure ~/.local/bin is on PATH" >> "$FISH_CONFIG"
                echo 'fish_add_path "$HOME/.local/bin"' >> "$FISH_CONFIG"
                log_success "Added ~/.local/bin to PATH in $FISH_CONFIG"
            fi
        fi

        if [ "$IS_FISH" = "false" ] && [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
            log_warn "Could not detect shell config file to add ~/.local/bin to PATH"
            log_info "Add manually: $PATH_LINE"
        fi
    else
        log_info "~/.local/bin already on PATH"
    fi

    # Export for the current session so the selected launcher works immediately.
    export PATH="$command_link_dir:$PATH"

    log_success "$primary_launcher command ready"
}

copy_config_templates() {
    local install_scope="${1:-cli}"
    log_info "Setting up configuration files..."

    # Create ~/.hermes directory structure (config at top level, code in subdir)
    mkdir -p "$HERMES_HOME"/{cron,sessions,logs,pairing,hooks,image_cache,audio_cache,memories,skills}

    # Create .env at ~/.hermes/.env (top level, easy to find)
    if [ ! -f "$HERMES_HOME/.env" ]; then
        if [ -f "$INSTALL_DIR/.env.example" ]; then
            cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
            log_success "Created ~/.hermes/.env from template"
        else
            touch "$HERMES_HOME/.env"
            log_success "Created ~/.hermes/.env"
        fi
    else
        log_info "~/.hermes/.env already exists, keeping it"
    fi
    # Restrict .env permissions — this file holds API keys and tokens.
    # 0600 ensures only the file owner can read/write, matching standard
    # practice for credential files (.netrc, .aws/credentials, .ssh/config).
    chmod 600 "$HERMES_HOME/.env"
    configure_browser_env_from_system_browser

    # Create config.yaml at ~/.hermes/config.yaml (top level, easy to find)
    if [ ! -f "$HERMES_HOME/config.yaml" ]; then
        if [ -f "$INSTALL_DIR/cli-config.yaml.example" ]; then
            cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
            log_success "Created ~/.hermes/config.yaml from template"
            if [ "$install_scope" = "desktop" ]; then
                local provider_anchor='  # provider: "local"          # auto-detected if omitted'
                local desktop_provider='  provider: "sensevoice"       # fresh Ansatz default'
                local anchor_count
                local config_tmp
                anchor_count="$(grep -Fxc "$provider_anchor" "$HERMES_HOME/config.yaml" || true)"
                config_tmp="$(mktemp "$HERMES_HOME/.config.yaml.desktop.XXXXXX")"
                if [ "$anchor_count" = "1" ]; then
                    awk -v old="$provider_anchor" -v new="$desktop_provider" \
                        '{ if ($0 == old) print new; else print }' \
                        "$HERMES_HOME/config.yaml" > "$config_tmp"
                    mv "$config_tmp" "$HERMES_HOME/config.yaml"
                    log_success "Selected SenseVoice for the fresh Desktop config"
                else
                    rm -f "$config_tmp"
                    log_warn "Desktop STT default was not applied: expected one provider anchor, found $anchor_count; keeping the valid auto-detected config"
                fi
            fi
        fi
    else
        log_info "~/.hermes/config.yaml already exists, keeping it"
    fi

    # Create SOUL.md if it doesn't exist (global persona file).
    # This MUST match DEFAULT_SOUL_MD in hermes_cli/default_soul.py — the
    # runtime (_ensure_default_soul_md) treats the old comment-only scaffold as
    # "never customized" and upgrades it to this text on next run, so any drift
    # here is self-healing, but keep them in sync to avoid a churn on first run.
    if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
        cat > "$HERMES_HOME/SOUL.md" << 'SOUL_EOF'
You are Hermes Agent, an intelligent AI assistant created by Nous Research. You are helpful, knowledgeable, and direct. You assist users with a wide range of tasks including answering questions, writing and editing code, analyzing information, creative work, and executing actions via your tools. You communicate clearly, admit uncertainty when appropriate, and prioritize being genuinely useful over being verbose unless otherwise directed below. Be targeted and efficient in your exploration and investigations.
SOUL_EOF
        log_success "Created ~/.hermes/SOUL.md (edit to customize personality)"
    fi

    log_success "Configuration directory ready: ~/.hermes/"

    # Seed bundled skills into ~/.hermes/skills/ (manifest-based, one-time per skill)
    if [ "$NO_SKILLS" = true ]; then
        # Blank-slate install: write the opt-out marker and skip seeding.
        # skills_sync.py and `ansatz update` both honor this marker, so the
        # default profile stays empty across future updates too.
        printf '%s\n' \
            "This profile opted out of bundled-skill seeding (installed with --no-skills)." \
            "Delete this file to re-enable sync on the next 'ansatz update'." \
            > "$HERMES_HOME/.no-bundled-skills" 2>/dev/null || true
        log_info "Skipping bundled skills (--no-skills). Wrote $HERMES_HOME/.no-bundled-skills"
        log_info "  Future 'ansatz update' runs will not inject bundled skills. Delete the marker to opt back in."
    else
        log_info "Syncing bundled skills to ~/.hermes/skills/ ..."
        if "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/tools/skills_sync.py" 2>/dev/null; then
            log_success "Skills synced to ~/.hermes/skills/"
        else
            # Fallback: simple directory copy if Python sync fails
            if [ -d "$INSTALL_DIR/skills" ] && [ ! "$(ls -A "$HERMES_HOME/skills/" 2>/dev/null | grep -v '.bundled_manifest')" ]; then
                cp -r "$INSTALL_DIR/skills/"* "$HERMES_HOME/skills/" 2>/dev/null || true
                log_success "Skills copied to ~/.hermes/skills/"
            fi
        fi
    fi
}

find_system_browser() {
    # Honor ONLY an explicit, user-set AGENT_BROWSER_EXECUTABLE_PATH override.
    #
    # We deliberately do NOT scan PATH or well-known app locations any more.
    # Auto-detection silently bound the install to whatever `command -v chromium`
    # resolved to — most damagingly a Snap Chromium (/snap/bin/chromium), whose
    # sandbox blocks agent-browser's control socket under /tmp, so every
    # browser_navigate hung until the 60s timeout fired ("opening web page
    # failed"). Every install now uses the bundled Playwright Chromium unless the
    # user explicitly points elsewhere.
    local override="${AGENT_BROWSER_EXECUTABLE_PATH:-}"

    if [ -z "$override" ]; then
        return 1
    fi

    # A Snap binary is never a valid target — its confinement is the very bug we
    # are fixing — so reject it even when set explicitly.
    case "$override" in
        /snap/*) return 1 ;;
    esac

    if [ -x "$override" ]; then
        echo "$override"
        return 0
    fi
    if command -v "$override" >/dev/null 2>&1; then
        command -v "$override"
        return 0
    fi

    return 1
}

strip_snap_browser_override() {
    # Existing installs created before the system-browser fallback was dropped
    # may carry an auto-written AGENT_BROWSER_EXECUTABLE_PATH pointing at a Snap
    # Chromium (/snap/bin/chromium). That path is the root cause of the "opening
    # web page failed" hang, and the runtime reads it straight from .env — so
    # removing the fallback in the installer is not enough on its own. Strip any
    # snap-pointing override here (and its auto-written comment) so the bundled
    # Chromium download runs and the agent stops using the broken binary. A
    # deliberately-set non-snap override is left untouched.
    local env_file="$HERMES_HOME/.env"

    [ -f "$env_file" ] || return 0
    grep -Eq '^AGENT_BROWSER_EXECUTABLE_PATH=/snap/' "$env_file" 2>/dev/null || return 0

    local tmp
    tmp="$(mktemp)" || return 0
    if grep -Ev '^AGENT_BROWSER_EXECUTABLE_PATH=/snap/|^# Hermes Agent browser tools' "$env_file" > "$tmp"; then
        mv "$tmp" "$env_file"
        log_warn "Removed stale Snap browser override (AGENT_BROWSER_EXECUTABLE_PATH=/snap/...) from $env_file"
        log_info "Hermes will use the bundled Chromium instead."
        # Drop it from this process too so the rest of the run doesn't re-detect it.
        unset AGENT_BROWSER_EXECUTABLE_PATH
    else
        rm -f "$tmp"
    fi
}

run_browser_install_with_timeout() {
    run_with_timeout "$@"
}

# Run a command with a hard wall-clock timeout, returning non-zero if it is
# killed. Prefers GNU coreutils `timeout` (Linux) or `gtimeout` (macOS via
# Homebrew) for an external-command target; otherwise (and always for a shell
# function target, which the `timeout` binary cannot exec) it uses a pure-shell
# watchdog: launch the command in its own process group, poll until it finishes,
# and SIGTERM (then SIGKILL) the whole group on timeout. The pure-shell path is
# what protects the bug-#39219 case — a stalled Electron download on macOS,
# where `timeout` is usually absent — turning an indefinite hang into a non-zero
# exit so callers (install_desktop) can self-heal via the mirror fallback.
#
# $1 (timeout) must be a bare integer number of seconds — the pure-shell loop
# compares it arithmetically (the `timeout` binary would also accept suffixes
# like 15m, but we normalize so both paths share one contract). On timeout the
# return code is 124, matching GNU `timeout`.
run_with_timeout() {
    local timeout_seconds="$1"
    shift

    # Normalize to a bare integer; fall back to the desktop default if a caller
    # ever passes a suffixed/empty value (the pure-shell loop needs an int).
    case "$timeout_seconds" in
        ''|*[!0-9]*) timeout_seconds=900 ;;
    esac

    # The `timeout` binary can only exec an external command, not a shell
    # function. Use it only when the target is NOT a function; functions always
    # go through the pure-shell watchdog (which runs them in a subshell of the
    # current shell and sees them directly — no fragile env export needed).
    if [ "$(type -t "$1" 2>/dev/null)" != "function" ]; then
        local timeout_bin=""
        if command -v timeout >/dev/null 2>&1; then
            timeout_bin="timeout"
        elif command -v gtimeout >/dev/null 2>&1; then
            timeout_bin="gtimeout"
        fi
        if [ -n "$timeout_bin" ]; then
            # GNU `timeout` runs the command in its own process group, so a
            # terminal Ctrl+C is delivered to `timeout` but never reaches the
            # child — the download looks frozen and ignores Ctrl+C (#35166).
            # `--foreground` keeps the command in the shell's foreground group
            # so Ctrl+C reaches it; `-k 10` sends SIGKILL 10s after the deadline
            # so a wedged download can't outlive the timeout. Both flags are
            # GNU-only — probe once and fall back to plain `timeout` on BusyBox
            # (Alpine). When neither binary exists (stock macOS) we drop to the
            # pure-shell watchdog below.
            if "$timeout_bin" --foreground -k 10 1 true >/dev/null 2>&1; then
                "$timeout_bin" --foreground -k 10 "$timeout_seconds" "$@"
            else
                "$timeout_bin" "$timeout_seconds" "$@"
            fi
            return $?
        fi
    fi

    # Pure-shell fallback: run in a new process group so we can kill the whole
    # subtree (npm spawns node + the Electron downloader as children).
    set -m
    ( "$@" ) &
    local cmd_pid=$!
    set +m

    local waited=0
    local rc
    while [ "$waited" -lt "$timeout_seconds" ]; do
        if ! kill -0 "$cmd_pid" 2>/dev/null; then
            # `|| rc=$?` keeps the non-zero child status without letting `set -e`
            # abort the caller here (this would fire if run_with_timeout were
            # ever called outside an if/|| context).
            rc=0; wait "$cmd_pid" 2>/dev/null || rc=$?
            return "$rc"
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Final boundary recheck: the command may have finished during the last
    # poll interval — don't kill (and mislabel as 124) a process that already
    # exited cleanly in the last second of the budget.
    if ! kill -0 "$cmd_pid" 2>/dev/null; then
        rc=0; wait "$cmd_pid" 2>/dev/null || rc=$?
        return "$rc"
    fi

    # Timed out: kill the process group (negative PID), escalate to KILL.
    kill -TERM "-$cmd_pid" 2>/dev/null || kill -TERM "$cmd_pid" 2>/dev/null || true
    sleep 2
    kill -KILL "-$cmd_pid" 2>/dev/null || kill -KILL "$cmd_pid" 2>/dev/null || true
    wait "$cmd_pid" 2>/dev/null || true
    return 124
}

run_npm_with_mirror_fallback() {
    local timeout_seconds="$1"
    shift
    if NPM_CONFIG_REGISTRY="$DESKTOP_NPM_REGISTRY" npm_config_registry="$DESKTOP_NPM_REGISTRY" \
        run_with_timeout "$timeout_seconds" "$@"; then
        return 0
    fi
    log_info "Domestic npm registry failed; retrying the bounded command through the official registry..."
    NPM_CONFIG_REGISTRY="$DESKTOP_NPM_OFFICIAL_REGISTRY" npm_config_registry="$DESKTOP_NPM_OFFICIAL_REGISTRY" \
        run_with_timeout "$timeout_seconds" "$@"
}

# Return success only when the host is an apt release NEWER than the newest one
# Playwright's platform resolver recognizes — the exact condition that makes
# `playwright install` hang uninterruptibly (#35166). We scope the override
# retry to this case rather than retrying on *any* failure, so a genuine
# network/disk/permission failure doesn't get a mismatched-glibc build forced
# onto it. Newest Playwright-known apt releases as of this writing: Ubuntu
# 24.04, Debian 13. Anything above triggers the fallback; everything Playwright
# already handles (and every non-apt distro) does not.
playwright_host_unrecognized() {
    # Compare dotted versions: returns 0 if $1 > $2.
    _ver_gt() {
        [ "$1" = "$2" ] && return 1
        [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
    }
    case "$DISTRO" in
        ubuntu) _ver_gt "${DISTRO_VERSION:-0}" "24.04" ;;
        debian) _ver_gt "${DISTRO_VERSION:-0}" "13" ;;
        *) return 1 ;;  # Non-apt or unknown — not the #35166 hang condition.
    esac
}

# Compute the PLAYWRIGHT_HOST_PLATFORM_OVERRIDE value to retry an install with
# when Playwright's platform resolver rejects the host. ubuntu24.04 is the
# newest Linux build Playwright has shipped across recent releases and runs on
# newer apt releases (its binaries are dynamically linked); we point too-new /
# unrecognized hosts at it. Only x64/arm64 Linux have Playwright builds — emit
# nothing for anything else so the caller skips the retry. Echoes the value
# (e.g. "ubuntu24.04-x64") or nothing.
playwright_fallback_platform() {
    case "$(uname -m)" in
        x86_64|amd64) echo "ubuntu24.04-x64" ;;
        aarch64|arm64) echo "ubuntu24.04-arm64" ;;
        *) : ;;  # No Playwright Linux build for this arch.
    esac
}

# Run a `playwright install ...` command, and if it fails or hangs (the
# uninterruptible "Installing Playwright Chromium with system dependencies"
# stall on apt releases Playwright doesn't recognize yet — Ubuntu 26.04,
# Debian 14, future distros — see #35166), retry it ONCE with
# PLAYWRIGHT_HOST_PLATFORM_OVERRIDE pinned to the newest known build.
#
# The override retry is scoped to the actual hang condition: it fires only when
# the host is an apt release NEWER than Playwright recognizes
# (playwright_host_unrecognized). On every release Playwright already supports
# (Ubuntu <=24.04, Debian <=13) and every non-apt distro, the first attempt is
# authoritative and a failure is reported as-is — we never force a
# mismatched-glibc build (microsoft/playwright#35114) onto a host Playwright
# handles correctly. This is deliberately narrower than a retry-on-any-failure:
# a network/disk/permission error on a supported host should surface, not get
# papered over with a platform override. Playwright's maintainers bless this
# env var as the supported escape hatch for unrecognized platforms
# (microsoft/playwright#33434); a hardcoded full distro/version table was
# rejected upstream (microsoft/playwright#33432), so we only need the
# newest-known floor here.
#
# An operator-provided PLAYWRIGHT_HOST_PLATFORM_OVERRIDE is always respected:
# it is inherited by the first attempt, and the retry is skipped.
#
# Run once against the currently selected PLAYWRIGHT_DOWNLOAD_HOST while
# preserving the supported platform-override retry.
_run_playwright_install_for_current_mirror() {
    local timeout_seconds="$1"
    shift

    # First attempt: native platform resolution (inherits any operator override).
    if run_browser_install_with_timeout "$timeout_seconds" "$@" 2>/dev/null; then
        return 0
    fi

    # Operator already pinned the platform — their choice already applied to the
    # attempt above; a second identical run won't help.
    if [ -n "${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-}" ]; then
        return 1
    fi

    # Only retry with an override on the apt releases too new for Playwright to
    # recognize (the #35166 hang). Any other failure is a real failure and is
    # surfaced unchanged.
    if ! playwright_host_unrecognized; then
        return 1
    fi

    local fallback
    fallback="$(playwright_fallback_platform)"
    if [ -z "$fallback" ]; then
        return 1  # No usable fallback build for this arch.
    fi

    log_warn "Playwright doesn't recognize ${DISTRO} ${DISTRO_VERSION} yet — retrying with PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=$fallback"
    log_info "(apt releases newer than Playwright knows hang at this step; see #35166)"
    PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="$fallback" \
        run_browser_install_with_timeout "$timeout_seconds" "$@"
}

# Usage: run_playwright_install <timeout_seconds> npx playwright install [args...]
# Every attempt is bounded by the helper above. Domestic mirrors are tried in
# reviewed order before Playwright's official default is allowed.
run_playwright_install() {
    local timeout_seconds="$1"
    shift

    local mirror
    for mirror in "$DESKTOP_PLAYWRIGHT_MIRROR" "$DESKTOP_PLAYWRIGHT_FALLBACK_MIRROR" "__official__"; do
        if [ "$mirror" = "__official__" ]; then
            if ( unset PLAYWRIGHT_DOWNLOAD_HOST; _run_playwright_install_for_current_mirror "$timeout_seconds" "$@" ); then
                return 0
            fi
        elif PLAYWRIGHT_DOWNLOAD_HOST="$mirror" \
            _run_playwright_install_for_current_mirror "$timeout_seconds" "$@"; then
            return 0
        fi
        log_warn "Playwright download source failed; trying the next reviewed source."
    done
    return 1
}

configure_browser_env_from_system_browser() {
    local env_file="$HERMES_HOME/.env"
    local browser_path="${DETECTED_BROWSER_EXECUTABLE:-}"

    if [ -z "$browser_path" ]; then
        browser_path="$(find_system_browser 2>/dev/null || true)"
    fi

    if [ -z "$browser_path" ]; then
        return 0
    fi

    mkdir -p "$HERMES_HOME"
    if [ ! -f "$env_file" ]; then
        touch "$env_file"
    fi

    if grep -q '^AGENT_BROWSER_EXECUTABLE_PATH=' "$env_file" 2>/dev/null; then
        log_info "AGENT_BROWSER_EXECUTABLE_PATH already configured"
        return 0
    fi

    {
        echo ""
        echo "# Hermes Agent browser tools — explicit browser override."
        echo "AGENT_BROWSER_EXECUTABLE_PATH=$browser_path"
    } >> "$env_file"
    log_success "Configured browser tools to use $browser_path"
}

install_node_deps() {
    if [ "$HAS_NODE" = false ]; then
        log_info "Skipping Node.js dependencies (Node not installed)"
        return 0
    fi

    if [ "$DISTRO" = "termux" ]; then
        log_info "Skipping automatic Node/browser dependency setup on Termux"
        log_info "Browser automation is not part of the tested Termux install path yet."
        log_info "If you want to experiment manually later, run: cd $INSTALL_DIR && npm install"
        return 0
    fi

    if [ -f "$INSTALL_DIR/package.json" ]; then
        log_info "Installing Node.js dependencies (browser tools)..."
        cd "$INSTALL_DIR"
        # Time-boxed: a stalled registry fetch would otherwise hang here with no
        # progress (same #39219 stall class as the desktop build below).
        # A failed npm install used to still print "✓ Node.js dependencies
        # installed", hiding the degradation from the user (#77003). Now it
        # fails the install outright instead of burying the warning (#85297).
        if ! run_npm_with_mirror_fallback "$NODE_DEPS_TIMEOUT" npm install --silent; then
            log_error "npm install failed or timed out; Node.js dependencies were not installed"
            restore_dirty_lockfiles "$INSTALL_DIR"
            return 1
        fi
        log_success "Node.js dependencies installed"

        # Install Playwright browser + system dependencies.
        # Playwright's --with-deps only supports apt-based systems natively.
        # For Arch/Manjaro we install the system libs via pacman first.
        # Other systems must install Chromium dependencies manually.
        local browser_engine_ready=false
        if [ "$SKIP_BROWSER" = true ]; then
            log_info "Skipping Playwright/Chromium install (--skip-browser)"
            log_info "Browser tools will be unavailable until you run manually:"
            log_info "  cd $INSTALL_DIR && npx playwright install chromium"
            log_info "On apt-based systems, an admin also needs to run:"
            log_info "  sudo npx playwright install-deps chromium"
        else
        log_info "Installing browser engine (Playwright Chromium)..."
        strip_snap_browser_override
        DETECTED_BROWSER_EXECUTABLE="$(find_system_browser 2>/dev/null || true)"
        if [ -n "$DETECTED_BROWSER_EXECUTABLE" ]; then
            log_success "Using explicit browser override: $DETECTED_BROWSER_EXECUTABLE"
            log_info "Skipping bundled Chromium download (AGENT_BROWSER_EXECUTABLE_PATH is set)."
            browser_engine_ready=true
        else
            case "$DISTRO" in
                ubuntu|debian|raspbian|pop|linuxmint|elementary|zorin|kali|parrot)
                    # Use --with-deps only when sudo is available non-interactively
                    # (root, or a user with passwordless sudo). Non-sudo users
                    # — typical for systemd service accounts and unprivileged
                    # operator users — would otherwise get blocked on an
                    # interactive sudo prompt that they can't satisfy. Fall back
                    # to the browser-only install in that case, and print the
                    # exact command the admin needs to run separately.
                    if [ "$(id -u)" -eq 0 ] || (command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null); then
                        log_info "Installing Playwright Chromium with system dependencies..."
                        if cd "$INSTALL_DIR" && run_playwright_install 600 npx playwright install --with-deps chromium; then
                            browser_engine_ready=true
                        else
                            log_warn "Playwright browser installation failed — browser tools will not work."
                            log_warn "Try running manually: cd $INSTALL_DIR && npx playwright install --with-deps chromium"
                        fi
                    else
                        log_warn "No sudo available — skipping system-library install (--with-deps)."
                        log_info "Ask an administrator to run, one time, as root:"
                        log_info "  sudo npx playwright install-deps chromium"
                        log_info "  (from $INSTALL_DIR, after Node.js deps are installed)"
                        log_info "Installing Chromium binary into this user's Playwright cache..."
                        if cd "$INSTALL_DIR" && run_playwright_install 600 npx playwright install chromium; then
                            browser_engine_ready=true
                        else
                            log_warn "Playwright browser installation failed — browser tools will not work."
                            log_warn "Try running manually: cd $INSTALL_DIR && npx playwright install chromium"
                        fi
                    fi
                    ;;
                arch|manjaro|cachyos|endeavouros|garuda)
                    if command -v pacman &> /dev/null; then
                        log_info "Arch-family distro detected — installing Chromium system dependencies via pacman..."
                        if command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
                            sudo NEEDRESTART_MODE=a pacman -S --noconfirm --needed \
                                nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib >/dev/null 2>&1 || true
                        elif [ "$(id -u)" -eq 0 ]; then
                            pacman -S --noconfirm --needed \
                                nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib >/dev/null 2>&1 || true
                        else
                            log_warn "Cannot install browser deps without sudo. Run manually:"
                            log_warn "  sudo pacman -S nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib"
                        fi
                    fi
                    if cd "$INSTALL_DIR" && run_playwright_install 600 npx playwright install chromium; then
                        browser_engine_ready=true
                    else
                        log_warn "Playwright browser installation failed — browser tools will not work."
                    fi
                    ;;
                fedora|rhel|centos|rocky|alma)
                    log_warn "Playwright does not support automatic dependency installation on RPM-based systems."
                    log_info "Install Chromium system dependencies manually before using browser tools:"
                    log_info "  sudo dnf install nss atk at-spi2-core cups-libs libdrm libxkbcommon mesa-libgbm pango cairo alsa-lib"
                    if cd "$INSTALL_DIR" && run_playwright_install 600 npx playwright install chromium; then
                        browser_engine_ready=true
                    else
                        log_warn "Playwright browser installation failed — install dependencies above and retry."
                    fi
                    ;;
                opensuse*|sles)
                    log_warn "Playwright does not support automatic dependency installation on zypper-based systems."
                    log_info "Install Chromium system dependencies manually before using browser tools:"
                    log_info "  sudo zypper install mozilla-nss libatk-1_0-0 at-spi2-core cups-libs libdrm2 libxkbcommon0 Mesa-libgbm1 pango cairo libasound2"
                    if cd "$INSTALL_DIR" && run_playwright_install 600 npx playwright install chromium; then
                        browser_engine_ready=true
                    else
                        log_warn "Playwright browser installation failed — install dependencies above and retry."
                    fi
                    ;;
                *)
                    log_warn "Playwright does not support automatic dependency installation on $DISTRO."
                    log_info "Install Chromium/browser system dependencies for your distribution, then run:"
                    log_info "  cd $INSTALL_DIR && npx playwright install chromium"
                    log_info "Browser tools will not work until dependencies are installed."
                    if cd "$INSTALL_DIR" && run_playwright_install 600 npx playwright install chromium; then
                        browser_engine_ready=true
                    else
                        log_warn "Playwright browser installation failed — browser tools will not work."
                    fi
                    ;;
            esac
        fi
        fi
        if [ "$browser_engine_ready" = true ]; then
            log_success "Browser engine setup complete"
        elif [ "$BUNDLED_SOURCE" = true ] && [ "$BOOTSTRAP_SCOPE" = "runtime" ] && [ "$SKIP_BROWSER" = false ]; then
            log_error "Playwright browser installation failed; packaged Desktop runtime is incomplete."
            restore_dirty_lockfiles "$INSTALL_DIR"
            return 1
        else
            log_warn "Browser engine setup incomplete; browser tools will remain unavailable."
        fi
    fi

    # Install TUI dependencies
    if [ -f "$INSTALL_DIR/ui-tui/package.json" ]; then
        log_info "Installing TUI dependencies..."
        cd "$INSTALL_DIR/ui-tui"
        # Time-boxed: a stalled registry fetch would otherwise hang here (#39219).
        # Report success only on actual success, same as node-deps above
        # (#77003) — and fail the install outright (#85297).
        if ! run_npm_with_mirror_fallback "$NODE_DEPS_TIMEOUT" npm install --silent; then
            log_error "TUI npm install failed or timed out; TUI dependencies were not installed"
            restore_dirty_lockfiles "$INSTALL_DIR"
            return 1
        fi
        log_success "TUI dependencies installed"
    fi

    # Keep the checkout clean so `ansatz update` doesn't autostash every run.
    restore_dirty_lockfiles "$INSTALL_DIR"
}

install_browser_use_cli() {
    # The Browser Use CLI is the default browser backend when it is runnable
    # (tools/browser_use_cli.py). Provision it here so fresh installs don't
    # silently fall back to the built-in browser tools. Best-effort: any
    # failure is non-fatal because browser_exec can still run via uvx and
    # `ansatz tools` can install it later.
    if [ "$SKIP_BROWSER" = true ]; then
        log_info "Skipping Browser Use CLI install (--skip-browser)"
        return 0
    fi
    if [ "$DISTRO" = "termux" ]; then
        return 0
    fi
    if [ -z "$UV_CMD" ]; then
        log_info "Skipping Browser Use CLI install (uv unavailable)"
        return 0
    fi
    # MANAGED-FIRST: only Hermes' managed copy short-circuits. A browser-use
    # on the user's PATH is a side install — resolution prefers the managed
    # copy, so it must be provisioned regardless.
    if [ -x "$HERMES_HOME/bin/browser-use" ]; then
        log_success "Browser Use CLI already installed"
        return 0
    fi

    log_info "Installing Browser Use CLI (default browser backend)..."
    # UV_TOOL_BIN_DIR keeps the binary inside Hermes' managed bin dir, where
    # the browser tool resolves it — no reliance on the user's PATH.
    if run_with_timeout 600 env UV_NO_CONFIG=1 UV_TOOL_BIN_DIR="$HERMES_HOME/bin" \
        "$UV_CMD" tool install browser-use >/dev/null 2>&1; then
        log_success "Browser Use CLI installed"
    else
        log_warn "Browser Use CLI install failed — browser automation falls back to built-in tools."
        log_info "Install later with: $UV_CMD tool install browser-use  (or via 'ansatz tools')"
    fi
}

install_computer_use_driver() {
    # cua-driver powers the computer_use toolset (background desktop control).
    # Provision it at install time so enabling the tool later — via
    # `ansatz tools`, the dashboard, or the desktop app — is a config flip,
    # not a surprise multi-minute binary fetch (the confusion this fixes:
    # users had to discover `ansatz computer-use install` on their own).
    # Best-effort and non-fatal: the enable paths still lazy-install via
    # install_cua_driver() when this step was skipped or failed.
    if [ "$SKIP_COMPUTER_USE" = true ]; then
        log_info "Skipping Computer Use (cua-driver) install (--skip-computer-use)"
        return 0
    fi
    case "$DISTRO" in
        termux)
            return 0
            ;;
    esac
    if command -v cua-driver >/dev/null 2>&1; then
        log_success "Computer Use driver (cua-driver) already installed"
        return 0
    fi
    # Non-admin macOS accounts can't receive the CuaDriver.app bundle in
    # /Applications; skip cleanly instead of failing loudly (#47865 class).
    if [ "$(uname -s)" = "Darwin" ] && [ -d /Applications ] && [ ! -w /Applications ]; then
        log_info "Skipping Computer Use driver (cua-driver): /Applications is not writable"
        return 0
    fi

    log_warn "Computer Use driver is not present in this reviewed package."
    log_info "Ask an administrator to supply the pinned cua-driver artifact; Hermes will not execute a remote installer."
}

run_setup_wizard() {
    if [ "$RUN_SETUP" = false ]; then
        log_info "Skipping setup wizard (--skip-setup)"
        return 0
    fi

    # The setup wizard reads from /dev/tty, so it works even when the
    # install script itself is piped (curl | bash). Only skip if no
    # terminal is available at all (e.g. Docker build, CI).
    #
    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO, so the wizard would proceed and
    # then crash on `< /dev/tty` below.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Setup wizard skipped (no terminal available). Run 'ansatz setup' after install."
        return 0
    fi

    echo ""
    log_info "Starting setup wizard..."
    echo ""

    cd "$INSTALL_DIR"

    # Run ansatz setup using the venv Python directly (no activation needed).
    # Redirect stdin from /dev/tty so interactive prompts work when piped from curl.
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/venv/bin/python" -m hermes_cli.main setup < /dev/tty
    else
        python -m hermes_cli.main setup < /dev/tty
    fi
}

maybe_start_gateway() {
    # Check if any messaging platform tokens were configured
    ENV_FILE="$HERMES_HOME/.env"
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi

    HAS_MESSAGING=false
    for VAR in TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN WHATSAPP_ENABLED; do
        VAL=$(grep "^${VAR}=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
        if [ -n "$VAL" ] && [ "$VAL" != "your-token-here" ]; then
            HAS_MESSAGING=true
            break
        fi
    done

    if [ "$HAS_MESSAGING" = false ]; then
        return 0
    fi

    echo ""
    log_info "Messaging platform token detected!"
    log_info "The gateway needs to be running for Hermes to send/receive messages."

    # If WhatsApp is enabled and no session exists yet, run foreground first for QR scan
    WHATSAPP_VAL=$(grep "^WHATSAPP_ENABLED=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
    WHATSAPP_SESSION="$HERMES_HOME/whatsapp/session/creds.json"
    if [ "$WHATSAPP_VAL" = "true" ] && [ ! -f "$WHATSAPP_SESSION" ]; then
        if [ "$IS_INTERACTIVE" = true ]; then
            echo ""
            log_info "WhatsApp is enabled but not yet paired."
            log_info "Running 'ansatz whatsapp' to pair via QR code..."
            echo ""
            if prompt_yes_no "Pair WhatsApp now?" "yes"; then
                HERMES_CMD="$(get_hermes_command_path)"
                $HERMES_CMD whatsapp || true
            fi
        else
            log_info "WhatsApp pairing skipped (non-interactive). Run 'ansatz whatsapp' to pair."
        fi
    fi

    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO. See #16746.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Gateway setup skipped (no terminal available). Run 'ansatz gateway install' later."
        return 0
    fi

    echo ""
    local should_install_gateway=false
    if [ "$DISTRO" = "termux" ]; then
        if prompt_yes_no "Would you like to start the gateway in the background?" "yes"; then
            should_install_gateway=true
        fi
    else
        if prompt_yes_no "Would you like to install the gateway as a background service?" "yes"; then
            should_install_gateway=true
        fi
    fi

    if [ "$should_install_gateway" = true ]; then
        HERMES_CMD="$(get_hermes_command_path)"

        if [ "$DISTRO" != "termux" ] && command -v systemctl &> /dev/null; then
            log_info "Installing systemd service..."
            if $HERMES_CMD gateway install 2>/dev/null; then
                log_success "Gateway service installed"
                if $HERMES_CMD gateway start 2>/dev/null; then
                    log_success "Gateway started! Your bot is now online."
                else
                    log_warn "Service installed but failed to start. Try: ansatz gateway start"
                fi
            else
                log_warn "Systemd install failed. You can start manually: ansatz gateway"
            fi
        else
            if [ "$DISTRO" = "termux" ]; then
                log_info "Termux detected — starting gateway in best-effort background mode..."
            else
                log_info "systemd not available — starting gateway in background..."
            fi
            nohup $HERMES_CMD gateway > "$HERMES_HOME/logs/gateway.log" 2>&1 &
            GATEWAY_PID=$!
            log_success "Gateway started (PID $GATEWAY_PID). Logs: ~/.hermes/logs/gateway.log"
            log_info "To stop: kill $GATEWAY_PID"
            log_info "To restart later: ansatz gateway"
            if [ "$DISTRO" = "termux" ]; then
                log_warn "Android may stop background processes when Termux is suspended or the system reclaims resources."
            fi
        fi
    else
        log_info "Skipped. Start the gateway later with: ansatz gateway"
    fi
}

write_bootstrap_marker() {
    # Writes $INSTALL_DIR/.hermes-bootstrap-complete, which tells the Hermes
    # desktop app (apps/desktop/electron/main.ts) and the macOS launcher fast
    # path (apps/bootstrap-installer) "a real install finished here -- don't
    # re-run first-run bootstrap."
    #
    # Schema mirrors install.ps1's Write-BootstrapMarker and main.ts's
    # writeBootstrapMarker(). Keep the three in lockstep:
    #   schemaVersion 1 + pinnedCommit (length >= 7) are what the desktop
    #   validator requires; desktopVersion is omitted because only the desktop
    #   app knows its own version.
    if [ ! -d "$INSTALL_DIR" ]; then
        log_warn "Skipping bootstrap marker: $INSTALL_DIR doesn't exist"
        return 0
    fi

    # Explicit --commit wins; otherwise read HEAD from the checkout we just
    # installed. If neither resolves, skip the marker entirely rather than
    # write one the desktop will reject -- an absent marker is a clean
    # "bootstrap needed", a malformed one is a confusing half-state.
    local pinned_commit="$INSTALL_COMMIT"
    if [ -z "$pinned_commit" ]; then
        pinned_commit=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null) || pinned_commit=""
    fi

    if [ -z "$pinned_commit" ]; then
        log_warn "Skipping bootstrap marker: could not resolve HEAD in $INSTALL_DIR"
        return 0
    fi

    local marker_path="$INSTALL_DIR/.hermes-bootstrap-complete"
    local tmp_path="$marker_path.tmp"

    # Atomic publish: the macOS launcher predicate only checks existence, so a
    # torn write would arm the fast path against a half-written marker.
    printf '{\n  "schemaVersion": 1,\n  "pinnedCommit": "%s",\n  "pinnedBranch": "%s",\n  "completedAt": "%s"\n}\n' \
        "$pinned_commit" \
        "$BRANCH" \
        "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" > "$tmp_path"
    mv -f "$tmp_path" "$marker_path"
}

print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│              ✓ Installation Complete!                   │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
    echo ""

    # Show file locations
    echo -e "${CYAN}${BOLD}📁 Your files:${NC}"
    echo ""
    echo -e "   ${YELLOW}Config:${NC}    $HERMES_HOME/config.yaml"
    echo -e "   ${YELLOW}API Keys:${NC}  $HERMES_HOME/.env"
    echo -e "   ${YELLOW}Data:${NC}      $HERMES_HOME/cron/, sessions/, logs/"
    echo -e "   ${YELLOW}Code:${NC}      $INSTALL_DIR"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}🚀 Commands:${NC}"
    echo ""
    echo -e "   ${GREEN}hermes${NC}              Start chatting"
    echo -e "   ${GREEN}ansatz setup${NC}        Configure API keys & settings"
    echo -e "   ${GREEN}ansatz config${NC}       View/edit configuration"
    echo -e "   ${GREEN}ansatz config edit${NC}  Open config in editor"
    echo -e "   ${GREEN}ansatz gateway install${NC} Install gateway service (messaging + cron)"
    echo -e "   ${GREEN}ansatz update${NC}       Update to latest version"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    if [ "$DISTRO" = "termux" ]; then
        echo -e "${YELLOW}⚡ 'hermes' was linked into $(get_command_link_display_dir), which is already on PATH in Termux.${NC}"
        echo ""
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo -e "${YELLOW}⚡ 'hermes' was linked into /usr/local/bin and is ready to use — no shell reload needed.${NC}"
        echo ""
    else
        echo -e "${YELLOW}⚡ Reload your shell to use 'hermes' command:${NC}"
        echo ""
        LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        if [ "$LOGIN_SHELL" = "zsh" ]; then
            echo "   source ~/.zshrc"
        elif [ "$LOGIN_SHELL" = "bash" ]; then
            echo "   source ~/.bashrc"
        elif [ "$LOGIN_SHELL" = "fish" ]; then
            echo "   source ~/.config/fish/config.fish"
        else
            echo "   source ~/.bashrc   # or ~/.zshrc"
        fi
        echo ""
    fi

    # Show Node.js warning if auto-install failed
    if [ "$HAS_NODE" = false ]; then
        echo -e "${YELLOW}"
        echo "Note: Node.js could not be installed automatically."
        echo "Browser tools need Node.js. Install manually:"
        if [ "$DISTRO" = "termux" ]; then
            echo "  pkg install nodejs"
        else
            echo "  https://nodejs.org/en/download/"
        fi
        echo -e "${NC}"
    fi

    # Show ripgrep note if not installed
    if [ "$HAS_RIPGREP" = false ]; then
        echo -e "${YELLOW}"
        echo "Note: ripgrep (rg) was not found. File search will use"
        echo "grep as a fallback. For faster search in large codebases,"
        if [ "$DISTRO" = "termux" ]; then
            echo "install ripgrep: pkg install ripgrep"
        else
            echo "install ripgrep: sudo apt install ripgrep (or brew install ripgrep)"
        fi
        echo -e "${NC}"
    fi
}

ensure_browser() {
    if ! command -v node >/dev/null 2>&1; then
        local node_bin="$HERMES_HOME/node/bin/node"
        if [ -x "$node_bin" ]; then
            export PATH="$HERMES_HOME/node/bin:$PATH"
        else
            log_error "Node.js not found. Run with --ensure node first."
            return 1
        fi
    fi

    local npm_bin
    npm_bin="$(command -v npm 2>/dev/null || echo "$HERMES_HOME/node/bin/npm")"
    if [ ! -x "$npm_bin" ]; then
        log_error "npm not found"
        return 1
    fi

    # agent-browser itself is intentionally NOT installed here (#43564 /
    # PR #44772 review): it resolves lazily via `npx agent-browser` instead,
    # which every consumer (tools/browser_tool.py, `ansatz update`'s npx
    # cache warm) already goes through. Eagerly npm-installing a second,
    # separately version-pinned copy here -- only reachable via this
    # explicit --ensure browser fallback in the first place -- was redundant
    # complexity and an extra credential/supply-chain surface for a path
    # npx already covers.
    log_info "Installing camofox browser server..."
    local log_file
    log_file="$(mktemp)"
    # Time-boxed (#39219): a stalled npm registry fetch here would otherwise
    # hang the installer with no progress, same class as the desktop build.
    if ! run_npm_with_mirror_fallback "$NODE_DEPS_TIMEOUT" "$npm_bin" install -g --prefix "$HERMES_HOME/node" --silent --ignore-scripts \
        "@askjo/camofox-browser@^1.5.2" \
        >"$log_file" 2>&1; then
        log_error "npm install failed or timed out:"
        cat "$log_file" >&2
        rm -f "$log_file"
        return 1
    fi
    rm -f "$log_file"
    export PATH="$HERMES_HOME/node/bin:$PATH"

    strip_snap_browser_override
    local sys_browser
    sys_browser="$(find_system_browser 2>/dev/null || true)"
    if [ -n "$sys_browser" ]; then
        configure_browser_env_from_system_browser "$sys_browser"
        log_info "Explicit browser override set -- Chromium download will be skipped when agent-browser installs on demand"
    fi

    return 0
}

ensure_mode() {
    detect_os

    IFS=',' read -ra DEPS <<< "$ENSURE_DEPS"
    for dep in "${DEPS[@]}"; do
        dep="$(echo "$dep" | tr -d '[:space:]')"
        case "$dep" in
            node)
                check_node
                ;;
            browser)
                check_node
                if [ "$HAS_NODE" = true ]; then
                    ensure_browser
                fi
                ;;
            ripgrep)
                if ! command -v rg &>/dev/null; then
                    HAS_RIPGREP=false
                    HAS_FFMPEG=true
                    install_system_packages
                fi
                ;;
            ffmpeg)
                if ! command -v ffmpeg &>/dev/null; then
                    HAS_FFMPEG=false
                    HAS_RIPGREP=true
                    install_system_packages
                fi
                ;;
            *)
                log_warn "Unknown dependency: $dep"
                ;;
        esac
    done
}


# Clear the cached Electron download + any half-written unpacked output so the
# next `npm run pack` re-downloads and re-stages from scratch. A corrupt zip in
# the per-user Electron download cache - most often a partial/resumed download
# that leaves concatenated junk - makes electron-builder's `unpack-electron`
# extract a tree MISSING the electron binary, so the `electron`->`Hermes` rename
# dies with ENOENT and every re-run repeats the broken extraction forever. This
# is the bash sibling of install.ps1's Clear-ElectronBuildCache and the Python
# _purge_electron_build_cache() used by `ansatz desktop`; install.sh was the only
# build path lacking it. Echoes the removed paths (one per line); best-effort.
clear_electron_build_cache() {
    local desktop_dir="$1"
    local removed=""

    # Per-user Electron download cache dirs, honoring the overrides @electron/get
    # respects, then the platform defaults (macOS: ~/Library/Caches/electron,
    # Linux: $XDG_CACHE_HOME/electron or ~/.cache/electron).
    local cache_dirs=()
    [ -n "${electron_config_cache:-}" ] && cache_dirs+=("$electron_config_cache")
    [ -n "${ELECTRON_CACHE:-}" ] && cache_dirs+=("$ELECTRON_CACHE")
    if [ "$OS" = "macos" ]; then
        cache_dirs+=("$HOME/Library/Caches/electron")
    else
        [ -n "${XDG_CACHE_HOME:-}" ] && cache_dirs+=("$XDG_CACHE_HOME/electron")
        cache_dirs+=("$HOME/.cache/electron")
    fi

    local dir zip
    for dir in "${cache_dirs[@]}"; do
        [ -d "$dir" ] || continue
        # Recurse: the bad copy may be the top-level zip OR a copy inside an
        # @electron/get hash subdir.
        while IFS= read -r zip; do
            [ -n "$zip" ] || continue
            if rm -f "$zip" 2>/dev/null; then
                removed="$removed$zip
"
            fi
        done <<EOF
$(find "$dir" -type f -name 'electron-*.zip' 2>/dev/null)
EOF
    done

    # A half-written unpacked dir from an interrupted prior pack poisons the
    # rename even after the zip is fixed (mac-arm64-unpacked / linux-unpacked).
    local release_dir="$desktop_dir/release"
    if [ -d "$release_dir" ]; then
        local unpacked
        while IFS= read -r unpacked; do
            [ -n "$unpacked" ] || continue
            if rm -rf "$unpacked" 2>/dev/null; then
                removed="$removed$unpacked
"
            fi
        done <<EOF
$(find "$release_dir" -maxdepth 1 -type d -name '*-unpacked' 2>/dev/null)
EOF
    fi

    printf '%s' "$removed"
}

# Run the desktop pack in $1 (the apps/desktop dir). `npm run pack` = tsc +
# vite build + electron-builder --dir, producing an unpacked app for the
# current OS. Signing auto-discovery is disabled so electron-builder falls back
# to an ad-hoc signature instead of grabbing an unrelated Developer ID from the
# keychain (a real signed/notarized .dmg needs Apple credentials — a separate
# release concern). Optional $2 is a registered mirror, or __official__ after
# both bounded domestic attempts failed.
_desktop_pack() {
    local desktop_dir="$1"
    local mirror="${2:-}"
    if [ "$mirror" = "__official__" ]; then
        ( cd "$desktop_dir" && env -u ELECTRON_MIRROR CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack )
    elif [ -n "$mirror" ]; then
        ( cd "$desktop_dir" && ELECTRON_MIRROR="$mirror" CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack )
    else
        ( cd "$desktop_dir" && CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack )
    fi
}

# Per-attempt wall-clock cap for the desktop npm install / electron-builder pack
# (#39219). A stalled (not failed) Electron download on a throttled/blocked link
# never returns, so without this the installer hangs forever on "Build desktop
# app". 900s is generous enough for a slow-but-progressing ~150MB fetch + build;
# override with DESKTOP_BUILD_TIMEOUT for very slow links.
DESKTOP_BUILD_TIMEOUT="${DESKTOP_BUILD_TIMEOUT:-900}"

# Wall-clock cap for the plain registry `npm install`s (browser-tools + TUI
# deps). Same #39219 stall class but no ~150MB Electron binary, so a shorter
# default; override with NODE_DEPS_TIMEOUT for very slow links.
NODE_DEPS_TIMEOUT="${NODE_DEPS_TIMEOUT:-600}"

# Electron package dir — workspace-local nest first, then root hoist.
_electron_dir() {
    local install_dir="$1"
    if [ -d "$install_dir/apps/desktop/node_modules/electron" ]; then
        printf '%s\n' "$install_dir/apps/desktop/node_modules/electron"
    else
        printf '%s\n' "$install_dir/node_modules/electron"
    fi
}

# True when dist/ holds a usable Electron binary (#38673 / run-electron-builder.mjs).
_electron_dist_ok() {
    local install_dir="$1"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    if [ "$OS" = "macos" ]; then
        [ -e "$electron_dir/dist/Electron.app/Contents/MacOS/Electron" ]
    else
        [ -e "$electron_dir/dist/electron" ]
    fi
}

# Best-effort: run electron/install.js to populate dist/ (optional mirror).
_restore_electron_dist() {
    local install_dir="$1"
    local mirror="${2:-}"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    _electron_dist_ok "$install_dir" && return 0

    [ -f "$electron_dir/install.js" ] || return 1
    command -v node >/dev/null 2>&1 || return 1

    rm -rf "$electron_dir/dist" 2>/dev/null || true
    rm -f "$electron_dir/path.txt" 2>/dev/null || true

    if [ "$mirror" = "__official__" ]; then
        ( cd "$electron_dir" && env -u ELECTRON_MIRROR node install.js ) || true
    elif [ -n "$mirror" ]; then
        ( cd "$electron_dir" && ELECTRON_MIRROR="$mirror" node install.js ) || true
    else
        ( cd "$electron_dir" && node install.js ) || true
    fi
    _electron_dist_ok "$install_dir"
}

_electron_pkg_staged_missing_dist() {
    local install_dir="$1"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    [ -f "$electron_dir/package.json" ] && [ -f "$electron_dir/install.js" ] && ! _electron_dist_ok "$install_dir"
}

_restore_electron_dist_with_fallback() {
    local install_dir="$1"
    _restore_electron_dist "$install_dir" "$DESKTOP_ELECTRON_PRIMARY_MIRROR" \
        || _restore_electron_dist "$install_dir" "$DESKTOP_ELECTRON_SECONDARY_MIRROR" \
        || _restore_electron_dist "$install_dir" "__official__"
}

# Build apps/desktop into a launchable native app. Mirrors install.ps1's
# Install-Desktop: a root-level npm install so the apps/* workspace resolves
# the desktop's own deps (Electron ~150MB), then `npm run pack`
# (electron-builder --dir) which emits an unpacked app for the current OS. Only invoked
# via the 'desktop' stage / --include-desktop, which the Electron app's own
# first-launch bootstrap never requests (it must not rebuild itself).
install_desktop_voice_deps() {
    # Desktop ships with working voice out of the box: eagerly install the
    # wake-word + local-STT stacks ([wake] + [voice] extras) instead of
    # leaving them to lazy first-use install. Policy change (Teknium, July
    # 2026, #70509 testing): the first ear-click used to trigger a
    # multi-minute onnxruntime pip install that froze the UI and blew RPC
    # timeouts. Lazy install remains the fallback for CLI-only installs and
    # for anything this best-effort step fails to fetch.
    local _prev_venv="${VIRTUAL_ENV:-}"
    if [ "$USE_VENV" = true ]; then
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi
    if [ -z "${UV_CMD:-}" ]; then
        install_uv || true
    fi
    if [ -z "${UV_CMD:-}" ]; then
        log_warn "uv unavailable — voice/wake deps will lazy-install at first use instead"
        return 0
    fi
    log_info "Installing voice + wake-word dependencies (onnxruntime, faster-whisper — 1-3min)..."
    if (cd "$INSTALL_DIR" && $UV_CMD pip install -e ".[wake,voice]") ; then
        log_success "Voice + wake-word dependencies installed"
    else
        log_warn "Voice/wake dependency install failed — they will lazy-install at first use"
    fi
    if [ "$USE_VENV" = true ] && [ -z "$_prev_venv" ]; then
        unset VIRTUAL_ENV
    fi
    return 0
}

install_desktop() {
    local desktop_dir="$INSTALL_DIR/apps/desktop"

    # The desktop stage only runs when a build is explicitly requested
    # (--include-desktop / 'desktop' stage), so a missing toolchain is a hard
    # failure, not a silent skip — a silent skip yields a "complete" install
    # with no app and a confusing "couldn't find a built desktop" at launch.
    # Always re-resolve Node here. Stages run in separate processes, so we can't
    # trust an earlier check; more importantly check_node now enforces the build
    # floor (Node >=26) and prepends the Hermes-managed Node to PATH, so
    # the build never runs on a too-old system Node — the cause of the opaque
    # "Build desktop app … exit code 1" failure (Vite crashes on old Node).
    check_node
    if ! command -v npm >/dev/null 2>&1; then
        log_error "Cannot build desktop app: Node.js / npm unavailable"
        log_info "Install Node.js and retry: cd $desktop_dir && npm run pack"
        return 1
    fi
    if [ ! -f "$desktop_dir/package.json" ]; then
        log_warn "Skipping desktop build (apps/desktop not present in checkout)"
        return 0
    fi

    # 1. Root workspace install so apps/desktop's deps (Electron, Vite,
    #    node-pty prebuilds) resolve. The browser-tools install runs in the
    #    repo-root package workspace, which does not pull apps/* deps.
    #
    #    Prefer `npm ci`: it deletes node_modules and reinstalls from the
    #    lockfile, so it always produces a complete tree. Bare `npm install`
    #    can report "up to date" against a stale node_modules/.package-lock.json
    #    marker while node_modules is actually empty (Windows workspace-hoisting
    #    flake) — leaving tsc/typescript unresolved and `npm run pack`'s
    #    `tsc -b` failing with no obvious cause. Fall back to `npm install`
    #    only if `npm ci` is unavailable or the lockfile is out of sync.
    #
    #    Both the install and the build below are wrapped in a hard wall-clock
    #    timeout (#39219): the Electron binary (~150MB) is fetched from GitHub,
    #    and on a throttled/blocked connection that download can *stall* — npm
    #    neither errors nor exits, so the installer sits on "Build desktop app"
    #    forever with only `npm warn deprecated` lines visible. A stall now
    #    converts to a non-zero exit, which feeds the existing self-heal /
    #    mirror-fallback escalation instead of hanging the whole install.
    #
    #    The `npm ci` and its `npm install` fallback SHARE one budget: a stalled
    #    link wedges both identically, so giving each a full DESKTOP_BUILD_TIMEOUT
    #    would double the worst-case hang. We compute a single deadline and pass
    #    the remaining seconds to the fallback (min 30s so it still gets a real
    #    attempt if `npm ci` failed fast rather than stalling).
    log_info "Installing desktop workspace dependencies (includes Electron ~150MB, 1-3min)..."
    local _deps_start _deps_remaining
    _deps_start=$(date +%s)
    if NPM_CONFIG_REGISTRY="$DESKTOP_NPM_REGISTRY" npm_config_registry="$DESKTOP_NPM_REGISTRY" \
         run_with_timeout "$DESKTOP_BUILD_TIMEOUT" bash -c 'cd "$1" && npm ci' _ "$INSTALL_DIR"; then
        log_success "Desktop workspace dependencies installed"
    elif _deps_remaining=$(( DESKTOP_BUILD_TIMEOUT - ($(date +%s) - _deps_start) )); \
         [ "$_deps_remaining" -lt 30 ] && _deps_remaining=30; \
         NPM_CONFIG_REGISTRY="$DESKTOP_NPM_OFFICIAL_REGISTRY" npm_config_registry="$DESKTOP_NPM_OFFICIAL_REGISTRY" \
         run_with_timeout "$_deps_remaining" bash -c 'cd "$1" && npm install' _ "$INSTALL_DIR"; then
        log_success "Desktop workspace dependencies installed"
    elif _electron_pkg_staged_missing_dist "$INSTALL_DIR"; then
        log_warn "Desktop dependency install failed with a missing Electron dist; attempting self-heal..."
        _restore_electron_dist_with_fallback "$INSTALL_DIR" || true
    else
        log_error "Desktop workspace npm install failed"
        # Common cause: a previous 'sudo npm'/'sudo npx' left root-owned files in
        # ~/.npm, so this non-root install can't write the shared cache. npm hides
        # it behind a confusing EEXIST / "File exists" message while the real errno
        # is EACCES (-13). Point the user at the fix instead of a raw npm trace.
        log_info "If the errors above mention EACCES / 'permission denied' / EEXIST while"
        log_info "writing the npm cache, your ~/.npm likely holds root-owned files from an"
        log_info "earlier 'sudo npm' or 'sudo npx'. Reclaim ownership and retry:"
        log_info "  sudo chown -R \"\$(id -un)\" ~/.npm && npm cache verify"
        log_info "Then re-run this installer, or build manually:"
        log_info "  cd \"$INSTALL_DIR\" && npm ci && cd apps/desktop && npm run pack"
        return 1
    fi

    # 2. Build with bounded primary, secondary and then official attempts.
    #    Electron download self-heals instead of failing the whole install:
    #      a) plain `npm run pack` (downloads Electron from GitHub),
    #      b) on failure, purge a corrupt cached zip + stale unpacked dir and
    #         retry (matches install.ps1 / `ansatz desktop`),
    #      c) on still-failing, fall back to a public Electron mirror — this is
    #         the GitHub-blocked/throttled case (the repeating "retrying" log).
    log_info "Building desktop app (this takes 1-3 minutes)..."
    local pack_ok=false
    if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir" "$DESKTOP_ELECTRON_PRIMARY_MIRROR"; then
        pack_ok=true
    else
        local purged=""
        local restored=false
        if ! _electron_dist_ok "$INSTALL_DIR"; then
            purged="$(clear_electron_build_cache "$desktop_dir")"
            if _restore_electron_dist "$INSTALL_DIR" "$DESKTOP_ELECTRON_SECONDARY_MIRROR"; then restored=true; fi
        fi
        if [ "$restored" = true ]; then
            log_warn "Desktop build failed; refreshed the Electron download and retrying once..."
            if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir" "$DESKTOP_ELECTRON_SECONDARY_MIRROR"; then
                pack_ok=true
            fi
        fi
    fi

    # (c) Official @electron/get source only after both domestic sources fail.
    if [ "$pack_ok" = false ]; then
        log_warn "Both approved domestic Electron sources failed; trying the pinned official artifact once."
        _electron_dist_ok "$INSTALL_DIR" || _restore_electron_dist "$INSTALL_DIR" "__official__" || true
        if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir" "__official__"; then
            pack_ok=true
        fi
    fi

    if [ "$pack_ok" = false ]; then
        log_error "Desktop app build failed"
        # If the log shows repeated "retrying" lines fetching the Electron zip,
        # the binary download is blocked/throttled (firewall, proxy, region) and
        # the mirror fallback above also couldn't reach a host. Try a mirror you
        # trust and rebuild (@electron/get honors ELECTRON_MIRROR):
        log_info "If the log shows Electron download retries, rebuild via a reachable mirror:"
        log_info "  ELECTRON_MIRROR=<mirror-base-url> \\"
        log_info "    bash -c 'cd \"$desktop_dir\" && CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack'"
        log_info "Otherwise build manually: cd $desktop_dir && npm run pack"
        return 1
    fi

    local app=""
    if [ "$OS" = "linux" ]; then
        if [ -x "$desktop_dir/release/linux-unpacked/Hermes" ]; then
            app="$desktop_dir/release/linux-unpacked/Hermes"
        elif [ -x "$desktop_dir/release/linux-unpacked/hermes" ]; then
            app="$desktop_dir/release/linux-unpacked/hermes"
        fi
    else
        local cand
        for cand in \
            "$desktop_dir/release/mac-arm64/Hermes.app" \
            "$desktop_dir/release/mac/Hermes.app"; do
            if [ -d "$cand" ]; then
                app="$cand"
                break
            fi
        done
    fi
    if [ -z "$app" ]; then
        log_error "Desktop build completed but no app was found under $desktop_dir/release/"
        return 1
    fi
    log_success "Desktop app built: $app"

    # Linux: Electron's chrome-sandbox helper needs root:root 4755 or the
    # sandboxed renderer will abort on startup.  Check the file is a regular
    # file (not a symlink) before chown/chmod so we don't follow an
    # attacker-controlled link to an arbitrary path.
    if [ "$OS" = "linux" ]; then
        local sandbox="$desktop_dir/release/linux-unpacked/chrome-sandbox"
        if [ -f "$sandbox" ] && [ ! -L "$sandbox" ]; then
            if [ "$(id -u)" -eq 0 ]; then
                chown root:root "$sandbox" && chmod 4755 "$sandbox" || {
                    log_error "Cannot configure Electron sandbox helper: $sandbox"
                    return 1
                }
            elif command -v sudo >/dev/null 2>&1; then
                sudo chown root:root "$sandbox" && sudo chmod 4755 "$sandbox" || {
                    log_error "Cannot configure Electron sandbox helper (sudo failed): $sandbox"
                    return 1
                }
            else
                log_error "Cannot configure Electron sandbox helper without sudo: $sandbox"
                return 1
            fi
        fi
    fi

    # macOS: route through the same config-aware signing fixup as
    # `ansatz desktop`, so install/repair and self-update agree about the app's
    # identity. The fixup preserves the Electron entitlement plists and signs
    # with a stable Designated Requirement (configured keychain identity, else
    # identifier-pinned ad-hoc), so macOS TCC grants — Full Disk Access,
    # Desktop/Downloads/Documents, Accessibility, microphone — survive the
    # rebuild instead of resetting on every update. The shell's
    # publisher-signing decision governed the build and is passed explicitly so
    # importing Python cannot reverse it by loading HERMES_HOME/.env. If the
    # helper is unavailable or fails, branch into the historical quarantine
    # strip + deep ad-hoc repair so a broken venv never leaves the bundle
    # unsigned/unlaunchable.
    if [ "$OS" = "macos" ] && [ -z "${CSC_LINK:-}" ] && [ -z "${APPLE_SIGNING_IDENTITY:-}" ] && command -v codesign >/dev/null 2>&1; then
        local config_python="$INSTALL_DIR/venv/bin/python"
        local fixup_ok=""
        if [ -x "$config_python" ]; then
            if HERMES_HOME="$HERMES_HOME" "$config_python" - "$desktop_dir" <<'PYEOF'
import sys
from pathlib import Path
from hermes_cli.main import _desktop_macos_relaunchable_fixup
ok = _desktop_macos_relaunchable_fixup(
    Path(sys.argv[1]), publisher_signing_configured=False
)
sys.exit(0 if ok else 1)
PYEOF
            then
                fixup_ok=1
            else
                log_warn "Config-aware macOS signing fixup failed; applying the historical ad-hoc fallback."
            fi
        fi
        if [ -z "$fixup_ok" ]; then
            xattr -cr "$app" 2>/dev/null || true
            codesign --force --deep --sign - "$app" >/dev/null 2>&1 || true
        fi
    fi

    # `npm install` + `npm run pack` rewrite lockfiles; restore them so the
    # checkout stays clean for the next `ansatz update`.
    restore_dirty_lockfiles "$INSTALL_DIR"
}

# Each --stage runs in its own process, so (unlike the monolithic main() where
# clone_repo cd's once and later steps inherit it) a stage that operates on the
# checkout must cd into it explicitly. Without this, install_deps/setup_path run
# from the desktop app's cwd and resolve `.` / the venv against the wrong tree.
require_install_dir() {
    if [ -z "$INSTALL_DIR" ] || [ ! -d "$INSTALL_DIR" ]; then
        log_error "Install directory not found: ${INSTALL_DIR:-<unset>}"
        log_info "The 'repository' stage must run before this one."
        return 1
    fi
    cd "$INSTALL_DIR"
}

# Desktop bootstrap stage protocol. Mirrors the Windows install.ps1 surface
# closely enough for the Electron bootstrap runner to show structured progress.
run_stage_body() {
    local stage="$1"

    case "$stage" in
        auth-prerequisites)
            print_banner
            detect_os
            resolve_install_layout
            install_uv
            check_python
            ;;
        prerequisites)
            print_banner
            detect_os
            resolve_install_layout
            validate_bundled_runtime_mirrors || return 1
            install_uv
            check_python
            if [ "$BUNDLED_SOURCE" != true ]; then
                check_git
            fi
            check_node
            check_network_prerequisites
            install_system_packages
            ;;
        repository)
            detect_os
            resolve_install_layout
            if [ "$BUNDLED_SOURCE" != true ]; then
                check_git
            fi
            clone_repo
            ;;
        venv)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            setup_venv
            ;;
        python-deps)
            detect_os
            resolve_install_layout
            validate_bundled_runtime_mirrors || return 1
            require_install_dir
            install_uv
            check_python
            install_deps
            ;;
        python-auth-deps)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            install_auth_deps
            ;;
        node-deps)
            detect_os
            resolve_install_layout
            validate_bundled_runtime_mirrors || return 1
            require_install_dir
            check_node
            install_node_deps || return
            install_uv
            install_browser_use_cli
            install_computer_use_driver
            ;;
        path)
            detect_os
            resolve_install_layout
            require_install_dir
            setup_path
            ;;
        config)
            detect_os
            resolve_install_layout
            require_install_dir
            copy_config_templates desktop
            ;;
        setup)
            detect_os
            resolve_install_layout
            require_install_dir
            run_setup_wizard
            ;;
        gateway)
            detect_os
            resolve_install_layout
            require_install_dir
            maybe_start_gateway
            ;;
        desktop)
            detect_os
            resolve_install_layout
            require_install_dir
            # Each stage runs in its own process, so the Hermes-managed Node
            # provisioned during prerequisites/node-deps (at $HERMES_HOME/node/bin)
            # isn't on PATH here. check_node re-adds it (or installs if missing)
            # so install_desktop can find npm instead of silently skipping.
            check_node
            install_desktop_voice_deps
            install_desktop
            ;;
        complete)
            detect_os
            resolve_install_layout
            print_success
            write_bootstrap_marker
            # Code-scoped stamp: write next to the install tree, not into
            # $HERMES_HOME. $HERMES_HOME is a shared data dir (it can be
            # bind-mounted into a Docker gateway too), so a stamp there gets
            # clobbered by the container's 'docker' stamp and wrongly blocks
            # 'ansatz update' on this host install. See detect_install_method().
            write_install_method_stamp
            ;;
        auth-complete)
            detect_os
            resolve_install_layout
            require_install_dir
            # Publish the guarded CLI launchers while only the authentication
            # environment exists. The exact auth-free commands are handled
            # before the full CLI import wall; every protected entrypoint still
            # fails closed until online authentication and runtime install.
            setup_path
            write_auth_bootstrap_marker
            ;;
        *)
            log_error "Unknown stage: $stage"
            return 2
            ;;
    esac
}

run_stage_protocol() {
    local stage="$1"
    if [ -z "$stage" ]; then
        log_error "--stage requires a stage name"
        if [ "$JSON_OUTPUT" = true ]; then
            emit_stage_json "" false false "missing stage name"
        fi
        return 2
    fi

    if [ "$NON_INTERACTIVE" = true ] && stage_needs_user_input "$stage"; then
        log_info "Skipping $stage (non-interactive bootstrap)"
        if [ "$JSON_OUTPUT" = true ]; then
            emit_stage_json "$stage" true true
        fi
        return 0
    fi

    # Run the stage body in a subshell so a stage helper that calls `exit 1`
    # on failure (clone_repo, install_deps, etc. were written for the monolithic
    # flow) only exits the subshell — the parent still reaches the JSON result
    # frame below. Without this, a failed --stage would terminate the process
    # before emitting the frame and the Rust/Electron parser would see "no
    # result frame" instead of a clean {ok:false} contract response.
    set +e
    ( run_stage_body "$stage" )
    local code=$?
    set -e

    if [ "$JSON_OUTPUT" = true ]; then
        if [ "$code" -eq 0 ]; then
            emit_stage_json "$stage" true false
        else
            emit_stage_json "$stage" false false "exit code $code"
        fi
    fi
    return "$code"
}

# ============================================================================
# Main
# ============================================================================

main() {
    print_banner

    detect_os
    resolve_install_layout
    install_uv
    check_python
    if [ "$BUNDLED_SOURCE" != true ]; then
        check_git
    fi
    check_node
    check_network_prerequisites
    install_system_packages

    clone_repo
    setup_venv
    install_deps
    install_node_deps || return
    install_browser_use_cli
    install_computer_use_driver
    setup_path
    copy_config_templates
    run_setup_wizard
    maybe_start_gateway

    if [ "$INCLUDE_DESKTOP" = true ]; then
        install_desktop_voice_deps
        install_desktop
    fi

    print_success

    write_bootstrap_marker

    # Code-scoped stamp: write next to the install tree, not into $HERMES_HOME.
    # $HERMES_HOME is a shared data dir (it can be bind-mounted into a Docker
    # gateway too), so a stamp there gets clobbered by the container's 'docker'
    # stamp and wrongly blocks 'ansatz update' on this host install.
    # See detect_install_method().
    write_install_method_stamp
}

if [ "$MANIFEST_MODE" = true ]; then
    emit_manifest
elif [ -n "$STAGE_NAME" ]; then
    run_stage_protocol "$STAGE_NAME"
elif [ -n "$ENSURE_DEPS" ]; then
    ensure_mode
else
    main
fi
