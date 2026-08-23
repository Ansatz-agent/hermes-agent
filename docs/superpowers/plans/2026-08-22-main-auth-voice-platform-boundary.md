# Main Authentication and Voice Platform Boundary Implementation Plan

> **SUPERSEDED — DO NOT EXECUTE:** The approved design was materially revised on 2026-08-22. `main` must now own the clean-install macOS/Windows packaging foundation and domestic-mirror-first authentication/runtime dependency policy. This common-only plan remains only as an audit record until it is replaced and reviewed.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Do not use subagents for this repository migration. Stop at every explicit review checkpoint.

**Goal:** Produce a reviewed `integration/main-auth-voice-base` branch based on the current remote `ansatz/main` that contains the complete common authentication hard gate and the complete Voice/SenseVoice behavior of the accepted macOS DMG, supports source execution on macOS and Windows, and contains no platform release overlay.

**Architecture:** Merge the original common authentication history, replay every later accepted common product fix in dependency order, import the blob-equivalent Voice commit, and extract only the source-runtime adapters required from the Windows branch. Common policy and behavior live in `main`; operating-system adapters implement common interfaces; DMG/NSIS delivery stays in platform overlays. Versioned path manifests and behavioral tests prove parity instead of treating a green test run as equivalence.

**Tech Stack:** Python 3.11-3.13, pytest, Ruff, uv, Electron 40, TypeScript 6, React 19, Vitest, Playwright, npm workspaces, macOS secure storage/Unix transport, Windows secure storage/named-pipe transport, git worktrees.

---

## Scope, authority, and stopping point

This plan changes only `integration/main-auth-voice-base`. It does not merge or push to `main`, rebuild installers, or rewrite the existing release references. After the candidate passes this plan, a separate explicit approval is required before `main` or either shipping overlay changes.

Worktree:

```text
/Users/zhouzhangchen/Desktop/自己的/acadamic/agent/hermes-agent/.worktrees/integration-main-auth-voice-base
```

Locked references:

```text
ansatz/main                                      9bd88c530716279a089ed18428dc785732b6e1be
feature/remote-auth-hard-gate                    763465daf019c8755813659b98a72c6c6f4662e3
feature/desktop-dmg-voice-confirmation           3ad4a126606079c77e7adca6d8661cd0c8c0a93b
integration/desktop-dmg-auth-e2e                 403e1c3873d1679720c1403d7e38acd289804d69
release/desktop-dmg-auth-e2e                     80db6d8265f805cec46817d913982e4c5f6405c4
integration/desktop-windows-auth-e2e behavior    c2d3d09aab921130171ff611e260c13e9c6d477c
integration/desktop-windows-auth-e2e docs tip    56b402c63b22da81f906ff1f7398a90cfd17bd81
shared pre-integration baseline                  4ef56cef4c6eecc009e2284fe2f1df20664f357a
```

The Voice paths in `3ad4a12660` have already been verified blob-equal to the final DMG. Authentication does not have that property at the original feature tip; the later replay is mandatory.

## Non-negotiable ownership rules

- `main` owns all authentication and Voice user behavior.
- `main` owns OS adapters necessary to run those common capabilities from source: secure credential storage and local owner transport for each supported OS.
- Platform branches own only delivery: installer, payload, packaged-resource lookup, mirrors, signing, artifact workflow, and installed-artifact evidence.
- No platform branch may delete GUI logout, fork `client_auth/runtime.py`, alter Auth Guard decisions, or modify Voice behavior.
- `hermes:bootstrap:get` is protected. Signed-out progress uses only sanitized `hermes:auth-bootstrap:get`.
- `runtime_ready`, runtime epoch suppression, and safe progress presentation are common behavior.
- Handwritten substitutes are forbidden when an accepted product commit exists.

## Task 0: Reconfirm the clean base and authoritative references

**Files:** None.

- [ ] **Step 1: Verify worktree isolation**

```bash
test "$(git branch --show-current)" = "integration/main-auth-voice-base"
test "$(git status --porcelain)" = ""
test "$(git merge-base ansatz/main HEAD)" = "9bd88c530716279a089ed18428dc785732b6e1be"
```

Expected: every command exits zero. Never use the dirty primary worktree.

- [ ] **Step 2: Verify remote `main` has not moved**

```bash
test "$(git ls-remote ansatz refs/heads/main | cut -f1)" = \
  "9bd88c530716279a089ed18428dc785732b6e1be"
```

Expected: PASS. If it fails, stop, merge the new remote main into this planning branch, update both documents, and repeat plan review before source work.

- [ ] **Step 3: Verify every locked reference**

```bash
test "$(git rev-parse feature/remote-auth-hard-gate)" = "763465daf019c8755813659b98a72c6c6f4662e3"
test "$(git rev-parse feature/desktop-dmg-voice-confirmation)" = "3ad4a126606079c77e7adca6d8661cd0c8c0a93b"
test "$(git rev-parse integration/desktop-dmg-auth-e2e)" = "403e1c3873d1679720c1403d7e38acd289804d69"
test "$(git rev-parse release/desktop-dmg-auth-e2e)" = "80db6d8265f805cec46817d913982e4c5f6405c4"
test "$(git rev-parse integration/desktop-windows-auth-e2e)" = "56b402c63b22da81f906ff1f7398a90cfd17bd81"
```

Expected: PASS. `56b402c63b` is documentation-only relative to behavior tip `c2d3d09aab`.

- [ ] **Step 4: Record the release/integration authority proof**

```bash
test "$(git merge-base release/desktop-dmg-auth-e2e integration/desktop-dmg-auth-e2e)" = \
  "4ef56cef4c6eecc009e2284fe2f1df20664f357a"
git diff --name-status 403e1c3873d1679720c1403d7e38acd289804d69 \
  80db6d8265f805cec46817d913982e4c5f6405c4
```

Expected: exactly 15 CI/evidence paths and no product path. Save the path list in sanitized acceptance evidence; do not copy the files.

- [ ] **Step 5: Run the untouched baseline checks**

```bash
npm ci
uv sync --extra dev
npm run typecheck --workspace apps/desktop
uv run pytest tests/hermes_cli/test_auth_commands.py tests/tools/test_transcription_tools.py -q
git diff --check
```

Expected: PASS. Stop on a pre-existing baseline failure.

## Task 1: Add an allowlist-based common-branch boundary gate

**Files:**

- Create: `docs/security/main-auth-voice-common-paths.txt`
- Create: `scripts/check_main_platform_boundary.py`
- Create: `tests/test_main_platform_boundary.py`
- Delete: `.github/workflows/desktop-windows-package.yml`

- [ ] **Step 1: Create the approved common path manifest**

Create `docs/security/main-auth-voice-common-paths.txt` as a sorted newline-separated list. Seed it from the exact product/test paths assigned to `main` or the `main` side of `split` in Appendix A and Appendix B. Also include the current common boundary design and implementation-plan paths, which already differ from `ansatz/main`. Do not include a directory wildcard.

Validate sorting and uniqueness:

```bash
test "$(LC_ALL=C sort docs/security/main-auth-voice-common-paths.txt | uniq | \
  shasum -a 256 | cut -d' ' -f1)" = \
  "$(shasum -a 256 docs/security/main-auth-voice-common-paths.txt | cut -d' ' -f1)"
```

Expected: PASS.

- [ ] **Step 2: Write the failing checker tests**

Create `tests/test_main_platform_boundary.py` with one parameterized test and one repository helper. The complete test shape is:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


CHECKER = Path(__file__).resolve().parents[1] / "scripts/check_main_platform_boundary.py"


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=repo, text=True, capture_output=True, check=False)


def make_repo(tmp_path: Path, candidate_path: str, allow: bool) -> Path:
    run(tmp_path, "git", "init", "-q")
    run(tmp_path, "git", "config", "user.email", "boundary@example.invalid")
    run(tmp_path, "git", "config", "user.name", "Boundary Test")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    run(tmp_path, "git", "add", "base.txt")
    run(tmp_path, "git", "commit", "-qm", "base")
    run(tmp_path, "git", "tag", "boundary-base")
    candidate = tmp_path / candidate_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("candidate\n", encoding="utf-8")
    run(tmp_path, "git", "add", candidate_path)
    run(tmp_path, "git", "commit", "-qm", "candidate")
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text(f"{candidate_path}\n" if allow else "", encoding="utf-8")
    return allowlist


@pytest.mark.parametrize(
    ("candidate_path", "allow", "expected"),
    [
        ("src/unapproved.py", False, 1),
        ("scripts/new-desktop-dmg-proof.mjs", True, 1),
        ("scripts/new-nsis-proof.mjs", True, 1),
        ("apps/desktop/electron/new-bundled-runtime.ts", True, 1),
        ("apps/desktop/electron/new-bootstrap-payload.ts", True, 1),
        ("apps/desktop/electron/bootstrap-process.ts", True, 1),
        ("apps/desktop/electron/windows-auth-owner.ts", True, 1),
        ("apps/desktop/electron/auth-runtime-contract.ts", True, 1),
        ("apps/desktop/e2e/installed-windows-auth.spec.ts", True, 1),
        ("apps/desktop/electron/windows-process-tree.ts", True, 1),
        ("apps/desktop/scripts/before-pack.mjs", True, 1),
        ("apps/desktop/scripts/package-audit.mjs", True, 1),
        ("scripts/build-desktop-windows.ps1", True, 1),
        ("scripts/desktop-windows-contract.test.mjs", True, 1),
        ("scripts/desktop-credential-login.mjs", True, 1),
        ("scripts/write_desktop_windows_electron_artifact.py", True, 1),
        ("scripts/test-desktop-windows-auth.ps1", True, 1),
        ("scripts/tests/test-install-ps1-longpath.ps1", True, 0),
        ("hermes_cli/client_auth/runtime.py", True, 0),
    ],
)
def test_candidate_boundary(
    tmp_path: Path,
    candidate_path: str,
    allow: bool,
    expected: int,
) -> None:
    allowlist = make_repo(tmp_path, candidate_path, allow)
    result = run(
        tmp_path,
        sys.executable,
        str(CHECKER),
        "--repo",
        str(tmp_path),
        "--base",
        "boundary-base",
        "--allowlist",
        str(allowlist),
    )
    assert result.returncode == expected, result.stdout + result.stderr
