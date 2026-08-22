# Main Authentication, Voice, and Cross-Platform Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The user explicitly forbids subagents for this migration.

**Goal:** Make `integration/main-auth-voice-base` a reviewed candidate in which `main` owns DMG-equivalent authentication and Voice behavior plus the macOS/Windows clean-install packaging foundation, with recursive domestic-mirror-first dependency handling.

**Architecture:** Reconstruct accepted behavior from locked macOS and Windows references instead of merging either release branch wholesale. A versioned migration ledger controls every imported commit/path, a versioned download-origin manifest controls every Hermes-managed network dependency, and platform adapters share one Auth Guard, bootstrap protocol, runtime epoch, progress schema, and Voice implementation. Build-time scripts generate platform payloads; generated archives, installers, credentials, logs, and CI evidence never become committed source.

**Tech Stack:** Node.js 26.7.0, npm 11.19.0, Electron 40, electron-builder 26, TypeScript 6, React 19, Vitest, Playwright, Python 3.11-3.13, uv, pytest, Ruff, macOS Keychain/Unix sockets, Windows Credential Manager/named pipes, DMG/ZIP, NSIS/MSI.

---

## Scope and authority

Work only in:

```text
/Users/zhouzhangchen/Desktop/自己的/acadamic/agent/hermes-agent/.worktrees/integration-main-auth-voice-base
```

Work only on:

```text
integration/main-auth-voice-base
```

This plan does not authorize a push or merge to `main`, deletion of the immutable reference branches, modification of the dirty primary worktree, or storage of test credentials. It may push only the integration branch after all gates pass and the user approves that push.

Locked references:

```text
ansatz/main                                      9bd88c530716279a089ed18428dc785732b6e1be
feature/remote-auth-hard-gate                    763465daf019c8755813659b98a72c6c6f4662e3
feature/desktop-dmg-voice-confirmation           3ad4a126606079c77e7adca6d8661cd0c8c0a93b
integration/desktop-dmg-auth-e2e                 403e1c3873d1679720c1403d7e38acd289804d69
release/desktop-dmg-auth-e2e                     80db6d8265f805cec46817d913982e4c5f6405c4
integration/desktop-windows-auth-e2e behavior    c2d3d09aab921130171ff611e260c13e9c6d477c
integration/desktop-windows-auth-e2e docs tip    56b402c63b22da81f906ff1f7398a90cfd17bd81
shared baseline                                  4ef56cef4c6eecc009e2284fe2f1df20664f357a
```

Candidate preparation commits already present and intentionally retained:

```text
2938971f77  fix(stt): preserve explicit Apple Silicon compute type
21afa522c8  docs: make main own cross-platform package foundation
136882a61e  docs: require recursive domestic mirror coverage
```

The obsolete plan `docs/superpowers/plans/2026-08-22-main-auth-voice-platform-boundary.md` remains an immutable audit record marked `SUPERSEDED — DO NOT EXECUTE`.

## Target file structure

The migration must converge on these responsibilities:

```text
hermes_cli/client_auth/**
  One central authentication client, secure-store abstraction, owner runtime,
  entrypoint guard, CLI commands, and static public help.

apps/desktop/electron/auth-*.ts
apps/desktop/electron/guarded-ipc.ts
apps/desktop/electron/desktop-runtime-gate.ts
apps/desktop/electron/authenticated-runtime-preparation.ts
apps/desktop/electron/bootstrap-progress.ts
  Shared Desktop authorization, safe bootstrap status, epoch suppression,
  protected IPC, and runtime readiness.

apps/desktop/electron/package-runtime/**
  Small platform adapters for packaged resource discovery, process ownership,
  bootstrap commands, and runtime placement. No authentication policy.

desktop_auth_runtime/**
  Locked minimal authentication project used to build the bundled pre-login
  runtime for both platforms.

apps/desktop/scripts/prepare-package-inputs.mjs
  One entrypoint that dispatches to macOS or Windows payload preparation.

apps/desktop/scripts/build-auth-toolchain.mjs
apps/desktop/scripts/build-backend-payload.mjs
apps/desktop/scripts/prepare-windows-git-runtime.mjs
  Reproducible platform payload builders with hashes and manifests.

docs/security/hermes-managed-download-origins.json
  Versioned inventory of every Hermes-managed build/install/repair/update/lazy
  download and its domestic-first or bundled delivery contract.

scripts/check_hermes_managed_downloads.py
  Static boundary check rejecting unregistered origins and unsafe remote script
  execution in Hermes-owned dependency paths.

scripts/check_main_auth_voice_parity.py
docs/security/main-auth-voice-migration-ledger.json
docs/security/main-auth-voice-product-paths.txt
  Mechanical DMG parity and replay completeness gates.
```

Keep existing large files such as `apps/desktop/electron/main.ts`, `hermes_cli/main.py`, and `scripts/install.{sh,ps1}` in their repository-established form. Add focused helpers for new policy instead of expanding those files with duplicated platform decisions.

## Task 0: Reconfirm the candidate and toolchain

**Files:** None.

- [ ] **Step 1: Verify worktree and branch identity**

Run:

```bash
test "$(git branch --show-current)" = "integration/main-auth-voice-base"
test "$(git merge-base ansatz/main HEAD)" = "9bd88c530716279a089ed18428dc785732b6e1be"
git status --short --branch
```

Expected: the branch matches, the merge base matches, and the only untracked paths are the three obsolete Task 1 drafts:

```text
docs/security/main-auth-voice-common-paths.txt
scripts/check_main_platform_boundary.py
tests/test_main_platform_boundary.py
```

Do not delete them with shell commands. Task 1 replaces them through `apply_patch` and commits the replacement.

- [ ] **Step 2: Recheck every remote/reference identity**

Run:

```bash
test "$(git ls-remote ansatz refs/heads/main | cut -f1)" = "9bd88c530716279a089ed18428dc785732b6e1be"
test "$(git rev-parse feature/remote-auth-hard-gate)" = "763465daf019c8755813659b98a72c6c6f4662e3"
test "$(git rev-parse feature/desktop-dmg-voice-confirmation)" = "3ad4a126606079c77e7adca6d8661cd0c8c0a93b"
test "$(git rev-parse integration/desktop-dmg-auth-e2e)" = "403e1c3873d1679720c1403d7e38acd289804d69"
test "$(git rev-parse release/desktop-dmg-auth-e2e)" = "80db6d8265f805cec46817d913982e4c5f6405c4"
test "$(git rev-parse integration/desktop-windows-auth-e2e)" = "56b402c63b22da81f906ff1f7398a90cfd17bd81"
```

Expected: all commands exit zero. If remote `main` moved, stop and revise the design/plan against the new base before importing product code.

- [ ] **Step 3: Verify the exact Node/npm contract**

Run:

```bash
export PATH="/private/tmp/hermes-node-v26.7.0-20260822/node-v26.7.0-darwin-arm64/bin:/usr/bin:/bin:/usr/sbin:/sbin"
test "$(node --version)" = "v26.7.0"
test "$(npm --version)" = "11.19.0"
env NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
  NPM_CONFIG_REPLACE_REGISTRY_HOST=always npm ci
```

Expected: all assertions pass and `npm ci` completes. The archive at this verified local path previously matched the official Node SHA-256 `7ee659a7768e641bbfd5360940660b8e8fd0052f77488f365562bac522fc15d4`. If the temporary toolchain is no longer present, stop and recreate a checksum-verified Node 26.7.0 toolchain before continuing. Do not use Node 25 or npm 12 for parity/build evidence.

