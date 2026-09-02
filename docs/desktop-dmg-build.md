# Build the Phase 1 macOS DMG

This fork provides one supported first-phase build for a macOS arm64 DMG. It
requires Node.js 26.7.0 and does not download Playwright-managed Chromium or
FFmpeg.

## Prerequisites

- macOS on Apple silicon
- Node.js 26.7.0
- npm compatible with the committed `package-lock.json`
- uv 0.12.5 for macOS arm64, either on `PATH`, under
  `$HERMES_HOME/bin/uv`, or under `~/.hermes/bin/uv`
- a relocatable uv-managed CPython 3.11 macOS arm64 runtime; the build finds
  it with `uv python find 3.11`
- network access to the npm registry and the configured Electron and
  electron-builder binary mirrors when dependencies are not already cached,
  plus the approved USTC or Tsinghua Python mirror for the authentication
  wheelhouse

The repository pins Node in both `.nvmrc` and `.node-version`. The build also
checks the running Node executable and stops before installing dependencies if
it is not exactly version 26.7.0.

## Build

When using the Node installation created by the official Hermes setup test:

```bash
PATH="/Users/zhouzhangchen/.hermes/node/bin:$PATH" npm run build:desktop:dmg
```

The command installs dependencies from `package-lock.json`, builds Hermes
Desktop, and invokes Electron Builder's macOS DMG target. It sets
`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` for the complete command and rejects the
result if the build log contains a Playwright browser-download attempt. The
script defaults both Electron download sources to `npmmirror` for reliable
builds in China; callers can override `ELECTRON_MIRROR` and
`ELECTRON_BUILDER_BINARIES_MIRROR`. Electron Builder still verifies its binary
downloads against the checksums shipped with the package.

Before Electron Builder runs, the command also creates the offline
authentication toolchain used before the login form can appear. The build
requires uv 0.12.5 and a relocatable CPython 3.11 macOS arm64 runtime, exports
`desktop_auth_runtime/uv.lock` as hash-locked requirements, downloads only
binary wheels from the USTC mirror with a Tsinghua retry, and archives Python.
The packaged toolchain also gzip-wraps the verified `uv` executable before
writing its manifest. This keeps the later macOS App signing pass from
modifying a bare Mach-O after its size and SHA-256 have been recorded; the
installer verifies the archive and extracts it atomically before use. The
resulting inputs are copied into
`apps/desktop/build/bootstrap/auth-toolchain/`; `beforePack` records every
asset size and SHA-256 in `manifest.json`. The packaged App verifies the same
manifest before invoking the installer.

The supported default build performs that preparation automatically. A
controlled release environment may instead provide all seven absolute inputs
below; partial overrides are not treated as a complete prebuilt toolchain:

```text
HERMES_AUTH_TOOLCHAIN_OUTPUT_DIR
HERMES_AUTH_TOOLCHAIN_UV_PATH
HERMES_AUTH_TOOLCHAIN_PYTHON_ARCHIVE
HERMES_AUTH_TOOLCHAIN_REQUIREMENTS
HERMES_AUTH_TOOLCHAIN_WHEELHOUSE
HERMES_AUTH_TOOLCHAIN_UV_VERSION
HERMES_AUTH_TOOLCHAIN_PYTHON_VERSION
```

Before npm or Electron Builder runs, the build uses the exact prepared uv
binary to run `uv lock --check` without a mirror override or network access.
A stale `uv.lock` therefore stops the build before an App or DMG can be
produced.

At first launch, authentication preparation is fully offline from these
packaged assets. After successful online account validation, full runtime
downloads use the fixed USTC/Tsinghua Python mirrors and the npmmirror npm,
Node, and Playwright endpoints. This automatic first-launch chain has no
GitHub, Astral, or nodejs.org fallback.

The post-login Python install deliberately does not run `uv sync --locked`
under a mirror override: uv includes the default index URL in lock identity,
so a generic PyPI lock would be reported as stale merely because a domestic
mirror is selected. Instead, the installer exports the already packaged lock
offline as pinned requirements with SHA-256 hashes, excludes the local project
from that export, and runs `uv pip sync --require-hashes` against USTC. A retry
uses the exact same hash set against Tsinghua. After dependency sync, the local
packaged project is installed editable with dependency resolution, build
isolation, and networking disabled. A missing hash, a URL/git requirement, a
failed mirror sync, or a failed offline project install stops bootstrap; there
is no unlocked package fallback.

The backend archive and App resources are product-only. `.github`, CI evidence
publishers, Gatekeeper verifiers, credential-login drivers, tests, and build
documents are rejected or excluded; the GitHub Actions verification workflow
is not part of the DMG payload.

For this local-testing milestone the macOS build explicitly uses Electron
Builder's ad-hoc identity (`mac.identity=-`). The pipeline then runs
`codesign --verify --deep --strict` against the packaged `Ansatz.app`; an
unsigned or partially signed bundle fails the build instead of producing a DMG
that opens to a blank renderer.

The resulting artifact is written under `apps/desktop/release/`. The retained
build log is `apps/desktop/build/logs/phase1-desktop-dmg-build.log`.

## Preflight only

To verify the host, Node version, browser-download policy, Electron mirror, and
whether auth inputs will be prebuilt or prepared without installing or
building anything:

```bash
PATH="/Users/zhouzhangchen/.hermes/node/bin:$PATH" scripts/build-desktop-dmg.sh --check
```

## Current milestone boundary

Playwright E2E sources and the `@playwright/test` development dependency remain
in the repository. Only the browser download is excluded from this production
build path. The generated application is ad-hoc signed for local testing; Apple
Developer signing, notarization, clean-machine distribution, and Windows builds
are separate milestones.