```

The fixture must create a temporary Git repository, commit a base, write the allowlist, add the candidate files, and invoke the checker through `subprocess.run`. The positive generic PowerShell cases are:

```text
scripts/tests/test-install-ps1-gitbash-compatibility.ps1
scripts/tests/test-install-ps1-longpath.ps1
scripts/tests/test-install-ps1-stage-protocol.ps1
```

Add a dedicated frozen-legacy test fixture that commits `apps/desktop/scripts/before-pack.mjs` and `after-pack.mjs` in `boundary-base`. A candidate changing only an allowed common path must pass while both hook blobs remain identical; modifying either hook or adding another `before-pack`/`after-pack` path must fail.

- [ ] **Step 3: Run the tests and confirm failure**

```bash
uv run pytest tests/test_main_platform_boundary.py -q
```

Expected: FAIL because `scripts/check_main_platform_boundary.py` does not exist.

- [ ] **Step 4: Implement the checker**

The checker must:

1. Read the explicit allowlist.
2. Compare `git diff --name-only --diff-filter=ACMR ansatz/main...HEAD` against it in the real repository; the `--repo`/`--base` arguments make the same logic testable in a temporary repository.
3. Reject every unapproved changed path.
4. Reject tracked delivery paths by exact directory/name tokens: `desktop_auth_runtime`, `desktop-dmg`, `desktop-windows-package`, `dmg-gatekeeper`, `nsis`, `bootstrap-payload`, `bootstrap-process`, `bootstrap-toolchain`, `bundled-runtime`, `windows-auth-owner`, `auth-runtime-contract`, `installed-windows-`, `windows-process-tree`, `before-pack`, `after-pack`, `stage-native-deps`, `set-exe-identity`, `exe-identity-options`, `package-audit`, `build-backend-payload`, `build-auth-toolchain`, `build-desktop-windows`, `desktop-windows-contract`, `desktop-credential-login`, `prepare-auth-toolchain-inputs`, `prepare-windows-git-runtime`, `write_desktop_windows_electron_artifact`, `test-desktop-windows-`, and exact-artifact credential drivers.
5. Never use the broad prefix `scripts/tests/test-install-ps1-`; only the packaged-only files `test-install-ps1-managed-uv.ps1` and `test-install-ps1-packaged-lock.ps1` are forbidden.
6. Treat `apps/desktop/scripts/before-pack.mjs` and `apps/desktop/scripts/after-pack.mjs` as frozen legacy exceptions already present in `ansatz/main`: they pass only while their blobs equal the locked base. They are not added to the common allowlist, and any modification, replacement, or new similarly named path fails. Their later removal belongs to platform-overlay reconstruction, not this behavior migration.

The CLI is:

```text
python scripts/check_main_platform_boundary.py \
  --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
```

Unknown arguments, missing base, missing allowlist, duplicate allowlist lines, and absolute paths must fail closed.

- [ ] **Step 5: Prove the current base violation and remove only it**

```bash
python scripts/check_main_platform_boundary.py \
  --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
```

Expected before deletion: FAIL naming `.github/workflows/desktop-windows-package.yml`. The two common boundary documents are already allowlisted. Delete only the Windows packaging workflow; do not delete `.github/workflows/tests.yml`, `.github/workflows/tests-os.yml`, or the three generic `install.ps1` tests.

- [ ] **Step 6: Run and commit the boundary gate**

```bash
uv run pytest tests/test_main_platform_boundary.py -q
python scripts/check_main_platform_boundary.py \
  --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git diff --check
git add docs/security/main-auth-voice-common-paths.txt scripts/check_main_platform_boundary.py \
  tests/test_main_platform_boundary.py .github/workflows/desktop-windows-package.yml
git commit -m "build: keep release overlays out of main"
```

Expected: PASS and one boundary commit.

## Task 2: Merge the original common authentication foundation

**Files:** All product/test files changed by `feature/remote-auth-hard-gate@763465daf0`, subject to the Task 1 boundary gate.

- [ ] **Step 1: Prove the feature contains no release overlay**

```bash
if git diff --name-only 4ef56cef4c..feature/remote-auth-hard-gate | \
  rg 'desktop_auth_runtime|desktop-dmg|desktop-windows-package|bootstrap-payload|bundled-runtime'; then
  echo "unexpected release overlay in authentication feature"
  false
fi
```

Expected: no output.

- [ ] **Step 2: Merge the reviewed authentication history**

```bash
git merge --no-ff feature/remote-auth-hard-gate \
  -m "merge: add common remote auth hard gate"
```

Expected: a merge commit; no release overlay path.

- [ ] **Step 3: Run the foundation gate**

```bash
uv run pytest tests/hermes_cli/client_auth tests/tui_gateway/test_account_auth.py tests/acp/test_entry.py -q
uv run python scripts/check_auth_entrypoints.py --check
uv run python scripts/generate_auth_free_help.py --check
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop -- \
  electron/auth-bridge.test.ts electron/auth-coordinator.test.ts \
  electron/guarded-ipc.test.ts src/components/auth-gate.test.tsx
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
```

Expected: PASS. This is only the foundation; safe pre-auth bootstrap IPC is added in Task 3.

## Task 3: Replay the accepted common authentication evolution in dependency order

**Files:** Exact `main` and `split` paths in Appendix A.

### Required replay order

Replay or extract the common side of these commits in this exact dependency order:

```text
08a2eedf67  login surface before bootstrap
706c5a4d0d  teardown-aware quit
6aed1cc0f1  lease clock-domain alignment
e042ce3861  ACP setup test closure
bffdd26edf  TUI watcher cleanup
f28a0f4b58  Desktop auth-owner E2E cleanup
8af9e3eefe  DesktopAuthContext/useDesktopAuth
cdc12484f1  GUI logout item
27567163b3  logout authentication tests
366fb3f5a8  bounded auth bridge requests
cdb5c65bc8  bounded auth bootstrap progress
c478db4d2f  common runtime gate/product status paths only
e51e448669  usable pre-runtime CLI paths only
817a5d0a6a  bridge timeout after HTTP
b22bdb8d31  dead local bridge recovery
af19ce56b1  Retry rebuilds bridge
50609240e2  auth recovery lint/test correction
bddbec2abb  safe bootstrap progress state and common event contract
c4d5ae2d40  pre-auth IPC isolation
f8d7c05fad  locked progress renderer
34026f7627  progress remains inside Auth Gate
a416087126  common progress lint paths only
34f1c119e2  Retry only after declared failure
c8fad50c49  common entrypoint closure paths only
f5a7372e4c  common entrypoint audit paths only
c893e264e9  focus elapsed-time resync
38230a6c9f  runtime epoch/instance stale-result suppression
4e4a5d42c7  expired native owner recovery
```

Do not move `50609240e2` before `af19ce56b1`. Do not pick `cdc12484f1` before `8af9e3eefe`.

- [ ] **Step 1: Add a replay-order contract before replaying**

Create `tests/test_main_auth_replay_contract.py`. It must parse a checked-in JSON ledger created in Step 2 and assert:

```python
assert before("8af9e3eefe", "cdc12484f1")
assert before("6aed1cc0f1", "4e4a5d42c7")
assert before("af19ce56b1", "50609240e2")
assert before("bddbec2abb", "c4d5ae2d40")
assert before("c4d5ae2d40", "f8d7c05fad")
assert before("f8d7c05fad", "34026f7627")
assert destination("c478db4d2f") == "split"
assert destination("89503cfb2b") == "split"
assert destination("e51e448669") == "split"
assert destination("a416087126") == "split"
assert every_main_or_split_commit_has_an_execution_step()
assert product_manifest_equals_ledger_projection()
```

Run:

```bash
uv run pytest tests/test_main_auth_replay_contract.py -q
```

Expected: FAIL because `docs/security/main-auth-voice-commit-ledger.json` does not exist.

- [ ] **Step 2: Create the machine-readable commit ledger**

Create `docs/security/main-auth-voice-commit-ledger.json` with one object for every one of the 165 commits in Appendix A:

```json
{
  "schema": 1,
  "baseline": "4ef56cef4c6eecc009e2284fe2f1df20664f357a",
  "tip": "403e1c3873d1679720c1403d7e38acd289804d69",
  "commits": [
    {
      "sha": "08a2eedf67",
      "destination": "main",
      "reason": "common protected startup ordering"
    }
  ]
}
```

The JSON order must equal `git log --reverse 4ef56cef4c..403e1c3873`. `split` entries must include exhaustive `main_paths` and `overlay_paths` arrays. An overlay commit that intentionally modifies an already-common interface may declare `common_interface_waiver_paths`; `6e57ead969` uses it for `apps/desktop/electron/bootstrap-runner.ts` and its test. Documentation-only historical commits use `drop`, not `main`; every `main` or `split` entry must be named by exactly one execution step. The product manifest projection is derived mechanically from the ledger in Task 8, so a missing replay or explicit interface-waiver path cannot be hidden by a handwritten manifest.

Run the contract again. Expected: PASS and exactly 165 commit objects.

- [ ] **Step 3: Extract the early split integration test**

```bash
git cherry-pick --no-commit 89503cfb2b
git restore --source=HEAD --staged --worktree -- \
  apps/desktop/electron/bootstrap-payload.integration.test.ts \
  apps/desktop/electron/bundled-runtime.integration.test.ts \
  apps/desktop/scripts/before-pack-payload.integration.test.mjs \
  apps/desktop/scripts/build-backend-payload.integration.test.mjs
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
git diff --cached --name-only
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git commit -C 89503cfb2b
```

Expected: only `auth-hard-gate.spec.ts` and `settings/helpers.test.ts` are committed.

- [ ] **Step 4: Replay the first common segment**

```bash
first_common_segment=(
  08a2eedf67 706c5a4d0d 6aed1cc0f1 e042ce3861 bffdd26edf f28a0f4b58
  8af9e3eefe cdc12484f1 27567163b3 366fb3f5a8 cdb5c65bc8
)
for replay_sha in "${first_common_segment[@]}"; do
  git cherry-pick "$replay_sha"
  python scripts/check_main_platform_boundary.py --base ansatz/main \
    --allowlist docs/security/main-auth-voice-common-paths.txt
