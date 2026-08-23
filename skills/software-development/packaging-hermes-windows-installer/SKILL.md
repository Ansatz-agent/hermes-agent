---
name: packaging-hermes-windows-installer
description: Build and verify Hermes Windows installers on macOS.
version: 0.1.0
author: yuxiaoy, Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [desktop, packaging, windows, nsis, release-engineering]
    related_skills: [hermes-agent-skill-authoring, systematic-debugging]
---

# Packaging Hermes Windows Installer Skill

Build a Windows x64 NSIS test installer from a Hermes source checkout on
macOS, using the repository's locked desktop toolchain. This workflow does not
replace Windows-native signing, installation, launch, login, and uninstall
acceptance tests required for a formal release.

## When to Use

- A tester needs a Windows `.exe`, but the available build host is macOS.
- The target branch contains the offline Windows payload preparation scripts.
- The build must use repository-local npm and Electron Builder dependencies,
  without GitHub Actions or a global Electron Builder installation.

Do not use this workflow for a signed production release, Windows ARM64, or a
branch that has not implemented `prepare:package:win` and `package-audit.mjs`.

## Prerequisites

- macOS with Git, Node 26.7.0, npm 12, `curl`, `tar`, `file`, and `bsdtar`.
- Enough free space for root `node_modules`, offline inputs, unpacked Windows
  resources, and the NSIS artifact. Budget at least 8 GiB.
- Network access to the npm mirror, GitHub release assets, and Python downloads
  while preparing inputs. The resulting installer is intended to install
  without those sources.
- A user-supplied source ref. Resolve it to an expected 40-character commit
  before building; never silently substitute the current branch or `FETCH_HEAD`.
- A project-local `tmp/` directory. Never place build scratch data in a system
  temporary directory.
- Exact auth-runtime inputs used by the verified workflow: uv 0.12.5 and a
  uv-managed CPython 3.13.15. On Apple Silicon, the verified uv archive is
  `uv-aarch64-apple-darwin.tar.gz`, SHA-256
  `5bb0e5fe008a773c3dbcb97ff79cd89e1241464fe9d2f986d52ad8f1b037bd62`.
  On Intel macOS, select the matching `x86_64-apple-darwin` release asset and
  verify its published checksum instead of reusing the Apple Silicon hash.

## How to Run

Run every command through `terminal` from the repository root. Use long
timeouts for dependency preparation and packaging, keep the terminal result
for evidence, and stop at the first failed command. Do not install Wine,
replace repository lockfiles, stash user changes, or clean caches unless the
user separately authorizes those actions.

## Quick Reference

| Phase | Proof |
|---|---|
| Pin source | Clean isolated worktree and full commit recorded |
| Install dependencies | Root `npm ci` completes without lockfile drift |
| Repair native input | `get-windows` binding is PE32+ x86-64 |
| Prepare offline payload | All bootstrap archives and manifests exist |
| Package | `Hermes-*-win-x64.exe` and `win-unpacked/` exist |
| Audit | Contract tests, package audit, types, entries, and hashes pass |
| Hand off | Unique Downloads filename plus Windows-native test checklist |

## Procedure

1. **Protect provenance and user work.** Inspect the requested checkout with:

   ```text
   terminal(command="git status --short", timeout=30)
   terminal(command="git rev-parse HEAD", timeout=30)
   ```

   If the checkout is dirty or on another ref, create an isolated worktree
   under the workspace's `tmp/` directory from the requested remote ref. Do
   not stash, reset, or overwrite the user's checkout. Record the resulting
   full commit as `SOURCE_COMMIT` and confirm it matches the expected
   40-character commit. Completion criterion: the build worktree is clean and
   `git rev-parse HEAD` equals the approved commit.

2. **Confirm that this branch supports the workflow.** Use `terminal` to verify
   the desktop package declares `prepare:package:win`, and that the branch has
   `apps/desktop/scripts/package-audit.mjs` plus its Windows contract tests.
   If any are absent, stop: do not invent equivalent payloads or build a
   deceptively incomplete installer. Completion criterion: every required
   script resolves from the target commit.

