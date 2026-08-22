# Main Authentication and Voice Platform Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Do not use subagents for this repository migration.

**Goal:** Produce a tested `integration/main-auth-voice-base` branch, based on the latest `ansatz/main`, that contains complete cross-platform authentication and Voice/SenseVoice behavior but no new macOS DMG or Windows packaging overlay.

**Architecture:** Reconstruct the common product layer from the original authentication branch and the single Voice/SenseVoice feature commit, then replay only the later authentication reliability fixes that are independent of packaged runtime preparation. Keep platform delivery files out through an executable repository-boundary check. Existing accepted macOS and Windows branches remain read-only behavioral references.

**Tech Stack:** Python 3.11-3.13, pytest, Ruff, Electron 40, TypeScript 6, React 19, Vitest, Playwright, uv, npm workspaces, git worktrees.

---

## Scope and stopping point

This plan ends with a pushed, reviewed candidate branch. It does not update remote `main` and does not rebuild either shipping installer branch. Those actions require a separate acceptance decision after this plan passes.

The worktree is:

```text
/Users/zhouzhangchen/Desktop/自己的/acadamic/agent/hermes-agent/.worktrees/integration-main-auth-voice-base
```

The authoritative references are:

```text
ansatz/main                                      9bd88c530716279a089ed18428dc785732b6e1be
feature/remote-auth-hard-gate                    763465daf019c8755813659b98a72c6c6f4662e3
feature/desktop-dmg-voice-confirmation           3ad4a126606079c77e7adca6d8661cd0c8c0a93b
release/desktop-dmg-auth-e2e                     80db6d8265f805cec46817d913982e4c5f6405c4
integration/desktop-windows-auth-e2e             c2d3d09aab921130171ff611e260c13e9c6d477c
```

### Task 0: Prepare and verify the isolated baseline

**Files:** None.

- [ ] **Step 1: Verify worktree isolation and base ancestry**

Run:

```bash
test "$(git branch --show-current)" = "integration/main-auth-voice-base"
test "$(git merge-base ansatz/main HEAD)" = "9bd88c530716279a089ed18428dc785732b6e1be"
git status --short --branch
```

Expected: the integration branch is active, its base is the recorded remote `main`, and only committed design/plan work is present.

- [ ] **Step 2: Install the locked development dependencies**

Run:

```bash
npm ci
uv sync --extra dev
```

Use the already configured npm and Python indexes/caches. Do not add mirror URLs, credentials, or machine-local paths to repository files.

- [ ] **Step 3: Run the existing baseline checks**

Run:

```bash
npm run typecheck --workspace apps/desktop
uv run pytest tests/hermes_cli/test_auth_commands.py tests/tools/test_transcription_tools.py -q
git diff --check
```

Expected: PASS. If the untouched baseline fails, record the exact failure and stop before importing authentication or Voice so the pre-existing failure is not attributed to the migration.

### Task 1: Enforce the common-branch boundary

**Files:**

- Create: `scripts/check_main_platform_boundary.py`
- Create: `tests/test_main_platform_boundary.py`
- Delete: `.github/workflows/desktop-windows-package.yml`

- [ ] **Step 1: Write the failing boundary test**

Create `tests/test_main_platform_boundary.py`:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_main_contains_no_release_overlay() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_main_platform_boundary.py"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Implement the path boundary checker**