done
```

Expected: each commit lands in original branch order. Run the focused tests introduced by each commit immediately after it lands.

- [ ] **Step 5: Extract the common runtime-gate side of `c478db4d2f`**

```bash

if ! git cherry-pick --no-commit c478db4d2f; then
  test -n "$(git diff --name-only --diff-filter=U)"
fi
git rm -q -r -f --ignore-unmatch -- \
  apps/desktop/scripts/build-backend-payload.mjs \
  desktop_auth_runtime tests/test_install_sh_bootstrap_scope.py uv.toml
git checkout HEAD -- \
  apps/desktop/electron/bootstrap-runner.ts \
  apps/desktop/electron/bootstrap-runner.test.ts \
  pyproject.toml scripts/install.sh uv.lock
```

Resolve and stage only any remaining ledger `main_paths`, then run:

```bash
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
git diff --cached --name-only
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git commit -C c478db4d2f
```

Expected: the staged set equals the ledger's common runtime-gate paths. `bootstrap-runner*`, dependency locks, bundled payloads, installer hunks, and root `uv.toml` remain unchanged.

- [ ] **Step 6: Extract the common pre-runtime CLI side of `e51e448669`**

```bash
if ! git cherry-pick --no-commit e51e448669; then
  test -n "$(git diff --name-only --diff-filter=U)"
fi
git rm -q -f --ignore-unmatch -- tests/test_install_sh_bootstrap_scope.py
git checkout HEAD -- scripts/install.sh
```

Resolve and stage only any remaining ledger `main_paths`, then run:

```bash
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
git diff --cached --name-only
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git commit -C e51e448669
```

Expected: only `hermes_cli/client_auth/cli.py`, `hermes_cli/main.py`, and `tests/hermes_cli/client_auth/test_account_commands.py` are committed. The auth-scope installer launcher publication remains in the platform overlay.

- [ ] **Step 7: Replay bridge recovery and the safe progress-state contract**

```bash
bridge_progress_state_segment=(
  817a5d0a6a b22bdb8d31 af19ce56b1 50609240e2
  bddbec2abb
)
for replay_sha in "${bridge_progress_state_segment[@]}"; do
  git cherry-pick "$replay_sha"
  python scripts/check_main_platform_boundary.py --base ansatz/main \
    --allowlist docs/security/main-auth-voice-common-paths.txt
done
```

Expected: `50609240e2` follows `af19ce56b1`; the sanitized state/event contract is present before its public channel is added.

- [ ] **Step 8: Add the signed-out IPC regression before exposing safe progress**

Extend `apps/desktop/electron/guarded-ipc.test.ts` and `apps/desktop/e2e/auth-hard-gate.spec.ts` with this contract:

```text
signed out -> invoke hermes:bootstrap:get -> AUTH_REQUIRED
signed out -> invoke hermes:auth-bootstrap:get -> sanitized bounded state
sanitized state -> no raw log, command, absolute path, Cookie, Session, CSRF, password, keychain field
```

Run now: Expected FAIL because `c4d5ae2d40` has not landed.

- [ ] **Step 9: Replay the safe public channel and locked progress renderer**

```bash
safe_gate_segment=(c4d5ae2d40 f8d7c05fad 34026f7627)
for replay_sha in "${safe_gate_segment[@]}"; do
  git cherry-pick "$replay_sha"
  python scripts/check_main_platform_boundary.py --base ansatz/main \
    --allowlist docs/security/main-auth-voice-common-paths.txt
done
```

Run the Step 8 regressions again. Expected: PASS; raw `hermes:bootstrap:get` remains protected and only the sanitized channel is public.

- [ ] **Step 10: Extract the common lint side of `a416087126`**

```bash
if ! git cherry-pick --no-commit a416087126; then
  test -n "$(git diff --name-only --diff-filter=U)"
fi
git rm -q -f --ignore-unmatch -- \
  apps/desktop/electron/bootstrap-process.ts \
  apps/desktop/electron/bootstrap-process.test.ts
```

Resolve and stage only `apps/desktop/electron/main.ts` and `apps/desktop/src/components/auth-bootstrap-progress.test.tsx`, then run:

```bash
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
git diff --cached --name-only
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git commit -C a416087126
```

Expected: only `apps/desktop/electron/main.ts` and `apps/desktop/src/components/auth-bootstrap-progress.test.tsx` are committed. `bootstrap-process*` never enters `main`.

- [ ] **Step 11: Replay the Retry and first entrypoint closure**

```bash
third_common_segment=(34f1c119e2 c8fad50c49)
for replay_sha in "${third_common_segment[@]}"; do
  git cherry-pick "$replay_sha"
  python scripts/check_main_platform_boundary.py --base ansatz/main \
    --allowlist docs/security/main-auth-voice-common-paths.txt
done
```

Expected: Retry appears only after declared failure, and `c8fad50c49` changes only common entrypoint files.

- [ ] **Step 12: Extract the common entrypoint side of `f5a7372e4c`**

```bash

if ! git cherry-pick --no-commit f5a7372e4c; then
  test -n "$(git diff --name-only --diff-filter=U)"
fi
git rm -q -f --ignore-unmatch -- \
  apps/desktop/electron/bootstrap-payload.ts \
  apps/desktop/electron/bootstrap-payload.integration.test.ts
```

Resolve and stage only any remaining ledger `main_paths`, then run:

```bash
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
git diff --cached --name-only
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git commit -C f5a7372e4c
```

Expected: the staged list equals the ledger's `main_paths` exactly; `git commit -C` preserves author/message.

- [ ] **Step 13: Replay focus and epoch reliability before owner recovery**

```bash
final_common_segment=(c893e264e9 38230a6c9f)
for replay_sha in "${final_common_segment[@]}"; do
  git cherry-pick "$replay_sha"
  python scripts/check_main_platform_boundary.py --base ansatz/main \
    --allowlist docs/security/main-auth-voice-common-paths.txt
done
```

Expected: focus resync precedes epoch suppression. Native-owner recovery is applied with its explicit conflict protocol in Step 14.

- [ ] **Step 14: Resolve the known conflicts by accepted dependency semantics**

The known conflict surfaces are:

```text
366fb3f5a8  apps/desktop/src/components/auth-gate.test.tsx
af19ce56b1  apps/desktop/electron/main.ts
4e4a5d42c7 apps/desktop/src/components/auth-gate.test.tsx
             tests/hermes_cli/client_auth/test_bridge.py
             two historical owner-recovery planning documents (drop)
```

Resolution rules:

- Keep `DesktopAuthContext/useDesktopAuth` and the GUI logout callback from `8af9e3eefe`.
- Preserve the timeout ordering from `817a5d0a6a`, dead-bridge recovery from `b22bdb8d31`, Retry reconstruction from `af19ce56b1`, and lint correction from `50609240e2`.
- Preserve lease-clock alignment from `6aed1cc0f1` before applying owner expiry recovery.
- The historical owner-recovery design/plan from `280751398d` is intentionally classified `drop`; resolve the two later modify/delete conflicts by leaving those documents absent from `main`.
- Never resolve by exposing `hermes:bootstrap:get` to signed-out code.

The rules for `366fb3f5a8` and `af19ce56b1` are invoked immediately when their earlier replay loops stop; do not defer an unresolved cherry-pick. Resolve and stage the named common path, then run both diff checks plus the unmerged-path assertion before `git cherry-pick --continue`.

Apply `4e4a5d42c7` explicitly:

```bash
if ! git cherry-pick 4e4a5d42c7; then
  test -n "$(git diff --name-only --diff-filter=U)"
