# Build the Phase 1 macOS DMG

This fork provides one supported first-phase build for a macOS arm64 DMG. It
requires Node.js 26.7.0 and does not download Playwright-managed Chromium or
FFmpeg.

## Prerequisites

- macOS on Apple silicon
- Node.js 26.7.0
- npm compatible with the committed `package-lock.json`
- network access to the npm registry and the configured Electron and
  electron-builder binary mirrors when dependencies are not already cached

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

For this local-testing milestone the macOS build explicitly uses Electron
Builder's ad-hoc identity (`mac.identity=-`). The pipeline then runs
`codesign --verify --deep --strict` against the packaged `Hermes.app`; an
unsigned or partially signed bundle fails the build instead of producing a DMG
that opens to a blank renderer.

The resulting artifact is written under `apps/desktop/release/`. The retained
build log is `apps/desktop/build/logs/phase1-desktop-dmg-build.log`.

## Preflight only

To verify the host, Node version, browser-download policy, and Electron mirror
without installing or building anything:

```bash
PATH="/Users/zhouzhangchen/.hermes/node/bin:$PATH" scripts/build-desktop-dmg.sh --check
```

## Current milestone boundary

Playwright E2E sources and the `@playwright/test` development dependency remain
in the repository. Only the browser download is excluded from this production
build path. The generated application is ad-hoc signed for local testing; Apple
Developer signing, notarization, clean-machine distribution, and Windows builds
are separate milestones.