Create `scripts/check_main_platform_boundary.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = frozenset(
    {
        ".github/workflows/desktop-windows-package.yml",
        "scripts/build-desktop-dmg.sh",
        "scripts/build-desktop-dmg.test.mjs",
        "scripts/desktop-dmg-contract.mjs",
        "scripts/desktop-dmg-contract.test.mjs",
        "scripts/build-desktop-windows.mjs",
        "scripts/build-desktop-windows.test.mjs",
        "scripts/desktop-windows-contract.mjs",
        "scripts/desktop-windows-contract.test.mjs",
        "scripts/desktop-credential-login.mjs",
        "scripts/desktop-credential-login.test.mjs",
        "scripts/test-desktop-windows-auth-host.ps1",
        "scripts/test-desktop-windows-install.ps1",
        "apps/desktop/scripts/build-auth-toolchain.mjs",
        "apps/desktop/scripts/build-auth-toolchain.test.mjs",
        "apps/desktop/scripts/prepare-auth-toolchain-inputs.mjs",
        "apps/desktop/scripts/prepare-auth-toolchain-inputs.test.mjs",
        "apps/desktop/scripts/prepare-windows-git-runtime.mjs",
    }
)
FORBIDDEN_PREFIXES = (
    "desktop_auth_runtime/",
    "scripts/tests/test-install-ps1-",
)


def tracked_paths() -> set[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
    )
    return {
        item.decode("utf-8")
        for item in output.split(b"\0")
        if item
    }


def violations(paths: set[str]) -> list[str]:
    return sorted(
        path
        for path in paths
        if path in FORBIDDEN_PATHS
        or any(path.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
    )


def main() -> int:
    found = violations(tracked_paths())
    if not found:
        return 0
    for path in found:
        print(f"platform release overlay is not allowed in main: {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run the test and prove the current remote-main workflow violates the boundary**

Run:

```bash
uv run pytest tests/test_main_platform_boundary.py -q
```

Expected: FAIL naming `.github/workflows/desktop-windows-package.yml`.

- [ ] **Step 4: Remove only the misplaced packaging workflow**

Delete `.github/workflows/desktop-windows-package.yml`. Do not delete `.github/workflows/tests.yml` or `.github/workflows/tests-os.yml`; those are common source-test workflows.

- [ ] **Step 5: Run the boundary test again**

Run:

```bash
uv run pytest tests/test_main_platform_boundary.py -q
python scripts/check_main_platform_boundary.py
git diff --check
```

Expected: PASS and no output from the checker or `git diff --check`.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_main_platform_boundary.py tests/test_main_platform_boundary.py .github/workflows/desktop-windows-package.yml
git commit -m "build: keep platform release overlays out of main"
```

### Task 2: Import the reviewed cross-platform authentication foundation

**Files:**

- Merge source: `feature/remote-auth-hard-gate@763465daf019c8755813659b98a72c6c6f4662e3`
- Verify: `hermes_cli/client_auth/**`
- Verify: `apps/desktop/electron/auth-bridge.ts`
- Verify: `apps/desktop/electron/auth-coordinator.ts`
- Verify: `apps/desktop/electron/auth-scope-token.ts`
- Verify: `apps/desktop/electron/guarded-ipc.ts`
- Verify: `apps/desktop/src/components/auth-gate.tsx`
- Verify: `apps/desktop/src/protected-root.tsx`
- Verify: `hermes_cli/client_auth/entrypoints.json`
- Verify: `scripts/check_auth_entrypoints.py`
- Verify: `scripts/generate_auth_free_help.py`

- [ ] **Step 1: Verify the source branch identity and boundary before merging**

Run:

```bash
test "$(git rev-parse feature/remote-auth-hard-gate)" = "763465daf019c8755813659b98a72c6c6f4662e3"
git diff --name-only 4ef56ce..feature/remote-auth-hard-gate | rg 'dmg|desktop-windows-package|desktop_auth_runtime' && exit 1 || true
```

Expected: the identity check passes and no packaging path is printed.

- [ ] **Step 2: Merge the reviewed feature history**

Run:

```bash
git merge --no-ff feature/remote-auth-hard-gate -m "merge: add cross-platform remote auth gate to main base"
```

Expected: a merge commit preserving the authentication feature history. The unrelated original worktree remains untouched.

- [ ] **Step 3: Verify the central Auth Guard contracts**

Run:

```bash
uv run pytest tests/hermes_cli/client_auth tests/tui_gateway/test_account_auth.py tests/acp/test_entry.py -q
uv run python scripts/check_auth_entrypoints.py --check
uv run python scripts/generate_auth_free_help.py --check
python scripts/check_main_platform_boundary.py
```