fi
git rev-parse --verify -q CHERRY_PICK_HEAD
git rm -q -f --ignore-unmatch -- \
  docs/superpowers/plans/2026-08-21-auth-owner-idle-recovery.md \
  docs/superpowers/specs/2026-08-21-auth-owner-idle-recovery-design.md
```

Resolve and stage only the remaining common auth-owner paths, then run:

```bash
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git cherry-pick --continue
```

Immediately run the focused test named by each conflicted path before continuing.

- [ ] **Step 15: Verify logout epoch isolation**

Run the tests introduced by `38230a6c9f` and add an E2E case that performs logout followed by a second synthetic account epoch. Assert that the prior preparation result is ignored and no `authenticated` runtime message from the first epoch reaches the renderer.

```bash
npm run test --workspace apps/desktop -- \
  electron/authenticated-runtime-preparation.test.ts \
  src/components/auth-gate.test.tsx
npm run test:e2e --workspace apps/desktop -- e2e/auth-hard-gate.spec.ts
```

Expected: PASS.

- [ ] **Step 16: Run the complete authentication replay gate**

```bash
uv run pytest tests/hermes_cli/client_auth tests/tui_gateway/test_account_auth.py tests/acp/test_entry.py -q
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop -- \
  electron/auth-bridge.test.ts electron/auth-coordinator.test.ts \
  electron/guarded-ipc.test.ts electron/desktop-runtime-gate.test.ts \
  electron/authenticated-runtime-preparation.test.ts \
  src/components/auth-gate.test.tsx src/i18n/auth-catalog.test.ts
```

Expected: PASS, including `ja` and `zh-hant` authentication progress catalogs.

## Task 4: Import complete Voice/SenseVoice behavior

**Files:** The 30 paths changed by `3ad4a126606079c77e7adca6d8661cd0c8c0a93b`, including `cli-config.yaml.example`.

- [ ] **Step 1: Prove the Voice source and shipping parity**

```bash
test "$(git diff-tree --no-commit-id --name-only -r 3ad4a12660 | wc -l | tr -d ' ')" = "30"
test "$(git ls-tree -r --name-only 3ad4a12660 | rg -c '^cli-config.yaml.example$')" = "1"
git diff --quiet 3ad4a12660 80db6d8265 -- \
  agent/transcription_registry.py tools/sensevoice_stt.py tools/transcription_tools.py \
  tools/lazy_deps.py apps/desktop/src/lib/voice-timing.ts \
  apps/desktop/src/lib/voice-barge-in.ts apps/desktop/src/app/chat/composer \
  apps/desktop/src/app/chat/index.tsx apps/desktop/src/app/contrib/wiring.tsx \
  apps/desktop/src/app/session/hooks/use-hermes-config.ts \
  apps/desktop/src/app/settings/constants.ts apps/desktop/src/app/settings/helpers.ts \
  hermes_cli/config_defaults.py apps/desktop/src/types/hermes.ts
```

Expected: PASS; the Voice feature is the complete shipping behavior.

- [ ] **Step 2: Cherry-pick Voice and resolve only overlapping common files**

```bash
git cherry-pick 3ad4a126606079c77e7adca6d8661cd0c8c0a93b
```

Conflict rules:

- `hermes.ts`: keep typed auth IPC and add Voice types/methods.
- i18n: keep every auth/progress key in `en`, `zh`, `ja`, `zh-hant`, and add Voice keys defined by the Voice commit.
- `tools_config.py` and `web_server.py`: authentication executes before transcription/provider work.
- `apps/desktop/src/app/contrib/surfaces.tsx`: preserve the GUI account/logout registration from `cdc12484f1` and add the Voice surface registration; neither side may replace the other wholesale.
- `pyproject.toml`: preserve common auth dependencies and add `sensevoice`; do not import bundled-runtime pins.
- `scripts/install.sh`: accept only the `3ad4a12660` source-install Voice hunk. Reject every hunk from `c478db4d2f`, `d0b334abba`, `86d0598a13`, `bf090797fe`, `9689aefd08`, and `82b23ebde6`.
- Include `cli-config.yaml.example`.

- [ ] **Step 3: Add deterministic Voice regression tests**

Add these tests without network, microphone, Keychain, or model download:

```text
tests/agent/test_transcription_registry.py::test_sensevoice_registry_is_lazy
tests/tools/test_sensevoice_stt.py::test_sensevoice_unavailable_is_voice_specific
tests/tools/test_transcription_tools.py::test_transcription_endpoint_requires_auth
apps/desktop/src/lib/voice-timing.test.ts::resets_after_recorder_teardown
apps/desktop/src/app/chat/composer/hooks/use-voice-recorder.test.tsx::does_not_start_before_protected_root
apps/desktop/src/app/chat/composer/hooks/use-voice-recorder.test.tsx::local_sensevoice_needs_no_provider_key
```

Run the three auth-isolation tests before implementation and record their expected failures; then add only the missing tests/wiring and rerun.

- [ ] **Step 4: Run the Voice gate**

```bash
uv run pytest tests/agent/test_transcription_registry.py tests/tools/test_sensevoice_stt.py \
  tests/tools/test_transcription_tools.py -q
npm run test --workspace apps/desktop -- \
  src/lib/voice-timing.test.ts \
  src/app/chat/composer/hooks/use-voice-recorder.test.tsx