- [ ] **Step 4: Run the branch baseline**

Run Python tests only through the repository runner:

```bash
scripts/run_tests.sh tests/tools/test_transcription_tools.py -q
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
git diff --check
```

Expected: PASS. The known macOS process-permission test may be run with normal macOS permissions if sandbox process inspection is denied; record that environmental distinction without weakening the assertion.

## Task 1: Replace the obsolete boundary draft with migration and ownership contracts

**Files:**

- Create: `docs/security/main-auth-voice-migration-ledger.json`
- Create: `docs/security/main-auth-voice-product-paths.txt`
- Create: `docs/security/hermes-managed-download-origins.json`
- Create: `scripts/check_main_auth_voice_migration.py`
- Create: `scripts/check_hermes_managed_downloads.py`
- Create: `tests/test_main_auth_voice_migration.py`
- Create: `tests/test_hermes_managed_downloads.py`
- Delete/replace obsolete drafts: `docs/security/main-auth-voice-common-paths.txt`, `scripts/check_main_platform_boundary.py`, `tests/test_main_platform_boundary.py`

- [ ] **Step 1: Write failing migration-ledger tests**

Create tests that assert:

```python
assert ledger["base"] == "9bd88c530716279a089ed18428dc785732b6e1be"
assert ledger["dmg_reference"] == "80db6d8265f805cec46817d913982e4c5f6405c4"
assert ledger["windows_reference"] == "c2d3d09aab921130171ff611e260c13e9c6d477c"
assert every_changed_path_has_one_owner()
assert every_replayed_commit_has_one_strategy()
assert product_path_manifest_equals_ledger_projection()
assert no_generated_artifact_is_committed()
assert no_secret_or_evidence_path_is_packaged()
```

The owner enum is exact:

```text
common-product
package-shared
package-macos
package-windows
ci-infrastructure
test-evidence
historical-drop
reference-equivalent
```

Run:

```bash
scripts/run_tests.sh tests/test_main_auth_voice_migration.py -q
```

Expected: FAIL because the ledger and checker do not yet exist.

- [ ] **Step 2: Write failing managed-download tests**

The test fixture creates a temporary repository and verifies that the checker rejects:

```text
curl URL | sh
Invoke-RestMethod URL | Invoke-Expression
an unregistered https:// origin in install.sh/install.ps1/bootstrap/lazy_deps
an official-first origin where a domestic primary is required
a child installer with no sanitized mirror environment
a download entry with no hash/signature contract
a runtime-generated download with only a GitHub source
```

It permits account-server traffic and user-configured provider endpoints because they are not dependency downloads.

Run:

```bash
scripts/run_tests.sh tests/test_hermes_managed_downloads.py -q
```

Expected: FAIL because the origin manifest and checker do not yet exist.

- [ ] **Step 3: Create the complete origin-manifest schema and seed entries**

Before writing the origin entries, populate `main-auth-voice-migration-ledger.json` with every commit returned by both commands below, in branch order. Each commit receives exactly one owner/strategy; historical documentation and superseded CI use explicit `historical-drop` or `test-evidence` entries rather than disappearing from the ledger:

```bash
git log --reverse --format=%H 4ef56cef4c..403e1c3873
git log --reverse --format=%H 4ef56cef4c..56b402c63b
```

Use schema version 1. Every entry has the following complete shape:

```json
{
  "id": "python-packages",
  "phases": ["auth-payload-build", "runtime-install", "repair", "lazy-feature"],
  "delivery": "domestic-first",
  "domestic_primary": "https://mirrors.ustc.edu.cn/pypi/simple",
  "domestic_secondary": "https://pypi.tuna.tsinghua.edu.cn/simple",
  "official_fallback": "https://pypi.org/simple",
  "integrity": "uv-lock-sha256",
  "idle_timeout_seconds": 90,
  "total_timeout_seconds": 600,
  "environment": ["UV_DEFAULT_INDEX", "HERMES_UV_FALLBACK_INDEX"],
  "owners": ["scripts/install.sh", "scripts/install.ps1", "apps/desktop/electron/bootstrap-process.ts"]
}
```

The initial manifest contains these exact IDs:

```text
python-packages
npm-packages
node-runtime
electron-runtime
electron-builder-binaries
playwright-browser
sensevoice-model
managed-uv
managed-python
portable-git
ripgrep
ffmpeg
browser-use-cli
cua-driver
```

Use `delivery: bundled` when no reviewed domestic source exists. Such an entry has `domestic_primary: null`, `official_fallback: null`, a non-null build-time provenance and SHA-256, and no clean-machine runtime download. Do not invent an unreviewed GitHub proxy.

- [ ] **Step 4: Implement the two checkers**

`check_main_auth_voice_migration.py` must inspect committed, staged, unstaged, and untracked candidate paths and reject:

```text
*.dmg, *.pkg, *.exe, *.msi, *.zip, *.tar.gz payload outputs
apps/desktop/release/** and apps/desktop/build/logs/**
.env or credential files
raw session/cookie/keychain evidence
an unowned changed path
a ledger path listed under two owners
```

`check_hermes_managed_downloads.py` must scan only Hermes-managed dependency paths and compare every literal URL and download subprocess to `hermes-managed-download-origins.json`. It must ignore tests' `.invalid` fixtures and product API/provider traffic by explicit path and call-site rule, never by a broad URL-domain exemption.

- [ ] **Step 5: Make the contract tests pass and commit**

Run:

```bash
scripts/run_tests.sh tests/test_main_auth_voice_migration.py tests/test_hermes_managed_downloads.py -q
python scripts/check_main_auth_voice_migration.py --base ansatz/main
python scripts/check_hermes_managed_downloads.py
git diff --check
```

Expected: PASS on the current candidate and no obsolete boundary draft remains.

Commit:

```bash
git add docs/security/main-auth-voice-migration-ledger.json \
  docs/security/main-auth-voice-product-paths.txt \
  docs/security/hermes-managed-download-origins.json \
  scripts/check_main_auth_voice_migration.py \
  scripts/check_hermes_managed_downloads.py \
  tests/test_main_auth_voice_migration.py \
  tests/test_hermes_managed_downloads.py
git commit -m "build: define auth voice package ownership contracts"
```

## Task 2: Replay the accepted Desktop packaging baseline and complete Voice feature

**Files:**

- Create: `tests/test_main_auth_voice_reference_contract.py`
- Modify: exact paths changed by the selected commits in Appendix A, including `apps/desktop/package.json`, `apps/desktop/scripts/**`, `scripts/build-desktop-dmg.sh`, Voice UI, Python transcription files, configuration, localization, and tests

- [ ] **Step 1: Add parity tests before replay**

Create `tests/test_main_auth_voice_reference_contract.py` and assert:

```python
assert voice_paths_at_candidate_equal("3ad4a126606079c77e7adca6d8661cd0c8c0a93b")
assert package_json_keeps_both("dist:mac:dmg", "dist:win:nsis")
assert package_files_exclude_ci_tests_docs_and_model_weights()
assert sensevoice_model_sources_are("modelscope-first", "hash-pinned")
```

Run:

```bash
scripts/run_tests.sh tests/test_main_auth_voice_reference_contract.py -q
```

Expected: FAIL because Voice and payload paths have not been replayed.

- [ ] **Step 2: Replay the macOS packaging foundation in order**

Replay these commits one at a time with `git cherry-pick -x`, stopping for conflicts and preserving the current `ansatz/main` dual-platform package skeleton:

