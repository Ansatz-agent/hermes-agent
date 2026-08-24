---
title: "Packaging Hermes Macos Arm64 Dmg — Build and verify Hermes macOS ARM64 DMG installers"
sidebar_label: "Packaging Hermes Macos Arm64 Dmg"
description: "Build and verify Hermes macOS ARM64 DMG installers"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Packaging Hermes Macos Arm64 Dmg

Build and verify Hermes macOS ARM64 DMG installers.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/packaging-hermes-macos-arm64-dmg` |
| Version | `0.1.0` |
| Author | yuxiaoy (Seauagain), Hermes Agent |
| License | MIT |
| Platforms | macos |
| Tags | `desktop`, `packaging`, `macos`, `arm64`, `dmg`, `release-engineering` |
| Related skills | [`hermes-agent-skill-authoring`](/docs/user-guide/skills/bundled/software-development/software-development-hermes-agent-skill-authoring), [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Packaging Hermes macOS ARM64 DMG Skill

Build a macOS Apple Silicon DMG from a Hermes source checkout with the
repository's official desktop packaging script. Preserve source provenance,
avoid the previous Playwright browser-download installation failure, and hand
off an independently verified test artifact.

## When to Use

- A tester needs a macOS ARM64 `.dmg` from a named Hermes branch or commit.
- The build must use the repository-local npm and Electron Builder toolchain,
  not GitHub Actions or a globally installed packager.
- The artifact should follow the repository's install-before-login runtime
  preparation and browser-download contract.

Do not use this procedure for Intel macOS, a universal binary, or a formally
signed and notarized production release.

## Prerequisites

- An Apple Silicon Mac. Confirm `uname -s` is `Darwin` and `uname -m` is
  `arm64`.
- The exact Node version in `.node-version`; the currently verified toolchain
  is Node 26.7.0 with npm 12.
- Git, npm, `hdiutil`, `codesign`, `shasum`, and enough disk space for
  `node_modules`, package inputs, the unpacked app, and the DMG. Budget at
  least 8 GiB.
- Network access when uncached npm, Electron, uv, Python, or wheel inputs must
  be fetched. This build is not fully offline; do not promise an offline build
  or modify the package to over-bundle unrelated dependencies.
- A project-local `tmp/` directory for task scratch and `TMPDIR`. Never use a
  system temporary directory for this workflow.
- A user-supplied branch, tag, or commit resolved to an expected 40-character
  commit before packaging.

## How to Run

Run every command through `terminal` from the repository root. Keep build logs
and command exit statuses as evidence. Use long timeouts for dependency
preparation and Electron packaging. Stop at the first unexplained failure; do
not clear shared caches, stash changes, or alter lockfiles to make a build pass.

## Quick Reference

| Phase | Required proof |
|---|---|
| Pin source | Clean isolated worktree and full commit recorded |
| Preflight | Host architecture and exact Node version pass `--check` |
| Package | Official repository script exits zero |
| Browser contract | Build log contains no Playwright browser download |
| Artifact integrity | App signature, DMG structure, size, and SHA-256 verified |
| Hand off | Unique Downloads filename and fresh-machine test gap reported |

## Procedure

1. **Protect provenance and user work.** Inspect the requested checkout:

   ```text
   terminal(command="git status --short", timeout=30)
   terminal(command="git rev-parse HEAD", timeout=30)
   ```

   If it is dirty or is not already the requested ref, create an isolated
   worktree under the workspace's `tmp/` directory from the explicit remote
   ref. Do not stash, reset, clean, or overwrite the user's checkout. Record
   the resulting full commit as `SOURCE_COMMIT` and confirm it matches the
   approved 40-character commit. Completion criterion: the build worktree is
   clean and `git rev-parse HEAD` is the approved source.

2. **Inspect the official entry point before invoking it.** Read
   `scripts/build-desktop-dmg.sh` in full. Confirm that it requires Darwin
   ARM64 and the `.node-version` value, exports
   `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`, performs `uv lock --check`, runs
   `npm ci`, invokes the desktop `dist:mac:dmg` target, and validates the
   produced artifact. If the requested ref does not contain those contracts,
   stop rather than inventing an equivalent build path.

3. **Use the exact repository Node version.** Place the requested Node binary
   first on `PATH` without changing the user's global installation, then
   record versions:

   ```text
   terminal(command="node --version && npm --version && uname -s && uname -m", timeout=30)
   terminal(command="bash scripts/build-desktop-dmg.sh --check", timeout=120)
   ```

   Completion criterion: preflight reports the exact `.node-version`, Darwin,
   ARM64, and the expected mirror/runtime preparation mode.

4. **Create task-local scratch.** Create a unique directory below repository
   `tmp/` and export its absolute path as `TMPDIR` for the build. This matters
   because the repository may use `mktemp` while constructing a restricted
   DMG fallback. Completion criterion: `TMPDIR` resolves inside this checkout,
   not `/tmp` or another system location.

5. **Run the official build.** From the repository root, invoke only the
   checked-in orchestrator:

   ```text
   terminal(command="env TMPDIR=<repo-root>/tmp/macos-arm64-dmg bash scripts/build-desktop-dmg.sh", timeout=7200)
   ```

   The orchestrator prepares or consumes the authentication toolchain, checks
   the Python lock offline with `uv lock --check`, installs the locked npm
   dependencies, and ultimately invokes the repository's `dist:mac:dmg`
   workspace target with the ad-hoc signing identity.

   Do not replace this with GitHub Actions, a global Electron Builder, or an
   improvised fully offline payload. Completion criterion: the script exits
   zero and prints the absolute path of a newly created macOS ARM64 DMG.

6. **Validate the browser-download contract.** The script normally performs
   this check itself. Run it explicitly against the retained log so the handoff
   has direct evidence:

   ```text
   terminal(command="node scripts/desktop-dmg-contract.mjs validate-log apps/desktop/build/logs/phase1-desktop-dmg-build.log", timeout=120)
   ```

   Completion criterion: it reports that no Playwright browser download was
   detected. This prevents recurrence of the installer failure caused by
   attempting to install browser tools during local-runtime setup.

7. **Resolve the fresh artifact without hardcoding branding.** Use the
   repository contract rather than assuming the product-name prefix:

   ```text
   terminal(command="node scripts/desktop-dmg-contract.mjs find-dmg apps/desktop/release", timeout=120)
   ```

   When multiple artifacts exist, pass the build start time supported by the
   contract or compare modification times. Never hand off an older cached
   DMG merely because its filename looks correct.

8. **Verify the app and DMG.** Locate the packaged `.app` under the fresh
   architecture-specific release directory, then run:

   ```text
   terminal(command="codesign --verify --deep --strict <packaged-app>", timeout=300)
   terminal(command="hdiutil verify <fresh-dmg>", timeout=600)
   terminal(command="shasum -a 256 <fresh-dmg>", timeout=300)
   ```

   Record the DMG byte size as well. The current test artifact is ad-hoc signed
   with identity `-`; it is not notarized and must not be described as a
   production-signed release. Completion criterion: all three commands exit
   zero and the full SHA-256 is captured.

9. **Handle a transient detach failure narrowly.** If Electron Builder fails
   only with a mounted-volume `resource busy` detach error after the packaged
   app exists, first inspect the complete log and confirm no packaging process
   is still active. Then retry once using the same repository-locked desktop
   builder command and configuration:

   ```text
   terminal(command="npm run --workspace apps/desktop builder -- --mac dmg --config.mac.identity=-", timeout=3600)
   ```

   Do not loop, force-detach arbitrary volumes, or treat another error as
   equivalent. If the retry fails, stop and diagnose it with
   `systematic-debugging`.

10. **Copy without overwriting.** Construct a destination name containing the
    product version, `mac-arm64`, and optionally the short source commit. Check
    that it does not collide with an existing file, copy it to
    `$HOME/Downloads`, and recompute the destination SHA-256. Completion
    criterion: source and destination digests are identical.

11. **Hand off honestly.** Report the source ref and full commit, absolute DMG
    path, byte size, SHA-256, signing/notarization state, and verification
    commands. State that a successful package build does not prove a
    fresh-machine install, first launch, browser-tools setup, login, dialogue,
    or trace upload. Those behaviors require tester acceptance on a clean Mac.

## Pitfalls

- **Wrong architecture:** this workflow is macOS ARM64 only. Do not relabel its
  output as Intel or universal.
- **Dirty source:** package preparation binds runtime inputs to source state.
  Always build in a clean isolated worktree at the requested commit.
- **Wrong Node:** the script intentionally rejects a version that differs from
  `.node-version`. Do not bypass the check.
- **Browser over-bundling:** the proven route sets
  `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`. Do not force Playwright-managed browser
  downloads into the installer to make it “more offline.”
- **Network assumptions:** the workflow is not fully offline when caches or
  toolchain inputs are missing. Report required mirrors instead of hiding the
  dependency.
- **Stale artifact:** the release directory may contain older DMGs. Use the
  contract helper and build timestamp to identify the fresh result.
- **Temporary DMG mount:** `resource busy` is retryable only under the narrow
  conditions in step 9; unrelated packaging failures require diagnosis.
- **Release claims:** ad-hoc signing proves bundle consistency, not developer
  identity, Gatekeeper acceptance, or notarization.

## Verification

Before declaring the artifact ready, collect fresh evidence that:

1. `git status --short` is clean and `git rev-parse HEAD` equals
   `SOURCE_COMMIT`.
2. `bash scripts/build-desktop-dmg.sh --check` passes on Darwin ARM64 with the
   pinned Node version.
3. `bash scripts/build-desktop-dmg.sh` exits zero using task-local `TMPDIR`.
4. `desktop-dmg-contract.mjs validate-log` reports no Playwright download.
5. `desktop-dmg-contract.mjs find-dmg` resolves the artifact created by this
   build.
6. `codesign --verify --deep --strict` and `hdiutil verify` both exit zero.
7. The copied artifact's byte size and `shasum -a 256` digest are recorded and
   its digest matches the source artifact.
8. The handoff labels the DMG as macOS ARM64, ad-hoc signed, not notarized, and
   awaiting fresh-machine functional acceptance.