```

Expected: PASS and zero external downloads.

## Task 5: Reconcile Windows source-runtime authentication without importing Windows delivery

**Files:**

- Modify: `hermes_cli/client_auth/runtime.py`
- Modify: `hermes_cli/client_auth/bridge.py`
- Modify: `tests/hermes_cli/client_auth/test_runtime.py`
- Modify: `tests/hermes_cli/client_auth/test_bridge.py`
- Modify: `apps/desktop/electron/auth-bridge.ts`
- Modify: `apps/desktop/electron/auth-bridge.test.ts`
- Modify: `apps/desktop/src/components/auth-gate.tsx`
- Modify: `apps/desktop/src/components/auth-gate.test.tsx`
- Create/modify only when uncoupled from packaging: `apps/desktop/electron/backend-probes.ts`, `external-open-policy.ts`, `media-permission-policy.ts`, `preview-webview-policy.ts`, `renderer-log.ts`, `trusted-renderer.ts`, and tests.
- Exclude: `apps/desktop/electron/windows-auth-owner.ts`, `desktop_auth_runtime/**`, Windows installer files, workflows, and installed-artifact tests.

- [ ] **Step 1: Write the Windows source-adapter regression contract**

The common Python tests must cover:

```text
WindowsNamedPipeEndpoint uses FILE_FLAG_OVERLAPPED
connect_current(timeout=_RUNTIME_REQUEST_TIMEOUT_SECONDS) retries until its monotonic deadline
owner startup race does not return runtime_unavailable early
OwnerBroker and RemoteRuntimeOwner preserve the current session identity
timeout and peer validation fail closed
Unix/macOS runtime-root behavior remains unchanged
```

Run the tests against the macOS-derived candidate with the existing Windows API fakes. Expected: the named-pipe deadline cases fail.

- [ ] **Step 2: Extract accepted source behavior from Windows commits**

Use `git show` and manual staged extraction, not wholesale cherry-pick:

```text
45eb464242  common owner recovery, bridge, runtime.py, and tests
9a32e19153  deadline/named-pipe review closure and native artifact assertions
d282099faf  common runtime-progress control reachability
772903c824  common auth-runtime contract/backend probe/runtime.py parts only
b2b6a610b1  platform-neutral authenticated Electron hardening parts only
```

For `45eb464242` and `9a32e19153`, extract the exact common paths in one patch each:

```bash
if ! git show 45eb464242 -- \
  apps/desktop/electron/auth-bridge.ts apps/desktop/electron/auth-bridge.test.ts \
  apps/desktop/src/components/auth-gate.tsx apps/desktop/src/components/auth-gate.test.tsx \
  hermes_cli/client_auth/bridge.py hermes_cli/client_auth/runtime.py \
  tests/hermes_cli/client_auth/test_background_modes.py \
  tests/hermes_cli/client_auth/test_bridge.py tests/hermes_cli/client_auth/test_runtime.py | \
  git apply --index -3; then
  test -n "$(git diff --name-only --diff-filter=U)"
fi
```

Resolve and stage only the listed common paths, then run:

```bash
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
git diff --cached --name-only
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git commit -C 45eb464242

if ! git show 9a32e19153 -- \
  apps/desktop/electron/auth-bridge.ts apps/desktop/electron/auth-bridge.test.ts \
  apps/desktop/src/components/auth-gate.tsx apps/desktop/src/components/auth-gate.test.tsx \
  hermes_cli/client_auth/runtime.py scripts/write_auth_native_artifact.py \
  tests/hermes_cli/client_auth/test_native_artifacts.py \
  tests/hermes_cli/client_auth/test_runtime.py | git apply --index -3; then
  test -n "$(git diff --name-only --diff-filter=U)"
fi
```

Resolve and stage only the listed common paths, then run:

```bash
git diff --check
git diff --cached --check
test -z "$(git diff --name-only --diff-filter=U)"
git diff --cached --name-only
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
git commit -C 9a32e19153
```

Extract the remaining commits with these exact path assignments:

```text
d282099faf:
  apps/desktop/src/components/auth-gate.tsx
  apps/desktop/src/components/auth-gate.test.tsx

772903c824:
  apps/desktop/electron/auth-bridge.ts
  apps/desktop/electron/backend-probes.ts
  apps/desktop/electron/backend-probes.test.ts
  hermes_cli/client_auth/runtime.py
  scripts/write_auth_native_artifact.py
  tests/hermes_cli/client_auth/test_native_artifacts.py
  tests/hermes_cli/client_auth/test_runtime.py

b2b6a610b1:
  apps/desktop/e2e/auth-hard-gate.spec.ts
  apps/desktop/e2e/fixtures.ts
  apps/desktop/electron/desktop-runtime-gate.ts
  apps/desktop/electron/desktop-runtime-gate.test.ts
  apps/desktop/electron/external-open-policy.ts
  apps/desktop/electron/external-open-policy.test.ts
  apps/desktop/electron/guarded-ipc.ts
  apps/desktop/electron/guarded-ipc.test.ts
  apps/desktop/electron/media-permission-policy.ts
  apps/desktop/electron/media-permission-policy.test.ts
  apps/desktop/electron/media-protocol.ts
  apps/desktop/electron/media-protocol.test.ts
  apps/desktop/electron/native-auth-decisions.ts
  apps/desktop/electron/native-auth-decisions.test.ts
  apps/desktop/electron/preview-webview-policy.ts
  apps/desktop/electron/preview-webview-policy.test.ts
  apps/desktop/electron/renderer-log.ts
  apps/desktop/electron/renderer-log.test.ts
  apps/desktop/electron/trusted-renderer.ts
  apps/desktop/electron/trusted-renderer.test.ts
```

For `d282099faf`, `772903c824`, and `b2b6a610b1`, run `git diff --check`, `git diff --cached --check`, and `test -z "$(git diff --name-only --diff-filter=U)"` immediately after each staged extraction. The first check detects worktree conflict markers, the second checks staged whitespace/markers, and the third makes unresolved index entries fatal. For `b2b6a610b1`, reconcile only the corresponding guarded wiring hunks in `apps/desktop/electron/main.ts`; stage that file only after all three checks show no packaged bootstrap/process-tree/resource lookup. The machine-readable ledger must contain the lists above before applying any patch, and the boundary/parity tests must fail if the staged set differs.

Expected: no `windows-auth-owner.ts`, workflow, installer, packaged runtime, installed-Windows E2E, or `desktop_auth_runtime` path is staged.

- [ ] **Step 3: Keep delivery-specific Windows ownership outside `main`**

Record these explicit exclusions in the commit ledger:

```text
windows-auth-owner.ts                  Windows overlay; resolves packaged auth venv
auth-runtime-contract.{ts,test.ts}     Windows overlay; validates packaged auth payload
desktop_auth_runtime/**                Windows/macOS overlay; bundled minimal project
scripts/install.ps1 packaged branches Windows overlay
desktop-windows-package.yml            Windows overlay
installed-windows-*.spec.ts            Windows overlay
scripts/test-desktop-windows-*          Windows overlay
```

Generic `scripts/install.ps1` source-install behavior and its existing gitbash/longpath/stage-protocol tests remain in `main`.

- [ ] **Step 4: Verify no platform product drift remains**

Add a test asserting that replacement Windows overlays may not delete or modify:

```text
apps/desktop/src/app/shell/hooks/use-account-statusbar-item.tsx
apps/desktop/src/app/contrib/surfaces.tsx account logout registration
hermes_cli/client_auth/runtime.py
hermes_cli/config.py desktop-bundle wording
hermes_cli/update_cmd.py desktop-bundle wording
hermes_cli/web_server.py desktop-bundle wording
```

Expected: PASS in the common candidate. The later overlay reconstruction must run the same test.

- [ ] **Step 5: Run cross-platform source-runtime units**

```bash
uv run pytest tests/hermes_cli/client_auth/test_runtime.py \
  tests/hermes_cli/client_auth/test_bridge.py \
  tests/hermes_cli/client_auth/test_native_artifacts.py -q
npm run test --workspace apps/desktop -- \
  electron/auth-bridge.test.ts electron/backend-probes.test.ts \
  electron/trusted-renderer.test.ts \
  src/components/auth-gate.test.tsx
```

Expected: PASS on macOS with Windows API fakes; real Windows execution is Task 9.

## Task 6: Resolve common dependencies and lock deterministically

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Do not create: root `uv.toml`
- Do not import: `desktop_auth_runtime/pyproject.toml`, `desktop_auth_runtime/uv.lock`, `desktop_auth_runtime/uv.toml`

- [ ] **Step 1: Assert the source dependency policy before lock generation**

Add a test that parses `pyproject.toml` and asserts:

```python
assert "keyring==25.7.0" in project_dependencies
assert "sensevoice" in optional_dependencies
assert exclude_newer == "14 days"
assert not Path("uv.toml").exists()
assert not Path("desktop_auth_runtime").exists()
```

The exact keyring pin must remain the accepted common auth pin. Keep `setuptools==83.0.0` only where it already exists in `ansatz/main`; do not copy the DMG-only `[all]` addition.

- [ ] **Step 2: Run the dependency test and record the pre-fix failure**

```bash
uv run pytest tests/test_main_auth_voice_dependencies.py -q
```

Expected: FAIL before Voice/auth dependency reconciliation.

- [ ] **Step 3: Regenerate, then check, the common lock**

```bash
uv lock
uv lock --check
uv sync --extra dev
uv run pytest tests/test_main_auth_voice_dependencies.py -q
```

Expected: PASS. Never hand-edit `uv.lock` and then use `uv lock --check` as if it regenerated the graph.

- [ ] **Step 4: Commit dependency reconciliation**

```bash
git add pyproject.toml uv.lock tests/test_main_auth_voice_dependencies.py
git commit -m "build: lock common auth and voice dependencies"
```

## Task 7: Preserve the complete common entrypoint inventory

**Files:**

- Modify: `hermes_cli/client_auth/entrypoints.json`
- Modify: `hermes_cli/client_auth/static_help.txt`
- Modify: `scripts/check_auth_entrypoints.py`
- Modify: `scripts/generate_auth_free_help.py`
- Modify: associated entrypoint tests and common entrypoint wiring from `c8fad50c49`, `f5a7372e4c`, and `e51e448669`.

- [ ] **Step 1: Check in the accepted non-installer baseline**

Create `docs/security/main-auth-entrypoints-dmg-baseline.json` from `f5a7372e4c:hermes_cli/client_auth/entrypoints.json`, removing only entries whose executable exists solely inside a packaged payload. Record each removed entry and reason in the file metadata.

- [ ] **Step 2: Regenerate the candidate inventory**

```bash
uv run python scripts/check_auth_entrypoints.py --write
uv run python scripts/generate_auth_free_help.py
```

- [ ] **Step 3: Assert no common entrypoint shrank**

Add a deterministic test:

```python
baseline = load_entries("docs/security/main-auth-entrypoints-dmg-baseline.json")
candidate = load_entries("hermes_cli/client_auth/entrypoints.json")
assert baseline <= candidate
```

It must also assert that signed-out background modes never call a password prompt and that the only public commands are login/logout/status/help/version.

- [ ] **Step 4: Run and commit the inventory gate**

```bash
uv run pytest tests/hermes_cli/client_auth/test_account_commands.py \
  tests/hermes_cli/client_auth/test_entrypoints.py \
  tests/hermes_cli/client_auth/test_background_modes.py -q
uv run python scripts/check_auth_entrypoints.py --check
uv run python scripts/generate_auth_free_help.py --check
git add docs/security/main-auth-entrypoints-dmg-baseline.json \
  hermes_cli/client_auth scripts/check_auth_entrypoints.py \
  scripts/generate_auth_free_help.py tests/hermes_cli/client_auth
git commit -m "fix: preserve every common authenticated entrypoint"
```

Expected: PASS and no entrypoint loss.

## Task 8: Add explicit product-file and behavior parity gates

**Files:**

- Create: `docs/security/main-auth-voice-dmg-product-paths.txt`
- Create: `docs/security/main-auth-voice-parity-waivers.json`
- Create: `scripts/generate_main_auth_voice_manifest.py`
- Create: `scripts/check_main_auth_voice_parity.py`
- Create: `tests/test_main_auth_voice_parity.py`

- [ ] **Step 1: Create the final-DMG common product manifest**

Generate, rather than handwrite, every common authentication and Voice product/test path expected at `80db6d8265`. The generator must:

1. Parse `docs/security/main-auth-voice-commit-ledger.json`.
2. Take every path changed by a `main` commit, every `main_paths` entry of a `split` commit, and every explicit `common_interface_waiver_paths` entry.
3. Intersect that set with paths changed by `4ef56cef4c..80db6d8265` and present in the shipping reference.
4. Reject any path assigned to both `main_paths` and `overlay_paths`.
5. Write a sorted, unique manifest and support `--check` so manual omission or addition fails.

Include safe progress, `runtime_ready`, logout, entrypoint, all Voice paths, source dependency files, and common tests. Delivery paths classified macOS remain absent. Root `uv.toml` is absent because it is overlay-owned and is guarded separately by Task 6.

- [ ] **Step 2: Write failing parity tests**

The tests must fail when:

```text
a manifest path is absent from the candidate
a checked-in manifest differs from the ledger-derived projection
a candidate blob differs without a waiver
a waiver lacks path, owner, reason, reference SHA, and expected invariant
an entrypoint baseline item is absent
hermes:bootstrap:get is auth-free
runtime_ready is absent from the common status contract
```

Run:

```bash
uv run pytest tests/test_main_auth_voice_parity.py -q
```

Expected: FAIL until manifests and approved differences are complete.

- [ ] **Step 3: Implement parity checking**

The checker first verifies the manifest is exactly the ledger-derived projection, then compares each manifest path between `HEAD` and `80db6d8265`. A difference is accepted only when `docs/security/main-auth-voice-parity-waivers.json` identifies one of these reasons:

```text
neutral-platform-wording
windows-source-runtime-adapter
common-test-strengthening
packaging-producer-interface-extraction
source-dependency-policy
source-install-scope
source-mode-no-packaged-progress-producer
```

No wildcard waiver is allowed. A waiver never permits weaker auth behavior. At minimum, `pyproject.toml` and `uv.lock` use `source-dependency-policy`; the accepted source-only hunk set in `scripts/install.sh` uses `source-install-scope`; and the absence of the packaged producer's indeterminate progress event in source mode uses `source-mode-no-packaged-progress-producer`. Every waiver records its invariant and reference SHA.

Preassign the known overlap surface so implementation cannot stall on ad hoc interpretation:

```text
apps/desktop/electron/bootstrap-runner.ts       source-mode-no-packaged-progress-producer
apps/desktop/electron/bootstrap-runner.test.ts  source-mode-no-packaged-progress-producer
apps/desktop/electron/main.ts                   packaging-producer-interface-extraction
docs/security/remote-auth-release-evidence.md   neutral-platform-wording
hermes_cli/main.py                              packaging-producer-interface-extraction
hermes_cli/web_server.py                        neutral-platform-wording
pyproject.toml                                  source-dependency-policy
scripts/install.sh                              source-install-scope
uv.lock                                         source-dependency-policy
```

- [ ] **Step 4: Run the complete parity gate**

```bash
python scripts/check_main_auth_voice_parity.py \
  --reference 80db6d8265f805cec46817d913982e4c5f6405c4
python scripts/generate_main_auth_voice_manifest.py --check
uv run pytest tests/test_main_auth_voice_parity.py -q
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
```

Expected: PASS with every difference explicit and reviewed.

## Task 9: Run macOS and Windows source-mode acceptance

**Files:**

- Modify: `.github/workflows/tests-os.yml` and `.github/workflows/tests.yml` only if required to retain the common native-auth evidence matrix.
- Never modify: platform packaging workflows.

- [ ] **Step 1: Run the complete macOS source gate locally**

```bash
uv run pytest tests/hermes_cli/client_auth tests/tui_gateway/test_account_auth.py \
  tests/acp/test_entry.py tests/agent/test_transcription_registry.py \
  tests/tools/test_sensevoice_stt.py tests/tools/test_transcription_tools.py -q
uv run ruff check hermes_cli/client_auth hermes_cli/main.py \
  agent/transcription_registry.py tools/sensevoice_stt.py tools/transcription_tools.py
npm run typecheck --workspace apps/desktop
npm run lint --workspace apps/desktop
npm run test --workspace apps/desktop
npm run test:e2e --workspace apps/desktop -- e2e/auth-hard-gate.spec.ts
```

Expected: PASS. Use temporary `HERMES_HOME`, test credential owner, fixed local contract server, fake media input, and zero real credentials.

- [ ] **Step 2: Verify macOS source behavior manually with a test account**

With the user entering the password directly into the GUI, verify login, logout, restart/online session validation, relock after safe session revocation, representative CLI/TUI/gateway/serve/cron/MCP/ACP denial while signed out, Desktop backend lifecycle, local SenseVoice readiness, and one Voice transcription smoke. Record only sanitized PASS/FAIL and timing.

- [ ] **Step 3: Preserve the common multi-OS native evidence workflow**

The common workflows may run source tests on Linux, macOS, and Windows using fixed local contract servers and ephemeral test owner state. They must not contain DMG/NSIS construction, exact-artifact credentials, production account credentials, or installed-artifact drivers. A workflow file is repository validation metadata and is never copied into a DMG or NSIS payload.

Run the workflow validation locally:

```bash
native_junit_dir=$(mktemp -d)
native_evidence_dir=$(mktemp -d)
uv run pytest tests/hermes_cli/client_auth -q \
  --junitxml "$native_junit_dir/auth.xml"
uv run python scripts/write_auth_native_artifact.py \
  --platform macos --owner-transport unix-getpeereid \
  --locked-start "$native_junit_dir/auth.xml" \
  --handle-noninheriting "$native_junit_dir/auth.xml" \
  --service-waiting "$native_junit_dir/auth.xml" \
  --output "$native_evidence_dir/macos.json"
uv run python scripts/check_auth_native_artifacts.py \
  --allow-partial-local "$native_evidence_dir"
uv run pytest tests/hermes_cli/client_auth/test_native_artifacts.py -q
```

Expected: PASS.

- [ ] **Step 4: Add and run Windows source acceptance on a clean Windows runner**

Add a `desktop-auth-source-windows` job to `.github/workflows/tests-os.yml`. It runs on `windows-latest`, installs only the source Node/Python dependencies, starts only a fixed local auth contract server, and executes the Desktop source-mode Auth Gate E2E. It must not build an installer, stage a payload, read repository secrets, upload a release artifact, or call `.github/workflows/desktop-windows-package.yml`.

Required checks:

```text
Python native-auth suite with real Windows named pipes
hermes login/logout/auth status and signed-out command matrix
Desktop source-mode Auth Gate E2E
restored-session online validation
logout and rapid account-switch epoch isolation
cold owner startup and Retry deadline race
Voice/SenseVoice readiness and fake-media transcription smoke
no NSIS build and no packaged payload
```

Expected: PASS. A Windows packaging job does not satisfy this step.

- [ ] **Step 5: Run repository quality gates**

```bash
uv lock --check
uv run python scripts/check_auth_entrypoints.py --check
uv run python scripts/generate_auth_free_help.py --check
python scripts/check_main_auth_voice_parity.py --reference 80db6d8265
python scripts/check_main_platform_boundary.py --base ansatz/main \
  --allowlist docs/security/main-auth-voice-common-paths.txt
bash -n scripts/install.sh
git diff --check ansatz/main...HEAD
git status --short --branch
```

Expected: PASS and no untracked implementation residue.

## Task 10: Record evidence and prepare, but do not execute, the `main` merge

**Files:**

- Create: `docs/security/main-auth-voice-acceptance-2026-08-22.md`

- [ ] **Step 1: Record sanitized acceptance evidence**

Record:

```text
Base SHA and candidate SHA
Candidate tree hash
macOS and Windows source-test environments
Auth suite PASS/FAIL
all-entrypoint gate PASS/FAIL
safe pre-auth IPC PASS/FAIL
logout/account epoch isolation PASS/FAIL
Voice parity and smoke PASS/FAIL
product-file parity PASS/FAIL and waiver count
platform-boundary PASS/FAIL
sensitive evidence: none recorded
```

Do not record account names, passwords, Cookies, Sessions, CSRF values, secure-store entries, or raw bridge/bootstrap logs.

- [ ] **Step 2: Commit evidence and validate its SHA semantics**

Record the candidate tree hash before the evidence commit, then commit the evidence and record the final commit SHA after commit:

```bash
git add docs/security/main-auth-voice-acceptance-2026-08-22.md
git commit -m "docs: record main auth voice acceptance"
git rev-parse HEAD^{tree}
git rev-parse HEAD
```

Update the evidence in a final documentation commit only if the file must contain the final commit SHA; otherwise the tree hash is the stable content identity.

- [ ] **Step 3: Refresh remote main and audit the complete delta**

```bash
git fetch ansatz main
git merge-base --is-ancestor ansatz/main HEAD
git diff --stat ansatz/main...HEAD
git diff --name-status ansatz/main...HEAD
git log --graph --decorate --oneline ansatz/main..HEAD
```

Expected: `ansatz/main` remains an ancestor and every changed path is approved common behavior/test/evidence. If main moved, stop and repeat the full gate after integration.

- [ ] **Step 4: Push only the candidate branch**

```bash
git push --set-upstream ansatz integration/main-auth-voice-base
```

Never push `HEAD:main` in this plan.

- [ ] **Step 5: Stop for merge approval**

Report the candidate commit/tree, every test result, parity waiver list, remaining risk, and exact release-overlay exclusions. A later task may merge into `main` only after explicit approval and a final independent review.

## Appendix A: Complete macOS integration-line commit classification

This appendix is normative. It covers every commit in `4ef56cef4c..403e1c3873` in branch order. `split` means only the ledger's exact `main_paths` are replayed; all other paths remain in the named overlay. `drop` means the commit is historical topology or superseded evidence and is not replayed into the common candidate.

| # | Commit | Destination | Reason |
| ---: | --- | --- | --- |
| 1 | `ecc610b0a3` | macOS | Phase-1 DMG milestone documentation. |
| 2 | `736aa6c99e` | macOS | Phase-1 DMG implementation plan. |
| 3 | `dd422c7cdc` | macOS | DMG build-log placement. |
| 4 | `f5351c3c60` | macOS | Packaged DMG runtime pin. |
| 5 | `8aa6263a35` | macOS | Packaged browser-download rule. |
| 6 | `3305431d68` | macOS | Reproducible DMG entry point. |
| 7 | `e28883110d` | macOS | Electron Builder artifact mirror. |
| 8 | `977322ecd7` | macOS | Ad-hoc artifact signing. |
| 9 | `8ced1927de` | drop | Superseded phase-one cloud-login design. |
| 10 | `cf1fe441f8` | drop | Superseded cloud-auth deployment review. |
| 11 | `470d8b1905` | drop | Superseded cloud-auth review closure. |
| 12 | `3ad4a12660` | main | Complete common Voice/SenseVoice behavior. |
| 13 | `abbc79d0cd` | macOS | Bundled backend payload. |
| 14 | `904d435685` | macOS | Packaged-test exclusion. |
| 15 | `6da3fb596f` | main | Common remote-auth design. |
| 16 | `0e052743b4` | main | Common auth review closure. |
| 17 | `9df13addde` | main | Admin-only account provisioning rule. |
| 18 | `d01cc64dca` | main | Common auth constraints. |
| 19 | `26aec19847` | main | Client account-creation closure. |
| 20 | `dd2c68ae22` | main | Common auth implementation plan. |
| 21 | `18871c0d83` | main | Auth implementation review corrections. |
| 22 | `ab29aace87` | main | Common release-evidence wording. |
| 23 | `73e72f3299` | main | Server-auth checkpoint documentation. |
| 24 | `32074405c7` | main | Fixed-server Django auth client. |
| 25 | `6cb18534cf` | main | Revocable session/runtime protocol. |
| 26 | `efa452f912` | main | Secure-store and memory owner abstractions. |
| 27 | `dc2d944942` | main | Auth before CLI bootstrap. |
| 28 | `d974f8dc9b` | main | Login/logout/status CLI. |
| 29 | `c2743bccca` | main | Closed Desktop auth bridge. |
| 30 | `2e86952916` | main | Cross-process auth owner. |
| 31 | `e1989c96c8` | main | Fail-closed direct entrypoints. |
| 32 | `07537af39c` | main | Shared execution-boundary guard. |
| 33 | `a80574b535` | main | Desktop starts through auth bridge. |
| 34 | `351c28136a` | main | Default-deny Desktop IPC. |
| 35 | `e9514f0d45` | main | Desktop account gate. |
| 36 | `82858193ce` | main | Backend scope binding. |
| 37 | `c961ee189c` | main | Common auth implementation progress. |
| 38 | `fa7b7e493e` | main | Connection-scoped Desktop auth. |
| 39 | `aed1a4f71d` | main | Auth-scope isolation documentation. |
| 40 | `b964a7cbe7` | main | Ink TUI account gate. |
| 41 | `c06e537ab0` | main | TUI gate documentation. |
| 42 | `ddfbcddf67` | main | Strict SSH host trust. |
| 43 | `f251d64495` | main | SSH trust documentation. |
| 44 | `ddd3431eaf` | main | Authenticated strict-SSH backends. |
| 45 | `250d8610a5` | main | Remote SSH auth documentation. |
| 46 | `ef0ee016c9` | main | TUI WebSocket auth recheck. |
| 47 | `dba0aef706` | main | Desktop/TUI/SSH gate tests. |
| 48 | `a6dcb9556a` | main | Auth generation/startup closure. |
| 49 | `07692896d0` | main | Background-service hard gate. |
| 50 | `cee8bdbaed` | main | Multi-OS native auth evidence matrix. |
| 51 | `dbef3a0522` | main | Common hard-gate remediation evidence. |
| 52 | `b281ebc1ee` | main | Native evidence derived from tests. |
| 53 | `763465daf0` | main | Common auth audit closure. |
| 54 | `dad43d9d9a` | drop | Historical macOS/auth merge topology; reconstruction replaces it. |
| 55 | `936ede7953` | macOS | macOS test-runner temporary-directory preservation. |
| 56 | `89503cfb2b` | split | Main keeps Auth Gate E2E/settings test; macOS keeps payload/bundled-runtime tests. |
| 57 | `11d2143924` | macOS | `dmgbuild` volume-write fallback. |
| 58 | `08a2eedf67` | main | Bootstrap remains behind login surface. |
| 59 | `706c5a4d0d` | main | Teardown-aware Desktop quit. |
| 60 | `6aed1cc0f1` | main | Lease clock-domain alignment. |
| 61 | `e042ce3861` | main | ACP setup remains gated. |
| 62 | `bffdd26edf` | main | TUI watcher lifecycle test fix. |
| 63 | `f28a0f4b58` | main | Auth-owner E2E cleanup. |
| 64 | `eb26a5ecea` | macOS | Installed DMG auth acceptance evidence. |
| 65 | `ab712d9946` | drop | Historical GUI logout design; accepted behavior is replayed from product commits. |
| 66 | `9d77f29d5a` | drop | Historical GUI logout plan; accepted behavior is replayed from product commits. |
| 67 | `8af9e3eefe` | main | Authenticated Desktop account context. |
| 68 | `cdc12484f1` | main | GUI account logout item. |
| 69 | `27567163b3` | main | Logout remains authenticated-only. |
| 70 | `07df7ea759` | drop | DMG-specific GUI logout acceptance record; release evidence stays in the overlay. |
| 71 | `26afd123eb` | macOS | Clean-machine packaged bootstrap design. |
| 72 | `25d37ca7f6` | macOS | Packaged bootstrap recovery plan. |
| 73 | `366fb3f5a8` | main | Bounded account-bridge requests. |
| 74 | `cdb5c65bc8` | main | Bounded auth-bootstrap status. |
| 75 | `df34e9da62` | macOS | Packaged bootstrap process-group supervision. |
| 76 | `c478db4d2f` | split | Main keeps `runtime_ready`, runtime gate, Auth Gate/status contracts; macOS keeps bundled locks, payload builder, mirrors, and installer bootstrap. |
| 77 | `ac3f53a2ff` | macOS | Packaged bootstrap recovery evidence. |
| 78 | `e51e448669` | split | Main keeps the auth-only CLI module, command wiring, and tests; the auth-scope installer launcher publication stays macOS. |
| 79 | `f68510d905` | macOS | Installer method-stamp contract. |
| 80 | `50e0ca5654` | macOS | Packaged bootstrap acceptance. |
| 81 | `f6a3ae1950` | macOS | Zero-residual DMG rerun evidence. |
| 82 | `06e19c3ba3` | macOS | Installed SenseVoice retry evidence. |
| 83 | `8bccbdb20a` | drop | Historical auth-bridge recovery design; product commits are authoritative. |
| 84 | `cc49af7546` | drop | Historical auth-bridge recovery plan; product commits are authoritative. |
| 85 | `817a5d0a6a` | main | Bridge timeout ordered after HTTP. |
| 86 | `b22bdb8d31` | main | Dead local bridge recovery. |
| 87 | `af19ce56b1` | main | Retry reconstructs auth bridge. |
| 88 | `50609240e2` | main | Recovery lint/test correction after Retry implementation. |
| 89 | `4bafb3bac2` | macOS | DMG bridge-recovery acceptance evidence. |
| 90 | `f773677cee` | macOS | Fresh-VM Gatekeeper design. |
| 91 | `d94baef98b` | macOS | Fresh-VM Gatekeeper plan. |
| 92 | `7666fc4da5` | macOS | Fresh-VM DMG contract. |
| 93 | `42bbebadf9` | macOS | Quarantined-DMG clean-macOS test. |
| 94 | `44467baafa` | macOS | Exact-DMG fresh-macOS workflow. |
| 95 | `dcad133898` | macOS | Private draft-DMG downloader. |
| 96 | `3536951e9b` | macOS | Draft-DMG title matcher. |
| 97 | `22800baacf` | macOS | Private prerelease exact-DMG downloader. |
| 98 | `527eb97a19` | macOS | Packaged payload validation. |
| 99 | `31cf387094` | macOS | Gatekeeper failure evidence. |
| 100 | `e57b25c985` | drop | Historical safe-progress design; the current boundary design replaces it. |
| 101 | `396cb551c1` | drop | Historical safe-progress plan; the current implementation plan replaces it. |
| 102 | `bddbec2abb` | main | Safe bounded progress state. |
| 103 | `6e57ead969` | macOS | Packaged process files plus an eight-line indeterminate-progress producer hunk in the common runner remain overlay-owned; the safety/state contract comes from `bddbec2abb`, and source mode records a no-producer parity waiver. |
| 104 | `c4d5ae2d40` | main | Pre-auth IPC isolation and safe preload API. |
| 105 | `f8d7c05fad` | main | Locked progress renderer and four-language catalog. |
| 106 | `34026f7627` | main | Progress remains inside Auth Gate. |
| 107 | `a416087126` | split | Main keeps `electron/main.ts` import ordering and the safe-progress renderer test lint fix; packaged `bootstrap-process*` lint stays macOS. |
| 108 | `34f1c119e2` | main | Retry appears only after failure. |
| 109 | `3796a43498` | macOS | Installed progress acceptance evidence. |
| 110 | `ad61acdccf` | macOS | Progress DMG fresh-macOS workflow. |
| 111 | `e7a204987c` | macOS | Progress DMG Gatekeeper evidence. |
| 112 | `5074c14377` | macOS | Quarantine-bypass diagnostics design. |
| 113 | `7824f1cb93` | macOS | Installed login-gate diagnostic driver. |
| 114 | `89fa9d4239` | macOS | Post-bypass diagnostic evidence. |
| 115 | `628123153f` | macOS | Credential-backed DMG CI design. |
| 116 | `65d2a6763d` | macOS | Credential-backed DMG CI plan. |
| 117 | `4caccc8f20` | macOS | Credential-backed artifact contract. |
| 118 | `26ebd2889e` | macOS | Installed-App login driver. |
| 119 | `f832c24aa0` | macOS | Artifact diagnostic CDP isolation. |
| 120 | `afb067fd8c` | macOS | Exact-DMG credential login. |
| 121 | `4b593d06b7` | macOS | Exact-DMG credential evidence. |
| 122 | `bca86e89b5` | macOS | Final DMG review design. |
| 123 | `960a0359d7` | macOS | Final DMG review plan. |
| 124 | `b6975728ff` | macOS | Final DMG rejection record. |
| 125 | `9c818b26d3` | macOS | DMG entrypoint-closure design. |
| 126 | `0aa1d8b792` | macOS | DMG entrypoint-closure plan. |
| 127 | `c8fad50c49` | main | Common cron/diagnostic/entrypoint closure; no delivery path. |
| 128 | `363d464a85` | macOS | Bundled-payload exclusion policy. |
| 129 | `e69bd4ec18` | macOS | Exact-DMG entry audit driver/workflow. |
| 130 | `a756c93e35` | macOS | Bundled-runtime Android exclusion. |
| 131 | `db821dbf08` | macOS | Exact-artifact workflow. |
| 132 | `eddb467418` | macOS | Replacement DMG acceptance evidence. |
| 133 | `f5a7372e4c` | split | Main keeps cron/gateway/entrypoint inventory and tests; macOS keeps payload integration changes. |
| 134 | `f3ca2085a4` | macOS | Exact-f5 artifact workflow. |
| 135 | `30d02d508f` | macOS | Final f5 DMG evidence. |
| 136 | `f47a1e47e0` | drop | Historical elapsed-timer design; accepted product commit is replayed. |
| 137 | `5a41a5cb49` | macOS | CI-isolated replacement-DMG requirement. |
| 138 | `38993cffe4` | macOS | Replacement-DMG timer plan. |
| 139 | `c893e264e9` | main | Auth Gate elapsed-time focus resync. |
| 140 | `744fb0b729` | macOS | Replacement-DMG timer evidence. |
| 141 | `d340ebd15e` | macOS | Zero-residual timer rerun evidence. |
| 142 | `cb26826fad` | drop | Historical logout stale-status design; accepted product commits are replayed. |
| 143 | `f4bdb9655f` | drop | Historical logout stale-status plan; accepted product commits are replayed. |
| 144 | `38230a6c9f` | main | Runtime epoch/instance stale-result suppression. |
| 145 | `d35641eb43` | macOS | Logout replacement-DMG evidence. |
| 146 | `38cbc945e7` | drop | Historical implementation asset; the new design and acceptance evidence replace it. |
| 147 | `5beace1d8f` | drop | Historical Feishu-summary distinction; not product source. |
| 148 | `280751398d` | drop | Historical native-owner recovery design/plan; accepted product fix `4e4a5d42c7` is replayed. |
| 149 | `4e4a5d42c7` | main | Expired native owner recovery. |
| 150 | `035fbf16da` | macOS | Owner-recovery DMG evidence. |
| 151 | `30f78f4207` | macOS | Offline first-launch toolchain design. |
| 152 | `cf79e75e42` | macOS | Offline first-launch bootstrap plan. |
| 153 | `aa97962e0d` | macOS | Offline auth-toolchain builder. |
| 154 | `6355c61c79` | macOS | Verified bundled auth toolchain. |
| 155 | `d0b334abba` | macOS | Login runtime from bundled assets. |
| 156 | `86d0598a13` | macOS | Domestic packaged first-launch mirrors. |
| 157 | `decb2f0fcb` | macOS | Packaged-stage bootstrap supervision. |
| 158 | `b0ad6ad4e5` | macOS | Toolchain import correction. |
| 159 | `e1de17db83` | macOS | DMG auth-toolchain preparation. |
| 160 | `02f5390996` | macOS | Offline bootstrap-payload documentation. |
| 161 | `ccc7e10274` | macOS | Packaged bootstrap heartbeat artifact test. |
| 162 | `82b23ebde6` | macOS | Signed toolchain-hash preservation. |
| 163 | `9689aefd08` | macOS | Signed runtime lock installed through mirrors. |
| 164 | `bf090797fe` | macOS | Mirrored packaged browser runtime. |
| 165 | `403e1c3873` | macOS | Domestic first-launch acceptance evidence. |

## Appendix B: Windows behavior reconciliation

The Windows branch contains delivery commits and common source-runtime fixes after its auth merge. The following table is normative for the common extraction. Every unlisted Windows-only commit remains in the Windows overlay or historical reference.

| Commit | Destination | Common extraction |
| --- | --- | --- |
| `1030e4fe3f` | main-equivalent | Behavior already replayed from macOS `08a2eedf67`. |
| `26a4b8d7ea` | main-equivalent | Behavior already replayed from `706c5a4d0d`. |
| `c560db9a19` | main-equivalent | Behavior already replayed from `6aed1cc0f1`. |
| `93d904e882` | main-equivalent | Behavior already replayed from `e042ce3861`. |
| `a4e987eef5` | main-equivalent | Behavior already replayed from `bffdd26edf`. |
| `7114e13656` | main-equivalent | Behavior already replayed from `817a5d0a6a`. |
| `4351fb5a82` | main-equivalent | Behavior already replayed from `b22bdb8d31`. |
| `677de12c95` | main-equivalent | Behavior already replayed from `af19ce56b1`. |
| `38c13118c4` | main-equivalent | Behavior already replayed from `e51e448669`. |
| `c2c4790e01`–`ca52c1ce6c` | main-equivalent/split | Compare with common safe-progress/runtime-gate replay; retain only missing source behavior and exclude payload/installer producers. |
| `d282099faf` | main | Keep common runtime-progress controls reachable. |
| `45eb464242` | split | Extract bridge, Auth Gate, `client_auth/bridge.py`, `client_auth/runtime.py`, and source tests; reject packaging paths. |
| `9a32e19153` | split | Extract deadline/named-pipe/native-evidence source fixes and tests; reject packaging paths. |
| `772903c824` | split | Extract auth-bridge timeout constants, backend-probe testability, `runtime.py`, native-evidence writer, and source tests. Packaged auth-runtime contract, `windows-auth-owner.ts`, installer/workflow, installed E2E, and packaged bootstrap stay Windows. |
| `b2b6a610b1` | split | Extract the exact common E2E/runtime-gate/external-open/guarded-IPC/media/native-decision/preview/renderer/trusted-renderer paths listed in Task 5 plus their guarded `main.ts` wiring. Installer, managed-uv packaging, Windows process tree, bootstrap runner/process, and installed-artifact paths stay Windows. |
| `ed3235595c` | Windows | Bundled minimal auth-pipe dependency belongs to `desktop_auth_runtime/**`. |
| `2b111d92ed`–`c5b19efe57` | Windows | Installer protocol, uv bootstrap, packaged login, and exact-artifact tests. |
| `07755bc139`, `d991c6a919` | Windows | Installed-runtime startup/process cleanup. |
| `af0e95514e` | Windows | Windows overlay design document; source behavior is extracted from the following fixes. |
| `8f54f28f17`, `1a877e86f9`, `c2d3d09aab` | Windows | CI fixture and managed-uv installer stage. |
| `56b402c63b` | Windows documentation | Latest documentation-only tip; not a new behavior source. |

## Appendix C: Conflict and parity decision record

| Surface | Common result | Forbidden resolution |
| --- | --- | --- |
| `guarded-ipc.ts` | `hermes:bootstrap:get` protected; sanitized `hermes:auth-bootstrap:get` public. | Restoring the original feature's raw bootstrap channel. |
| `preload.ts` | Expose only bounded `authBootstrap` operations before login. | Exposing raw bootstrap transcript/state. |
| `auth-gate.tsx` | Preserve `useDesktopAuth`, GUI logout, safe progress, `runtime_ready`, focus resync, and epoch-aware status. | Choosing an early branch version wholesale. |
| `electron/main.ts` | Preserve bridge recovery, scope ordering, runtime gate, safe progress, stale-result suppression, and source/packaged dependency injection. | Resolving with macOS or Windows file wholesale. |
| `client_auth/runtime.py` | Preserve accepted macOS owner recovery and accepted Windows named-pipe/deadline behavior behind common interfaces. | Keeping a Windows-overlay private fork. |
| `pyproject.toml` / `uv.lock` | Main's 14-day policy plus common `keyring` and `sensevoice`; regenerate lock. | Importing fixed DMG timestamp, root `uv.toml`, bundled `[all]` pin, or hand-editing lock. |
| `scripts/install.sh` | Source-install Voice hunk only. | Importing bundled payload, mirror, signed-lock, browser-runtime, or DMG bootstrap hunks. |
| entrypoint inventory | Candidate is a superset of accepted non-installer entries. | Regenerating a smaller manifest and treating it as success. |