```text
f5351c3c60  build: pin desktop dmg runtime
8aa6263a35  build: reject playwright browser downloads
3305431d68  build: add reproducible macos dmg entry point
e28883110d  build: mirror electron builder tool downloads
977322ecd7  build: ad hoc sign local macos artifacts
3ad4a12660  feat(desktop): add automatic SenseVoice dictation
abbc79d0cd  feat(desktop): bundle backend bootstrap payload
904d435685  fix(desktop): exclude dependency tests from package
```

After each commit run:

```bash
test -z "$(git diff --name-only --diff-filter=U)"
git diff --check
python scripts/check_main_auth_voice_migration.py --base ansatz/main
```

Do not resolve `apps/desktop/package.json` by taking either side wholesale. Preserve macOS and Windows targets, existing `beforePack`/`afterPack`, current Electron 40 dependencies, and both platform build commands.

After `f5351c3c60`, run `test "$(cat .node-version)" = "26.7.0"`. Expected: PASS; the toolchain pin is now part of candidate source.

- [ ] **Step 3: Run Voice and packaging baseline tests**

Run:

```bash
scripts/run_tests.sh tests/agent/test_transcription_registry.py \
  tests/tools/test_sensevoice_stt.py tests/tools/test_transcription_tools.py -q
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop -- \
  src/lib/voice-timing.test.ts \
  src/app/chat/composer/hooks/use-voice-recorder.test.tsx
node --test scripts/desktop-dmg-contract.test.mjs
```

Expected: PASS without downloading the SenseVoice model.

- [ ] **Step 4: Commit conflict resolutions separately**

If replay conflict resolutions were necessary, commit only the resolution after the replayed commits:

```bash
git add apps/desktop/package.json apps/desktop/electron/main.ts \
  apps/desktop/src/hermes.ts apps/desktop/src/i18n/en.ts \
  apps/desktop/src/i18n/zh.ts apps/desktop/src/i18n/types.ts \
  pyproject.toml uv.lock scripts/install.sh
git commit -m "fix: reconcile main packaging and voice baseline"
```

Expected: no commit is created when there were no remaining resolution changes.

## Task 3: Merge the reviewed authentication foundation

**Files:** Every product/test path changed by `feature/remote-auth-hard-gate@763465daf0`.

- [ ] **Step 1: Verify the feature authority and merge**

Run:

```bash
test "$(git merge-base feature/remote-auth-hard-gate HEAD)" = "4ef56cef4c6eecc009e2284fe2f1df20664f357a"
git merge --no-ff feature/remote-auth-hard-gate \
  -m "merge: add central remote auth hard gate"
```

Expected: a merge commit. Resolve overlaps by keeping the Voice UI and the Auth Gate; never choose a whole-file side for `electron/main.ts`, `src/hermes.ts`, localization, `hermes_cli/main.py`, `tools_config.py`, `web_server.py`, `pyproject.toml`, or `uv.lock`.

- [ ] **Step 2: Run the foundation tests**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth \
  tests/hermes_cli/test_auth_commands.py \
  tests/tui_gateway/test_account_auth.py tests/acp/test_entry.py -q
python scripts/check_auth_entrypoints.py --check
python scripts/generate_auth_free_help.py --check
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop -- \
  electron/auth-bridge.test.ts electron/auth-coordinator.test.ts \
  electron/guarded-ipc.test.ts src/components/auth-gate.test.tsx
```

Expected: PASS. Account-server traffic remains fixed at `https://c2sml.cn/agent`; no account creation/recovery UI exists.

- [ ] **Step 3: Verify the secure-store invariant**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth/test_store.py \
  tests/hermes_cli/client_auth/test_runtime.py -q
rg -n -i 'password|session|cookie|csrf|keychain' \
  apps/desktop/build/logs tests/.artifacts 2>/dev/null