3. **Prepare task-local toolchain inputs.** Create directories beneath
   `tmp/windows-package/`. Place the exact uv 0.12.5 binary there, verify its
   checksum, then use it to install and locate Python 3.13.15:

   ```text
   terminal(command="tmp/windows-package/uv/uv --version", timeout=30)
   terminal(command="tmp/windows-package/uv/uv python install 3.13.15", timeout=900)
   terminal(command="tmp/windows-package/uv/uv python find 3.13.15", timeout=30)
   ```

   Retain the absolute uv and Python paths as
   `HERMES_AUTH_TOOLCHAIN_UV_PATH` and
   `HERMES_AUTH_TOOLCHAIN_HOST_PYTHON`. Completion criterion: both paths are
   executable and report the pinned versions.

4. **Install the locked JavaScript dependency tree.** From the repository root,
   run `npm ci` with the proven mirrors and without browser or signing probes:

   ```text
   terminal(command="env NPM_CONFIG_REGISTRY=https://registry.npmmirror.com NPM_CONFIG_REPLACE_REGISTRY_HOST=always PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ CSC_IDENTITY_AUTO_DISCOVERY=false npm ci", timeout=3600)
   ```

   Completion criterion: installation exits zero and `git status --short`
   shows no tracked lockfile or source changes.

5. **Install the Windows `get-windows` native binding.** A macOS `npm ci` can
   leave only the host binding. From the repository root, explicitly ask the
   locked `node-pre-gyp` for the Windows x64 prebuild:

   ```text
   terminal(command="cd node_modules/get-windows && node ../@mapbox/node-pre-gyp/bin/node-pre-gyp install --fallback-to-build=false --target_platform=win32 --target_arch=x64", timeout=900)
   terminal(command="file node_modules/get-windows/lib/binding/napi-9-win32-unknown-x64/node-get-windows.node", timeout=30)
   ```

   Completion criterion: `file` identifies the binding as PE32+ x86-64. A
   Mach-O result is a hard stop because it would produce a broken package.

6. **Cache the pinned PortableGit input.** Download
   `PortableGit-2.55.0.3-64-bit.7z.exe` into
   `apps/desktop/build/package-input-cache/` and verify SHA-256
   `ab00566336b5472120f9a52d34f2e79c5406535792acb0548001ffd0bd090e5d`
   before using it. Use `terminal(command="shasum -a 256 <portable-git-file>",
   timeout=120)` and compare the entire digest. Completion criterion: filename
   and digest both match; delete or quarantine a mismatched download rather
   than retrying the build with it.

7. **Prepare the offline Windows payload.** Point `TMPDIR` at the task-local
   scratch directory and pass the exact toolchain paths to the repository
   script:

   ```text
   terminal(command="env TMPDIR=<repo-root>/tmp/windows-package/runtime HERMES_AUTH_TOOLCHAIN_UV_PATH=<absolute-uv-0.12.5> HERMES_AUTH_TOOLCHAIN_HOST_PYTHON=<absolute-python-3.13.15> NPM_CONFIG_REGISTRY=https://registry.npmmirror.com NPM_CONFIG_REPLACE_REGISTRY_HOST=always ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ npm run prepare:package:win --workspace apps/desktop", timeout=3600)
   ```

   Verify that the prepared input contains `install.ps1`,
   `payload-manifest.json`, `hermes-backend.tar.gz`,
   `git-bash-runtime.tar.xz`, and the auth-toolchain manifest, all bound to
   `SOURCE_COMMIT`. Completion criterion: no required input is absent and the
   recorded source commit is exact.

8. **Build renderer and main-process assets.** Run:

   ```text
   terminal(command="npm run build --workspace apps/desktop", timeout=1800)
   ```

   Completion criterion: the desktop build stamp names `SOURCE_COMMIT` and the
   compiled output exists without tracked source changes.