Expected: all tests pass; the entrypoint manifest and static help are current; the platform boundary remains clean.

- [ ] **Step 4: Verify Desktop authentication units**

Run:

```bash
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop -- electron/auth-bridge.test.ts electron/auth-coordinator.test.ts electron/auth-scope-token.test.ts electron/guarded-ipc.test.ts src/components/auth-gate.test.tsx
```

Expected: all selected Desktop checks pass.

### Task 3: Replay platform-independent login reliability and GUI logout fixes

**Files:**

- Modify: `apps/desktop/electron/auth-bridge.ts`
- Modify: `apps/desktop/electron/auth-bridge.test.ts`
- Modify: `apps/desktop/electron/auth-coordinator.ts`
- Modify: `apps/desktop/electron/auth-coordinator.test.ts`
- Modify: `apps/desktop/electron/main.ts`
- Modify: `apps/desktop/electron/hardening.test.ts`
- Modify: `apps/desktop/src/components/auth-gate.tsx`
- Modify: `apps/desktop/src/components/auth-gate.test.tsx`
- Create: `apps/desktop/src/app/shell/hooks/use-account-statusbar-item.tsx`
- Modify: `apps/desktop/src/app/contrib/surfaces.tsx`
- Modify: `apps/desktop/src/app/contrib/surfaces.test.tsx`
- Modify: `apps/desktop/src/i18n/en.ts`
- Modify: `apps/desktop/src/i18n/types.ts`
- Modify: `apps/desktop/src/i18n/zh.ts`
- Modify: `hermes_cli/client_auth/bridge.py`
- Modify: `hermes_cli/client_auth/runtime.py`
- Modify: `tests/hermes_cli/client_auth/test_bridge.py`
- Modify: `tests/hermes_cli/client_auth/test_runtime.py`

- [ ] **Step 1: Replay the bounded-request and dead-bridge fixes in their accepted order**

Run:

```bash
git cherry-pick 366fb3f5a8 817a5d0a6a b22bdb8d31 af19ce56b1
```

Conflict rule: keep the feature branch's source-mode backend resolution and apply only timeout ordering, coordinator bridge recreation, Retry bridge replacement, and their tests. Reject hunks that introduce bundled payload lookup, `desktop_auth_runtime`, `process.resourcesPath` auth-toolchain lookup, or installer execution.

- [ ] **Step 2: Replay the accepted GUI logout surface**

Run:

```bash
git cherry-pick cdc12484f1 27567163b3
```

Expected behavior: an authenticated user can invoke Logout from the normal Desktop account surface; the action remains absent before authentication.

- [ ] **Step 3: Replay native-owner recovery**

Run:

```bash
git cherry-pick 4e4a5d42c7
```

Conflict rule: keep the final owner-idle recovery, lease/epoch invalidation, bounded login operation, and normalized renderer status. Do not bring the accompanying DMG acceptance documents into the common branch.

- [ ] **Step 4: Run reliability regression tests**

Run:

```bash
uv run pytest tests/hermes_cli/client_auth/test_bridge.py tests/hermes_cli/client_auth/test_runtime.py -q
npm run test --workspace apps/desktop -- electron/auth-bridge.test.ts electron/auth-coordinator.test.ts electron/hardening.test.ts src/components/auth-gate.test.tsx src/app/contrib/surfaces.test.tsx
python scripts/check_main_platform_boundary.py
```

Expected: dead bridges recover only after a safe Retry, owner expiry does not strand login, logout is authenticated-only, and no package overlay appears.

### Task 4: Import Voice/SenseVoice without DMG history

**Files:**