```

Expected: tests PASS and the evidence search finds no real credential/session content.

## Task 4: Replay final-DMG authentication, logout, progress, and epoch behavior

**Files:**

- Modify/create: `apps/desktop/electron/auth-bridge*`, `auth-coordinator*`, `auth-scope-token*`, `authenticated-runtime-preparation*`, `bootstrap-progress*`, `desktop-runtime-gate*`, `guarded-ipc*`, `main.ts`, `preload.ts`
- Modify/create: `apps/desktop/src/components/auth-gate*`, `auth-bootstrap-progress*`, `desktop-auth-context*`, account status/logout surface files, localization
- Modify/create: `hermes_cli/client_auth/**`, protected entrypoint wrappers/manifests/tests
- Modify: central CLI/TUI/gateway/serve/cron/MCP/ACP/background entrypoints

- [ ] **Step 1: Lock replay order in the ledger**

Add the following exact order to `main-auth-voice-migration-ledger.json`:

```text
08a2eedf67 706c5a4d0d 6aed1cc0f1 e042ce3861 bffdd26edf f28a0f4b58
8af9e3eefe cdc12484f1 27567163b3 366fb3f5a8 cdb5c65bc8 df34e9da62
c478db4d2f e51e448669 f68510d905 817a5d0a6a b22bdb8d31 af19ce56b1
50609240e2 bddbec2abb 6e57ead969 c4d5ae2d40 f8d7c05fad 34026f7627
a416087126 34f1c119e2 c8fad50c49 363d464a85 a756c93e35 f5a7372e4c
c893e264e9 38230a6c9f 4e4a5d42c7
```

The ledger records `cherry-pick`, `path-extract`, or `reference-equivalent` for each SHA and exhaustive path lists for every path-extract.

- [ ] **Step 2: Add failing behavioral tests before replay**

Tests must fail on the foundation for these final-DMG behaviors:

```text
login surface mounts before protected bootstrap
protected IPC returns AUTH_REQUIRED while signed out
only sanitized auth-bootstrap status is public
Retry is absent during slow progress and appears only on declared failure
bridge HTTP timeout occurs before process teardown timeout
Retry recreates a dead local bridge
GUI logout is visible only after authentication
logout increments runtime epoch and suppresses stale preparation completion
rapid account switching cannot publish prior-account state
expired native owner recovers without bypassing online validation
all CLI/TUI/gateway/serve/cron/MCP/ACP/background entries remain gated
```

Run targeted Python and Vitest suites and record the expected failing names before replay.

- [ ] **Step 3: Replay each accepted commit in the locked order**

Run each command separately. After each command, resolve conflicts using Appendix B, run `test -z "$(git diff --name-only --diff-filter=U)"`, `git diff --check`, and the migration checker before continuing:

```bash
git cherry-pick -x 08a2eedf67
git cherry-pick -x 706c5a4d0d
git cherry-pick -x 6aed1cc0f1
git cherry-pick -x e042ce3861
git cherry-pick -x bffdd26edf
git cherry-pick -x f28a0f4b58
git cherry-pick -x 8af9e3eefe
git cherry-pick -x cdc12484f1
git cherry-pick -x 27567163b3
git cherry-pick -x 366fb3f5a8
git cherry-pick -x cdb5c65bc8
git cherry-pick -x df34e9da62
git cherry-pick -x c478db4d2f
git cherry-pick -x e51e448669
git cherry-pick -x f68510d905
git cherry-pick -x 817a5d0a6a
git cherry-pick -x b22bdb8d31
git cherry-pick -x af19ce56b1
git cherry-pick -x 50609240e2
git cherry-pick -x bddbec2abb
git cherry-pick -x 6e57ead969
git cherry-pick -x c4d5ae2d40
git cherry-pick -x f8d7c05fad
git cherry-pick -x 34026f7627
git cherry-pick -x a416087126
git cherry-pick -x 34f1c119e2
git cherry-pick -x c8fad50c49
git cherry-pick -x 363d464a85
git cherry-pick -x a756c93e35
git cherry-pick -x f5a7372e4c
git cherry-pick -x c893e264e9
git cherry-pick -x 38230a6c9f
git cherry-pick -x 4e4a5d42c7
```

These commits are no longer split between a common branch and a macOS overlay: the approved design now places their accepted product and clean-install package behavior in `main`. CI/evidence commits remain excluded and are reconstructed in Task 9.

- [ ] **Step 4: Run the complete final-DMG behavior gate**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth \
  tests/hermes_cli/test_auth_commands.py \
  tests/tui_gateway/test_account_auth.py tests/acp/test_entry.py \
  tests/test_auth_entrypoint_manifest.py -q
python scripts/check_auth_entrypoints.py --check
python scripts/generate_auth_free_help.py --check
npm run test --workspace apps/desktop -- \
  electron/auth-bridge.test.ts electron/auth-coordinator.test.ts \
  electron/authenticated-runtime-preparation.test.ts \
  electron/bootstrap-progress.test.ts electron/desktop-runtime-gate.test.ts \
  electron/guarded-ipc.test.ts src/components/auth-gate.test.tsx \
  src/components/auth-bootstrap-progress.test.tsx
```

Expected: PASS with the same visible/auth lifecycle behavior as `80db6d8265`.

- [ ] **Step 5: Commit only new integration conflict fixes**

If replay left necessary cross-platform integration edits, commit them separately:

```bash
git add apps/desktop/electron/main.ts apps/desktop/electron/preload.ts \
  apps/desktop/src/components/auth-gate.tsx apps/desktop/src/hermes.ts \
  apps/desktop/src/i18n/en.ts apps/desktop/src/i18n/zh.ts \
  apps/desktop/src/i18n/types.ts hermes_cli/main.py \
  hermes_cli/tools_config.py hermes_cli/web_server.py
git commit -m "fix: reconcile final dmg auth behavior with main"
```

## Task 5: Make the authentication and backend payload foundation cross-platform

**Files:**

- Create/modify: `desktop_auth_runtime/pyproject.toml`, `desktop_auth_runtime/uv.lock`, `desktop_auth_runtime/uv.toml`
- Create/modify: `apps/desktop/scripts/build-auth-toolchain.mjs`, `build-auth-toolchain.test.mjs`, `prepare-auth-toolchain-inputs.mjs`, `prepare-auth-toolchain-inputs.test.mjs`, `build-backend-payload.mjs`, `build-backend-payload.integration.test.mjs`
- Create: `apps/desktop/scripts/prepare-package-inputs.mjs`, `prepare-package-inputs.test.mjs`
- Modify: `apps/desktop/package.json`, `apps/desktop/scripts/before-pack.mjs`
- Create/modify: `apps/desktop/electron/package-runtime/**`, `bootstrap-payload*`, `bootstrap-toolchain*`, `bootstrap-process*`, `bootstrap-runner*`
- Modify: `scripts/install.sh`, `scripts/install.ps1`

- [ ] **Step 1: Import accepted package contracts and payload validation**

Replay these accepted package fixes/tests before generalizing the payload builders:

```bash
git cherry-pick -x 936ede7953
git cherry-pick -x 89503cfb2b
git cherry-pick -x 11d2143924
git cherry-pick -x 7666fc4da5
git cherry-pick -x 42bbebadf9
git cherry-pick -x 527eb97a19
```

After each commit run conflict, whitespace, migration, and package-content checks. Do not import the corresponding credential workflows or historical evidence.

- [ ] **Step 2: Import the accepted bundled-auth toolchain implementation**

Replay in order, one command at a time:

```bash
git cherry-pick -x aa97962e0d
git cherry-pick -x 6355c61c79
git cherry-pick -x d0b334abba
git cherry-pick -x 86d0598a13
git cherry-pick -x decb2f0fcb
git cherry-pick -x b0ad6ad4e5
git cherry-pick -x e1de17db83
git cherry-pick -x ccc7e10274
git cherry-pick -x 82b23ebde6
git cherry-pick -x 9689aefd08
git cherry-pick -x bf090797fe
```

Keep final DMG behavior for macOS. Generalize only platform selection, paths, archive format, executable names, and process APIs.

- [ ] **Step 3: Write the failing dual-platform preparation contract**

`prepare-package-inputs.test.mjs` must assert:

```javascript
assert.deepEqual(planFor('darwin', 'arm64').outputs, [
  'bootstrap/install.sh',
  'bootstrap/hermes-backend.tar.gz',
  'bootstrap/payload-manifest.json',
  'bootstrap/auth-toolchain/manifest.json'
])
assert.deepEqual(planFor('win32', 'x64').outputs, [
  'bootstrap/install.ps1',
  'bootstrap/hermes-backend.tar.gz',
  'bootstrap/payload-manifest.json',
  'bootstrap/auth-toolchain/manifest.json',
  'bootstrap/git-bash-runtime.tar.xz'
])
assert.throws(() => planFor('darwin', 'x64-on-arm64-builder'))
assert.throws(() => publishWithMissingHash())
```

Run:

```bash
node --test apps/desktop/scripts/prepare-package-inputs.test.mjs
```

Expected: FAIL because the shared dispatcher does not exist.

- [ ] **Step 4: Implement the shared package-input dispatcher**

The public interface is exact:

```javascript
export function packageInputPlan({ platform, arch, repoRoot, desktopRoot })
export async function preparePackageInputs({ platform, arch, repoRoot, desktopRoot, env })
```

The dispatcher calls existing focused builders, verifies every manifest size/SHA-256, and atomically publishes only to `apps/desktop/build/bootstrap`. It rejects symlinks, unsupported platform/arch pairs, dirty or zero commit stamps, missing locks, and any output outside the build directory.

- [ ] **Step 5: Wire direct build commands to preparation**

`apps/desktop/package.json` must contain:

```json
{
  "prepare:package:mac": "node scripts/prepare-package-inputs.mjs --platform darwin",
  "prepare:package:win": "node scripts/prepare-package-inputs.mjs --platform win32",
  "dist:mac:dmg": "npm run prepare:package:mac && npm run build && npm run builder -- --mac dmg",
  "dist:win:nsis": "npm run prepare:package:win && npm run build && npm run builder -- --win nsis"
}
```

Tests assert these commands cannot bypass preparation. The existing generic `dist:mac`, `dist:win`, ZIP, and MSI commands may remain but must either invoke the same preparation or be explicitly marked non-release development builds.

- [ ] **Step 6: Run payload contracts and commit**

Run:

```bash
node --test apps/desktop/scripts/build-auth-toolchain.test.mjs \
  apps/desktop/scripts/prepare-auth-toolchain-inputs.test.mjs \
  apps/desktop/scripts/prepare-package-inputs.test.mjs \
  apps/desktop/scripts/before-pack.test.mjs
npm run test --workspace apps/desktop -- \
  electron/bootstrap-payload.integration.test.ts \
  electron/bootstrap-toolchain.integration.test.ts \
  electron/bundled-runtime.integration.test.ts
scripts/run_tests.sh tests/test_install_sh_bootstrap_marker.py \
  tests/test_install_ps1_uv_powershell_host.py -q
bash -n scripts/install.sh
git diff --check
```

Expected: PASS on macOS units and Windows fakes without downloading runtime dependencies.

Commit:

```bash
git add desktop_auth_runtime apps/desktop/electron apps/desktop/scripts \
  apps/desktop/package.json scripts/install.sh scripts/install.ps1
git commit -m "feat(desktop): add cross-platform packaged runtime foundation"
```

## Task 6: Enforce recursive domestic-mirror-first behavior

**Files:**

- Modify: `docs/security/hermes-managed-download-origins.json`
- Create: `apps/desktop/electron/runtime-download-policy.ts`, `runtime-download-policy.test.ts`
- Modify: `apps/desktop/electron/bootstrap-process.ts`, `bootstrap-process.test.ts`
- Modify: `apps/desktop/scripts/prepare-auth-toolchain-inputs.mjs`, tests
- Modify: `scripts/install.sh`, `scripts/install.ps1`
- Modify: `tools/lazy_deps.py`, `tools/sensevoice_stt.py`, `tools/browser_use_cli.py`, `tools/computer_use/cua_backend.py`
- Modify: `hermes_cli/tools_config.py`
- Modify: `tests/tools/test_lazy_deps.py`, `tests/tools/test_sensevoice_stt.py`, `tests/tools/test_browser_use_cli.py`, `tests/hermes_cli/test_tools_config.py`, `tests/test_install_sh_browser_install.py`, `tests/test_install_ps1_browser_install.py`

- [ ] **Step 1: Write failing recursive propagation tests**

The tests must prove:

```text
uv/pip receive USTC first and Tsinghua second for every transitive dependency
uv tool install browser-use receives the same indexes
npm and npm lifecycle children receive registry.npmmirror.com
Node/Playwright/Electron receive their registered mirror variables
SenseVoice tries ModelScope before its hash-pinned fallback
repair/update/lazy feature paths use the same policy as first install
inherited attacker PIP_*, UV_*, npm, Node, Electron, Playwright, and HF variables are removed
an unregistered third-party installer cannot execute
official fallback occurs only after bounded domestic failures
```

Run:

```bash
scripts/run_tests.sh tests/test_hermes_managed_downloads.py \
  tests/tools/test_lazy_deps.py tests/tools/test_sensevoice_stt.py \
  tests/tools/test_browser_use_cli.py tests/hermes_cli/test_tools_config.py -q
npm run test --workspace apps/desktop -- \
  electron/runtime-download-policy.test.ts electron/bootstrap-process.test.ts
```

Expected: targeted failures show the existing nested official/remote-script paths.

- [ ] **Step 2: Implement one immutable policy builder**

Use this public TypeScript shape:

```typescript
export type ManagedDownloadPhase =
  | 'auth-payload-build'
  | 'runtime-install'
  | 'repair'
  | 'update'
  | 'lazy-feature'

export function buildManagedDownloadEnvironment(
  source: NodeJS.ProcessEnv,
  phase: ManagedDownloadPhase
): NodeJS.ProcessEnv
```

The Python side reads the same checked-in JSON manifest and exposes:

```python
def managed_download_environment(
    phase: str,
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    ...
```

Both implementations copy only an explicit safe environment allowlist and add registered mirror variables. They never accept a renderer-provided URL.

- [ ] **Step 3: Remove unsafe nested download behavior**

Replace:

```text
curl URL | bash
irm URL | iex
raw.githubusercontent.com installer execution
official-first uv/Node/Git archive lookup
lazy uv tool install without mirror environment
```

with one of:

```text
locked package resolution through a domestic mirror
hash-verified direct download from a registered domestic origin
a bundled build artifact with provenance and SHA-256
a clear manual-action failure when no trusted domestic/bundled path exists
```

Do not silently disable Computer Use or Browser Use. The UI/CLI must report why automatic installation is unavailable and how an administrator can supply the pinned artifact.

- [ ] **Step 4: Add a controlled-proxy integration test**

The test launches local primary/secondary/official fixtures, rewrites only the manifest fixture endpoints, and records request order. Assert:

```python
assert requests == ["domestic-primary"]
assert failed_requests == ["domestic-primary", "domestic-secondary", "official"]
assert no_request_contains_auth_headers_or_session_values()
assert retry_resumes_without_marking_partial_download_ready()
```

This is a local deterministic test; it does not contact real mirrors.

- [ ] **Step 5: Run the complete download gate and commit**

Run:

```bash
scripts/run_tests.sh tests/test_hermes_managed_downloads.py \
  tests/tools/test_lazy_deps.py tests/tools/test_sensevoice_stt.py \
  tests/tools/test_browser_use_cli.py tests/hermes_cli/test_tools_config.py \
  tests/test_install_sh_browser_install.py tests/test_install_ps1_browser_install.py -q
npm run test --workspace apps/desktop -- \
  electron/runtime-download-policy.test.ts electron/bootstrap-process.test.ts
python scripts/check_hermes_managed_downloads.py
bash -n scripts/install.sh
git diff --check
```

Expected: PASS and no Hermes-managed install/lazy path uses unregistered or official-first download behavior.

Commit:

```bash
git add docs/security/hermes-managed-download-origins.json \
  apps/desktop/electron/runtime-download-policy.ts \
  apps/desktop/electron/runtime-download-policy.test.ts \
  apps/desktop/electron/bootstrap-process.ts \
  apps/desktop/electron/bootstrap-process.test.ts \
  apps/desktop/scripts/prepare-auth-toolchain-inputs.mjs \
  apps/desktop/scripts/prepare-auth-toolchain-inputs.test.mjs \
  scripts/install.sh scripts/install.ps1 tools/lazy_deps.py \
  tools/sensevoice_stt.py tools/browser_use_cli.py \
  tools/computer_use/cua_backend.py hermes_cli/tools_config.py \
  tests/test_hermes_managed_downloads.py tests/tools \
  tests/hermes_cli/test_tools_config.py \
  tests/test_install_sh_browser_install.py tests/test_install_ps1_browser_install.py
git commit -m "fix(installer): enforce recursive domestic mirror policy"
```

## Task 7: Reconcile the accepted Windows clean-install package

**Files:**

- Create/modify: `apps/desktop/electron/windows-auth-owner*`, `auth-runtime-contract*`
- Create/modify: `apps/desktop/scripts/prepare-windows-git-runtime.mjs`, `package-audit.mjs`
- Create/modify: `scripts/build-desktop-windows.mjs`, `build-desktop-windows.test.mjs`, `desktop-windows-contract.mjs`, `desktop-windows-contract.test.mjs`
- Create/modify: `scripts/test-desktop-windows-install.ps1`, `test-desktop-windows-auth-host.ps1`
- Create/modify: `scripts/tests/test-install-ps1-managed-uv.ps1`, `test-install-ps1-packaged-lock.ps1`
- Create/modify: `apps/desktop/e2e/installed-windows-smoke.spec.ts`, `installed-windows-auth.spec.ts`
- Modify: `scripts/install.ps1`, `apps/desktop/package.json`, root `package.json`, `apps/desktop/electron/main.ts`

- [ ] **Step 1: Extract Windows-specific accepted sources from `c2d3d09aab`**

Import the exact unique paths listed above from the Windows behavior reference, then reconcile shared-file hunks from these accepted commits:

```bash
git restore --source=c2d3d09aab -- \
  apps/desktop/electron/windows-auth-owner.ts \
  apps/desktop/electron/windows-auth-owner.test.ts \
  apps/desktop/electron/auth-runtime-contract.ts \
  apps/desktop/electron/auth-runtime-contract.test.ts \
  apps/desktop/scripts/prepare-windows-git-runtime.mjs \
  apps/desktop/scripts/package-audit.mjs \
  apps/desktop/e2e/installed-windows-smoke.spec.ts \
  apps/desktop/e2e/installed-windows-auth.spec.ts \
  scripts/build-desktop-windows.mjs \
  scripts/build-desktop-windows.test.mjs \
  scripts/desktop-windows-contract.mjs \
  scripts/desktop-windows-contract.test.mjs \
  scripts/test-desktop-windows-install.ps1 \
  scripts/test-desktop-windows-auth-host.ps1 \
  scripts/tests/test-install-ps1-managed-uv.ps1 \
  scripts/tests/test-install-ps1-packaged-lock.ps1
git add apps/desktop/electron apps/desktop/scripts apps/desktop/e2e scripts
```

```text
11985b01c7  validate Windows NSIS artifacts
6d1e7001b9  reproducible Windows NSIS entry
ba0daf8137  Windows NSIS installation smoke
5d2283c07e  launch npm through Node on Windows
0d19fc1565  Windows executable version identity
375d929497  Windows identity contract isolation
85a51a53b1  bundled Windows Voice runtime
aa87cb7d26  defer package audit until build
dde41d82b7  materialize Git runtime hard links
539b6935ad  require bundled Git runtime in smoke
fea4d6a508  bounded pre-auth progress
0add19da41  bounded Windows process trees
92e8081d2b  fail closed on bundled lock
49e5e00fff  staged managed uv
dba6380561  isolated packaged npm config
3258a6ba23  verified bootstrap completion
f15a137792  cold bootstrap manifest handling
ba6f6c31bd  locked authentication runtime
c2c4790e01  auth/runtime bootstrap scopes
2143a033c0  scope propagation
76e4bbd338  safe progress
822156b2dc  runtime auth-session gate
3e9a5a54c3  post-login runtime preparation
ca52c1ce6c  authenticated runtime progress
ed3235595c  native auth pipe runtime
d991c6a919  venv process cleanup
b2b6a610b1  package security closure
45eb464242  owner recovery
9a32e19153  deadlines and named-pipe review closure
772903c824  packaged auth runtime hardening
```

The migration ledger must name every extracted path. Do not import an earlier Windows copy over a later DMG-equivalent common product file.

Every SHA in Appendix A5 must be marked as `path-extract`, `reference-equivalent`, `ci-infrastructure`, or `test-evidence`; the ledger completeness test rejects an unclassified Windows commit.

- [ ] **Step 2: Preserve one common product contract**

Resolve shared files so that:

```text
Auth Gate, logout, epoch suppression, progress UI, Voice UI: final DMG behavior
secure store: Keychain on macOS, Credential Manager on Windows
owner transport: Unix socket on macOS, named pipe with SID/ACL validation on Windows
package bootstrap: install.sh on macOS, install.ps1 on Windows
runtime and progress schema: identical
fixed account server and protected entrypoint inventory: identical
```

No Windows adapter may maintain a private copy of `client_auth/runtime.py` or bypass the central Auth Guard.

- [ ] **Step 3: Make the direct NSIS command self-contained**

Root `package.json` retains:

```json
{
  "build:desktop:windows": "node scripts/build-desktop-windows.mjs",
  "test:desktop:windows-contract": "node --test scripts/desktop-windows-contract.test.mjs scripts/build-desktop-windows.test.mjs"
}
```

`apps/desktop` `dist:win:nsis` must prepare payloads itself as defined in Task 5. `build:desktop:windows` may wrap it with logging/audit but cannot be the only route that produces a valid clean-install package.

- [ ] **Step 4: Run Windows contracts on macOS with fakes**

Run:

```bash
npm run test:desktop:windows-contract
npm run test --workspace apps/desktop -- \
  electron/windows-auth-owner.test.ts \
  electron/auth-runtime-contract.test.ts \
  electron/bootstrap-process.test.ts \
  electron/desktop-runtime-gate.test.ts
scripts/run_tests.sh tests/hermes_cli/client_auth/test_runtime.py \
  tests/hermes_cli/client_auth/test_bridge.py \
  tests/hermes_cli/client_auth/test_native_artifacts.py -q
```

Expected: PASS. This is not Windows artifact acceptance; that occurs in Task 11.

- [ ] **Step 5: Commit Windows reconciliation**

Run:

```bash
python scripts/check_main_auth_voice_migration.py --base ansatz/main
python scripts/check_hermes_managed_downloads.py
git diff --check
git add apps/desktop/electron apps/desktop/scripts apps/desktop/e2e \
  apps/desktop/package.json package.json scripts/install.ps1 \
  scripts/build-desktop-windows.mjs scripts/build-desktop-windows.test.mjs \
  scripts/desktop-windows-contract.mjs scripts/desktop-windows-contract.test.mjs \
  scripts/test-desktop-windows-install.ps1 \
  scripts/test-desktop-windows-auth-host.ps1 scripts/tests
git commit -m "feat(desktop): add Windows clean-install auth voice package"
```

## Task 8: Reconcile locks and prove product/path parity

**Files:**

- Modify: `pyproject.toml`, `uv.lock`, root `uv.toml` when required by reproducible packaged locks, `package-lock.json`
- Create: `scripts/check_main_auth_voice_parity.py`, `tests/test_main_auth_voice_parity.py`, `tests/test_main_auth_voice_dependencies.py`
- Modify: `docs/security/main-auth-voice-product-paths.txt`, migration ledger

- [ ] **Step 1: Write failing dependency/parity tests**

Assert:

```python
assert common_dependencies_include("keyring==25.7.0")
assert sensevoice_extra_is_present()
assert minimal_auth_lock_is_platform_complete(["darwin-arm64", "windows-x64"])
assert every_packaged_requirement_has_hashes()
assert npm_lock_matches_package_json()
assert accepted_dmg_product_paths_match_or_have_path_specific_waivers()
assert protected_entrypoints_are_a_superset_of_final_dmg()
```

Run:

```bash
scripts/run_tests.sh tests/test_main_auth_voice_dependencies.py \
  tests/test_main_auth_voice_parity.py -q
```

Expected: FAIL until locks and parity manifests are reconciled.

- [ ] **Step 2: Regenerate locks, never hand-edit them**

Run:

```bash
env UV_DEFAULT_INDEX=https://mirrors.ustc.edu.cn/pypi/simple uv lock
env UV_DEFAULT_INDEX=https://mirrors.ustc.edu.cn/pypi/simple \
  uv lock --project desktop_auth_runtime
env NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
  NPM_CONFIG_REPLACE_REGISTRY_HOST=always npm install --package-lock-only
uv lock --check
env NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
  NPM_CONFIG_REPLACE_REGISTRY_HOST=always npm ci --ignore-scripts
```

Expected: lock checks PASS. Re-enable only repository-approved npm lifecycle scripts for the full build; `--ignore-scripts` here validates lock resolution without executing downloads.

- [ ] **Step 3: Implement mechanical DMG parity**

The checker compares the candidate with `80db6d8265` over the ledger-derived common product and macOS package paths. A waiver object has this exact shape:

```json
{
  "path": "apps/desktop/electron/package-runtime/platform.ts",
  "reason": "adds Windows adapter without changing macOS behavior",
  "tests": ["apps/desktop/electron/package-runtime/platform.test.ts"]
}
```

No directory-level waiver is allowed. Voice paths from `3ad4a12660` require blob equality unless the waiver identifies an authentication integration test.

- [ ] **Step 4: Run the full static gate and commit**

Run:

```bash
scripts/run_tests.sh tests/test_main_auth_voice_dependencies.py \
  tests/test_main_auth_voice_parity.py \
  tests/test_main_auth_voice_migration.py \
  tests/test_hermes_managed_downloads.py -q
python scripts/check_main_auth_voice_parity.py --reference 80db6d8265
python scripts/check_main_auth_voice_migration.py --base ansatz/main
python scripts/check_hermes_managed_downloads.py
uv lock --check
git diff --check
```

Expected: PASS with only reviewed path-specific waivers.

Commit:

```bash
git add pyproject.toml uv.lock uv.toml desktop_auth_runtime \
  package.json package-lock.json scripts/check_main_auth_voice_parity.py \
  tests/test_main_auth_voice_dependencies.py tests/test_main_auth_voice_parity.py \
  docs/security/main-auth-voice-product-paths.txt \
  docs/security/main-auth-voice-migration-ledger.json
git commit -m "build: lock cross-platform auth voice package parity"
```

## Task 9: Repair packaging workflows without creating CI-only product code

**Files:**

- Modify: `.github/workflows/desktop-windows-package.yml`
- Create: `.github/workflows/desktop-macos-package.yml`
- Create: `tests/test_desktop_packaging_workflows.py`
- Modify: `scripts/desktop-windows-contract.test.mjs`

- [ ] **Step 1: Write failing workflow contract tests**

Assert both workflows:

```text
use Node 26.7.0 and npm 11.19.0
invoke the same dist:mac:dmg or dist:win:nsis command available locally
never patch product source on the runner
never enable a CI-only auth bypass
never echo secret values
upload only built artifacts and sanitized reports
inspect artifacts to prove .github/** is absent
use HERMES_E2E_USERNAME and HERMES_E2E_PASSWORD only in the credential login step
```

Run:

```bash
scripts/run_tests.sh tests/test_desktop_packaging_workflows.py -q
npm run test:desktop:windows-contract
```

Expected: FAIL because the current Windows workflow references missing commands/files and the macOS workflow does not exist.

- [ ] **Step 2: Repair Windows and add macOS workflow**

The jobs call:

```text
env NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
  NPM_CONFIG_REPLACE_REGISTRY_HOST=always npm ci
npm run dist:win:nsis --workspace apps/desktop
npm run dist:mac:dmg --workspace apps/desktop
```

They run package audits and exact installed-artifact tests after construction. Workflow-only helpers remain under `.github` or test directories and must never be required at App runtime.

- [ ] **Step 3: Prove workflow files are excluded from packages**

Add artifact audits that enumerate `app.asar` and `Resources` and reject:

```text
.github/
desktop-*-package.yml
credential login drivers
test account identifiers
raw logs
```

- [ ] **Step 4: Run syntax/contracts and commit**

Run:

```bash
scripts/run_tests.sh tests/test_desktop_packaging_workflows.py -q
npm run test:desktop:windows-contract
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/desktop-windows-package.yml")'
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/desktop-macos-package.yml")'
git diff --check
```

Expected: PASS.

Commit:

```bash
git add .github/workflows/desktop-windows-package.yml \
  .github/workflows/desktop-macos-package.yml \
  tests/test_desktop_packaging_workflows.py \
  scripts/desktop-windows-contract.test.mjs
git commit -m "ci: verify clean macOS and Windows packages"
```

## Task 10: Run complete pre-build regression

**Files:** None unless a genuine regression is found; each fix must use a new failing test and a separate commit.

- [ ] **Step 1: Run Desktop gates**

```bash
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop
npm exec --workspace apps/desktop -- playwright test e2e/auth-hard-gate.spec.ts --reporter=list
```

Expected: PASS.

- [ ] **Step 2: Run Python auth/Voice and entrypoint gates**

```bash
scripts/run_tests.sh tests/hermes_cli/client_auth \
  tests/hermes_cli/test_auth_commands.py \
  tests/tools/test_sensevoice_stt.py tests/tools/test_transcription_tools.py \
  tests/agent/test_transcription_registry.py \
  tests/tui_gateway tests/acp -q
python scripts/check_auth_entrypoints.py --check
python scripts/generate_auth_free_help.py --check
```

Expected: PASS.

- [ ] **Step 3: Run build, syntax, lock, and security gates**

```bash
node --test apps/desktop/scripts/*.test.mjs
uv run ruff check .
uv lock --check
bash -n scripts/install.sh
python scripts/check_main_auth_voice_parity.py --reference 80db6d8265
python scripts/check_main_auth_voice_migration.py --base ansatz/main
python scripts/check_hermes_managed_downloads.py
git diff --check ansatz/main...HEAD
```

Expected: PASS.

## Task 11: Build and accept the real macOS DMG

**Files:**

- Create: `docs/security/acceptance/main-auth-voice-macos-2026-08-22.md`
- Do not commit: DMG, App, runtime cache, logs, credentials, session data

- [ ] **Step 1: Build from the direct main command**

Run with Node 26.7.0/npm 11.19.0:

```bash
npm run dist:mac:dmg --workspace apps/desktop
```

Expected: a real `Hermes-0.17.0-mac-*.dmg`; preparation, Electron build, payload audit, signing status, and DMG creation all complete.

- [ ] **Step 2: Inspect the exact artifact**

Record version, architecture, SHA-256, build commit, payload manifest hashes, signing identity/status, and artifact path. Mount the DMG and verify:

```text
Hermes.app exists
minimum auth runtime and its manifest exist
backend payload and install.sh exist
native files match the App architecture
.github, tests, raw source logs, credentials, and sessions are absent
```

- [ ] **Step 3: Perform a recoverable clean-state installation**

If `/Applications/Hermes.app` exists, move it to a timestamped backup; do not delete it. Back up Hermes application state and Keychain test-session references using the approved clean-state procedure, then install from the mounted DMG and launch only `/Applications/Hermes.app`.

Expected before login:

```text
safe authentication preparation progress is visible
login form appears automatically
protected main UI is absent
backend/Agent/gateway/protected HTTP/WS are not running
protected IPC returns AUTH_REQUIRED
no registration/recovery UI exists
```

- [ ] **Step 4: Run credentialed lifecycle without exposing credentials**

Pause for the user to type credentials locally when interactive input is required. Verify:

```text
wrong password produces a generic safe error
correct login starts runtime preparation only after authentication
domestic mirror stages are visible and bounded
main UI mounts after runtime_ready
conversation read, Agent call, tool call, and SenseVoice transcription work
quit/reopen restores Keychain session and validates online
logout returns immediately to login, stops new work, and clears the session
rapid login to another account cannot show prior runtime state
```

Do not record username, password, Cookie, Session, CSRF, or Keychain contents.

- [ ] **Step 5: Record sanitized macOS acceptance**

Record every item as PASS/FAIL, commands, timings, artifact SHA-256, App version, macOS version/architecture, and only redacted errors.

## Task 12: Build and accept the real Windows NSIS installer

**Files:**

- Create: `docs/security/acceptance/main-auth-voice-windows-2026-08-22.md`
- Workflow/evidence only; no product change unless a failing test is added first

- [ ] **Step 1: Push the candidate branch only after local gates pass**

With user approval:

```bash
git push --set-upstream ansatz integration/main-auth-voice-base
```

Never push `HEAD:main`.

- [ ] **Step 2: Run the Windows packaging workflow**

The Windows x64 job uses Node 26.7.0/npm 11.19.0 and invokes:

```text
npm run dist:win:nsis --workspace apps/desktop
```

Expected: a real x64 NSIS installer plus package audit, SHA-256, and sanitized logs.

- [ ] **Step 3: Run clean Windows installed-artifact acceptance**

The test installs the exact uploaded NSIS artifact into a clean Windows VM and verifies the same lifecycle as Task 11, using Windows Credential Manager and named pipes. It also verifies:

```text
PowerShell 5.1 syntax
managed uv and locked dependency installation
bundled PortableGit integrity
process-tree teardown
no password prompt in background entries
uninstall metadata and shortcuts
no workflow/test/credential files in installed resources
```

Credential secrets are read only by the login step and masked by the runner. Artifact/log upload occurs after secret-bearing processes exit and includes only the sanitized allowlist.

- [ ] **Step 4: Record sanitized Windows acceptance**

Record PASS/FAIL, installer SHA-256, version, architecture, Windows image version, workflow run/commit identity, and redacted failures. Do not store credentials or session material.

## Task 13: Final evidence and merge-decision handoff

**Files:**

- Create: `docs/security/main-auth-voice-cross-platform-acceptance-2026-08-22.md`

- [ ] **Step 1: Run final verification against the exact candidate**

```bash
git fetch ansatz main
test "$(git rev-parse ansatz/main)" = "9bd88c530716279a089ed18428dc785732b6e1be"
git merge-base --is-ancestor ansatz/main HEAD
python scripts/check_main_auth_voice_parity.py --reference 80db6d8265
python scripts/check_main_auth_voice_migration.py --base ansatz/main
python scripts/check_hermes_managed_downloads.py
git diff --check ansatz/main...HEAD
git status --short --branch
```

Expected: PASS and no implementation residue or generated artifacts.

- [ ] **Step 2: Write the final evidence matrix**

Include:

```text
base/candidate commit and candidate tree hash
DMG/NSIS artifact names and SHA-256
macOS/Windows environment
DMG product parity and waiver list
all protected entrypoints
pre-auth isolation
login/wrong-password/restore/revocation/logout/account-switch
recursive mirror coverage and controlled-proxy result
Voice/SenseVoice
package-content audit
signing/notarization status
every command with PASS/FAIL
remaining risks and blockers
sensitive evidence recorded: none
```

- [ ] **Step 3: Commit evidence and stop**

```bash
git add docs/security/acceptance/main-auth-voice-macos-2026-08-22.md \
  docs/security/acceptance/main-auth-voice-windows-2026-08-22.md \
  docs/security/main-auth-voice-cross-platform-acceptance-2026-08-22.md
git commit -m "docs: record cross-platform auth voice acceptance"
git rev-parse HEAD
git rev-parse HEAD^{tree}
```

Report one conclusion:

```text
PASS — candidate may enter the main merge decision
```

or:

```text
FAIL — list each blocker, reproduction, affected files, and next repair
```

Do not merge `main`, delete worktrees, or delete source/reference branches.

## Appendix A: Authoritative replay groups

### A1. Common authentication foundation

```text
feature/remote-auth-hard-gate@763465daf019c8755813659b98a72c6c6f4662e3
```

### A2. Accepted final-DMG common behavior replay

```text
08a2eedf67 706c5a4d0d 6aed1cc0f1 e042ce3861 bffdd26edf f28a0f4b58
8af9e3eefe cdc12484f1 27567163b3 366fb3f5a8 cdb5c65bc8
c478db4d2f e51e448669 817a5d0a6a b22bdb8d31 af19ce56b1 50609240e2
bddbec2abb c4d5ae2d40 f8d7c05fad 34026f7627 a416087126 34f1c119e2
c8fad50c49 f5a7372e4c c893e264e9 38230a6c9f 4e4a5d42c7
```

### A3. Accepted final-DMG package foundation

```text
f5351c3c60 8aa6263a35 3305431d68 e28883110d 977322ecd7
abbc79d0cd 904d435685 936ede7953 89503cfb2b 11d2143924
df34e9da62 f68510d905 7666fc4da5 42bbebadf9 527eb97a19
6e57ead969 363d464a85 a756c93e35
aa97962e0d 6355c61c79 d0b334abba 86d0598a13 decb2f0fcb b0ad6ad4e5
e1de17db83 ccc7e10274 82b23ebde6 9689aefd08 bf090797fe
```

`86d0598a13` is the accepted starting mirror behavior. Task 6 strengthens it to recursive coverage without changing authentication semantics.

### A4. Accepted Voice feature

```text
3ad4a126606079c77e7adca6d8661cd0c8c0a93b
```

### A5. Accepted Windows packaging and parity references

```text
11985b01c7 6d1e7001b9 ba0daf8137 5d2283c07e 0d19fc1565 375d929497
85a51a53b1 aa87cb7d26 dde41d82b7 539b6935ad
fea4d6a508 0add19da41 92e8081d2b c0dbe0df1c 6972412531 b6a56cd666
49e5e00fff 127fc83825 4ace75a22e 4b830a1039 a600cdc466 dba6380561
3258a6ba23 f15a137792 7114e13656 4351fb5a82 677de12c95 f59191d717
0ac1923a41 b243965d72 38c13118c4 ba6f6c31bd c2c4790e01 2143a033c0
76e4bbd338 822156b2dc 3e9a5a54c3 ca52c1ce6c 2b111d92ed 3bf9a63772
8294a9b41f 80bd13c9b5 c5b19efe57 cc4c297cd2 ed3235595c d282099faf
07755bc139 d991c6a919 b2b6a610b1 45eb464242 9a32e19153 772903c824
8f54f28f17 1a877e86f9 c2d3d09aab
```

Equivalent macOS behavior is not replayed a second time from Windows. Unique platform adapters, installers, tests, and later correctness fixes are extracted according to the ledger.

## Appendix B: Conflict rules

| Surface | Required result | Rejected resolution |
| --- | --- | --- |
| `apps/desktop/package.json` | Existing main macOS/Windows targets plus payload preparation for both direct release commands. | Taking the DMG or Windows file wholesale. |
| `apps/desktop/electron/main.ts` | Final DMG Auth Guard/epoch/progress behavior plus Windows adapter injection. | Platform-specific duplicate authentication flow. |
| `apps/desktop/src/hermes.ts` | Voice APIs and protected authenticated APIs together. | Dropping either feature set. |
| localization | Complete main catalog plus login/logout/progress/Voice strings in every supported language. | Replacing current catalogs with an older branch. |
| `hermes_cli/main.py` | Central gate for every entrypoint plus current main CLI features. | Early bootstrap before auth or DMG-only CLI fork. |
| `hermes_cli/client_auth/runtime.py` | Shared policy, Unix socket on macOS, SID-validated named pipe on Windows. | Windows private copy or insecure TCP fallback. |
| `scripts/install.sh` / `.ps1` | Same stage protocol and recursive mirror contract; only command/path syntax differs. | Official-first nested installer or raw remote script execution. |
| `pyproject.toml` / `uv.lock` | Current main dependencies plus auth/Voice, regenerated locks, hashed packaged export. | Copying a stale lock or hand editing. |
| SenseVoice | ModelScope first, pinned size/SHA-256, resumable download, Voice failure isolated from auth. | Provider requirement or unverified model source. |
| workflows | Same local build commands and artifact behavior; excluded from packages. | CI-only source patch, auth bypass, or runtime dependency. |

## Appendix C: Review checkpoints

Stop and obtain review after:

1. Task 1 ownership/download contracts.
2. Task 4 final-DMG authentication behavior replay.
3. Task 7 Windows package reconciliation.
4. Task 9 workflow repair.
5. Task 10 full pre-build regression.
6. Task 11 macOS clean-install acceptance.
7. Task 12 Windows clean-install acceptance.

At each checkpoint report current commit, changed paths, exact test commands/results, parity waivers, and unresolved risks. Never collapse multiple failed checkpoints into one undocumented repair.