9. **Invoke the locked Electron Builder directly.** The root Windows release
   orchestrator correctly rejects a macOS host, while the desktop `builder`
   wrapper may inject the host macOS `electronDist`. For this cross-build only,
   invoke the repository-installed CLI from `apps/desktop`:

   ```text
   terminal(command="cd apps/desktop && env NODE_OPTIONS=--max-old-space-size=16384 CSC_IDENTITY_AUTO_DISCOVERY=false node ../../node_modules/electron-builder/out/cli/cli.js --win nsis --x64 --publish never", timeout=7200)
   ```

   This is still the repository's locked packaging toolchain; it does not use
   GitHub Actions or a global builder. Completion criterion:
   `apps/desktop/release/Hermes-*-win-x64.exe` and
   `apps/desktop/release/win-unpacked/` both exist.

10. **Run contract and package audits.** Use the full recorded commit:

    ```text
    terminal(command="npm run test:desktop:windows-contract", timeout=1800)
    terminal(command="npm exec --workspace apps/desktop vitest run scripts/windows-auth-toolchain.integration.test.mjs", timeout=1800)
    terminal(command="npm run typecheck --workspace apps/desktop", timeout=1800)
    terminal(command="node apps/desktop/scripts/package-audit.mjs --resources apps/desktop/release/win-unpacked/resources --expected-commit <SOURCE_COMMIT>", timeout=1800)
    ```

    The auth-toolchain suite may skip its Windows-only execution arm on macOS;
    its host-independent tests must pass. Completion criterion: every runnable
    test passes and package audit reports the exact source commit.

11. **Inspect the delivered binary, not only the staging tree.** Use `file` on
    both the NSIS installer and `win-unpacked/Hermes.exe`. Then inspect the
    installer with `terminal(command="bsdtar -tf <installer.exe>", timeout=300)`
    and confirm it carries the bootstrap manifests, backend archive,
    PortableGit archive, install script, install stamp, and `Hermes.exe`.
    Finally run `terminal(command="shasum -a 256 <installer.exe>", timeout=300)`
    and record the byte size. Completion criterion: x64 identities, required
    entries, size, and SHA-256 are all captured in the handoff report.

12. **Copy without overwriting and hand off.** Construct a filename containing
    the product version, target ref slug, and short source commit. Verify the
    destination does not already exist, then copy it to `$HOME/Downloads` with
    `terminal`. Report source commit, absolute destination, byte size, SHA-256,
    signing state, and unverified Windows-native checks. Completion criterion:
    the copied file's checksum equals the release artifact's checksum.

## Pitfalls

- **Unsigned output:** this route produces an unsigned tester artifact. Do not
  present it as a signed production release.
- **Wine warning:** without `wine64`, Electron Builder may warn that it cannot
  edit the executable and may retain the stock Electron icon. Do not install
  Wine automatically; report the limitation and obtain approval first.
- **Wrapper trap:** `npm run builder -- --win nsis --x64` can inject a macOS
  Electron distribution. Use the direct locked CLI only for this Mac cross-build.
- **Host-native gap:** macOS cannot prove NSIS install, first launch, Windows
  Credential Manager, account login, protected runtime gating, or uninstall.
- **Cache ambiguity:** never accept a cached archive solely because its name
  matches. Recompute its digest before every release candidate build.
- **Dirty tree:** dependency or packaging commands must not be allowed to fold
  unrelated user edits into the build. Stop if tracked files change.
- **Wrong architecture:** the procedure is Windows x64 only. Do not relabel it
  as ARM64 or universal.

## Verification

The Mac-side result is ready for tester handoff only when all of these are true:

- Source checkout is clean and matches the approved 40-character commit.
- Pinned uv, Python, PortableGit, and `get-windows` inputs were verified.
- Offline bootstrap archives and manifests exist and bind to that commit.
- Windows packaging contracts, host-independent auth tests, typecheck, and
  `package-audit.mjs` pass.
- Installer and inner executable identify as Windows x64 binaries.
- Installer entries include the complete offline bootstrap payload.
- Downloads copy has a unique filename and matching byte size and SHA-256.
- The handoff explicitly labels the artifact unsigned and lists these required
  Windows-native checks: install, launch, login, protected runtime, offline
  dependency use, restart, and uninstall.

Do not claim release readiness until a Windows tester records those native
results. A successful macOS cross-build proves packaging integrity, not the
complete end-user lifecycle.
