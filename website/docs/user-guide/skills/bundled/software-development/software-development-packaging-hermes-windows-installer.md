---
title: "Packaging Hermes Windows Installer — Build and verify Windows Setup installers from any host"
sidebar_label: "Packaging Hermes Windows Installer"
description: "Build and verify Windows Setup installers from any host"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Packaging Hermes Windows Installer

Build and verify Windows Setup installers from any host.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/packaging-hermes-windows-installer` |
| Version | `0.2.0` |
| Author | yuxiaoy, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `desktop`, `packaging`, `windows`, `nsis`, `release-engineering` |
| Related skills | [`hermes-agent-skill-authoring`](/docs/user-guide/skills/bundled/software-development/software-development-hermes-agent-skill-authoring), [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Packaging Hermes Windows Installer Skill

Build and verify the Windows x64 **Setup installer** used by this repository.
The outer Tauri/NSIS installer carries a committed source snapshot and the
Windows bootstrap resources. On first launch, the Windows bootstrapper extracts
that snapshot and runs the existing installer script to install or update the
Desktop application. This is a two-stage installer; it is not the same artifact
as a direct Electron Builder Windows package.

## When to Use

- A tester or release engineer needs the repository's Windows Setup installer.
- The source repository is private and the installer must not fetch the source
  from GitHub during installation or update.
- The requested build should preserve the existing setup flow while moving the
  source archive and required Windows bootstrap inputs inside the Setup.

Do not use this workflow for a signed production release, Windows ARM64, or a
branch that does not provide `prepare:package:win`, the bootstrap resource
preparation script, and the Windows contract tests.

## Prerequisites

- A supported build host with the repository's Node, npm, Rust/Tauri, and NSIS
  toolchain, or a Windows VM where the missing stages can be completed. First
  identify the current host and available targets; choose native or
  cross-build steps accordingly instead of copying commands written for another
  host.
- Enough disk for root `node_modules`, the staged Windows payload, Tauri build
  output, and a Windows VM. Keep all task scratch data below `tmp/` in the
  checkout.
- Network access while preparing inputs and, unless all later dependencies are
  separately cached, during the Windows first-run Desktop stage. The bundled
  source itself must not require GitHub.
- A user-approved source ref resolved to its full 40-character commit. Never
  silently substitute the current branch or `FETCH_HEAD`.
- The repository-pinned auth inputs (uv 0.12.5 and CPython 3.13.15) and the
  verified PortableGit input
  `PortableGit-2.55.0.3-64-bit.7z.exe` with SHA-256
  `ab00566336b5472120f9a52d34f2e79c5406535792acb0548001ffd0bd090e5d`.
  If the preparation script cannot discover the host toolchain automatically,
  use its supported `HERMES_AUTH_TOOLCHAIN_UV_PATH` and
  `HERMES_AUTH_TOOLCHAIN_HOST_PYTHON` overrides, while still staging Windows
  target assets in the payload.

## How to Run

Run commands through `terminal` from the repository root. Preserve command
output for the handoff report and stop at the first failed phase. Before
building, inspect the host OS, available Rust targets, and repository scripts;
then select the host-appropriate invocation. Do not install a host-specific
cross-compilation workaround or a global packaging tool without an explicit
need and approval.

Do not stash, reset, overwrite user changes, replace lockfiles, or clean shared
caches. If the current checkout is dirty, build from a clean isolated worktree.

## Quick Reference

| Phase | Proof |
|---|---|
| Pin source | Clean worktree and full source commit recorded |
| Install dependencies | Locked `npm ci` completes without lockfile drift |
| Prepare Windows payload | Source archive, manifests, auth assets, Git runtime, and native bundle exist |
| Build Setup | Windows x64 Tauri/NSIS Setup artifact exists |
| Simulate first run | Windows VM extracts bundled source and reaches Desktop stage |
| Audit | Contract tests and final installed Desktop package audit pass |
| Hand off | Unique artifact name, size, hash, commit, and lifecycle results recorded |

## Procedure

1. **Protect provenance and user work.** Inspect the requested checkout:

   ```text
   terminal(command="git status --short", timeout=30)
   terminal(command="git rev-parse HEAD", timeout=30)
   ```

   If it is dirty or on another ref, create an isolated worktree under
   `tmp/` from the approved ref. Record the resulting full commit as
   `SOURCE_COMMIT`. Completion criterion: the worktree is clean and its
   `git rev-parse HEAD` equals the approved commit.

2. **Confirm the route exists in that commit.** Verify the desktop package
   exposes `prepare:package:win`, the bootstrap package exposes
   `prepare:resources:win`, and the branch contains:

   ```text
   apps/desktop/scripts/build-backend-payload.mjs
   apps/desktop/scripts/prepare-package-inputs.mjs
   apps/desktop/scripts/package-audit.mjs
   apps/bootstrap-installer/scripts/prepare-resources.mjs
   scripts/install.ps1
   ```

   Also locate the Windows contract tests. Completion criterion: every required
   script and test resolves from `SOURCE_COMMIT`; do not invent a substitute
   packaging path.

3. **Install the locked JavaScript dependencies.** Use the repository lockfiles
   and a reachable package mirror appropriate for the current host:

   ```text
   terminal(command="npm ci", timeout=3600)
   ```

   Browser downloads and signing probes may be disabled when the repository
   documents those switches. Do not accept a lockfile or tracked source change
   from this step. Completion criterion: `npm ci` exits zero and the worktree
   remains clean.

4. **Prepare the Windows payload.** Run the repository's Windows preparation
   entry point from the host selected in the environment check:

   ```text
   terminal(command="npm run prepare:package:win --workspace apps/desktop", timeout=3600)
   ```

   The script creates a source archive from the committed Git tree and stages
   the target-specific bootstrap inputs. Confirm that the prepared resources
   include all of the following:

   ```text
   install.ps1
   hermes-backend.tar.gz
   payload-manifest.json
   auth-toolchain/uv.exe
   auth-toolchain/python-embed.zip
   auth-toolchain/requirements*.txt or a hash-locked equivalent
   get-windows-win32-x64.tar.gz
   git-bash-runtime.tar.xz
   ```

   The archive must contain the application source and lockfiles but no `.git`
   directory or uncommitted generated files. The manifest must bind the source
   archive, installer script, auth toolchain, Git runtime, and native bundle to
   `SOURCE_COMMIT`. Completion criterion: every required entry exists and every
   recorded commit/hash matches the prepared bytes.

5. **Stage bootstrap resources and build the outer Setup.** Prefer the root
   Windows Setup command:

   ```text
   terminal(command="npm run build:setup:windows", timeout=7200)
   ```

   If the current host cannot execute that command natively, determine the
   available target and toolchain from the environment and adapt the invocation
   or continue the build in the Windows VM. Do not copy a fixed host-specific
   target flag into this skill. Completion criterion: a Windows x64 Tauri/NSIS
   Setup artifact exists and its embedded resource tree contains the payload
   verified in step 4.

6. **Inspect the outer artifact.** Use host-appropriate binary, archive, and
   checksum tools to record the Setup filename, byte size, SHA-256, and embedded
   entries. The outer artifact is expected to contain bootstrap resources, not
   the final Desktop `app.asar`, `node_modules`, or `Ansatz.exe`. An NSIS stub
   may identify as a 32-bit PE while the embedded Tauri executable is Windows
   x64; inspect the inner executable before reporting an architecture problem.
   Completion criterion: the artifact identity, commit, hashes, and required
   bootstrap entries are captured.

7. **Simulate the user installation in a Windows VM.** Transfer only the Setup
   artifact (and any separately documented test inputs) to a clean VM. Execute
   it as a user would and observe the complete bootstrap sequence:

   - the Tauri bootstrap resolves its bundled resource root;
   - the source archive is validated and extracted transactionally;
   - the bundled `install.ps1` is invoked with `-BundledSource` and
     `-BundledToolchain`;
   - the existing stages run, including the Desktop stage when requested;
   - updates install a new Setup and replace the source snapshot without a
     GitHub source checkout.

   Record stage logs, source/install markers, failures, and network accesses.
   Bundled source delivery is expected to work without GitHub; npm, Electron,
   Python packages, or Playwright may still need the network unless those
   artifacts are separately cached. Completion criterion: the VM reaches a
   usable installed Desktop runtime and the source marker names `SOURCE_COMMIT`.

8. **Audit the final installed Desktop package.** Run the host-independent
   contract tests and type checks from the build worktree:

   ```text
   terminal(command="npm run test:desktop:windows-contract", timeout=1800)
   terminal(command="npm exec --workspace apps/desktop vitest run scripts/windows-auth-toolchain.integration.test.mjs", timeout=1800)
   terminal(command="npm run typecheck --workspace apps/desktop", timeout=1800)
   ```

   After Stage-Desktop completes in Windows, run `package-audit.mjs` against the installed
   `win-unpacked/resources` tree, not merely against the outer Setup resources.
   Verify the final `app.asar`, Windows native modules, bootstrap manifest, and
   source commit. Completion criterion: all runnable tests pass and the final
   Desktop package audit reports the exact commit.

9. **Complete Windows-native acceptance checks.** In the VM, test install,
   first launch, login/configuration, protected runtime behavior, restart,
   update/replace, offline source lookup, and uninstall. Distinguish failures
   caused by missing online third-party dependencies from failures in bundled
   source extraction or bootstrap orchestration. Completion criterion: each
   check has a recorded pass/fail result and supporting log evidence.

10. **Hand off without overwriting an artifact.** Construct a unique filename
    containing the product version, target ref slug, and short `SOURCE_COMMIT`.
    Verify the destination is unused, copy the Setup, and recompute its hash.
    Report the absolute destination, byte size, SHA-256, signing state, source
    commit, network assumptions, and Windows-native results. Completion
    criterion: the copied checksum equals the built artifact checksum.

## Pitfalls

- **Two-stage boundary:** the Setup built on the host is an outer bootstrap
  installer. It intentionally does not contain the final Electron application;
  `Ansatz.exe`, `app.asar`, and native Desktop modules are produced or staged
  during the Windows first-run phase.
- **Source versus full offline install:** the source archive and source version
  no longer come from GitHub. This does not by itself bundle Node, Electron,
  npm packages, Python dependencies, or Playwright browsers.
- **Target-specific inputs:** Windows `uv.exe`, the embeddable Python archive,
  Windows wheels, `get-windows-win32-x64.tar.gz`, and the Git runtime must be
  selected from the manifest. Never substitute the current host's runtime.
- **Host-dependent archive bytes:** extraction and tar/xz tools can vary by
  host, so Git-runtime archive hashes and sizes may differ between valid builds.
  Compare the manifest, required entries, and Windows behavior; do not require
  an unexplained cross-host byte-for-byte match.
- **Native dependency boundary:** the bundled `get-windows` archive is restored
  after Windows `npm ci`; other native modules may still be installed or
  rebuilt in Windows and must be audited there.
- **Host/build confusion:** do not infer the target architecture from an NSIS
  stub alone, and do not assume a direct Electron Builder artifact is equivalent
  to this Setup artifact.
- **Unsigned output:** this workflow produces a tester artifact unless signing
  is separately configured. Never label it as a signed production release.
- **Dirty provenance:** never allow unrelated worktree edits, generated files,
  or lockfile changes to enter the source archive.

## Verification

The result is ready for tester handoff only when all of these are true:

- The isolated worktree is clean and matches the approved 40-character commit.
- The Windows payload manifest binds the committed source archive, installer
  script, auth toolchain, Git runtime, and native bundle to that commit.
- The outer Setup is a Windows x64 artifact with the complete bootstrap payload.
- The Windows VM completed bundled-source extraction and the requested Desktop
  stage without fetching the source from GitHub.
- The final installed Desktop tree passed `package-audit.mjs` and contains the
  expected Windows `app.asar` and native modules.
- Contract tests, host-independent auth tests, and type checks pass.
- The handoff includes artifact size/hash, signing state, network limitations,
  and the recorded install, launch, login, update, restart, and uninstall
  results.

Do not claim release readiness until Windows-native lifecycle results are
recorded. A successful host-side Setup build proves bootstrap packaging
integrity, not the complete end-user lifecycle.