- Cherry-pick source: `3ad4a126606079c77e7adca6d8661cd0c8c0a93b`
- Modify: `agent/transcription_registry.py`
- Modify: `apps/desktop/src/app/chat/composer/controls.tsx`
- Modify: `apps/desktop/src/app/chat/composer/hooks/use-composer-voice.ts`
- Modify: `apps/desktop/src/app/chat/composer/hooks/use-voice-conversation.ts`
- Modify: `apps/desktop/src/app/chat/composer/hooks/use-voice-recorder.ts`
- Modify: `apps/desktop/src/app/chat/composer/index.tsx`
- Modify: `apps/desktop/src/app/chat/composer/types.ts`
- Modify: `apps/desktop/src/app/chat/index.tsx`
- Modify: `apps/desktop/src/app/contrib/surfaces.tsx`
- Modify: `apps/desktop/src/app/contrib/wiring.tsx`
- Modify: `apps/desktop/src/app/session/hooks/use-hermes-config.ts`
- Modify: `apps/desktop/src/app/settings/constants.ts`
- Modify: `apps/desktop/src/app/settings/helpers.ts`
- Modify: `apps/desktop/src/hermes.ts`
- Modify: `apps/desktop/src/i18n/en.ts`
- Modify: `apps/desktop/src/i18n/types.ts`
- Modify: `apps/desktop/src/i18n/zh.ts`
- Modify: `apps/desktop/src/lib/voice-barge-in.ts`
- Create: `apps/desktop/src/lib/voice-timing.ts`
- Modify: `apps/desktop/src/types/hermes.ts`
- Modify: `hermes_cli/config_defaults.py`
- Modify: `hermes_cli/tools_config.py`
- Modify: `hermes_cli/web_server.py`
- Modify: `pyproject.toml`
- Modify: `scripts/install.sh`
- Modify: `tools/lazy_deps.py`
- Create: `tools/sensevoice_stt.py`
- Modify: `tools/transcription_tools.py`
- Modify: `uv.lock`

- [ ] **Step 1: Prove the selected commit contains Voice but not the preceding DMG commits**

Run:

```bash
test "$(git rev-parse 3ad4a12660)" = "3ad4a126606079c77e7adca6d8661cd0c8c0a93b"
test "$(git diff-tree --no-commit-id --name-only -r 3ad4a12660 | wc -l | tr -d ' ')" = "30"
git diff-tree --no-commit-id --name-only -r 3ad4a12660 | rg 'build-desktop-dmg|desktop-dmg-contract' && exit 1 || true
```

Expected: 30 changed files and no DMG build path.

- [ ] **Step 2: Cherry-pick the Voice commit**

Run:

```bash
git cherry-pick 3ad4a126606079c77e7adca6d8661cd0c8c0a93b
```

Resolve overlapping authentication files by preserving both concerns:

- `apps/desktop/src/hermes.ts`: retain the typed authentication IPC and add Voice methods/types.
- `apps/desktop/src/i18n/en.ts`, `types.ts`, `zh.ts`: retain every authentication key and add every Voice key.
- `hermes_cli/tools_config.py` and `hermes_cli/web_server.py`: enforce authentication before exposing Voice operations.
- `pyproject.toml` and `uv.lock`: retain `keyring` and authentication scripts while adding the `sensevoice` optional dependencies.
- `scripts/install.sh`: retain source-install Voice support but reject any DMG bootstrap scope or bundled payload hunk.

- [ ] **Step 3: Verify the exact Voice dependency boundary**

Run:

```bash
uv lock --check
uv run python -c "from tools.sensevoice_stt import SenseVoiceSTT; print(SenseVoiceSTT.__name__)"
python scripts/check_main_platform_boundary.py
```

Expected: the lock is current, SenseVoice imports without downloading a model, and no packaging path is present.

### Task 5: Add missing Voice regression coverage

**Files:**

- Create: `tests/agent/test_transcription_registry.py`
- Create: `tests/tools/test_sensevoice_stt.py`
- Modify: `tests/tools/test_transcription_tools.py`
- Create: `apps/desktop/src/lib/voice-timing.test.ts`
- Create: `apps/desktop/src/app/chat/composer/hooks/use-voice-recorder.test.tsx`
- Create: `apps/desktop/src/app/chat/composer/sensevoice-readiness.test.tsx`

- [ ] **Step 1: Add Python tests that make no network or model download**

Implement these named tests using pytest's `monkeypatch` and temporary paths:

- `test_sensevoice_registry_is_lazy`: clear `sherpa_onnx` from `sys.modules`, replace Python import with a recorder for that module, build the transcription registry, and assert registry discovery neither imports `sherpa_onnx` nor invokes the model resolver.
- `test_sensevoice_unavailable_is_voice_specific`: replace the SenseVoice dependency loader with one that raises its normal unavailable exception and assert the public transcription result reports the Voice-specific unavailable code without `AUTH_REQUIRED`.
- `test_transcription_endpoint_requires_auth`: replace the Auth Guard with a deterministic signed-out scope, invoke the transcription HTTP handler, and assert its normalized response is `AUTH_REQUIRED` before the SenseVoice loader is called.

Use fakes only at the dependency boundary; never contact GitHub, Hugging Face, ModelScope, or the account server.

- [ ] **Step 2: Run the focused Python tests**

Run:

```bash
uv run pytest tests/agent/test_transcription_registry.py tests/tools/test_sensevoice_stt.py tests/tools/test_transcription_tools.py -q
```

Expected: PASS with zero downloads.

- [ ] **Step 3: Add Desktop Voice tests**

Implement these named Vitest cases:

- `does not start recording before the protected root is mounted`: render the signed-out Auth Gate, inject a recorder factory spy, and assert the protected Voice composer is absent and the factory is never invoked.
- `reports SenseVoice readiness without creating a provider credential requirement`: inject an authenticated Hermes bridge whose readiness response reports SenseVoice available, render the Voice control, and assert the control becomes ready without invoking any provider-key prompt.
- `uses monotonic elapsed timing and resets after recorder teardown`: drive the existing injected clock through start, elapsed update, stop, and restart; assert the second session starts from zero and ignores the first session's completion callback.

The tests must stub media input and Hermes IPC. They must not access the developer microphone, Keychain, or network.

- [ ] **Step 4: Run the focused Desktop Voice tests**

Run:

```bash
npm run test --workspace apps/desktop -- src/lib/voice-timing.test.ts src/app/chat/composer/hooks/use-voice-recorder.test.tsx src/app/chat/composer/sensevoice-readiness.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Commit the new regression suite**

```bash
git add tests/agent/test_transcription_registry.py tests/tools/test_sensevoice_stt.py tests/tools/test_transcription_tools.py apps/desktop/src/lib/voice-timing.test.ts apps/desktop/src/app/chat/composer/hooks/use-voice-recorder.test.tsx apps/desktop/src/app/chat/composer/sensevoice-readiness.test.tsx
git commit -m "test: cover shared SenseVoice behavior"
```

### Task 6: Close the final common authentication entrypoints

**Files:**

- Modify: `hermes_cli/client_auth/cli.py`
- Modify: `hermes_cli/client_auth/entrypoints.json`
- Modify: `hermes_cli/client_auth/static_help.txt`
- Modify: `hermes_cli/main.py`
- Modify: `cron/__init__.py`
- Modify: `cron/scripts/classify_items.py`
- Modify: `gateway/__init__.py`
- Modify: `scripts/check_auth_entrypoints.py`
- Modify: `scripts/discord-voice-doctor.py`
- Modify: `scripts/keystroke_diagnostic.py`
- Modify: `tests/hermes_cli/client_auth/test_account_commands.py`
- Modify: `tests/hermes_cli/client_auth/test_entrypoints.py`

- [ ] **Step 1: Restore only the accepted functional entrypoint closure**

Use the accepted artifact-audit tree as a source for these exact paths only:

```bash
git restore --source=f5a7372e4c -- cron/__init__.py gateway/__init__.py hermes_cli/client_auth/entrypoints.json scripts/check_auth_entrypoints.py tests/hermes_cli/client_auth/test_entrypoints.py
git restore --source=c8fad50c49 -- cron/scripts/classify_items.py scripts/discord-voice-doctor.py scripts/keystroke_diagnostic.py
git restore --source=e51e448669 -- hermes_cli/client_auth/cli.py hermes_cli/main.py tests/hermes_cli/client_auth/test_account_commands.py
```

Do not restore `apps/desktop/electron/bootstrap-payload.ts`, `desktop_auth_runtime/**`, installer-scope tests, or any build script.

- [ ] **Step 2: Regenerate deterministic inventories**

Run:

```bash
uv run python scripts/check_auth_entrypoints.py --write
uv run python scripts/generate_auth_free_help.py
```

- [ ] **Step 3: Verify public-command and background-mode behavior**

Run:

```bash
uv run pytest tests/hermes_cli/client_auth/test_account_commands.py tests/hermes_cli/client_auth/test_entrypoints.py tests/hermes_cli/client_auth/test_background_modes.py -q
uv run python scripts/check_auth_entrypoints.py --check
uv run python scripts/generate_auth_free_help.py --check
```

Expected: signed-out mode permits only login/logout/status/help/version; background entrypoints never prompt for a password.

- [ ] **Step 4: Commit the functional closure**

```bash
git add hermes_cli/client_auth hermes_cli/main.py cron/__init__.py cron/scripts/classify_items.py gateway/__init__.py scripts/check_auth_entrypoints.py scripts/generate_auth_free_help.py scripts/discord-voice-doctor.py scripts/keystroke_diagnostic.py tests/hermes_cli/client_auth
git commit -m "fix: preserve auth guard across common entrypoints"
```

### Task 7: Prove source-mode Desktop has no packaging bypass

**Files:**

- Modify: `apps/desktop/e2e/auth-hard-gate.spec.ts`
- Modify: `apps/desktop/electron/main.ts`
- Modify: `apps/desktop/src/main.tsx`
- Modify: `apps/desktop/src/protected-root.tsx`
- Test: `apps/desktop/electron/guarded-ipc.test.ts`
- Test: `apps/desktop/src/components/auth-gate.test.tsx`

- [ ] **Step 1: Add source-mode assertions to the existing Auth Guard E2E**

Before submitting credentials, extend the signed-out test to assert that the `protected-root` resource is absent from the Performance resource list, every representative protected IPC call rejects with `AUTH_REQUIRED`, Electron's child-process inventory contains no Hermes backend/agent/gateway child, and the sandbox contains no protected HTTP or WebSocket listener. Reuse the existing `page.evaluate` IPC helper and Electron process-inspection fixtures instead of adding a renderer escape hatch.

Launch with a temporary `HERMES_HOME` and the existing in-memory/headless credential-owner mode so the test never reads the developer Keychain.

- [ ] **Step 2: Run the E2E and confirm it fails if source startup still mounts protected code**

Run:

```bash
npm run test:e2e --workspace apps/desktop -- e2e/auth-hard-gate.spec.ts
```

Expected before any necessary wiring fix: FAIL at the first source-mode bypass assertion. If it already passes, do not change production code solely to manufacture a diff.

- [ ] **Step 3: Make the minimum source-mode startup correction**

The startup order in `apps/desktop/electron/main.ts` and `apps/desktop/src/main.tsx` must remain:

```text
create unprotected login window
-> establish/validate AuthScope
-> authorize guarded IPC
-> start protected backend
-> dynamically import and mount protected-root exactly once
```

Do not add an environment-variable bypass, offline session acceptance, package-resource lookup, or automatic password retry.

- [ ] **Step 4: Run the focused Desktop gate**

Run:

```bash
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop -- electron/guarded-ipc.test.ts src/components/auth-gate.test.tsx
npm run test:e2e --workspace apps/desktop -- e2e/auth-hard-gate.spec.ts
```

Expected: PASS; the protected root mounts once and only after authenticated runtime scope exists.

- [ ] **Step 5: Commit only if production or test files changed**

```bash
git add apps/desktop/e2e/auth-hard-gate.spec.ts apps/desktop/electron/main.ts apps/desktop/src/main.tsx apps/desktop/src/protected-root.tsx apps/desktop/electron/guarded-ipc.test.ts apps/desktop/src/components/auth-gate.test.tsx
git commit -m "test: prove source desktop auth hard gate"
```

### Task 8: Run the common release gate

**Files:**

- Create: `docs/security/main-auth-voice-acceptance-2026-08-22.md`
- Verify: all changed source, lock, tests, and generated inventories

- [ ] **Step 1: Run complete Python authentication and Voice regression**

Run:

```bash
uv run pytest tests/hermes_cli/client_auth tests/tui_gateway/test_account_auth.py tests/acp/test_entry.py tests/agent/test_transcription_registry.py tests/tools/test_sensevoice_stt.py tests/tools/test_transcription_tools.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run common Python quality gates**

Run:

```bash
uv run ruff check hermes_cli/client_auth hermes_cli/main.py agent/transcription_registry.py tools/sensevoice_stt.py tools/transcription_tools.py tests/hermes_cli/client_auth tests/agent/test_transcription_registry.py tests/tools/test_sensevoice_stt.py tests/tools/test_transcription_tools.py
uv lock --check
uv run python scripts/check_auth_entrypoints.py --check
uv run python scripts/generate_auth_free_help.py --check
python scripts/check_main_platform_boundary.py
```

Expected: PASS.

- [ ] **Step 3: Run complete Desktop common gate**

Run:

```bash
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop
npm run test:e2e --workspace apps/desktop -- e2e/auth-hard-gate.spec.ts
```

Expected: PASS. This is source-mode validation, not a DMG or NSIS build.

- [ ] **Step 4: Run syntax and repository checks**

Run:

```bash
bash -n scripts/install.sh
git diff --check ansatz/main...HEAD
git status --short --branch
```

Expected: no whitespace errors and no untracked implementation residue.

- [ ] **Step 5: Record sanitized evidence**

Create `docs/security/main-auth-voice-acceptance-2026-08-22.md` with:

```markdown
# Main Authentication and Voice Acceptance

- Base commit: `9bd88c530716279a089ed18428dc785732b6e1be`
- Candidate commit: output of `git rev-parse HEAD` captured immediately before this document is committed
- Environment: macOS arm64, Python and Node versions from the commands below
- Authentication tests: PASS/FAIL
- Desktop source hard gate: PASS/FAIL
- Voice/SenseVoice tests: PASS/FAIL
- Platform boundary check: PASS/FAIL
- Sensitive evidence: none recorded
```

Record versions with `sw_vers`, `uname -m`, `python --version`, `node --version`, and `npm --version`. Do not record an account name, password, Cookie, Session, CSRF value, Keychain entry, or raw auth bridge log.

- [ ] **Step 6: Commit acceptance evidence**

```bash
git add docs/security/main-auth-voice-acceptance-2026-08-22.md
git commit -m "docs: record main auth voice acceptance"
```

### Task 9: Prepare the merge decision without changing `main`

**Files:**

- Verify: branch history and diff only

- [ ] **Step 1: Refresh the target ref and detect movement**

Run:

```bash
git fetch ansatz main
git merge-base --is-ancestor ansatz/main HEAD
```

Expected: success. If `ansatz/main` moved and is not an ancestor, merge the new remote `main` into the candidate, rerun Task 8, and record the new base.

- [ ] **Step 2: Audit the final delta**

Run:

```bash
git diff --stat ansatz/main...HEAD
git diff --name-status ansatz/main...HEAD
python scripts/check_main_platform_boundary.py
git log --graph --decorate --oneline ansatz/main..HEAD
```

Expected: authentication, Voice/SenseVoice, tests, common docs, and common CI only. No DMG payload, NSIS payload, platform installer bootstrap, exact-artifact credential driver, or platform packaging workflow.

- [ ] **Step 3: Push only the candidate branch**

Run:

```bash
git push --set-upstream ansatz integration/main-auth-voice-base
```

Do not push `HEAD:main` in this task.

- [ ] **Step 4: Stop for approval**

Report the candidate commit, test results, remaining risks, and exact excluded packaging paths. Only after explicit approval may a subsequent task fast-forward or merge the candidate into `ansatz/main` and create replacement macOS/Windows release branches.
